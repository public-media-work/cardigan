"""Tests for api.services.style_engine.lint.run_lint -- the deterministic
validator checklist (task 2a).

All rule data and sample phase output is synthetic -- built inline as
StyleRules(raw=...) plus small doc-builder helpers -- never depends on
config/house_style.yaml or prompts/*.md. Mirrors the fixture/helper style
of tests/test_style_stages.py and tests/test_style_scanner_limits.py.
"""

from __future__ import annotations

from api.services.style_engine.lint import run_lint
from api.services.style_engine.rules import StyleRules

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PERMISSIVE_SPEAKER_PATTERN = r"^\*\*[A-Z][\w.'-]*(?:\s[A-Z][\w.'-]*)*:\*\*"


def _rules(
    title_max: int = 80,
    short_max: int = 90,
    long_max: int = 350,
    keyword_min: int = 15,
    keyword_max: int = 20,
    speaker_label_pattern: str | None = _PERMISSIVE_SPEAKER_PATTERN,
    no_honorifics: bool = True,
    review_notes_placement: str | None = "top",
) -> StyleRules:
    speaker_label: dict = {}
    if speaker_label_pattern:
        speaker_label = {"pattern": speaker_label_pattern, "no_honorifics": no_honorifics}

    review_notes: dict = {}
    if review_notes_placement is not None:
        review_notes = {"placement": review_notes_placement, "format": "html_comment", "tier": "flag"}

    raw = {
        "meta": {"version": 1},
        "limits": {
            "fields": {
                "title": {"max": title_max},
                "short_description": {"max": short_max},
                "long_description": {"max": long_max},
                "keywords": {"count": {"min": keyword_min, "max": keyword_max}},
            },
            "content_type_overrides": {},
        },
        "phases": {
            "formatter": {
                "speaker_label": speaker_label,
                "review_notes": review_notes,
            },
        },
    }
    return StyleRules(raw=raw)


def _seo_output(
    title: str = "Wisconsin budget deal reached in Madison",
    short: str = "Lawmakers reach a bipartisan budget agreement in Madison.",
    long: str = (
        "Wisconsin lawmakers reached a bipartisan state budget agreement after "
        "weeks of negotiation, Gov. Evers said Tuesday in Madison."
    ),
    tags: list[str] | None = None,
    include_tags_section: bool = True,
    legacy_fenced_tags: bool = False,
) -> str:
    if tags is None:
        tags = [f"keyword{i}" for i in range(1, 18)]  # 17 items -- within 15-20
    tags_section = ""
    if include_tags_section:
        tags_line = ", ".join(tags)
        if legacy_fenced_tags:
            # Pre-#380 outputs carried the list in a fenced code block.
            tags_section = (
                "## Tags (Platform-Specific)\n\n### YouTube Tags (15-20 recommended)\n\n```\n" f"{tags_line}\n```\n"
            )
        else:
            # Current prompts/seo.md shape: plain labelled paragraph (#380).
            tags_section = (
                "## Tags (Platform-Specific)\n\n### YouTube Tags (15-20 recommended)\n\n"
                "**Tags (paste-ready):**\n"
                f"{tags_line}\n"
            )
    return (
        "# SEO Report\n\n"
        "### Title (Final Recommendation)\n\n"
        "**Recommended:**\n"
        f"{title}\n\n"
        "---\n\n"
        "### Short Description (90 chars max)\n\n"
        "**Recommended:**\n"
        f"{short}\n\n"
        "---\n\n"
        "### Long Description (350 chars max)\n\n"
        "**Recommended:**\n"
        f"{long}\n\n"
        "---\n\n" + tags_section
    )


_DEFAULT_FORMATTER_BODY = (
    "**John Smith:**\n"
    "Today we discuss the state budget agreement reached in Madison this week.\n\n"
    "**Sarah Johnson:**\n"
    "That's right, and it comes after weeks of intense negotiation."
)

_FORMATTER_HEADER = (
    "# Formatted Transcript\n"
    "**Project:** 2WLI1234HD\n"
    "**Program:** Here & Now\n"
    "**Duration:** 10:00\n"
    "**Date Processed:** 2026-07-10\n"
)


def _formatter_output(
    body: str = _DEFAULT_FORMATTER_BODY,
    review_notes: str | None = None,
    review_notes_before_header: bool = False,
    status: str = "ready_for_editing",
) -> str:
    notes_block = f"{review_notes}\n\n" if review_notes else ""
    if review_notes_before_header:
        return f"{notes_block}{_FORMATTER_HEADER}\n---\n\n{body}\n\n---\n\n**Status:** {status}\n"
    return f"{_FORMATTER_HEADER}\n---\n\n{notes_block}{body}\n\n---\n\n**Status:** {status}\n"


def _analyst_output(
    text: str = "Analyst structural breakdown of the segment with themes, topics, and speaker notes.",
) -> str:
    return text


def _violations_for(result, phase: str, rule_id: str) -> list:
    return [v for v in result[phase].violations if v.rule_id == rule_id]


# ---------------------------------------------------------------------------
# run_lint -- top-level shape
# ---------------------------------------------------------------------------


class TestRunLintShape:
    def test_always_returns_all_three_canonical_phases(self):
        rules = _rules()
        result = run_lint({}, rules)
        assert set(result.keys()) == {"analyst", "formatter", "seo"}

    def test_ignores_unknown_phases_in_context(self):
        rules = _rules()
        result = run_lint({"timestamp_output": "irrelevant"}, rules)
        assert set(result.keys()) == {"analyst", "formatter", "seo"}


