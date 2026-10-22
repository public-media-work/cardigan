"""Unit tests for item assembly and the legacy adapter (#426).

Pure tests over ``api.services.items`` and ``api.services.legacy_item_adapter``
— no DB, no network. ``SEO_MD`` mirrors the documented output shape in
``prompts/seo.md``; where a heading is asserted verbatim it is because the
extractor keys on it.
"""

import json
from pathlib import Path

from api.models.item import NO_CHANGE_NEEDED, CurrentState, GenerationState, ItemCategory, Origin
from api.services.items import (
    AFFIRM_WITHOUT_BASIS,
    COPY_GENERATION_OFF,
    DETERMINISTIC_DATE_KEYS,
    ITEM_SPECS,
    LADDER_FLAGGED,
    NOT_GENERATED,
    SPECS_BY_KEY,
    SST_FIELD_IDS,
    SST_PREMIERE_FIELD_ID,
    SST_STATUS_FIELD_ID,
    Candidate,
    Proposal,
    build_items,
    derive_current_state,
    normalize_sst_record,
    premiere_date_proposals,
    split_title,
)
from api.services.legacy_item_adapter import extract_legacy_proposals

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

TITLE_ID = SPECS_BY_KEY["title"].sst_field_id
SHORT_ID = SPECS_BY_KEY["short_description"].sst_field_id


def sst(**by_key):
    """Build a normalized SST context, addressing fields by item key."""
    context = {field_id: None for field_id in SST_FIELD_IDS}
    context["record_id"] = "recTEST"
    for key, value in by_key.items():
        if key == "ladder":
            context[SST_STATUS_FIELD_ID] = value
        elif key == "premiere":
            context[SST_PREMIERE_FIELD_ID] = value
        else:
            context[SPECS_BY_KEY[key].sst_field_id] = value
    return context


def by_key(rows):
    return {row["key"]: row for row in rows}


def legacy(**kwargs):
    return extract_legacy_proposals(phase_models={"seo": "haiku", "formatter": "sonnet"}, **kwargs)


class TestRegistry:
    def test_keys_are_unique(self):
        keys = [spec.key for spec in ITEM_SPECS]
        assert len(keys) == len(set(keys))

    def test_sst_field_ids_are_unique(self):
        ids = [spec.sst_field_id for spec in ITEM_SPECS if spec.sst_field_id]
        assert len(ids) == len(set(ids))

    def test_no_context_items(self):
        """Speakers, places, themes and angles are bundle parts, not items."""
        assert not any(spec.key.startswith("context.") for spec in ITEM_SPECS)
        assert {spec.category for spec in ITEM_SPECS} <= set(ItemCategory)

    def test_transcript_has_no_sst_field(self):
        assert SPECS_BY_KEY["formatted_transcript"].sst_field_id is None

    def test_dates_are_deterministic(self):
        assert all(SPECS_BY_KEY[key].origin == Origin.deterministic for key in DETERMINISTIC_DATE_KEYS)

    def test_imdb_link_is_never_modelled(self):
        """A url field humans use as a scratchpad ("done, pending")."""
        assert "imdb_link" not in SPECS_BY_KEY
        assert "fldPHQv2Gi4OMe6cA" not in SST_FIELD_IDS


class TestCurrentStateDerivation:
    def test_never_fetched_is_unknown(self):
        assert derive_current_state("x", None, fetched=False) == CurrentState.unknown

    def test_blank_field_is_empty_even_on_an_approved_record(self):
        state = derive_current_state("", "Edited & Approved by Digital", fetched=True)
        assert state == CurrentState.empty

    def test_flagged_wins_over_a_value(self):
        assert derive_current_state("x", LADDER_FLAGGED, fetched=True) == CurrentState.flagged

    def test_value_on_approved_record_is_reviewed(self):
        state = derive_current_state("x", "Final Copy & Images Approved by Producer", fetched=True)
        assert state == CurrentState.reviewed

    def test_value_without_approval_is_unreviewed(self):
        assert derive_current_state("x", "Ready for Review", fetched=True) == CurrentState.unreviewed


