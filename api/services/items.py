"""Item registry and assembly (#426, building on #329).

Turns *proposals* — values some generator produced — plus the job's SST record
into :class:`~api.models.item.Item` rows. Pure functions only: no DB access,
no HTTP. Persistence lives in ``api.services.database.upsert_job_items``.

Who produces proposals:

- the generators in ``api.services.generation.generators`` (#427), one per
  item, run by ``api.services.generation.runner`` against the job's context
  bundle. ``premiere_date_proposals`` (here) is the shared function behind
  the four deterministic date generators.

SST fields are addressed by **field id**, never by name. The base renames
fields in place (``Single Source Status (BETA)`` lost its suffix and two copy
fields were renamed while #329 sat unmerged), and a name lookup misses a
renamed field silently — the item would claim the field was empty.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

from api.models.item import (
    NO_CHANGE_NEEDED,
    REVIEW_ONLY,
    CopyMode,
    CurrentState,
    FlagOrigin,
    FlagTarget,
    GenerationState,
    ItemCategory,
    ItemFlag,
    Origin,
    ReviewState,
)
from api.services.style_engine.rules import load_rules

logger = logging.getLogger(__name__)

# Values longer than this are omitted from list responses unless the caller
# passes ?include=values. formatted_transcript is routinely 50-150KB.
LARGE_VALUE_CHARS = 4000

# ---------------------------------------------------------------------------
# Single Source Status — fld26q5kEJicSYHX8
#
# The only review signal the SST exposes. It is a RECORD-level signal, not
# field-level: it says the record was approved, not that this field was read.
# ---------------------------------------------------------------------------
SST_STATUS_FIELD_ID = "fld26q5kEJicSYHX8"
SST_PREMIERE_FIELD_ID = "fldxIBOo7vNdy2qWY"  # Digital Premiere
SST_MEDIA_ID_FIELD_ID = "fld8k42kJeWMHA963"  # Media ID

LADDER_REVIEWED = frozenset(
    {
        "Edited & Approved by Digital",
        "Edited & Approved by Promotions",
        "Final Copy & Images Approved by Producer",
    }
)
LADDER_FLAGGED = "Producer Issue - See Promo Notes"

# Release Title convention (ruling on #459, 2026-10-22): the program name is
# appended — "Title | Program" — except for these programs, which prepend it
# for analytics scripting.
TITLE_SEPARATOR = " | "
PREFIX_PROGRAMS: tuple[str, ...] = ("Wisconsin Life",)

# Why an item is unavailable, when the reason is structural rather than per-job.
COPY_GENERATION_OFF = "copy_generation_off"
NOT_GENERATED = "not_generated"
NO_VALUE_EXTRACTED = "no_value_extracted"
# A generator affirmed an item that has no basis — there was nothing to keep.
AFFIRM_WITHOUT_BASIS = "affirm_without_basis"
# copy_generation=review on a copy item with no basis: there is nothing to review.
COPY_REVIEW_NO_BASIS = "copy_review_no_basis"


@dataclass(frozen=True)
class ItemSpec:
    """Static declaration of one item key.

    ``sst_field_id`` names the SST field the item mirrors. ``None`` means the
    output has no SST field and is never pushed.
    """

    key: str
    category: ItemCategory
    origin: Origin  # what the item's generator produces; see Proposal.origin
    sst_field_id: Optional[str] = None
    sst_field: Optional[str] = None  # display name, documentation only — lookups use the id
    limit_key: Optional[str] = None  # key into house_style limits.fields
    unavailable_reason: Optional[str] = None  # non-None => nothing can produce it yet


@dataclass(frozen=True)
class Candidate:
    """An alternative value a generator offers beside its proposal.

    ``flags`` are the generator's own findings about this candidate; code
    stamps them ``origin=model`` and adds its own measurements.
    """

    value: str
    angle: Optional[str] = None
    flags: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class Proposal:
    """One generator's answer for one item.

    ``value=None`` with a ``missing_reason`` means the generator ran and could
    not produce a value; ``missing_state`` says whether that's a failure or a
    missing input.

    ``affirm=True`` means the generator read the basis and judged it right as
    written ("no change needed"). ``value`` is ignored. It is only a valid
    answer when the item has a basis; without one it reads as ``failed``.

    ``flags`` are the generator's findings (stamped ``origin=model``); each
    may set ``target`` to ``basis`` when it is about the existing copy.
    """

    value: Optional[str]
    origin: Origin
    generator: str
    model: Optional[str] = None
    backend: Optional[str] = None
    rationale: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    missing_reason: Optional[str] = None
    missing_state: GenerationState = GenerationState.failed
    affirm: bool = False
    alternatives: tuple[Candidate, ...] = ()
    flags: tuple[Mapping[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# The registry. Order here is the order the job page renders.
# ---------------------------------------------------------------------------
ITEM_SPECS: tuple[ItemSpec, ...] = (
    # -- copy ----------------------------------------------------------------
    ItemSpec(
        "title",
        ItemCategory.copy,
        Origin.inferred,
        sst_field_id="fldXqxjjxR4z5IJv6",
        sst_field="Release Title",
        limit_key="title",
    ),
    ItemSpec(
        "short_description",
        ItemCategory.copy,
        Origin.inferred,
        sst_field_id="fldDwTtKlOCdgKHpW",
        sst_field="Short Description",
        limit_key="short_description",
    ),
    ItemSpec(
        "long_description",
        ItemCategory.copy,
        Origin.inferred,
        sst_field_id="fld6HsWiKL77bFqo1",
        sst_field="Long Description",
        limit_key="long_description",
    ),
    # The SEO phase writes these from its role prompt and the reports drift;
    # their sections do not align 1:1 with AirTable fields. Parsing them would
    # produce confidently-wrong values rather than missing ones. They become
    # available when a generator produces them (#427).
    ItemSpec(
        "social_description",
        ItemCategory.copy,
        Origin.inferred,
        sst_field_id="fldntHlzk6PfIT5k2",
        sst_field="Social Media Description or Producer Draft",
        limit_key="social_description",
        unavailable_reason="structured_output:seo",
    ),
    ItemSpec(
        "facebook_description",
        ItemCategory.copy,
        Origin.inferred,
        sst_field_id="fldnprt2bJEsndv96",
        sst_field="Facebook & YT Description",
        limit_key="facebook_description",
        unavailable_reason="structured_output:seo",
    ),
    # -- metadata ------------------------------------------------------------
    ItemSpec(
        "keywords",
        ItemCategory.metadata,
        Origin.inferred,
        sst_field_id="fldjdPEXZyvx3rc6Y",
        sst_field="General Keywords/Tags",
        limit_key="keywords",
        unavailable_reason="structured_output:seo",
    ),
    ItemSpec(
        "hashtags",
        ItemCategory.metadata,
        Origin.inferred,
        sst_field_id="fldYSGo5EBidQYL7W",
        sst_field="Hashtags",
        limit_key="hashtags",
        unavailable_reason="structured_output:seo",
    ),
    ItemSpec(
        "social_tags",
        ItemCategory.metadata,
        Origin.inferred,
        sst_field_id="fldcenwfu4nEWjPbt",
        sst_field="Social Media Tags",
        limit_key="social_tags",
        unavailable_reason="structured_output:seo",
    ),
    # All four derive from Digital Premiere. Verified equal in 4/4 sampled
    # 6POL records (Media Manager == Entered into YouTube == Digital Premiere).
    # They are INTENT dates ("when this ideally entered the system"); a date a
    # human already set becomes the basis, so the editor sees a suggestion
    # rather than a silent overwrite.
    ItemSpec(
        "media_manager_date",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="fldYDsAyzPfpvYy41",
        sst_field="Media Manager",
    ),
    ItemSpec(
        "entered_youtube_date",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="fldlza0yrvJlZZL35",
        sst_field="Entered into YouTube",
    ),
    ItemSpec(
        "protrack_date",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="fldOYf9FmJY92v3ML",
        sst_field="ProTrack",
    ),
    ItemSpec(
        "transcript_date",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="fldmWY1YglRIpbF2E",
        sst_field="Transcript",
    ),
    # Links await integrations Cardigan lacks.
    # NOTE: imdb_link (fldPHQv2Gi4OMe6cA) is deliberately absent. It is a
    # url-typed field humans use as a scratchpad — live values include
    # "pending" and "done, pending".
    ItemSpec(
        "youtube_link",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="fldwV4AKLOqH77End",
        sst_field="YouTube Link",
        unavailable_reason="integration:youtube_api",
    ),
    ItemSpec(
        "final_website_link",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="flddW2yRjRcIY6g8U",
        sst_field="Final Website Link",
        unavailable_reason="integration:portal",
    ),
    ItemSpec(
        "media_manager_iframe",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="flduc22XbwzMwlCyt",
        sst_field="Media Manager iFrame",
        unavailable_reason="integration:mm_api",
    ),
    ItemSpec(
        "eidr_id",
        ItemCategory.metadata,
        Origin.deterministic,
        sst_field_id="fldcMsVsRnP7Bk5BS",
        sst_field="EIDR ID",
        unavailable_reason="integration:eidr",
    ),
    # -- outputs with no SST field ------------------------------------------
    # Reviewable, editable and copyable; never pushed. Delivery to an editor
    # is an open question on the map (Drive export, #259).
    ItemSpec(
        "formatted_transcript",
        ItemCategory.transcript,
        Origin.inferred,  # today's formatter is a model; a scripted one would be deterministic
    ),
)

SPECS_BY_KEY: dict[str, ItemSpec] = {spec.key: spec for spec in ITEM_SPECS}

DETERMINISTIC_DATE_KEYS: tuple[str, ...] = (
    "media_manager_date",
    "entered_youtube_date",
    "protrack_date",
    "transcript_date",
)

# Every SST field the registry reads, for a fields[]-scoped fetch.
SST_FIELD_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [spec.sst_field_id for spec in ITEM_SPECS if spec.sst_field_id]
        + [SST_STATUS_FIELD_ID, SST_PREMIERE_FIELD_ID, SST_MEDIA_ID_FIELD_ID]
    )
)


# ---------------------------------------------------------------------------
# The SST record
# ---------------------------------------------------------------------------


def normalize_sst_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a raw SST record to ``{field_id: value}`` plus ``record_id``.

    Expects a record fetched with ``returnFieldsByFieldId=true`` — see
    ``AirtableClient.get_sst_record(..., by_field_id=True)``. Only the fields
    the registry reads are kept.
    """
    fields = record.get("fields", {}) or {}
    context: dict[str, Any] = {field_id: fields.get(field_id) for field_id in SST_FIELD_IDS}
    context["record_id"] = record.get("id")
    return context