# ---------------------------------------------------------------------------
# lint.output_missing -- all 3 phases
# ---------------------------------------------------------------------------


class TestOutputMissing:
    def test_absent_key_fires_for_all_three_phases(self):
        rules = _rules()
        result = run_lint({}, rules)
        for phase in ("analyst", "formatter", "seo"):
            violations = _violations_for(result, phase, "lint.output_missing")
            assert len(violations) == 1
            assert violations[0].model_fixable is False
            assert violations[0].severity == "error"

    def test_comment_only_content_fires(self):
        rules = _rules()
        context = {"analyst_output": "<!-- nothing but a provenance comment here, no real analyst content at all -->"}
        result = run_lint(context, rules)
        violations = _violations_for(result, "analyst", "lint.output_missing")
        assert len(violations) == 1

    def test_substantial_content_does_not_fire(self):
        rules = _rules()
        context = {"analyst_output": _analyst_output("x" * 60)}
        result = run_lint(context, rules)
        assert _violations_for(result, "analyst", "lint.output_missing") == []

    def test_none_value_fires(self):
        rules = _rules()
        result = run_lint({"seo_output": None}, rules)
        assert len(_violations_for(result, "seo", "lint.output_missing")) == 1


# ---------------------------------------------------------------------------
# lint.placeholder_text -- all 3 phases
# ---------------------------------------------------------------------------


class TestPlaceholderText:
    def test_media_id_literal_fires(self):
        rules = _rules()
        context = {"analyst_output": _analyst_output("Project {media_id} covers the state budget debate in detail.")}
        result = run_lint(context, rules)
        violations = _violations_for(result, "analyst", "lint.placeholder_text")
        assert len(violations) == 1
        assert violations[0].model_fixable is True
        assert violations[0].severity == "error"

    def test_today_literal_fires(self):
        rules = _rules()
        context = {"formatter_output": _formatter_output().replace("2026-07-10", "{TODAY'S DATE}")}
        result = run_lint(context, rules)
        assert len(_violations_for(result, "formatter", "lint.placeholder_text")) == 1

    def test_insert_literal_fires(self):
        rules = _rules()
        context = {
            "analyst_output": _analyst_output("[INSERT episode summary here] plus additional analyst notes text.")
        }
        result = run_lint(context, rules)
        assert len(_violations_for(result, "analyst", "lint.placeholder_text")) == 1

    def test_model_name_literal_fires(self):
        rules = _rules()
        context = {
            "analyst_output": _analyst_output("Generated by {model name} during the analyst structural pass today.")
        }
        result = run_lint(context, rules)
        assert len(_violations_for(result, "analyst", "lint.placeholder_text")) == 1

    def test_recommended_empty_value_fires(self):
        rules = _rules()
        seo = _seo_output(title="")  # blank line follows **Recommended:**
        result = run_lint({"seo_output": seo}, rules)
        violations = _violations_for(result, "seo", "lint.placeholder_text")
        assert len(violations) == 1
        assert violations[0].model_fixable is True

    def test_recommended_bracket_placeholder_fires(self):
        rules = _rules()
        seo = _seo_output(title="[55-60 character title summarizing the episode...]")
        result = run_lint({"seo_output": seo}, rules)
        assert len(_violations_for(result, "seo", "lint.placeholder_text")) == 1

    def test_normal_content_does_not_fire(self):
        rules = _rules()
        result = run_lint({"seo_output": _seo_output()}, rules)
        assert _violations_for(result, "seo", "lint.placeholder_text") == []


# ---------------------------------------------------------------------------
# lint.seo.title_over_limit / short_over_limit / long_over_limit
# ---------------------------------------------------------------------------


class TestSeoOverLimit:
    def test_title_over_limit_uses_rules_not_hardcoded(self):
        # title_max=20 -- proves the bound comes from StyleRules, not 80.
        rules = _rules(title_max=20)
        title = "This title is deliberately much longer than twenty characters"
        assert len(title) > 20
        result = run_lint({"seo_output": _seo_output(title=title)}, rules)
        violations = _violations_for(result, "seo", "lint.seo.title_over_limit")
        assert len(violations) == 1
        assert violations[0].severity == "error"
        assert violations[0].model_fixable is True

    def test_same_title_under_default_80_limit_does_not_fire(self):
        rules = _rules(title_max=80)
        title = "This title is deliberately much longer than twenty characters"
        assert len(title) <= 80
        result = run_lint({"seo_output": _seo_output(title=title)}, rules)
        assert _violations_for(result, "seo", "lint.seo.title_over_limit") == []

    def test_short_description_over_limit(self):
        rules = _rules(short_max=20)
        short = "This short description exceeds twenty characters easily."
        result = run_lint({"seo_output": _seo_output(short=short)}, rules)
        violations = _violations_for(result, "seo", "lint.seo.short_over_limit")
        assert len(violations) == 1
        assert violations[0].model_fixable is True

    def test_short_description_within_limit_does_not_fire(self):
        rules = _rules(short_max=90)
        result = run_lint({"seo_output": _seo_output()}, rules)
        assert _violations_for(result, "seo", "lint.seo.short_over_limit") == []

    def test_long_description_over_limit(self):
        rules = _rules(long_max=30)
        long_desc = "This long description is deliberately far longer than thirty characters allow."
        result = run_lint({"seo_output": _seo_output(long=long_desc)}, rules)
        violations = _violations_for(result, "seo", "lint.seo.long_over_limit")
        assert len(violations) == 1
        assert violations[0].model_fixable is True

    def test_long_description_within_limit_does_not_fire(self):
        rules = _rules(long_max=350)
        result = run_lint({"seo_output": _seo_output()}, rules)
        assert _violations_for(result, "seo", "lint.seo.long_over_limit") == []


