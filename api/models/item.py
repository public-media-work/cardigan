"""Item models — one output of one job (#426, building on #329).

An **item** is one reviewable output of a job: a keyword list, a short
description, a formatted transcript. Every item says where its value came from
(``origin``), what it was built on (``basis_value``), what Cardigan proposes
(``proposed_value``) and what the editor changed it to (``edited_value``).

Three independent state axes, each owned by a different actor:

generation (the system)
    ``ready | failed | unavailable``, with a reason. ``unavailable`` covers
    items nothing can produce yet — an integration Cardigan lacks, a phase
    whose output isn't a parseable contract, or copy generation switched off.
review (the editor)
    ``proposed -> accepted | dismissed``, and back to ``proposed``.
    Re-extraction never touches it.
push (per target)
    Lives in ``item_pushes``, one row per (item, target). Absent until the
    push endpoint exists — the REST API stays read-only for SST until then.

Item keys mirror the AirTable SST field they correspond to, one to one. An
output with no SST field (the formatted transcript) keeps a key outside that
set and can be reviewed, edited and copied, but never pushed.

A ready item can also be an **affirmation**: the generator read the basis and
judged it right as written. It carries no proposal, and its value is the
basis — "keep as is" is an answer, not a missing one. Only an item with a
basis can be affirmed; with nothing to keep, a null proposal is a failure.

Nothing blocks. Flags are advisory; the editor is the gate, not the linter.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field

# generation_reason on a ready item whose generator kept the basis as written.
NO_CHANGE_NEEDED = "no_change_needed"
# generation_reason on a ready copy item produced under copy_generation=review:
# the generator could only affirm or flag the basis, never rewrite it (#427).
REVIEW_ONLY = "review_only"


class CopyMode(str, Enum):
    """How far copy generators may go (#427). Instance setting, with a
    per-job override.

    off      no model touches copy items; they show the basis and take a
             hand edit.
    review   a generator may affirm or flag the basis, never write text.
             Code enforces it: proposals and alternatives are dropped.
    suggest  affirm, edit lightly, offer alternatives; first-draft copy
             when there is no basis.
    """

    off = "off"
    review = "review"
    suggest = "suggest"

    @classmethod
    def parse(cls, value: Any, default: "CopyMode | None" = None) -> "CopyMode":
        """Accept a CopyMode, its string, or #426's boolean (True=suggest)."""
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            return cls.suggest if value else cls.off
        if isinstance(value, str) and value in cls._value2member_map_:
            return cls(value)
        return default if default is not None else cls.suggest


class ItemCategory(str, Enum):
    """Editor-facing grouping on the job page."""

    metadata = "metadata"
    copy = "copy"
    transcript = "transcript"


class Origin(str, Enum):
    """How the proposed value was made. Never changes after generation.

    If a model touched the value at any point, it is ``inferred`` — the label
    is a trust signal and must never overstate how much a script did. A hand
    edit fills ``edited_value`` and leaves this alone.
    """

    deterministic = "deterministic"
    inferred = "inferred"


class GenerationState(str, Enum):
    """Whether the system produced a value. Always paired with a reason
    when not ``ready``."""

    ready = "ready"
    failed = "failed"
    unavailable = "unavailable"


class ReviewState(str, Enum):
    """The editor's verdict. Owned by the editor alone."""

    proposed = "proposed"
    accepted = "accepted"
    dismissed = "dismissed"


class PushState(str, Enum):
    """Per-target push record state (``item_pushes``)."""

    not_pushed = "not_pushed"
    pushed = "pushed"
    push_failed = "push_failed"
    stale = "stale"  # the live value no longer matches basis_value / the pushed value


class CurrentState(str, Enum):
    """Provenance of the target's current value — read-only context.

    Derived from two inputs: whether the field holds a value, and where the
    record sits on ``Single Source Status``. The ladder is record-level, not
    field-level, so ``reviewed`` is a conservative claim about the record
    rather than a precise one about this field.
    """

    empty = "empty"  # no value — Cardigan is first author
    unreviewed = "unreviewed"  # value present, nobody has blessed the record
    reviewed = "reviewed"  # a human approved the record; refine carefully
    flagged = "flagged"  # "Producer Issue - See Promo Notes"
    unknown = "unknown"  # never fetched (no record to read)


class FlagOrigin(str, Enum):
    """Who raised a flag. A model's judgement and a script's measurement
    deserve different trust — a model has flagged the editor's own copy as
    inaccurate (editorial-review prototype, 2026-10-21)."""

    model = "model"
    code = "code"


class FlagTarget(str, Enum):
    """Which text a flag is about. Flags on existing copy (``basis``) are
    the editor's to weigh; flags on Cardigan's text are Cardigan's to fix."""

    basis = "basis"
    proposal = "proposal"
    alternative = "alternative"


class ItemFlag(BaseModel):
    """An advisory finding. Never blocks.

    ``check`` names what raised it (``char_limit``, ``style_engine``, …) so the
    job page can group flags and new checks need no schema change.
    """

    check: str = "style_engine"
    rule_id: str
    severity: str = Field(..., description="'error' | 'warning' — advisory only, never blocking")
    message: str
    model_fixable: bool = False
    origin: FlagOrigin = FlagOrigin.code
    target: FlagTarget = FlagTarget.proposal


class ItemAlternative(BaseModel):
    """Another candidate value, offered beside the proposal.

    Choosing one is an editor action: the client copies its value into
    ``edited_value``, so ``proposed_value`` stays a record of what the
    generator said. Length is measured by code, never reported by a model —
    a model has claimed a 421-character text was under 350.
    """

    value: str
    angle: Optional[str] = Field(None, description="What this candidate leans on, e.g. 'place', 'question'")
    flags: List[ItemFlag] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def length(self) -> int:
        return len(self.value)


class Item(BaseModel):
    """Complete item record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int

    key: str = Field(..., description="Mirrors the SST field, e.g. 'short_description'")
    category: ItemCategory
    origin: Origin

    generation_state: GenerationState = GenerationState.ready
    generation_reason: Optional[str] = Field(
        None,
        description="Why not ready, e.g. 'copy_generation_off', 'integration:youtube_api', "
        "'structured_output:seo', 'no_value_extracted'. On a ready item, 'no_change_needed' "
        "marks an affirmation of the basis",
    )
    review_state: ReviewState = ReviewState.proposed

    basis_value: Optional[str] = Field(None, description="The target's value when this item was generated")
    basis_fetched_at: Optional[datetime] = None
    current_state: CurrentState = CurrentState.unknown
    proposed_value: Optional[str] = Field(None, description="What Cardigan proposes; always the full text")
    edited_value: Optional[str] = Field(None, description="The editor's hand edit, if any")
    rationale: Optional[str] = Field(None, description="One line: what changed and why")

    flags: List[ItemFlag] = Field(default_factory=list)
    alternatives: List[ItemAlternative] = Field(
        default_factory=list, description="Other candidates; choosing one copies it into edited_value"
    )
    char_limit: Optional[int] = Field(
        None,
        description="From config/house_style.yaml. For the title it applies to the part before "
        "(or, for Wisconsin Life, after) the program name",
    )

    generator: Optional[str] = None
    model: Optional[str] = Field(None, description="The resolved model id, never an alias")
    input_revisions: Dict[str, Any] = Field(
        default_factory=dict,
        description="What the item was built from: {'slices': {name: revision}, 'items': {key: generated_at}}",
    )
    stale: bool = Field(False, description="An input changed after this item was generated; re-run to refresh")
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    generated_at: Optional[datetime] = None

    created_at: datetime
    updated_at: datetime

    @property
    def value(self) -> Optional[str]:
        """What a copy would use: the edit, else the proposal, else — for an
        affirmation — the basis it kept."""
        if self.edited_value is not None:
            return self.edited_value
        if self.keeps_basis():
            return self.basis_value
        return self.proposed_value

    def keeps_basis(self) -> bool:
        """True when the item's answer is the existing text: an affirmation,
        or a review-only pass. An unedited one pushes nothing."""
        return self.is_affirmation() or (
            self.generation_state == GenerationState.ready
            and self.proposed_value is None
            and self.generation_reason == REVIEW_ONLY
        )

    def is_affirmation(self) -> bool:
        """True when the generator kept the basis as written. A push of an
        unedited affirmation writes nothing."""
        return (
            self.generation_state == GenerationState.ready
            and self.proposed_value is None
            and self.generation_reason == NO_CHANGE_NEEDED
        )

    def is_suggestion(self) -> bool:
        """True when the proposal builds on existing text (render as a diff)."""
        return bool(self.basis_value and self.basis_value.strip())

    def has_errors(self) -> bool:
        """Advisory only — never consulted as a gate. Flags on the basis are
        the editor's to weigh, so only flags on Cardigan's own text count.
        Flags on alternatives live on each alternative and never count."""
        return any(flag.severity == "error" and flag.target != FlagTarget.basis for flag in self.flags)


class ItemUpdate(BaseModel):
    """PATCH /jobs/{job_id}/items/{key} — the editor's fields only.

    Anything else is a 422: generation state, origin, basis and provenance
    belong to the system. Send ``edited_value: null`` to clear an edit.
    """

    model_config = ConfigDict(extra="forbid")

    edited_value: Optional[str] = None
    review_state: Optional[ReviewState] = None


class ItemPush(BaseModel):
    """One push record — an item's state on one target."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    target: str
    target_record_id: Optional[str] = None
    target_field_id: Optional[str] = None
    state: PushState = PushState.not_pushed
    pushed_value: Optional[str] = None
    pushed_at: Optional[datetime] = None
    pushed_by: Optional[str] = None
    error: Optional[str] = None


class ItemList(BaseModel):
    """Response for GET /jobs/{job_id}/items."""

    items: List[Item]
    total: int
    job_id: int
    values_included: bool = Field(
        True,
        description="False when large values were omitted; re-request with ?include=values",
    )
    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Counts by origin, review state, generation state and category",
    )