class TestNormalizeSstRecord:
    def test_reads_fields_by_id(self):
        record = {"id": "rec1", "fields": {TITLE_ID: "A title", SST_STATUS_FIELD_ID: "Ready for Review"}}

        context = normalize_sst_record(record)

        assert context[TITLE_ID] == "A title"
        assert context[SST_STATUS_FIELD_ID] == "Ready for Review"
        assert context["record_id"] == "rec1"

    def test_ignores_fields_keyed_by_name(self):
        """A name-keyed record must not be mistaken for an id-keyed one: the
        base renames fields in place, so names are never trusted."""
        record = {"id": "rec1", "fields": {"Release Title": "A title"}}

        assert normalize_sst_record(record)[TITLE_ID] is None


class TestLegacyAdapter:
    def test_seo_fields_are_inferred_with_provenance(self):
        proposals = legacy(seo_md=SEO_MD)

        title = proposals["title"]
        assert title.value.startswith("Polling the failed surplus deal")
        assert title.origin == Origin.inferred
        assert title.generator == "legacy_adapter:seo"
        assert title.model == "haiku"

    def test_transcript_is_inferred(self):
        """Today's formatter is a model, so its transcript is inferred."""
        proposal = legacy(formatter_md="Speaker 1: hello")["formatted_transcript"]
        assert proposal.origin == Origin.inferred
        assert proposal.model == "sonnet"

    def test_missing_output_yields_no_proposal(self):
        assert legacy() == {}

    def test_present_output_without_a_section_yields_an_empty_proposal(self):
        proposals = legacy(seo_md="# SEO Report\n\nNothing parseable here.\n")
        assert proposals["title"].value is None

    def test_never_reads_the_analyst(self):
        """The analyst's material belongs to the context bundle."""
        import inspect

        assert "analyst" not in inspect.signature(extract_legacy_proposals).parameters


class TestPremiereDates:
    def test_dates_follow_digital_premiere(self):
        proposals = premiere_date_proposals(sst(premiere="2026-05-28T22:00:00.000Z"))

        assert {proposals[key].value for key in DETERMINISTIC_DATE_KEYS} == {"2026-05-28"}
        assert all(proposals[key].origin == Origin.deterministic for key in DETERMINISTIC_DATE_KEYS)

    def test_no_record_means_unavailable(self):
        proposal = premiere_date_proposals(None)["media_manager_date"]
        assert proposal.value is None
        assert proposal.missing_state == GenerationState.unavailable
        assert proposal.missing_reason == "no_sst_record"

    def test_record_without_premiere_means_unavailable(self):
        proposal = premiere_date_proposals(sst())["protrack_date"]
        assert proposal.missing_reason == "field:digital_premiere"