# ---------------------------------------------------------------------------
# lint.seo.keywords_count (warning)
# ---------------------------------------------------------------------------


class TestSeoKeywordsCount:
    def test_within_bounds_no_violation(self):
        rules = _rules(keyword_min=15, keyword_max=20)
        result = run_lint({"seo_output": _seo_output(tags=[f"kw{i}" for i in range(17)])}, rules)
        assert _violations_for(result, "seo", "lint.seo.keywords_count") == []

    def test_below_min_fires_warning(self):
        rules = _rules(keyword_min=15, keyword_max=20)
        result = run_lint({"seo_output": _seo_output(tags=["only", "three", "tags"])}, rules)
        violations = _violations_for(result, "seo", "lint.seo.keywords_count")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].model_fixable is True

    def test_above_max_fires_warning(self):
        rules = _rules(keyword_min=5, keyword_max=10)
        result = run_lint({"seo_output": _seo_output(tags=[f"kw{i}" for i in range(20)])}, rules)
        violations = _violations_for(result, "seo", "lint.seo.keywords_count")
        assert len(violations) == 1
        assert violations[0].severity == "warning"

    def test_unidentifiable_section_skips_silently(self):
        rules = _rules(keyword_min=15, keyword_max=20)
        seo = _seo_output(include_tags_section=False)
        result = run_lint({"seo_output": seo}, rules)
        assert _violations_for(result, "seo", "lint.seo.keywords_count") == []

    def test_legacy_fenced_tags_still_extract(self):
        """Outputs generated before #380 carry the tags in a code fence.

        Those rows still exist and can be re-linted; the fenced shape must
        keep extracting alongside the current unfenced one.
        """
        rules = _rules(keyword_min=15, keyword_max=20)
        result = run_lint(
            {"seo_output": _seo_output(tags=["only", "three", "tags"], legacy_fenced_tags=True)},
            rules,
        )
        violations = _violations_for(result, "seo", "lint.seo.keywords_count")
        assert len(violations) == 1

    def test_label_followed_by_fence_uses_fenced_list(self):
        """A model that emits the label AND a fence must not count ``` as a tag."""
        rules = _rules(keyword_min=15, keyword_max=20)
        seo = (
            "# SEO Report\n\n"
            "### YouTube Tags (15-20 recommended)\n\n"
            "**Tags (paste-ready):**\n"
            "```\n"
            "only, three, tags\n"
            "```\n"
        )
        result = run_lint({"seo_output": seo}, rules)
        violations = _violations_for(result, "seo", "lint.seo.keywords_count")
        assert len(violations) == 1
        assert "3" in violations[0].message


# ---------------------------------------------------------------------------
# lint.formatter.review_notes_in_body
# ---------------------------------------------------------------------------


