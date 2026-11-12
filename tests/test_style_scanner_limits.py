"""Tests for the style_engine forbidden-phrase/voice scanner and field limits checker.

Covers api.services.style_engine.scanner (scan_forbidden, scan_person_voice)
and api.services.style_engine.limits (check_field_limits). All rule data is
synthetic, constructed directly as StyleRules(raw=...) per the task-0.5
brief's guidance -- no dependency on config/house_style.yaml. Mirrors the
fixture/helper style of tests/test_style_rules.py.
"""

from __future__ import annotations

from api.services.style_engine.limits import check_field_limits
from api.services.style_engine.rules import StyleRules
from api.services.style_engine.scanner import scan_forbidden, scan_person_voice

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _voice_rules(**overrides) -> StyleRules:
    raw = {
        "meta": {"version": 1},
        "voice": {
            "forbidden_phrases": [
                {"match": "watch as", "category": "viewer_directive", "severity": "error"},
                {"match": "discover", "category": "viewer_directive", "severity": "error"},
                {
                    "match": r"\bfree\b",
                    "category": "sales",
                    "severity": "warning",
                    "regex": True,
                },
            ],
            "first_person_markers": [r"\bwe\b", r"\bwe'll\b", r"\bour\b"],
            "second_person_markers": [r"\byou\b", r"\byou'll\b", r"\byou're\b", r"\byour\b"],
        },
    }
    raw.update(overrides)
    return StyleRules(raw=raw)


def _limits_rules(**overrides) -> StyleRules:
    raw = {
        "meta": {"version": 1},
        "limits": {
            "fields": {
                "title": {"max": 80, "full_max": 100},
                "short_description": {"max": 90},
                "keywords": {"count": {"min": 15, "max": 20}},
                "social_tags": {},
            },
            "content_type_overrides": {
                "short": {"keywords": {"count": {"min": 5, "max": 10}}},
            },
        },
    }
    raw.update(overrides)
    return StyleRules(raw=raw)


# ---------------------------------------------------------------------------
# scan_forbidden -- literal phrase, word-boundary
# ---------------------------------------------------------------------------


class TestScanForbiddenLiteral:
    def test_standalone_word_matches(self):
        rules = _voice_rules()
        violations = scan_forbidden("You can discover Wisconsin history here.", rules, "seo")
        matched = [v for v in violations if v.rule_id == "voice.forbidden.viewer_directive"]
        assert any('"discover"' in v.message.lower() or "discover" in v.message for v in matched)

    def test_prefix_inside_longer_word_does_not_match(self):
        # \bdiscover\b requires a boundary after "discover"; "discovered" has
        # no boundary between "r" and "e", so this must NOT fire.
        rules = _voice_rules()
        violations = scan_forbidden("She discovered a great trick.", rules, "seo")
        assert violations == []

    def test_phrase_matches_case_insensitively(self):
        rules = _voice_rules()
        violations = scan_forbidden("Watch as the story unfolds.", rules, "seo")
        assert len(violations) == 1
        assert violations[0].rule_id == "voice.forbidden.viewer_directive"
        assert violations[0].span == (0, 8)

    def test_no_match_returns_empty_list(self):
        rules = _voice_rules()
        assert scan_forbidden("A calm, descriptive sentence.", rules, "seo") == []


# ---------------------------------------------------------------------------
# scan_forbidden -- regex entry (known false-positive limitation)
# ---------------------------------------------------------------------------


class TestScanForbiddenRegex:
    def test_free_fires_inside_gluten_free_known_limitation(self):
        # "free" after a hyphen still has a word boundary on both sides, so
        # \bfree\b DOES fire inside "gluten-free" -- this is a known
        # false-positive-prone case, which is exactly why the entry is
        # authored as severity "warning" rather than "error".
        rules = _voice_rules()
        violations = scan_forbidden("Try this gluten-free recipe tonight.", rules, "seo")
        matches = [v for v in violations if v.rule_id == "voice.forbidden.sales"]
        assert len(matches) == 1
        assert matches[0].severity == "warning"

    def test_free_fires_as_standalone_word_too(self):
        rules = _voice_rules()
        violations = scan_forbidden("Admission is free this weekend.", rules, "seo")
        matches = [v for v in violations if v.rule_id == "voice.forbidden.sales"]
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# scan_forbidden -- violation shape
# ---------------------------------------------------------------------------


class TestScanForbiddenViolationShape:
    def test_phase_and_field_propagated(self):
        rules = _voice_rules()
        violations = scan_forbidden("Watch as it happens.", rules, "seo", field="title")
        assert violations[0].phase == "seo"
        assert violations[0].field == "title"

    def test_model_fixable_true(self):
        rules = _voice_rules()
        violations = scan_forbidden("Watch as it happens.", rules, "seo")
        assert violations[0].model_fixable is True

    def test_multiple_matches_produce_multiple_violations(self):
        rules = _voice_rules()
        violations = scan_forbidden("Watch as it happens, then watch as it ends.", rules, "seo")
        matches = [
            v for v in violations if v.rule_id == "voice.forbidden.viewer_directive" and "watch" in v.message.lower()
        ]
        assert len(matches) == 2


