"""Unit tests for item extraction and derivation (#329).

Pure tests over ``api.services.items`` — no DB, no network. The fixtures below
mirror the documented output shapes in ``prompts/analyst.md`` and
``prompts/seo.md``; where a heading is asserted verbatim it is because the
extractor keys on it.
"""

import pytest

from api.models.item import CurrentState, ItemLayer, ItemSource, ItemStatus
from api.services.items import (
    DETERMINISTIC_DATE_KEYS,
    ITEM_SPECS,
    LADDER_FLAGGED,
    SPECS_BY_KEY,
    SST_STATUS_FIELD,
    build_items,
    derive_current_state,
    extract_context_items,
    extract_deliverable_items,
    normalize_sst_record,
)

# Mirrors prompts/analyst.md's "Brainstorming Document Structure".
ANALYST_MD = """# Brainstorming Document
**Project:** 6POL0111

## Summary

An episode about the state budget.

## Key Themes

1. **Redistricting**: maps and courts
2. **Budget**: the surplus fight

## Speakers & Roles

| Speaker | Role/Title | Context | First Appearance |
|---------|------------|---------|------------------|
| Zac Schultz | Host | Anchors the panel | 0:00 |

## Structural Breakdown

### Act 1: Introduction (0:00 - 2:00)
- Cold open

## SEO Keywords (Preliminary)

**Primary:** wisconsin politics

**Secondary:**
- state budget

**Location-Specific:**
- Madison
- Dane County

**Topical:**
- redistricting

## Editorial Opportunities

- **Hook**: The surplus nobody spent
- **Unique Angle**: Two parties, one stalemate

## Production Notes

Caption quality good.
"""

# Mirrors prompts/seo.md — note "### Title (Final Recommendation)"; the
# extractor matches on the "### Title" prefix.
SEO_MD = """# SEO Report

## Optimized Metadata

### Title (Final Recommendation)

**Recommended:**
Polling the failed surplus deal | Inside Wisconsin Politics

**Alternatives:**
1. Something else

---

### Short Description (90 chars max)

**Recommended:**
Zac Schultz breaks down the collapsed surplus negotiation.

---

### Long Description (350 chars max)

**Recommended:**
A longer description of the episode with rather more detail in it.
"""


class TestContextExtraction:
    def test_extracts_all_four_context_items(self):
        context = extract_context_items(ANALYST_MD)

        assert "Redistricting" in context["context.themes"]
        assert "Zac Schultz" in context["context.speakers"]
        assert "Madison" in context["context.place_names"]
        assert "surplus nobody spent" in context["context.editorial_angles"]

    def test_place_names_come_from_the_location_specific_block_only(self):
        """Location-Specific is nested inside SEO Keywords; sibling blocks
        (Primary/Secondary/Topical) must not bleed in."""
        context = extract_context_items(ANALYST_MD)
        place_names = context["context.place_names"]

        assert "Madison" in place_names
        assert "Dane County" in place_names
        assert "redistricting" not in place_names  # the Topical block
        assert "state budget" not in place_names  # the Secondary block

    def test_section_stops_at_next_heading(self):
        context = extract_context_items(ANALYST_MD)

        assert "Caption quality" not in context["context.editorial_angles"]
        assert "Act 1" not in context["context.speakers"]

    def test_missing_sections_yield_none_not_an_error(self):
        context = extract_context_items("# Brainstorming Document\n\nNothing here.\n")

        assert set(context) == {
            "context.themes",
            "context.speakers",
            "context.place_names",
            "context.editorial_angles",
        }
        assert all(value is None for value in context.values())

    def test_empty_input_is_safe(self):
        assert all(value is None for value in extract_context_items("").values())


class TestDeliverableExtraction:
    def test_extracts_the_three_governed_fields(self):
        values = extract_deliverable_items(seo_md=SEO_MD)

        assert values["title"] == "Polling the failed surplus deal | Inside Wisconsin Politics"
        assert values["short_description"] == "Zac Schultz breaks down the collapsed surplus negotiation."
        assert values["long_description"].startswith("A longer description")

    def test_transcript_is_taken_whole(self):
        body = "SPEAKER: line one.\n\nSPEAKER: line two.\n"
        values = extract_deliverable_items(formatter_md=body)

        assert values["formatted_transcript"] == body

    def test_no_extractor_for_the_drifting_fields(self):
        """The five drifting SEO fields must never be parsed out of prose —
        that would yield confidently-wrong values rather than missing ones."""
        values = extract_deliverable_items(seo_md=SEO_MD)

        for key in ("social_description", "social_tags", "facebook_description", "hashtags", "keywords"):
            assert key not in values