def derive_current_state(
    current_value: Any,
    ladder_value: Optional[str],
    *,
    fetched: bool,
) -> CurrentState:
    """Derive ``current_state`` from the field and the record ladder.

    Precedence:

    1. never fetched              -> ``unknown``
    2. record is Producer Issue   -> ``flagged`` (regardless of field value)
    3. field is blank             -> ``empty``   (Cardigan is first author)
    4. record approved by a human -> ``reviewed``
    5. otherwise                  -> ``unreviewed``
    """
    if not fetched:
        return CurrentState.unknown

    if ladder_value == LADDER_FLAGGED:
        return CurrentState.flagged

    if current_value is None or not str(current_value).strip():
        return CurrentState.empty

    if ladder_value in LADDER_REVIEWED:
        return CurrentState.reviewed

    return CurrentState.unreviewed


def _premiere_date(sst_context: Mapping[str, Any]) -> Optional[date]:
    """Parse ``Digital Premiere`` into a date. None when absent or malformed."""
    raw = sst_context.get(SST_PREMIERE_FIELD_ID)
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # AirTable returns ISO-8601, commonly "2026-05-28T22:00:00.000Z".
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        logger.debug("Could not parse Digital Premiere value %r", text)
        return None


# ---------------------------------------------------------------------------
# Deterministic generators
# ---------------------------------------------------------------------------