class TestReviewNotesInBody:
    def test_review_notes_after_first_hr_fires(self):
        rules = _rules(review_notes_placement="top")
        formatter = _formatter_output(review_notes="<!-- REVIEW NOTES: speaker unclear at 2:30 -->")
        result = run_lint({"formatter_output": formatter}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.review_notes_in_body")
        assert len(violations) == 1
        assert violations[0].model_fixable is False
        assert violations[0].severity == "error"

    def test_review_notes_before_first_hr_does_not_fire(self):
        rules = _rules(review_notes_placement="top")
        formatter = _formatter_output(
            review_notes="<!-- REVIEW NOTES: speaker unclear at 2:30 -->",
            review_notes_before_header=True,
        )
        result = run_lint({"formatter_output": formatter}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.review_notes_in_body") == []

    def test_needs_review_marker_after_first_hr_fires(self):
        rules = _rules(review_notes_placement="top")
        body = _DEFAULT_FORMATTER_BODY + "\n\n**Status:** needs_review\nNEEDS_REVIEW: speaker unclear"
        formatter = _formatter_output(body=body)
        result = run_lint({"formatter_output": formatter}, rules)
        assert len(_violations_for(result, "formatter", "lint.formatter.review_notes_in_body")) == 1

    def test_review_notes_heading_after_first_hr_fires(self):
        rules = _rules(review_notes_placement="top")
        body = _DEFAULT_FORMATTER_BODY + "\n\n## Review Notes\n- Speaker unclear at 2:30"
        formatter = _formatter_output(body=body)
        result = run_lint({"formatter_output": formatter}, rules)
        assert len(_violations_for(result, "formatter", "lint.formatter.review_notes_in_body")) == 1

    def test_no_review_notes_present_does_not_fire(self):
        rules = _rules(review_notes_placement="top")
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.review_notes_in_body") == []

    def test_config_placement_not_top_skips_check(self):
        rules = _rules(review_notes_placement="bottom")
        formatter = _formatter_output(review_notes="<!-- REVIEW NOTES: speaker unclear at 2:30 -->")
        result = run_lint({"formatter_output": formatter}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.review_notes_in_body") == []


# ---------------------------------------------------------------------------
# lint.formatter.speaker_label_inconsistent (warning)
# ---------------------------------------------------------------------------


class TestSpeakerLabelInconsistent:
    def test_single_word_label_fires(self):
        rules = _rules()
        body = "**Sarah:**\nDialogue text here that is long enough.\n\n**John Smith:**\nMore dialogue text."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("Sarah" in v.message and "single word" in v.message for v in violations)
        assert all(v.model_fixable is True and v.severity == "warning" for v in violations)

    def test_two_word_labels_do_not_fire_single_word_check(self):
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("single word" in v.message for v in violations)

    def test_honorific_label_fires(self):
        rules = _rules(no_honorifics=True)
        body = "**Dr. Sarah Johnson:**\nDialogue text that is long enough to matter.\n\n**John Smith:**\nMore text."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("honorific" in v.message for v in violations)

    def test_no_honorifics_false_does_not_flag_honorific(self):
        rules = _rules(no_honorifics=False)
        body = "**Dr. Sarah Johnson:**\nDialogue text that is long enough to matter.\n\n**John Smith:**\nMore text."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("honorific" in v.message for v in violations)

    def test_superset_label_pair_fires(self):
        rules = _rules()
        body = (
            "**Sarah Johnson:**\nDialogue text that is long enough to matter here.\n\n"
            "**Dr. Sarah Johnson:**\nMore dialogue text that is also long enough."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("labeled inconsistently" in v.message for v in violations)

    def test_identical_labels_do_not_fire_superset_check(self):
        rules = _rules()
        body = (
            "**Sarah Johnson:**\nFirst turn of dialogue that is long enough to matter here.\n\n"
            "**John Smith:**\nA reply.\n\n"
            "**Sarah Johnson:**\nSecond turn from the same speaker, same exact label as before."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("labeled inconsistently" in v.message for v in violations)

    def test_no_pattern_configured_returns_no_violations(self):
        rules = _rules(speaker_label_pattern=None)
        body = "**Sarah:**\nDialogue text here that is long enough to matter for this test."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent") == []

    def test_header_fields_do_not_leak_into_label_collection(self):
        # _body_region scoping (strictly between the two "---" rules) must
        # keep "**Project:**" / "**Date Processed:**"-style header fields
        # out of the candidate pool -- otherwise "Project" (a single bold
        # word) would spuriously trip the single-word check every time.
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("Project" in v.message for v in violations)
        assert not any("Date Processed" in v.message for v in violations)
        assert not any("Program" in v.message for v in violations)
        assert not any("Duration" in v.message for v in violations)

    def test_field_label_stoplist_skips_note_annotation(self):
        # A "**Note:**" inline annotation in the body shares the loose
        # bold-colon shape with a real speaker label but is a known
        # non-name field -- must be skipped at collection, not flagged as
        # a malformed single-word speaker label.
        body = (
            "**John Smith:**\nDialogue text that is long enough to matter here.\n\n"
            "**Note:** inline annotation about the edit.\n\n"
            "**Sarah Johnson:**\nMore dialogue text that is also long enough."
        )
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("Note" in v.message for v in violations)

    def test_field_label_stoplist_does_not_suppress_real_single_word_label(self):
        # The stoplist must be narrow -- a genuine single-word speaker label
        # not on the stoplist still fires.
        body = "**Sarah:**\nDialogue text here that is long enough.\n\n**John Smith:**\nMore dialogue text."
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("Sarah" in v.message and "single word" in v.message for v in violations)


# ---------------------------------------------------------------------------
# lint.formatter.speaker_label_inconsistent -- liveness against the REAL
# house_style.yaml strict pattern (2+ words required). Regression coverage
# for the bug where candidate labels were COLLECTED using the configured
# pattern itself: since the real config/house_style.yaml pattern requires
# 2+ words, a malformed single-word label like "**Sarah:**" would never
# enter the candidate pool at all, and the single-word branch could never
# fire in production. Collection must use a loose built-in pattern;
# classification (single-word / honorific / superset) runs over whatever
# that loose pattern finds, independent of how strict the configured
# pattern is.
# ---------------------------------------------------------------------------

# Copied verbatim from config/house_style.yaml's phases.formatter.speaker_label.pattern.
_REAL_STRICT_SPEAKER_PATTERN = r"^\*\*[A-Z][\w.'-]+(?: [A-Z][\w.'-]+)+:\*\*"


class TestSpeakerLabelInconsistentLiveAgainstRealPattern:
    def test_single_word_label_fires_with_real_strict_pattern_configured(self):
        rules = _rules(speaker_label_pattern=_REAL_STRICT_SPEAKER_PATTERN)
        body = (
            "**Sarah:**  \nDialogue text here that is long enough to matter for this test.\n\n"
            "**John Smith:**\nMore dialogue text that is also long enough."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("Sarah" in v.message and "single word" in v.message for v in violations)
        assert all(v.model_fixable is True and v.severity == "warning" for v in violations)

    def test_honorific_label_fires_with_real_strict_pattern_configured(self):
        rules = _rules(speaker_label_pattern=_REAL_STRICT_SPEAKER_PATTERN, no_honorifics=True)
        body = (
            "**Dr. Sarah Johnson:**\nDialogue text that is long enough to matter here.\n\n"
            "**John Smith:**\nMore dialogue text that is also long enough."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("honorific" in v.message for v in violations)

    def test_header_fields_do_not_leak_with_real_strict_pattern_configured(self):
        rules = _rules(speaker_label_pattern=_REAL_STRICT_SPEAKER_PATTERN)
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("Date Processed" in v.message for v in violations)


# ---------------------------------------------------------------------------
# lint.formatter.speaker_label_inconsistent -- numbered generic labels
# (task 2c2). Regression coverage for the bug where _LOOSE_SPEAKER_LABEL_RE's
# continuation token required [A-Z], so "**Speaker 1:**" / "**Reporter 2:**"
# never even entered the candidate pool (confirmed in the Stage-2 agreement
# study: job 17 used "**Speaker 1:**"/"**Speaker 2:**" and lint never flagged
# it, while the LLM validator did). Generic numbered labels are LEGITIMATE
# per analyst rules -- the point of collecting them is superset detection,
# NOT flagging them as malformed. A 2-token "Speaker 1" candidate with no
# honorific must flow through the existing classification and pass silently.
# ---------------------------------------------------------------------------


class TestSpeakerLabelNumberedGenericLabels:
    def test_numbered_label_alone_produces_no_violation(self):
        rules = _rules()
        body = "**Speaker 1:**\nDialogue text here that is long enough to matter for this test."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent") == []

    def test_two_numbered_labels_produce_no_violation(self):
        rules = _rules()
        body = (
            "**Speaker 1:**\nDialogue text here that is long enough to matter for this test.\n\n"
            "**Speaker 2:**\nMore dialogue text that is also long enough to matter here."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent") == []

    def test_reporter_numbered_label_also_collected_without_violation(self):
        rules = _rules()
        body = "**Reporter 2:**\nDialogue text here that is long enough to matter for this test."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent") == []

    def test_generic_numbered_label_and_unrelated_real_name_still_behaves_sensibly(self):
        # "Speaker 1" (generic) and "John Smith" (real name) share no words
        # -- collection must not spuriously pair them as a superset match.
        rules = _rules()
        body = (
            "**Speaker 1:**\nDialogue text here that is long enough to matter for this test.\n\n"
            "**John Smith:**\nMore dialogue text that is also long enough to matter here."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert violations == []

    def test_field_label_stoplist_still_honored_alongside_numbered_label(self):
        rules = _rules()
        body = (
            "**Speaker 1:**\nDialogue text here that is long enough to matter for this test.\n\n"
            "**Note:** inline annotation about the edit.\n\n"
            "**John Smith:**\nMore dialogue text that is also long enough to matter here."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert not any("Note" in v.message for v in violations)

    def test_numbered_label_actually_enters_candidate_pool_via_superset_detection(self):
        # Decisive regression check: "Speaker" (bare, single word) and
        # "Speaker 1" (numbered) share an overlapping word -- the superset
        # check only fires if "Speaker 1" was actually collected as a
        # candidate. Before the fix, "**Speaker 1:**" was entirely invisible
        # to collection (continuation token required [A-Z]), so this pair
        # would never be flagged as the same speaker labeled two ways --
        # this test would fail against the pre-fix regex.
        rules = _rules()
        body = (
            "**Speaker:**\nDialogue text here that is long enough to matter for this test.\n\n"
            "**Speaker 1:**\nMore dialogue text that is also long enough to matter here."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("labeled inconsistently" in v.message for v in violations)

    def test_numbered_label_collection_does_not_suppress_real_single_word_label(self):
        # A genuine single-word real name alongside a numbered generic label
        # must still fire the single-word check -- collecting "Speaker 1"
        # must not interfere with classification of unrelated candidates.
        rules = _rules()
        body = (
            "**Speaker 1:**\nDialogue text here that is long enough to matter for this test.\n\n"
            "**Sarah:**\nMore dialogue text that is also long enough to matter here."
        )
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert any("Sarah" in v.message and "single word" in v.message for v in violations)
        # "Speaker 1" is 2 tokens and not on the stoplist -- it must not be
        # flagged itself, and must not pair with "Sarah" (disjoint words).
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# lint.formatter.content_past_duration (warning)
# ---------------------------------------------------------------------------


class TestContentPastDuration:
    def test_within_duration_does_not_fire(self):
        rules = _rules()
        body = "**John Smith:**\nThe segment wraps up around (9:30) in the recording, right on schedule."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 10}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.content_past_duration") == []

    def test_exactly_at_slack_boundary_does_not_fire(self):
        # duration_minutes=10 -> limit = 600 + 60 = 660s = 11:00 exactly.
        rules = _rules()
        body = "**John Smith:**\nThe segment closes right at (11:00) in the recording, exactly on the slack line."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 10}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.content_past_duration") == []

    def test_one_second_past_slack_boundary_fires(self):
        rules = _rules()
        body = "**John Smith:**\nThe segment somehow references (11:01) which is one second past the slack line."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 10}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.content_past_duration")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].model_fixable is False

    def test_duration_missing_skips_check(self):
        rules = _rules()
        body = "**John Smith:**\nThe segment somehow references (99:59) which would otherwise be way over."
        context = {"formatter_output": _formatter_output(body=body)}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.content_past_duration") == []

    def test_duration_zero_skips_check(self):
        rules = _rules()
        body = "**John Smith:**\nThe segment somehow references (99:59) which would otherwise be way over."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 0}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.content_past_duration") == []

    def test_marker_inside_html_comment_does_not_fire(self):
        # Same defect class fixed for the coverage-vs-duration path (01805ef):
        # a (MM:SS) marker mentioned in passing inside a review-note HTML
        # comment describes something about the source content, not a
        # genuine past-duration marker in the visible transcript body.
        rules = _rules()
        body = "**John Smith:**\nThe segment closes out cleanly and reaches its natural conclusion."
        review_notes = "<!-- REVIEW NOTES:\n- Hendrickson paragraph attribution (99:59) needs checking. -->"
        formatter = _formatter_output(body=body, review_notes=review_notes)
        context = {"formatter_output": formatter, "duration_minutes": 10}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.content_past_duration") == []

    def test_same_marker_in_body_text_still_fires(self):
        # The same (99:59) marker, this time in the visible transcript body
        # rather than a review-note comment, must still fire.
        rules = _rules()
        body = "**John Smith:**\nThe segment somehow references (99:59) which would otherwise be way over."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 10}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.content_past_duration")
        assert len(violations) == 1
        assert "(99:59)" in violations[0].message


# ---------------------------------------------------------------------------
# lint.formatter.truncation_suspect (warning)
# ---------------------------------------------------------------------------


class TestTruncationSuspect:
    def test_missing_terminal_punctuation_fires(self):
        rules = _rules()
        body = "**John Smith:**\nThe budget debate continued late into the evening and"
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].model_fixable is False

    def test_terminal_punctuation_present_does_not_fire(self):
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_ignores_status_footer_when_finding_last_line(self):
        rules = _rules()
        # Body ends cleanly; the Status footer line ("**Status:** ready_for_editing")
        # has no terminal punctuation but must not be treated as the last prose line.
        result = run_lint({"formatter_output": _formatter_output(status="ready_for_editing")}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_ignores_trailing_html_comment_when_finding_last_line(self):
        rules = _rules()
        body = _DEFAULT_FORMATTER_BODY + "\n\n<!-- trailing note with no punctuation at all -->"
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_cutoff_hidden_behind_trailing_comment_still_fires(self):
        rules = _rules()
        body = "**John Smith:**\nThe budget debate continued late into the evening and\n\n<!-- trailing comment -->"
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert len(_violations_for(result, "formatter", "lint.formatter.truncation_suspect")) == 1


# ---------------------------------------------------------------------------
# lint.formatter.truncation_suspect -- coverage-vs-duration path (task 2c2).
#
# The punctuation-only check above fired ZERO times across 21 real
# production jobs in the Stage-2 agreement study, despite 8 real
# truncations, because every one of those transcripts happened to close
# with a complete, punctuated sign-off paragraph even though the content
# stopped well short of the full episode. This complementary path compares
# the LAST parsed (MM:SS)/(H:MM:SS) timecode marker in the body against
# duration_minutes -- duration_minutes=100 (6000s) is used throughout so
# the boundary math (0.85 * 6000 = 5100.0 exactly) has no floating-point
# surprises: (1:24:54) = 5094s = 84.9% coverage (fires), (1:25:00) = 5100s
# = 85% exactly (silent, boundary).
# ---------------------------------------------------------------------------


class TestTruncationSuspectCoverageVsDuration:
    def test_fires_at_84_9_percent_coverage(self):
        rules = _rules()
        body = (
            "**John Smith:**\n"
            "The discussion opens early in the recording and covers several topics.\n\n"
            "**Sarah Johnson:**\n"
            "By (1:24:54) the conversation had wrapped up its main points nicely."
        )
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 100}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].model_fixable is False

    def test_silent_at_85_percent_boundary(self):
        rules = _rules()
        body = (
            "**John Smith:**\n"
            "The discussion opens early in the recording and covers several topics.\n\n"
            "**Sarah Johnson:**\n"
            "By (1:25:00) the conversation had wrapped up its main points nicely."
        )
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 100}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_no_markers_skips_coverage_path_silently(self):
        rules = _rules()
        body = "**John Smith:**\nThe discussion wraps up its main points nicely and closes right on time."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 100}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_missing_duration_skips_coverage_path_silently(self):
        rules = _rules()
        body = "**John Smith:**\nThe recording only ever reaches (0:10) before this closes out cleanly."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_zero_duration_skips_coverage_path_silently(self):
        rules = _rules()
        body = "**John Smith:**\nThe recording only ever reaches (0:10) before this closes out cleanly."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 0}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_coverage_path_fires_independently_when_punctuation_is_clean(self):
        # Last prose line IS properly punctuated (punctuation path silent),
        # but the last timecode marker falls well short of the duration.
        rules = _rules()
        body = "**John Smith:**\nThe recording only ever reaches (1:00) before this closes out cleanly."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 100}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 1
        assert "covers only" in violations[0].message

    def test_punctuation_path_fires_independently_when_coverage_is_sufficient(self):
        # Last prose line lacks terminal punctuation (punctuation path
        # fires), but the last timecode marker covers the full duration.
        rules = _rules()
        body = "**John Smith:**\nThe budget debate wraps up around (1:39:50) and then trails off and"
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 100}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 1
        assert "terminal punctuation" in violations[0].message

    def test_ignores_marker_inside_html_comment(self):
        # Discovered against real production data (job 8/12/15 in the Stage-2
        # study): every (MM:SS) marker in the real 21-job corpus lives inside
        # a "<!-- REVIEW NOTES: ... -->" aside, not the visible transcript
        # body -- e.g. "'The mounds' (1:01) appears without prior
        # introduction" is a content-origin note, not a coverage signal.
        # Scanning raw_output unstripped produces both false pairings
        # (matching the wrong LLM flag by rule_id) and outright false
        # positives (flagging a short, complete clip as truncated because an
        # unrelated review note happened to mention an early timestamp).
        rules = _rules()
        body = "**John Smith:**\nThe segment closes out cleanly and reaches its natural conclusion."
        review_notes = '<!-- REVIEW NOTES:\n- "The mounds" (1:01) appears without prior introduction. -->'
        formatter = _formatter_output(body=body, review_notes=review_notes)
        context = {"formatter_output": formatter, "duration_minutes": 1.25855}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_message_contains_both_timestamps(self):
        rules = _rules()
        body = (
            "**John Smith:**\n"
            "The discussion opens early in the recording and covers several topics.\n\n"
            "**Sarah Johnson:**\n"
            "By (1:24:54) the conversation had wrapped up its main points nicely."
        )
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 100}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 1
        assert "1:24:54" in violations[0].message
        assert "1:40:00" in violations[0].message