class TestBuildItems:
    def test_one_row_per_spec(self):
        rows = build_items(job_id=1, proposals={})
        assert [row["key"] for row in rows] == [spec.key for spec in ITEM_SPECS]

    def test_every_row_starts_proposed(self):
        rows = build_items(job_id=1, proposals=legacy(seo_md=SEO_MD))
        assert {row["review_state"] for row in rows} == {"proposed"}

    def test_ready_item_carries_its_proposal(self):
        row = by_key(build_items(job_id=1, proposals=legacy(seo_md=SEO_MD)))["title"]

        assert row["generation_state"] == "ready"
        assert row["generation_reason"] is None
        assert row["origin"] == "inferred"
        assert row["proposed_value"].startswith("Polling")
        assert row["generator"] == "legacy_adapter:seo"
        assert row["generated_at"] is not None

    def test_no_record_means_no_basis_and_unknown(self):
        rows = build_items(job_id=1, proposals=legacy(seo_md=SEO_MD))

        assert all(row["basis_value"] is None for row in rows)
        assert all(row["current_state"] == "unknown" for row in rows)
        assert all(row["basis_fetched_at"] is None for row in rows)

    def test_basis_is_the_target_value_at_generation(self):
        """A non-empty basis is what makes a proposal a suggestion."""
        context = sst(short_description="What the producer wrote.", ladder="Edited & Approved by Digital")

        row = by_key(build_items(job_id=1, proposals=legacy(seo_md=SEO_MD), sst_context=context))["short_description"]

        assert row["basis_value"] == "What the producer wrote."
        assert row["current_state"] == "reviewed"
        assert row["proposed_value"].startswith("Zac Schultz")

    def test_blank_target_is_a_fill(self):
        row = by_key(build_items(job_id=1, proposals=legacy(seo_md=SEO_MD), sst_context=sst(title="   ")))["title"]

        assert row["basis_value"] is None
        assert row["current_state"] == "empty"

    def test_structurally_unavailable_items(self):
        rows = by_key(build_items(job_id=1, proposals=legacy(seo_md=SEO_MD)))

        assert rows["keywords"]["generation_state"] == "unavailable"
        assert rows["keywords"]["generation_reason"] == "structured_output:seo"
        assert rows["youtube_link"]["generation_reason"] == "integration:youtube_api"
        assert rows["keywords"]["proposed_value"] is None

    def test_unavailable_items_still_carry_their_basis(self):
        context = sst(keywords="politics, budget")

        row = by_key(build_items(job_id=1, proposals={}, sst_context=context))["keywords"]

        assert row["generation_state"] == "unavailable"
        assert row["basis_value"] == "politics, budget"

    def test_missing_phase_means_not_generated(self):
        row = by_key(build_items(job_id=1, proposals={}))["title"]
        assert row["generation_state"] == "unavailable"
        assert row["generation_reason"] == NOT_GENERATED

    def test_unparseable_output_is_a_failure(self):
        proposals = legacy(seo_md="# SEO Report\n\nNothing parseable here.\n")

        row = by_key(build_items(job_id=1, proposals=proposals))["title"]

        assert row["generation_state"] == "failed"
        assert row["generation_reason"] == "no_value_extracted"

    def test_copy_generation_off(self):
        """Copy items still exist, with their basis, but nothing is proposed."""
        context = sst(title="The current title")

        rows = by_key(
            build_items(
                job_id=1,
                proposals=legacy(seo_md=SEO_MD, formatter_md="text"),
                sst_context=context,
                copy_generation=False,
            )
        )

        title = rows["title"]
        assert title["generation_state"] == "unavailable"
        assert title["generation_reason"] == COPY_GENERATION_OFF
        assert title["proposed_value"] is None
        assert title["basis_value"] == "The current title"
        # Non-copy items are unaffected.
        assert rows["formatted_transcript"]["generation_state"] == "ready"

    def test_dates_are_deterministic_and_never_auto_accepted(self):
        """Review is the editor's axis: a fill-only date is still proposed."""
        proposals = premiere_date_proposals(sst(premiere="2026-05-28"))

        row = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst(premiere="2026-05-28")))[
            "media_manager_date"
        ]

        assert row["origin"] == "deterministic"
        assert row["proposed_value"] == "2026-05-28"
        assert row["review_state"] == "proposed"
        assert row["model"] is None

    def test_a_set_date_becomes_the_basis(self):
        context = sst(premiere="2026-05-28", media_manager_date="2026-05-30")

        row = by_key(build_items(job_id=1, proposals=premiere_date_proposals(context), sst_context=context))[
            "media_manager_date"
        ]

        assert row["basis_value"] == "2026-05-30"
        assert row["proposed_value"] == "2026-05-28"

    def test_char_limit_flag_is_advisory(self):
        proposals = {
            "short_description": Proposal(value="x" * 500, origin=Origin.inferred, generator="test"),
        }

        row = by_key(build_items(job_id=1, proposals=proposals))["short_description"]

        assert row["generation_state"] == "ready"
        assert row["review_state"] == "proposed"
        assert [flag["check"] for flag in row["flags"]] == ["char_limit"]

    def test_spend_is_recorded_when_a_generator_reports_it(self):
        proposals = {
            "title": Proposal(
                value="T",
                origin=Origin.inferred,
                generator="seo_title",
                model="m",
                input_tokens=100,
                output_tokens=20,
                cost_usd=0.001,
            )
        }

        row = by_key(build_items(job_id=1, proposals=proposals))["title"]

        assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (100, 20, 0.001)

    def test_rows_never_carry_an_edit(self):
        """Hand edits belong to the editor; assembly never writes one."""
        rows = build_items(job_id=1, proposals=legacy(seo_md=SEO_MD))
        assert all("edited_value" not in row for row in rows)