def premiere_date_proposals(sst_context: Optional[Mapping[str, Any]]) -> dict[str, Proposal]:
    """Propose the four intent dates from ``Digital Premiere``.

    Without a record, or without a premiere date on it, the dates are
    unavailable — there is nothing to derive them from.
    """
    premiere = _premiere_date(sst_context) if sst_context is not None else None
    reason = "no_sst_record" if sst_context is None else "field:digital_premiere"

    proposals: dict[str, Proposal] = {}
    for key in DETERMINISTIC_DATE_KEYS:
        if premiere is None:
            proposals[key] = Proposal(
                value=None,
                origin=Origin.deterministic,
                generator="premiere_dates",
                missing_reason=reason,
                missing_state=GenerationState.unavailable,
            )
        else:
            proposals[key] = Proposal(
                value=premiere.isoformat(),
                origin=Origin.deterministic,
                generator="premiere_dates",
                rationale="Digital Premiere date",
            )
    return proposals


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def split_title(title: str) -> tuple[str, Optional[str]]:
    """Split a Release Title into (title part, program name or None).

    "Wisconsin Life | Title" -> ("Title", "Wisconsin Life");
    "Title | Program" -> ("Title", "Program"); no separator -> (title, None).
    """
    for program in PREFIX_PROGRAMS:
        prefix = program + TITLE_SEPARATOR
        if title.startswith(prefix):
            return title[len(prefix) :], program
    if TITLE_SEPARATOR in title:
        part, program = title.rsplit(TITLE_SEPARATOR, 1)
        return part, program
    return title, None