# ---------------------------------------------------------------------------
# lint.formatter.truncation_suspect -- completeness-gate consumption path
# (task 2c3). context["completeness_check"] is the worker's own
# CompletenessResult.to_dict() (api/services/completeness.py), stashed after
# every real formatter phase run -- lint doesn't recompute the word-count
# ratio itself, it just surfaces the gate's already-computed verdict.
# ---------------------------------------------------------------------------


def _completeness_check(
    is_complete: bool = False,
    coverage_ratio: float = 0.62,
    source_word_count: int = 5000,
    output_word_count: int = 3100,
    skipped: bool = False,
    reason: str = "TRUNCATION DETECTED",
) -> dict:
    return {
        "is_complete": is_complete,
        "coverage_ratio": coverage_ratio,
        "source_word_count": source_word_count,
        "output_word_count": output_word_count,
        "skipped": skipped,
        "reason": reason,
    }


class TestCompletenessGateConsumption:
    def test_absent_key_does_not_fire(self):
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_incomplete_and_not_skipped_fires(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "completeness_check": _completeness_check(is_complete=False),
        }
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].model_fixable is False
        assert "0.62" in violations[0].message
        assert "3100" in violations[0].message
        assert "5000" in violations[0].message

    def test_is_complete_true_does_not_fire(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "completeness_check": _completeness_check(is_complete=True),
        }
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_skipped_does_not_fire_even_when_incomplete(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "completeness_check": _completeness_check(is_complete=False, skipped=True),
        }
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_missing_keys_do_not_crash_and_skip_gracefully(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "completeness_check": {"is_complete": False},
        }
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_non_mapping_value_does_not_crash_and_skips(self):
        rules = _rules()
        context = {"formatter_output": _formatter_output(), "completeness_check": "not a dict"}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.truncation_suspect") == []

    def test_fires_alongside_and_independently_of_punctuation_path(self):
        # Punctuation path fires on its own (last line lacks terminal
        # punctuation); completeness-gate path fires independently on the
        # same phase -- both land as lint.formatter.truncation_suspect
        # violations, not deduplicated against each other.
        rules = _rules()
        body = "**John Smith:**\nThe budget debate continued late into the evening and"
        context = {
            "formatter_output": _formatter_output(body=body),
            "completeness_check": _completeness_check(is_complete=False),
        }
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert len(violations) == 2