# ---------------------------------------------------------------------------
# scan_forbidden -- rule_id derivation (entry `id` vs category)
# ---------------------------------------------------------------------------


class TestScanForbiddenRuleId:
    def test_entry_id_distinguishes_same_category_entries(self):
        # house_style.yaml authors an explicit `id` per forbidden phrase, but
        # many share one `category` (e.g. nine viewer_directive entries). The
        # rule_id must derive from `id` when present so `watch as` and
        # `watch how` violations are distinguishable (feedback-loop keying).
        rules = StyleRules(
            raw={
                "meta": {"version": 1},
                "voice": {
                    "forbidden_phrases": [
                        {"id": "watch_as", "match": "watch as", "category": "viewer_directive", "severity": "error"},
                        {"id": "watch_how", "match": "watch how", "category": "viewer_directive", "severity": "error"},
                    ]
                },
            }
        )
        violations = scan_forbidden("Watch as it starts, then watch how it ends.", rules, "seo")
        rule_ids = {v.rule_id for v in violations}
        assert "voice.forbidden.watch_as" in rule_ids
        assert "voice.forbidden.watch_how" in rule_ids

    def test_missing_id_falls_back_to_category(self):
        # Entries without an `id` (the synthetic fixture) keep the historical
        # category-based rule_id -- backward compatible.
        rules = _voice_rules()
        violations = scan_forbidden("You can discover Wisconsin history here.", rules, "seo")
        assert any(v.rule_id == "voice.forbidden.viewer_directive" for v in violations)


# ---------------------------------------------------------------------------
# scan_person_voice
# ---------------------------------------------------------------------------


class TestScanPersonVoice:
    def test_first_person_marker_fires_error(self):
        rules = _voice_rules()
        violations = scan_person_voice("We break down the budget numbers.", rules, "seo")
        first_person = [v for v in violations if v.rule_id == "voice.first_person"]
        assert len(first_person) == 1
        assert first_person[0].severity == "error"

    def test_first_person_span_correct(self):
        rules = _voice_rules()
        text = "We break down the budget numbers."
        violations = scan_person_voice(text, rules, "seo")
        first_person = [v for v in violations if v.rule_id == "voice.first_person"][0]
        start, end = first_person.span
        assert text[start:end].lower() == "we"

    def test_second_person_marker_fires_warning(self):
        # "Your" only matches the \byour\b marker -- "you'll"/"you're" would
        # also (correctly) trigger the bare \byou\b marker, since a
        # possessive/contraction word boundary sits after "you"; that
        # multi-marker-overlap behavior is covered by
        # test_both_kinds_can_fire_together below, kept separate here so
        # this test can assert an exact count.
        rules = _voice_rules()
        violations = scan_person_voice("Your favorite host returns tonight.", rules, "seo")
        second_person = [v for v in violations if v.rule_id == "voice.second_person"]
        assert len(second_person) == 1
        assert second_person[0].severity == "warning"

    def test_second_person_span_correct(self):
        rules = _voice_rules()
        text = "Your favorite host returns tonight."
        violations = scan_person_voice(text, rules, "seo")
        second_person = [v for v in violations if v.rule_id == "voice.second_person"][0]
        start, end = second_person.span
        assert text[start:end].lower() == "your"

    def test_overlapping_markers_each_produce_their_own_violation(self):
        # Documents the overlap behavior explicitly: "you'll" matches BOTH
        # the bare \byou\b marker and the \byou'll\b marker (both are
        # authored as separate entries in house_style.yaml), so it
        # legitimately produces two second-person violations.
        rules = _voice_rules()
        violations = scan_person_voice("You'll love this new episode.", rules, "seo")
        second_person = [v for v in violations if v.rule_id == "voice.second_person"]
        assert len(second_person) == 2

    def test_no_markers_returns_empty(self):
        rules = _voice_rules()
        assert scan_person_voice("A calm, descriptive sentence about Wisconsin.", rules, "seo") == []

    def test_both_kinds_can_fire_together(self):
        rules = _voice_rules()
        violations = scan_person_voice("We think you'll enjoy our new show.", rules, "seo")
        rule_ids = {v.rule_id for v in violations}
        assert "voice.first_person" in rule_ids
        assert "voice.second_person" in rule_ids

    def test_field_propagated(self):
        rules = _voice_rules()
        violations = scan_person_voice("We break down the budget.", rules, "seo", field="short_description")
        assert violations[0].field == "short_description"


# ---------------------------------------------------------------------------
# check_field_limits -- string max, error tier
# ---------------------------------------------------------------------------