_TERM_RE = re.compile(r"[A-Z][\w'’-]*[a-z][\w'’-]*")
_WORD_RE = re.compile(r"[\w'’-]+")


def _bare(word: str) -> str:
    """Lowercase, without a possessive or trailing hyphen."""
    word = word.lower().strip("'’-")
    for suffix in ("'s", "’s"):
        if word.endswith(suffix):
            word = word[: -len(suffix)]
    return word


def unsupported_terms(value: Optional[str], known_text: Optional[str]) -> list[str]:
    """Capitalised words in ``value`` that appear nowhere in ``known_text``.

    A cheap deterministic grounding check for names: a model that misspells a
    guest ("Jusstin" for "Justin", 2WLIGrizzWorksSM, 2026-10-22) produces a
    term the captions and SST never contain. Advisory — a correct name the
    sources simply don't spell out will be flagged too.
    """
    if not value or not known_text:
        return []
    known = {_bare(word) for word in _WORD_RE.findall(known_text)}
    found: list[str] = []
    for term in _TERM_RE.findall(value):
        bare = _bare(term)
        if bare and bare not in known and term not in found:
            parts = [p for p in re.split(r"[-’']", bare) if p]
            if not parts or not all(part in known for part in parts):
                found.append(term)
    return found


def _one_edit_apart(a: str, b: str) -> bool:
    """True when a and b differ by exactly one insertion, deletion or substitution."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (len(b) - j) + (len(a) - i) == 1


def spelling_conflicts(value: Optional[str], known_text: Optional[str]) -> list[tuple[str, str, int, int]]:
    """Names in ``value`` that the sources mostly spell another way.

    Returns ``(used, preferred, used_count, preferred_count)`` for each
    capitalised term that appears in ``known_text`` but is one edit away from
    a more frequent capitalised term there. Catches a typo inside a source — the Lower Thirds field
    read "Jusstin Howland" while the captions and descriptions said "Justin"
    (2WLIGrizzWorksSM, 2026-10-22) — which the unsupported-term check can't,
    because the typo is "known".
    """
    if not value or not known_text:
        return []
    counts: dict[str, int] = {}
    spelled: dict[str, str] = {}
    for term in _TERM_RE.findall(known_text):
        bare = _bare(term)
        if len(bare) >= 4:
            counts[bare] = counts.get(bare, 0) + 1
            spelled.setdefault(bare, term)

    conflicts: list[tuple[str, str, int, int]] = []
    seen: set[str] = set()
    for term in _TERM_RE.findall(value):
        bare = _bare(term)
        if len(bare) < 4 or bare in seen:
            continue
        seen.add(bare)
        used = counts.get(bare, 0)
        if used == 0:
            continue  # not in any source: unsupported_terms already flags it
        rivals = [(n, other) for other, n in counts.items() if n > used and _one_edit_apart(bare, other)]
        if rivals:
            n, other = max(rivals)
            conflicts.append((term, spelled[other], used, n))
    return conflicts


def _grounding_flags(value: Optional[str], known_text: Optional[str], target: "FlagTarget") -> list[dict[str, Any]]:
    def flag(rule_id: str, message: str) -> dict[str, Any]:
        return {
            "check": "grounding",
            "rule_id": rule_id,
            "severity": "warning",
            "message": message,
            "model_fixable": target != FlagTarget.basis,
            "origin": FlagOrigin.code.value,
            "target": target.value,
        }

    flags = [
        flag("grounding.unsupported_term", f"'{term}' appears nowhere in the captions, the SST record or the glossary")
        for term in unsupported_terms(value, known_text)
    ]
    flags += [
        flag(
            "grounding.spelling_conflict",
            f"'{used}' appears {used_n}x in the sources; '{preferred}' appears {preferred_n}x. Check the spelling.",
        )
        for used, preferred, used_n, preferred_n in spelling_conflicts(value, known_text)
    ]
    return flags


def join_title(part: str, program: Optional[str]) -> str:
    """Assemble a Release Title from its part and program, in the house position.

    The inverse of :func:`split_title`. A program name already on ``part`` is
    stripped first, so a model that ignores "don't include the program" can't
    double it.
    """
    part = part.strip()
    if not program:
        return part
    stripped, found = split_title(part)
    if found == program:
        part = stripped.strip()
    if program in PREFIX_PROGRAMS:
        return f"{program}{TITLE_SEPARATOR}{part}"
    return f"{part}{TITLE_SEPARATOR}{program}"


@dataclass(frozen=True)
class CharLimit:
    """A field's house-style limits. For the title, ``max`` applies to the
    title part and ``full_max`` to the whole string (#459)."""

    max: Optional[int] = None
    full_max: Optional[int] = None


def _char_limit(limit_key: Optional[str], limits: Mapping[str, Any]) -> CharLimit:
    """Look up a field's length limits from house_style, if it has any."""
    if not limit_key:
        return CharLimit()
    entry = limits.get(limit_key) or {}
    maximum, full = entry.get("max"), entry.get("full_max")
    return CharLimit(
        max=maximum if isinstance(maximum, int) else None,
        full_max=full if isinstance(full, int) else None,
    )


def _limit_flag(rule_id: str, message: str, target: FlagTarget) -> dict[str, Any]:
    return {
        "check": "char_limit",
        "rule_id": rule_id,
        "severity": "error",
        "message": message,
        "model_fixable": target != FlagTarget.basis,
        "origin": FlagOrigin.code.value,
        "target": target.value,
    }


def _char_limit_flags(
    value: Optional[str],
    limit: CharLimit,
    target: FlagTarget = FlagTarget.proposal,
    *,
    key: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Advisory flags when a value runs past its house-style limits.

    A title is measured without its program name against ``max``, and whole
    against ``full_max``.
    """
    if value is None:
        return []
    flags: list[dict[str, Any]] = []
    measured, program = split_title(value) if key == "title" else (value, None)
    if limit.max is not None and len(measured) > limit.max:
        what = "characters excluding the program name" if program else "characters"
        flags.append(_limit_flag("char_limit.max", f"{len(measured)} {what}; the limit is {limit.max}", target))
    if limit.full_max is not None and len(value) > limit.full_max:
        flags.append(
            _limit_flag(
                "char_limit.full_max", f"{len(value)} characters in full; the limit is {limit.full_max}", target
            )
        )
    return flags


def _model_flags(flags: tuple[Mapping[str, Any], ...], default_target: FlagTarget) -> list[dict[str, Any]]:
    """Stamp a generator's own findings as model-raised. Origin is not the
    generator's to claim; target defaults to what the flag came attached to.

    Each flag is validated on its own. A malformed one is dropped and logged,
    so it can't take the well-formed flags — or the code-measured ones stored
    beside them — down with it.
    """
    stamped: list[dict[str, Any]] = []
    for flag in flags:
        try:
            valid = ItemFlag(
                **{**flag, "origin": FlagOrigin.model.value, "target": flag.get("target", default_target.value)}
            )
        except (TypeError, ValueError) as err:
            logger.warning("Dropping malformed generator flag %r: %s", flag, err)
            continue
        stamped.append(valid.model_dump(mode="json"))
    return stamped


def _alternatives(
    candidates: tuple[Candidate, ...], limit: CharLimit, key: str, known_text: Optional[str] = None
) -> list[dict[str, Any]]:
    """Alternatives with code-measured limit flags beside the model's own."""
    return [
        {
            "value": candidate.value,
            "angle": candidate.angle,
            "flags": _model_flags(candidate.flags, FlagTarget.alternative)
            + _char_limit_flags(candidate.value, limit, FlagTarget.alternative, key=key)
            + _grounding_flags(candidate.value, known_text, FlagTarget.alternative),
        }
        for candidate in candidates
    ]


def build_items(
    *,
    job_id: int,
    proposals: Mapping[str, Proposal],
    sst_context: Optional[Mapping[str, Any]] = None,
    copy_generation: CopyMode | bool | str = CopyMode.suggest,
    content_type: str = "full",
    known_text: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build every item row for a job, ready for ``upsert_job_items``.

    Args:
        job_id: owning job.
        proposals: key -> Proposal from whatever generators ran. A key with
            no proposal ships as ``unavailable``/``not_generated``.
        sst_context: ``normalize_sst_record`` output. ``None`` means there was
            no record to read: every basis is null and every
            ``current_state`` is ``unknown``.
        copy_generation: the job's copy mode (``off | review | suggest``; a
            boolean is read as #426's on/off). Off, copy items still exist —
            with their basis — as ``unavailable``/``copy_generation_off``, and
            an editor can still type into them. Under ``review`` a copy item
            keeps its basis: any proposal or alternatives are dropped here,
            whatever the generator returned, and only affirmations and flags
            survive (``review_only``).
        content_type: 'full' | 'short' | 'clip', selects house-style limits.
        known_text: the job's source text (captions, SST record, glossary).
            When given, copy proposals and alternatives are checked for names
            that appear nowhere in it (``grounding.unsupported_term``).

    Rows never carry ``review_state`` or ``edited_value`` for an existing
    item; ``upsert_job_items`` owns that guarantee. Pure — no DB, no network.
    """
    copy_mode = CopyMode.parse(copy_generation)
    fetched = sst_context is not None
    sst: Mapping[str, Any] = sst_context or {}
    ladder = sst.get(SST_STATUS_FIELD_ID)
    now = datetime.utcnow()

    try:
        limits = load_rules().limits_for(content_type=content_type)
    except Exception:  # pragma: no cover - config problems must not break extraction
        logger.warning("Could not load house-style limits; items will carry no char_limit", exc_info=True)
        limits = {}

    rows: list[dict[str, Any]] = []
    for spec in ITEM_SPECS:
        raw_basis = sst.get(spec.sst_field_id) if spec.sst_field_id else None
        basis_value = str(raw_basis) if raw_basis is not None and str(raw_basis).strip() else None
        current_state = (
            derive_current_state(raw_basis, ladder, fetched=fetched) if spec.sst_field_id else CurrentState.unknown
        )
        char_limit = _char_limit(spec.limit_key, limits)

        proposal = proposals.get(spec.key)
        origin = proposal.origin if proposal else spec.origin
        proposed_value: Optional[str] = None
        reason: Optional[str] = None

        affirmed = False
        is_copy = spec.category == ItemCategory.copy
        review = is_copy and copy_mode == CopyMode.review

        if spec.unavailable_reason:
            state, reason = GenerationState.unavailable, spec.unavailable_reason
        elif is_copy and copy_mode == CopyMode.off:
            state, reason = GenerationState.unavailable, COPY_GENERATION_OFF
        elif review and basis_value is None:
            state, reason = GenerationState.unavailable, COPY_REVIEW_NO_BASIS
        elif proposal is None:
            state, reason = GenerationState.unavailable, NOT_GENERATED
        elif proposal.affirm:
            if basis_value is not None:
                state, reason, affirmed = GenerationState.ready, NO_CHANGE_NEEDED, True
            else:
                state, reason = GenerationState.failed, AFFIRM_WITHOUT_BASIS
        elif review and (proposal.value is not None or proposal.alternatives):
            # Review may only affirm or flag. The text is dropped, not the run.
            state, reason, affirmed = GenerationState.ready, REVIEW_ONLY, True
        elif proposal.value is None:
            state = proposal.missing_state
            reason = proposal.missing_reason or NO_VALUE_EXTRACTED
        else:
            state, proposed_value = GenerationState.ready, proposal.value

        produced = state == GenerationState.ready

        flags = _char_limit_flags(proposed_value, char_limit, key=spec.key)
        if is_copy:
            flags += _grounding_flags(proposed_value, known_text, FlagTarget.proposal)
        if affirmed:
            # The editor keeps the basis, so its measurements are the ones that matter.
            flags += _char_limit_flags(basis_value, char_limit, FlagTarget.basis, key=spec.key)
        if produced:
            default_target = FlagTarget.basis if affirmed else FlagTarget.proposal
            flags = _model_flags(proposal.flags, default_target) + flags
        rows.append(
            {
                "job_id": job_id,
                "key": spec.key,
                "category": spec.category.value,
                "origin": origin.value,
                "generation_state": state.value,
                "generation_reason": reason,
                "review_state": ReviewState.proposed.value,
                "basis_value": basis_value,
                "basis_fetched_at": now if fetched else None,
                "current_state": current_state.value,
                "proposed_value": proposed_value,
                "rationale": proposal.rationale if produced else None,
                "flags": flags,
                "alternatives": (
                    _alternatives(proposal.alternatives, char_limit, spec.key, known_text if is_copy else None)
                    if produced and not review
                    else []
                ),
                "char_limit": char_limit.max,
                "generator": proposal.generator if proposal else None,
                "model": proposal.model if proposal else None,
                "backend": proposal.backend if proposal else None,
                "input_tokens": proposal.input_tokens if produced else None,
                "output_tokens": proposal.output_tokens if produced else None,
                "cost_usd": proposal.cost_usd if produced else None,
                "generated_at": now if produced else None,
            }
        )

    return rows