class TestCurrentStateDerivation:
    def test_never_fetched_is_unknown(self):
        assert derive_current_state("anything", "Ready for Review", fetched=False) == CurrentState.unknown

    @pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
    def test_blank_value_is_empty(self, blank):
        assert derive_current_state(blank, None, fetched=True) == CurrentState.empty

    def test_empty_wins_over_an_approved_record(self):
        """Cardigan is first author for a blank field regardless of where the
        record sits on the ladder — the 'fill gaps' half of the split."""
        state = derive_current_state("", "Final Copy & Images Approved by Producer", fetched=True)

        assert state == CurrentState.empty

    @pytest.mark.parametrize(
        "ladder",
        [
            "Edited & Approved by Digital",
            "Edited & Approved by Promotions",
            "Final Copy & Images Approved by Producer",
        ],
    )
    def test_approved_ladder_positions_are_reviewed(self, ladder):
        assert derive_current_state("a value", ladder, fetched=True) == CurrentState.reviewed

    @pytest.mark.parametrize("ladder", [None, "", "Ready for Review"])
    def test_unblessed_records_are_unreviewed(self, ladder):
        assert derive_current_state("a value", ladder, fetched=True) == CurrentState.unreviewed

    def test_producer_issue_flags_even_a_blank_field(self):
        """Producer Issue outranks emptiness — Promo Notes must be surfaced
        before anything is written to that record."""
        assert derive_current_state("", LADDER_FLAGGED, fetched=True) == CurrentState.flagged
        assert derive_current_state("a value", LADDER_FLAGGED, fetched=True) == CurrentState.flagged


class TestRegistry:
    def test_keys_are_unique(self):
        keys = [spec.key for spec in ITEM_SPECS]

        assert len(keys) == len(set(keys))

    def test_imdb_link_is_never_modelled(self):
        """It's a url-typed field humans use as a scratchpad ("pending",
        "done, pending"). Writing it would destroy tracking notes."""
        assert "imdb_link" not in SPECS_BY_KEY
        assert all(spec.airtable_field != "IMDb Link" for spec in ITEM_SPECS)

    def test_context_items_never_target_an_airtable_field(self):
        for spec in ITEM_SPECS:
            if spec.layer == ItemLayer.context:
                assert spec.airtable_field is None

    def test_blocked_specs_declare_a_reason(self):
        for spec in ITEM_SPECS:
            if spec.is_blocked:
                assert spec.blocked_on.startswith(("integration:", "structured_output:"))


class TestNormalizeSstRecord:
    def test_maps_fields_via_the_registry(self):
        record = {
            "id": "recABC",
            "fields": {
                "Media ID": "6POL0111",
                "Release Title": "Existing title",
                "YouTube Link": "https://youtu.be/abc",
                SST_STATUS_FIELD: "Final Copy & Images Approved by Producer",
                "Digital Premiere": "2026-05-28T22:00:00.000Z",
            },
        }

        context = normalize_sst_record(record)

        assert context["record_id"] == "recABC"
        assert context["title"] == "Existing title"
        assert context["youtube_link"] == "https://youtu.be/abc"
        assert context[SST_STATUS_FIELD] == "Final Copy & Images Approved by Producer"
        assert context["digital_premiere"] == "2026-05-28T22:00:00.000Z"

    def test_absent_fields_normalize_to_none(self):
        context = normalize_sst_record({"id": "recABC", "fields": {}})

        assert context["title"] is None
        assert context[SST_STATUS_FIELD] is None