class TestAffirmation:
    """A generator can answer "no change needed" — distinct from not generating."""

    def test_affirming_a_basis_is_ready_with_no_proposal(self):
        proposals = {"short_description": Proposal(value=None, origin=Origin.inferred, generator="g", affirm=True)}

        row = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst(short_description="Keep me.")))[
            "short_description"
        ]

        assert row["generation_state"] == "ready"
        assert row["generation_reason"] == NO_CHANGE_NEEDED
        assert row["proposed_value"] is None
        assert row["basis_value"] == "Keep me."
        assert row["generated_at"] is not None

    def test_affirming_nothing_is_a_failure(self):
        proposals = {"short_description": Proposal(value=None, origin=Origin.inferred, generator="g", affirm=True)}

        row = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst()))["short_description"]

        assert row["generation_state"] == "failed"
        assert row["generation_reason"] == AFFIRM_WITHOUT_BASIS

    def test_affirm_ignores_a_stray_value(self):
        proposals = {"title": Proposal(value="Stray", origin=Origin.inferred, generator="g", affirm=True)}

        row = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst(title="Kept | Program")))["title"]

        assert row["proposed_value"] is None

    def test_an_affirmed_basis_over_its_limit_is_flagged_on_the_basis(self):
        proposals = {"short_description": Proposal(value=None, origin=Origin.inferred, generator="g", affirm=True)}

        row = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst(short_description="x" * 500)))[
            "short_description"
        ]

        assert [(f["check"], f["target"], f["model_fixable"]) for f in row["flags"]] == [("char_limit", "basis", False)]


class TestFlagProvenance:
    def test_code_flags_say_so(self):
        proposals = {"short_description": Proposal(value="x" * 500, origin=Origin.inferred, generator="g")}

        flag = by_key(build_items(job_id=1, proposals=proposals))["short_description"]["flags"][0]

        assert (flag["origin"], flag["target"]) == ("code", "proposal")

    def test_generator_flags_are_stamped_model_whatever_they_claim(self):
        claimed = {"check": "accuracy", "rule_id": "a.1", "severity": "warning", "message": "m", "origin": "code"}
        proposals = {"title": Proposal(value="T", origin=Origin.inferred, generator="g", flags=(claimed,))}

        flag = by_key(build_items(job_id=1, proposals=proposals))["title"]["flags"][0]

        assert flag["origin"] == "model"
        assert flag["target"] == "proposal"

    def test_generator_flags_on_an_affirmation_default_to_the_basis(self):
        finding = {"check": "accuracy", "rule_id": "a.1", "severity": "warning", "message": "m"}
        proposals = {
            "title": Proposal(value=None, origin=Origin.inferred, generator="g", affirm=True, flags=(finding,))
        }

        flag = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst(title="Kept")))["title"]["flags"][0]

        assert flag["target"] == "basis"

    def test_a_malformed_generator_flag_drops_alone(self):
        good = {"check": "accuracy", "rule_id": "a.1", "severity": "warning", "message": "m"}
        bad_target = {**good, "target": "both"}
        missing = {"check": "accuracy"}
        proposals = {
            "short_description": Proposal(
                value="x" * 500, origin=Origin.inferred, generator="g", flags=(bad_target, good, missing)
            )
        }

        flags = by_key(build_items(job_id=1, proposals=proposals))["short_description"]["flags"]

        assert [(f["check"], f["origin"]) for f in flags] == [("accuracy", "model"), ("char_limit", "code")]


