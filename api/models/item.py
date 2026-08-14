"""Item models — the editor-facing primitive (issue #329).

An **item** is one reviewable unit of a job's output: a proposed value, the
current Airtable value it would replace, and a status. Items replace the
agent-shaped ``*_output.md`` documents as the thing an editor approves.

Two layers:

``context``
    Never published. Subjects, speakers, place names, editorial angles — the
    analyst's brainstorming material, reviewable as provenance.
``deliverable``
    Maps 1:1 to an Airtable SST field (except ``formatted_transcript``, which
    has no SST destination today).

Nothing blocks. Flags carry a severity but never gate approval — the editor is
the gate, not the linter. This supersedes ``validator_output.md`` and the
``jobs.validation_result`` column: validation becomes a per-item status plus
advisory flags.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ItemLayer(str, Enum):
    """Whether an item is publishable."""

    context = "context"  # never published
    deliverable = "deliverable"  # maps to an Airtable SST field


class ItemCategory(str, Enum):
    """Editor-facing grouping on the approval surface."""

    context = "context"
    metadata = "metadata"
    copy = "copy"
    transcript = "transcript"


class ItemSource(str, Enum):
    """What produced the proposed value."""

    llm = "llm"  # a pipeline phase
    deterministic = "deterministic"  # computed by code (dates, links)
    human = "human"  # edited in place by the editor


class ItemStatus(str, Enum):
    """Review state. No blocking state exists by design."""

    pending_review = "pending_review"
    approved = "approved"
    kicked_back = "kicked_back"
    awaiting_source = "awaiting_source"  # nothing can produce a value yet


class CurrentState(str, Enum):
    """Provenance of the *current* Airtable value.

    Derived from two inputs: whether the field holds a value, and where the
    record sits on ``Single Source Status (BETA)``. The ladder is
    record-level, not field-level, so ``reviewed`` is a conservative claim
    about the record rather than a precise one about this field.
    """

    empty = "empty"  # no value — Cardigan is first author, fill freely
    unreviewed = "unreviewed"  # value present, nobody has blessed the record
    reviewed = "reviewed"  # a human approved the record; refine carefully
    flagged = "flagged"  # "Producer Issue - See Promo Notes"
    unknown = "unknown"  # never fetched (no media_id) — 27/30 jobs today


class ItemFlag(BaseModel):
    """An advisory finding from the deterministic style engine.

    Severity is carried through from ``style_engine.types.Violation`` but
    never blocks: an item with three ``error`` flags is still approvable.
    """

    rule_id: str
    severity: str = Field(..., description="'error' | 'warning' — advisory only, never blocking")
    message: str
    model_fixable: bool = False


class Item(BaseModel):
    """Complete item record."""

    id: int
    job_id: int

    key: str = Field(..., description="Stable identifier, e.g. 'title', 'context.themes'")
    layer: ItemLayer
    category: ItemCategory
    source: ItemSource

    proposed_value: Optional[str] = Field(None, description="What Cardigan proposes; full text")
    current_value: Optional[str] = Field(None, description="Airtable's value at fetch time")
    current_state: CurrentState = CurrentState.unknown
    current_fetched_at: Optional[datetime] = None

    status: ItemStatus = ItemStatus.pending_review
    source_blocked_on: Optional[str] = Field(
        None,
        description="Why awaiting_source: 'integration:<name>' (no system to ask) or "
        "'structured_output:<phase>' (agent answers, but not parseably)",
    )
    flags: List[ItemFlag] = Field(default_factory=list)
    kickback_note: Optional[str] = None
    kicked_back_at: Optional[datetime] = None

    phase: Optional[str] = Field(None, description="Producing phase, for provenance")
    model: Optional[str] = None
    char_limit: Optional[int] = Field(None, description="From config/house_style.yaml")
    airtable_field: Optional[str] = None
    airtable_field_id: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    def is_publishable(self) -> bool:
        """Approved deliverables bound for an Airtable field."""
        return (
            self.status == ItemStatus.approved
            and self.layer == ItemLayer.deliverable
            and self.airtable_field is not None
        )

    def needs_overwrite_confirmation(self) -> bool:
        """True when publishing would overwrite work a human may own.

        ``empty`` is always safe — Cardigan is first author regardless of
        where the record sits on the ladder, which is the "fill gaps vs.
        refine what's there" split.
        """
        return self.current_state in (
            CurrentState.reviewed,
            CurrentState.flagged,
            CurrentState.unknown,
        )

    def has_errors(self) -> bool:
        """Advisory only — never consulted as a gate."""
        return any(flag.severity == "error" for flag in self.flags)


class ItemUpdate(BaseModel):
    """Partial update (PATCH /jobs/{job_id}/items/{key})."""

    status: Optional[ItemStatus] = None
    proposed_value: Optional[str] = Field(None, description="Editor's in-place edit; sets source=human")
    kickback_note: Optional[str] = None


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
        description="Counts by status and category, for the approval surface header",
    )