class TestBuildItems:
    def _context(self, **overrides):
        base = {
            SST_STATUS_FIELD: None,
            "digital_premiere": "2026-05-28T22:00:00.000Z",
        }
        base.update(overrides)
        return base

    def test_builds_one_row_per_registered_spec(self):
        rows = build_items(job_id=1, analyst_md=ANALYST_MD, seo_md=SEO_MD)

        assert len(rows) == len(ITEM_SPECS)
        assert {row["key"] for row in rows} == set(SPECS_BY_KEY)

    def test_no_sst_context_means_everything_is_unknown(self):
        """The state for 27 of 30 production jobs today (#331)."""
        rows = build_items(job_id=1, seo_md=SEO_MD, sst_context=None)

        assert all(row["current_state"] == CurrentState.unknown.value for row in rows)
        assert all(row["current_fetched_at"] is None for row in rows)

    def test_drifting_fields_ship_blocked_with_no_value(self):
        rows = {row["key"]: row for row in build_items(job_id=1, seo_md=SEO_MD)}

        for key in ("social_description", "social_tags", "facebook_description", "hashtags", "keywords"):
            assert rows[key]["status"] == ItemStatus.awaiting_source.value
            assert rows[key]["source_blocked_on"] == "structured_output:seo"
            assert rows[key]["proposed_value"] is None

    def test_link_items_are_blocked_on_integrations(self):
        rows = {row["key"]: row for row in build_items(job_id=1)}

        assert rows["youtube_link"]["source_blocked_on"] == "integration:youtube_api"
        assert rows["final_website_link"]["source_blocked_on"] == "integration:portal"
        assert rows["media_manager_iframe"]["source_blocked_on"] == "integration:mm_api"
        assert rows["eidr_id"]["source_blocked_on"] == "integration:eidr"

    def test_dates_derive_from_digital_premiere_and_auto_approve_when_empty(self):
        rows = {row["key"]: row for row in build_items(job_id=1, sst_context=self._context())}

        for key in DETERMINISTIC_DATE_KEYS:
            assert rows[key]["proposed_value"] == "2026-05-28"
            assert rows[key]["status"] == ItemStatus.approved.value
            assert rows[key]["source"] == ItemSource.deterministic.value

    def test_dates_never_silently_overwrite_an_existing_value(self):
        """Fill-only: a date a human already set goes to the editor instead."""
        context = self._context(entered_youtube_date="2026-01-01")
        rows = {row["key"]: row for row in build_items(job_id=1, sst_context=context)}

        assert rows["entered_youtube_date"]["status"] == ItemStatus.pending_review.value
        assert rows["media_manager_date"]["status"] == ItemStatus.approved.value

    def test_dates_block_when_premiere_is_missing(self):
        context = self._context(digital_premiere=None)
        rows = {row["key"]: row for row in build_items(job_id=1, sst_context=context)}

        for key in DETERMINISTIC_DATE_KEYS:
            assert rows[key]["status"] == ItemStatus.awaiting_source.value
            assert rows[key]["source_blocked_on"] == "field:digital_premiere"

    def test_malformed_premiere_does_not_raise(self):
        context = self._context(digital_premiere="not a date")
        rows = {row["key"]: row for row in build_items(job_id=1, sst_context=context)}

        assert rows["protrack_date"]["status"] == ItemStatus.awaiting_source.value

    def test_char_limits_come_from_house_style(self):
        rows = {row["key"]: row for row in build_items(job_id=1, seo_md=SEO_MD)}

        assert rows["short_description"]["char_limit"] == 90
        assert rows["long_description"]["char_limit"] == 350
        assert rows["title"]["char_limit"] == 80
        assert rows["formatted_transcript"]["char_limit"] is None

    def test_provenance_records_the_producing_phase_and_model(self):
        rows = {
            row["key"]: row
            for row in build_items(
                job_id=1,
                analyst_md=ANALYST_MD,
                seo_md=SEO_MD,
                phase_models={"seo": "anthropic/claude-sonnet-5", "analyst": "openai/gpt-5"},
            )
        }

        assert rows["title"]["phase"] == "seo"
        assert rows["title"]["model"] == "anthropic/claude-sonnet-5"
        assert rows["context.themes"]["model"] == "openai/gpt-5"

    def test_producer_approved_record_marks_populated_fields_reviewed(self):
        context = self._context(
            **{SST_STATUS_FIELD: "Final Copy & Images Approved by Producer", "title": "Existing title"}
        )
        rows = {row["key"]: row for row in build_items(job_id=1, seo_md=SEO_MD, sst_context=context)}

        assert rows["title"]["current_state"] == CurrentState.reviewed.value
        assert rows["title"]["current_value"] == "Existing title"
        # The proposal still stands — 'reviewed' means confirm, not refuse.
        assert rows["title"]["proposed_value"].startswith("Polling the failed surplus deal")
        # A blank field on the same approved record is still Cardigan's to fill.
        assert rows["short_description"]["current_state"] == CurrentState.empty.value

    def test_nothing_is_ever_blocked_by_a_flag(self):
        """No status in the model can prevent an editor from approving."""
        rows = build_items(job_id=1, analyst_md=ANALYST_MD, seo_md=SEO_MD)

        assert all(row["status"] != "blocked" for row in rows)
        assert all(row["status"] != "paused" for row in rows)


class TestShippedConfigGate:
    def test_item_extraction_is_off_by_default(self):
        """The worker's item hook performs an Airtable lookup per completed
        job. It must stay inert in the shipped config until the approval
        surface exists; the dev instance flips it on locally."""
        import json
        from pathlib import Path

        config = json.loads(Path("config/llm-config.json").read_text())

        assert config["routing"]["items"]["enabled"] is False