class TestAlternatives:
    def test_alternatives_carry_code_measured_limit_flags(self):
        proposals = {
            "title": Proposal(
                value="Main",
                origin=Origin.inferred,
                generator="g",
                alternatives=(Candidate("Short one", angle="place"), Candidate("y" * 120, angle="question")),
            )
        }

        alternatives = by_key(build_items(job_id=1, proposals=proposals))["title"]["alternatives"]

        assert [alt["angle"] for alt in alternatives] == ["place", "question"]
        assert alternatives[0]["flags"] == []
        assert [(f["rule_id"], f["target"]) for f in alternatives[1]["flags"]] == [
            ("char_limit.max", "alternative"),
            ("char_limit.full_max", "alternative"),
        ]

    def test_no_alternatives_unless_the_item_was_produced(self):
        proposals = {
            "title": Proposal(value="T", origin=Origin.inferred, generator="g", alternatives=(Candidate("A"),))
        }

        row = by_key(build_items(job_id=1, proposals=proposals, copy_generation=False))["title"]

        assert row["alternatives"] == []

    def test_an_affirmation_can_still_offer_alternatives(self):
        proposals = {
            "title": Proposal(
                value=None, origin=Origin.inferred, generator="g", affirm=True, alternatives=(Candidate("A"),)
            )
        }

        row = by_key(build_items(job_id=1, proposals=proposals, sst_context=sst(title="Kept")))["title"]

        assert [alt["value"] for alt in row["alternatives"]] == ["A"]


class TestTitleLimits:
    """Ruling on #459: 80 for the title part, 100 for the whole string."""

    def flags(self, title):
        proposals = {"title": Proposal(value=title, origin=Origin.inferred, generator="g")}
        return [f["rule_id"] for f in by_key(build_items(job_id=1, proposals=proposals))["title"]["flags"]]

    def test_split_appended_program(self):
        assert split_title("Track records | Inside Wisconsin Politics") == (
            "Track records",
            "Inside Wisconsin Politics",
        )

    def test_split_prepended_program(self):
        assert split_title("Wisconsin Life | Grizz Works") == ("Grizz Works", "Wisconsin Life")

    def test_split_without_program(self):
        assert split_title("Episode intro") == ("Episode intro", None)

    def test_program_name_does_not_count_toward_80(self):
        # The #459 case: 88 characters in full, 60 before the program name.
        title = "What are the track records of Tom Tiffany and David Crowley? | Inside Wisconsin Politics"
        assert len(title) == 88
        assert self.flags(title) == []

    def test_title_part_over_80_is_flagged(self):
        assert self.flags("x" * 81 + " | Here & Now") == ["char_limit.max"]

    def test_wisconsin_life_part_is_measured_after_the_prefix(self):
        assert self.flags("Wisconsin Life | " + "x" * 80) == []
        assert self.flags("Wisconsin Life | " + "x" * 81) == ["char_limit.max"]

    def test_whole_string_over_100_is_flagged(self):
        assert self.flags("x" * 75 + " | " + "y" * 30) == ["char_limit.full_max"]

    def test_char_limit_column_is_the_title_part_limit(self):
        proposals = {"title": Proposal(value="T", origin=Origin.inferred, generator="g")}
        assert by_key(build_items(job_id=1, proposals=proposals))["title"]["char_limit"] == 80


class TestShippedConfigGate:
    def test_item_extraction_is_off_by_default(self):
        """The worker's item hook performs an AirTable lookup per completed
        job. It must stay inert in the shipped config until the job page
        exists; the dev instance flips it on locally."""
        config = json.loads(Path("config/llm-config.json").read_text())

        assert config["routing"]["items"]["enabled"] is False

    def test_copy_generation_defaults_on(self):
        config = json.loads(Path("config/llm-config.json").read_text())

        assert config["routing"]["items"]["copy_generation"] is True