class TestCheckFieldLimitsStringMax:
    def test_over_limit_produces_exactly_one_error_violation(self):
        rules = _limits_rules()
        short_desc = "x" * 189
        fields = {"short_description": short_desc}
        violations = check_field_limits(fields, rules, "seo")
        assert len(violations) == 1
        v = violations[0]
        assert v.severity == "error"
        assert v.rule_id == "limits.short_description.max"
        assert "189" in v.message
        assert "90" in v.message

    def test_original_string_not_mutated(self):
        rules = _limits_rules()
        short_desc = "x" * 189
        fields = {"short_description": short_desc}
        check_field_limits(fields, rules, "seo")
        assert fields["short_description"] == "x" * 189
        assert len(fields["short_description"]) == 189

    def test_exactly_at_limit_boundary_no_violation(self):
        rules = _limits_rules()
        short_desc = "x" * 90
        violations = check_field_limits({"short_description": short_desc}, rules, "seo")
        assert violations == []

    def test_one_over_limit_boundary_violates(self):
        rules = _limits_rules()
        short_desc = "x" * 91
        violations = check_field_limits({"short_description": short_desc}, rules, "seo")
        assert len(violations) == 1


class TestCheckFieldLimitsTitle:
    """#459: ``max`` measures the title part, ``full_max`` the whole string."""

    IWP = " | Inside Wisconsin Politics"

    def test_program_suffix_does_not_count_toward_max(self):
        title = "x" * 59 + self.IWP  # 87 in full, as on 6POL0215
        assert check_field_limits({"title": title}, _limits_rules(), "seo") == []

    def test_title_part_over_max_violates(self):
        title = "x" * 81 + " | Here & Now"  # 94 in full, under full_max
        violations = check_field_limits({"title": title}, _limits_rules(), "seo")
        assert [v.rule_id for v in violations] == ["limits.title.max"]
        assert "81" in violations[0].message
        assert "program name" in violations[0].message

    def test_wisconsin_life_prefix_does_not_count_toward_max(self):
        title = "Wisconsin Life | " + "x" * 80
        assert check_field_limits({"title": title}, _limits_rules(), "seo") == []

    def test_whole_string_over_full_max_violates(self):
        title = "x" * 75 + self.IWP  # 103 in full
        violations = check_field_limits({"title": title}, _limits_rules(), "seo")
        assert [v.rule_id for v in violations] == ["limits.title.full_max"]
        assert "103" in violations[0].message

    def test_title_without_program_is_measured_whole(self):
        violations = check_field_limits({"title": "x" * 81}, _limits_rules(), "seo")
        assert [v.rule_id for v in violations] == ["limits.title.max"]


# ---------------------------------------------------------------------------
# check_field_limits -- list count, warning tier
# ---------------------------------------------------------------------------


class TestCheckFieldLimitsListCount:
    def test_below_min_count_produces_warning(self):
        rules = _limits_rules()
        keywords = [f"kw{i}" for i in range(12)]
        violations = check_field_limits({"keywords": keywords}, rules, "seo")
        assert len(violations) == 1
        assert violations[0].severity == "warning"
        assert violations[0].rule_id == "limits.keywords.count"

    def test_within_bounds_is_clean(self):
        rules = _limits_rules()
        keywords = [f"kw{i}" for i in range(17)]
        violations = check_field_limits({"keywords": keywords}, rules, "seo")
        assert violations == []

    def test_above_max_count_produces_warning(self):
        rules = _limits_rules()
        keywords = [f"kw{i}" for i in range(25)]
        violations = check_field_limits({"keywords": keywords}, rules, "seo")
        assert len(violations) == 1
        assert violations[0].severity == "warning"


# ---------------------------------------------------------------------------
# check_field_limits -- content_type override
# ---------------------------------------------------------------------------


class TestCheckFieldLimitsContentTypeOverride:
    def test_short_content_type_changes_keyword_bounds(self):
        rules = _limits_rules()
        keywords = [f"kw{i}" for i in range(12)]
        # Under "full" (default) bounds {min:15,max:20}, 12 items warns.
        full_violations = check_field_limits({"keywords": keywords}, rules, "seo")
        assert len(full_violations) == 1
        # Under "short" override {min:5,max:10}... 12 is still over max=10.
        short_violations = check_field_limits({"keywords": keywords}, rules, "seo", content_type="short")
        assert len(short_violations) == 1  # now flagged as ABOVE max instead of below min

    def test_short_content_type_clean_within_its_own_bounds(self):
        rules = _limits_rules()
        keywords = [f"kw{i}" for i in range(7)]
        violations = check_field_limits({"keywords": keywords}, rules, "seo", content_type="short")
        assert violations == []


# ---------------------------------------------------------------------------
# check_field_limits -- skips
# ---------------------------------------------------------------------------


class TestCheckFieldLimitsSkips:
    def test_none_value_skipped(self):
        rules = _limits_rules()
        violations = check_field_limits({"short_description": None}, rules, "seo")
        assert violations == []

    def test_field_without_limit_entry_skipped(self):
        rules = _limits_rules()
        violations = check_field_limits({"unknown_field": "x" * 500}, rules, "seo")
        assert violations == []

    def test_field_with_empty_limit_dict_skipped(self):
        rules = _limits_rules()
        violations = check_field_limits({"social_tags": "x" * 500}, rules, "seo")
        assert violations == []

    def test_only_fields_present_in_input_are_checked(self):
        rules = _limits_rules()
        # title has a limit but isn't in `fields` -- must not appear.
        violations = check_field_limits({"short_description": "ok"}, rules, "seo")
        assert violations == []