# ---------------------------------------------------------------------------
# lint.formatter.seam_gap -- seam-coverage-gate consumption path (task 2c3).
# context["seam_coverage"] is the worker's own SeamCoverageResult.to_dict()
# (api/services/seam_coverage.py), stashed after every real formatter phase
# run -- catches localized chunk-boundary drops the global word-count ratio
# can't see.
# ---------------------------------------------------------------------------


def _seam_coverage(
    has_gap: bool = True,
    dropped_spans: list[dict] | None = None,
    captions_checked: int = 120,
    blocking: bool | None = None,
) -> dict:
    if dropped_spans is None:
        dropped_spans = [
            {
                "start_timecode": "00:07:10,000",
                "end_timecode": "00:07:22,000",
                "caption_count": 5,
                "sample_text": "the budget conference committee reached agreement late",
            }
        ]
    if blocking is None:
        blocking = has_gap  # a real drop (net content loss) blocks by default
    return {
        "has_gap": has_gap,
        "blocking": blocking,
        "net_coverage_ratio": 0.9 if blocking else 1.05,
        "dropped_spans": dropped_spans,
        "captions_checked": captions_checked,
    }


class TestSeamCoverageGateConsumption:
    def test_absent_key_does_not_fire(self):
        rules = _rules()
        result = run_lint({"formatter_output": _formatter_output()}, rules)
        assert _violations_for(result, "formatter", "lint.formatter.seam_gap") == []

    def test_has_gap_fires(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "seam_coverage": _seam_coverage(has_gap=True),
        }
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.seam_gap")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].model_fixable is False
        assert "1" in violations[0].message
        assert "00:07:10,000" in violations[0].message

    def test_no_gap_does_not_fire(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "seam_coverage": _seam_coverage(has_gap=False, dropped_spans=[]),
        }
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.seam_gap") == []

    def test_non_blocking_gap_does_not_fire(self):
        """A detected but non-blocking gap (reconstruction, no net content loss)
        must NOT surface as a validator flag — matches the worker's pause gate."""
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "seam_coverage": _seam_coverage(has_gap=True, blocking=False),
        }
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.seam_gap") == []

    def test_multiple_spans_count_named_in_message(self):
        spans = [
            {
                "start_timecode": "00:03:00,000",
                "end_timecode": "00:03:20,000",
                "caption_count": 4,
                "sample_text": "first dropped span",
            },
            {
                "start_timecode": "00:12:00,000",
                "end_timecode": "00:12:40,000",
                "caption_count": 6,
                "sample_text": "second dropped span",
            },
        ]
        rules_ = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "seam_coverage": _seam_coverage(has_gap=True, dropped_spans=spans),
        }
        result = run_lint(context, rules_)
        violations = _violations_for(result, "formatter", "lint.formatter.seam_gap")
        assert len(violations) == 1
        assert "2" in violations[0].message
        assert "00:03:00,000" in violations[0].message

    def test_missing_dropped_spans_does_not_crash_and_skips(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "seam_coverage": {"has_gap": True},
        }
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.seam_gap") == []

    def test_non_mapping_value_does_not_crash_and_skips(self):
        rules = _rules()
        context = {"formatter_output": _formatter_output(), "seam_coverage": "not a dict"}
        result = run_lint(context, rules)
        assert _violations_for(result, "formatter", "lint.formatter.seam_gap") == []


# ---------------------------------------------------------------------------
# model_fixable contract -- explicit per rule_id, since escalation routing
# depends on these being correct.
# ---------------------------------------------------------------------------


class TestModelFixableContract:
    def test_output_missing_is_not_model_fixable(self):
        rules = _rules()
        result = run_lint({}, rules)
        for phase in ("analyst", "formatter", "seo"):
            for v in _violations_for(result, phase, "lint.output_missing"):
                assert v.model_fixable is False

    def test_placeholder_text_is_model_fixable(self):
        rules = _rules()
        context = {"analyst_output": _analyst_output("Contains {media_id} literally in analyst prose here today.")}
        result = run_lint(context, rules)
        for v in _violations_for(result, "analyst", "lint.placeholder_text"):
            assert v.model_fixable is True

    def test_seo_over_limit_checks_are_model_fixable(self):
        rules = _rules(title_max=5, short_max=5, long_max=5)
        result = run_lint({"seo_output": _seo_output()}, rules)
        for rule_id in ("lint.seo.title_over_limit", "lint.seo.short_over_limit", "lint.seo.long_over_limit"):
            violations = _violations_for(result, "seo", rule_id)
            assert violations, f"expected {rule_id} to fire for this fixture"
            for v in violations:
                assert v.model_fixable is True

    def test_seo_keywords_count_is_model_fixable(self):
        rules = _rules(keyword_min=15, keyword_max=20)
        result = run_lint({"seo_output": _seo_output(tags=["one", "two"])}, rules)
        violations = _violations_for(result, "seo", "lint.seo.keywords_count")
        assert violations
        for v in violations:
            assert v.model_fixable is True

    def test_review_notes_in_body_is_not_model_fixable(self):
        rules = _rules()
        formatter = _formatter_output(review_notes="<!-- REVIEW NOTES: unresolved -->")
        result = run_lint({"formatter_output": formatter}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.review_notes_in_body")
        assert violations
        for v in violations:
            assert v.model_fixable is False

    def test_speaker_label_inconsistent_is_model_fixable(self):
        rules = _rules()
        body = "**Sarah:**\nDialogue text here that is long enough to matter for this test."
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.speaker_label_inconsistent")
        assert violations
        for v in violations:
            assert v.model_fixable is True

    def test_content_past_duration_is_not_model_fixable(self):
        rules = _rules()
        body = "**John Smith:**\nReferences a time far past the end (99:59) of the recording here."
        context = {"formatter_output": _formatter_output(body=body), "duration_minutes": 10}
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.content_past_duration")
        assert violations
        for v in violations:
            assert v.model_fixable is False

    def test_truncation_suspect_is_not_model_fixable(self):
        rules = _rules()
        body = "**John Smith:**\nThe budget debate continued late into the evening and"
        result = run_lint({"formatter_output": _formatter_output(body=body)}, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert violations
        for v in violations:
            assert v.model_fixable is False

    def test_completeness_gate_truncation_suspect_is_not_model_fixable(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "completeness_check": _completeness_check(is_complete=False),
        }
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.truncation_suspect")
        assert violations
        for v in violations:
            assert v.model_fixable is False

    def test_seam_gap_is_not_model_fixable(self):
        rules = _rules()
        context = {
            "formatter_output": _formatter_output(),
            "seam_coverage": _seam_coverage(has_gap=True),
        }
        result = run_lint(context, rules)
        violations = _violations_for(result, "formatter", "lint.formatter.seam_gap")
        assert violations
        for v in violations:
            assert v.model_fixable is False
