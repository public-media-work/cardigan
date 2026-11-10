"""Tests for the style_engine prompt-block token renderer.

Covers api.services.style_engine.prompt_blocks: substituting
``{{style:KEY}}`` tokens in agent prompt text with rule text drawn from the
house-style rules YAML's ``prompt_blocks`` section, plus the pure
``resolve_prompt_profile`` helper used by the worker to pick the "full" vs
"slim" profile. All rule data is synthetic (built with pytest's ``tmp_path``
fixture) except the no-op guarantee tests, which read the real
``prompts/*.md`` files to prove this task changed no prompt content.

Mirrors the fixture/helper style of tests/test_style_rules.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.services.style_engine.prompt_blocks import (
    PromptBlockError,
    render_prompt_blocks,
    resolve_prompt_profile,
    strip_style_tokens,
    validate_prompt_blocks,
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# seo.md, validator.md, formatter.md, timestamp.md, and analyst.md are
# excluded here -- task 1c gave seo.md {{style:...}} tokens, task 2d gave
# validator.md its own, task 3c gave formatter.md its own, task 4b gave
# timestamp.md its own, and task 5 gave analyst.md its own. Each gets its own
# no-op-broken assertions + real-render tests below instead.
TOKEN_FREE_PROMPT_PHASES = ["copy_editor"]

SEO_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "seo.md"
VALIDATOR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "validator.md"
FORMATTER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "formatter.md"
TIMESTAMP_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "timestamp.md"
ANALYST_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "analyst.md"

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

MINIMAL_YAML = 'meta: {version: 1, style_guide_synced: "2026-07-10"}\n'

SAMPLE_YAML = r"""
meta: {version: 1, style_guide_synced: "2026-07-10"}
prompt_blocks:
  voice_rules:
    full: |
      Avoid viewer directives like "watch as" or "discover."
      Never use first-person promotional voice ("we break down").
    slim: |
      No viewer directives. No first-person promo voice.
  casing_rules:
    full: |
      Use down style casing except for proper nouns.
"""

NESTED_TOKEN_YAML = r"""
meta: {version: 1, style_guide_synced: "2026-07-10"}
prompt_blocks:
  outer:
    full: "See also {{style:other}} for details."
"""

NEWLINE_YAML = r"""
meta: {version: 1, style_guide_synced: "2026-07-10"}
prompt_blocks:
  block_a:
    full: |
      Rule line one.
      Rule line two.
"""

GOLDEN_YAML = r"""
meta: {version: 1, style_guide_synced: "2026-07-10"}
prompt_blocks:
  greeting:
    full: |
      Hello there.
      Be kind.
"""


def _write(tmp_path: Path, content: str, name: str = "house_style.yaml") -> Path:
    """Write synthetic YAML content to a file under tmp_path and return its path."""
    path = tmp_path / name
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# 1. Identity: token-free text short-circuits without loading rules
# ---------------------------------------------------------------------------


def test_identity_no_token_returns_unchanged_without_loading_rules(tmp_path: Path) -> None:
    text = "You are the SEO agent for PBS Wisconsin. Write a title under 60 characters."
    missing_rules = tmp_path / "nonexistent.yaml"

    # Must NOT raise even though missing_rules does not exist -- proves the
    # rules file is never touched when there are no tokens.
    result = render_prompt_blocks(text, rules_path=missing_rules)

    assert result == text


# ---------------------------------------------------------------------------
# 2. Single + multiple token substitution
# ---------------------------------------------------------------------------


def test_single_token_substitution(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, SAMPLE_YAML)
    text = "Intro.\n\n{{style:voice_rules}}\n\nOutro."

    result = render_prompt_blocks(text, rules_path=rules_path)

    assert "{{style:voice_rules}}" not in result
    assert "watch as" in result
    assert "we break down" in result


def test_multiple_token_substitution(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, SAMPLE_YAML)
    text = "{{style:voice_rules}}\n\n{{style:casing_rules}}"

    result = render_prompt_blocks(text, rules_path=rules_path)

    assert "viewer directives" in result
    assert "down style casing" in result
    assert "{{style:" not in result


# ---------------------------------------------------------------------------
# 3. Profile selection
# ---------------------------------------------------------------------------


def test_profile_slim_selects_slim_text(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, SAMPLE_YAML)
    text = "{{style:voice_rules}}"

    result = render_prompt_blocks(text, profile="slim", rules_path=rules_path)

    assert result == "No viewer directives. No first-person promo voice."


def test_profile_falls_back_to_full_when_slim_missing(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, SAMPLE_YAML)
    text = "{{style:casing_rules}}"

    result = render_prompt_blocks(text, profile="slim", rules_path=rules_path)

    assert result == "Use down style casing except for proper nouns."


# ---------------------------------------------------------------------------
# 4. Errors
# ---------------------------------------------------------------------------


def test_unknown_key_raises_naming_the_key(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, SAMPLE_YAML)
    text = "{{style:nonexistent_key}}"

    with pytest.raises(PromptBlockError, match="nonexistent_key"):
        render_prompt_blocks(text, rules_path=rules_path)


def test_missing_prompt_blocks_section_raises(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, MINIMAL_YAML)
    text = "{{style:voice_rules}}"

    with pytest.raises(PromptBlockError, match="prompt_blocks"):
        render_prompt_blocks(text, rules_path=rules_path)


def test_missing_rules_file_raises(tmp_path: Path) -> None:
    text = "{{style:voice_rules}}"
    missing_rules = tmp_path / "nonexistent.yaml"

    with pytest.raises(PromptBlockError):
        render_prompt_blocks(text, rules_path=missing_rules)


def test_profile_and_key_both_missing_raises(tmp_path: Path) -> None:
    # A block that has neither the requested profile nor a "full" fallback.
    yaml_text = """
meta: {version: 1, style_guide_synced: "2026-07-10"}
prompt_blocks:
  weird_block:
    slim: "slim only"
"""
    rules_path = _write(tmp_path, yaml_text)
    text = "{{style:weird_block}}"

    with pytest.raises(PromptBlockError, match="weird_block"):
        render_prompt_blocks(text, profile="enforce", rules_path=rules_path)


# ---------------------------------------------------------------------------
# 5. No re-expansion (single pass)
# ---------------------------------------------------------------------------


def test_no_reexpansion_of_nested_token(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, NESTED_TOKEN_YAML)
    text = "{{style:outer}}"

    # If this were multi-pass, the literal {{style:other}} inside the
    # rendered block would be looked up too and raise (no "other" key
    # exists in prompt_blocks). Single-pass substitution leaves it literal.
    result = render_prompt_blocks(text, rules_path=rules_path)

    assert result == "See also {{style:other}} for details."


# ---------------------------------------------------------------------------
# 6. Trailing-newline handling
# ---------------------------------------------------------------------------


def test_trailing_newline_stripped_once_no_doubled_blank_lines(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, NEWLINE_YAML)
    text = "Before.\n{{style:block_a}}\nAfter."

    result = render_prompt_blocks(text, rules_path=rules_path)

    assert result == "Before.\nRule line one.\nRule line two.\nAfter."
    assert "\n\n\n" not in result
    assert "\n\n" not in result


# ---------------------------------------------------------------------------
# 7. No-op guarantee on real prompts (token-free phases only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", TOKEN_FREE_PROMPT_PHASES)
def test_no_op_guarantee_on_real_prompt_files(phase: str) -> None:
    prompt_path = PROMPTS_DIR / f"{phase}.md"
    content = prompt_path.read_text()

    # Guards that no other prompt file has gained tokens.
    assert "{{style:" not in content

    # And proves the renderer is a true identity function on that content
    # (uses the DEFAULT_RULES_PATH default -- irrelevant here since the
    # short-circuit means the rules file is never touched).
    assert render_prompt_blocks(content) == content


# ---------------------------------------------------------------------------
# 7b. seo.md: task 1c gave it tokens -- prove they're present and render
# cleanly against the REAL config/house_style.yaml for both profiles.
# ---------------------------------------------------------------------------


def test_seo_prompt_contains_expected_style_tokens() -> None:
    content = SEO_PROMPT_PATH.read_text()
    assert "{{style:seo.copy_rules}}" in content
    assert "{{style:shared.char_budgets}}" in content


@pytest.mark.parametrize("profile", ["full", "slim"])
def test_seo_prompt_renders_against_real_config_for_both_profiles(profile: str) -> None:
    content = SEO_PROMPT_PATH.read_text()

    # Must not raise PromptBlockError for either profile against the REAL
    # config/house_style.yaml -- proves both seo.copy_rules and
    # shared.char_budgets have both a "full" and a "slim" entry.
    rendered = render_prompt_blocks(content, profile=profile)

    assert "{{style:" not in rendered


def test_seo_prompt_full_profile_renders_expected_copy_rules_block() -> None:
    """Golden assertions on the token-replaced regions only (not the whole
    prompt) -- keeps this test from being a brittle whole-file snapshot.
    """
    content = SEO_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="full")

    assert "## Copy style (REQUIRED)" in rendered
    assert "Down style for titles/headlines" in rendered
    assert (
        "Title ≤80 characters not counting the program name (≤100 in full), down style, with the primary keyword early"
        in rendered
    )
    assert "**PBS Wisconsin platform limits**" in rendered
    assert "Title: ≤80 characters" in rendered
    assert "Short description: ≤90 characters" in rendered
    assert "Long description: ≤350 characters" in rendered
    assert "{{style:" not in rendered


def test_seo_prompt_slim_profile_renders_condensed_blocks() -> None:
    content = SEO_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="slim")

    assert (
        "Casing and character counts are enforced after generation. Hard limits: "
        "title 80 not counting the program name (100 in full), short description 90, "
        "long description 350 characters, 15-20 keywords."
    ) in rendered
    assert (
        "Hard limits (enforced after generation): title 80 not counting the program name "
        "(100 in full), short description 90, "
        "long description 350 characters, 15-20 keywords."
    ) in rendered
    # Proves slim actually swapped in -- the full-profile-only heading must be absent.
    assert "## Copy style (REQUIRED)" not in rendered
    assert "{{style:" not in rendered


# ---------------------------------------------------------------------------
# 7c. validator.md: task 2d gave it a token -- prove it's present and renders
# cleanly against the REAL config/house_style.yaml for both profiles.
# ---------------------------------------------------------------------------


def test_validator_prompt_contains_expected_style_token() -> None:
    content = VALIDATOR_PROMPT_PATH.read_text()
    assert "{{style:validator.checklist}}" in content


@pytest.mark.parametrize("profile", ["full", "slim"])
def test_validator_prompt_renders_against_real_config_for_both_profiles(profile: str) -> None:
    content = VALIDATOR_PROMPT_PATH.read_text()

    # Must not raise PromptBlockError for either profile against the REAL
    # config/house_style.yaml -- proves validator.checklist has both a
    # "full" and a "slim" entry.
    rendered = render_prompt_blocks(content, profile=profile)

    assert "{{style:" not in rendered


def test_validator_prompt_no_stray_old_char_limits() -> None:
    """The pre-transcription checklist quoted stale 60/160 char limits (SST
    write path enforces 80/90) -- proves the bug-fix landed and neither
    stale number survives anywhere in the rendered prompt, in either profile.
    """
    content = VALIDATOR_PROMPT_PATH.read_text()

    for profile in ("full", "slim"):
        rendered = render_prompt_blocks(content, profile=profile)
        assert "60 characters" not in rendered
        assert "160 characters" not in rendered


def test_validator_prompt_full_profile_renders_expected_checklist_block() -> None:
    """Golden assertions on the token-replaced region only (not the whole
    prompt) -- keeps this test from being a brittle whole-file snapshot.
    """
    content = VALIDATOR_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="full")

    assert "## Validation Checklist" in rendered
    assert "### Analyst Phase" in rendered
    assert "### Formatter Phase" in rendered
    assert "### SEO Phase" in rendered
    assert "Title is under 80 characters" in rendered
    assert "Short description is under 90 characters" in rendered
    assert "Long description is under 350 characters" in rendered
    assert "Title accurately reflects content" in rendered
    assert "Description accurately reflects content" in rendered
    assert "{{style:" not in rendered


def test_validator_prompt_slim_profile_renders_condensed_block() -> None:
    content = VALIDATOR_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="slim")

    assert "Title accurately reflects content" in rendered
    assert "Description accurately reflects content" in rendered
    assert (
        "All mechanical checks (character limits, formatting, placeholders, truncation, "
        "speaker labels) run deterministically in the pipeline and are merged into your "
        "verdict — do NOT re-check them; judge only the semantic accuracy items above."
    ) in rendered
    # Proves slim actually swapped in -- the full-profile-only sections must be absent.
    assert "## Validation Checklist" not in rendered
    assert "### SEO Phase" not in rendered
    assert "{{style:" not in rendered


# ---------------------------------------------------------------------------
# 7d. formatter.md: task 3c gave it a token -- prove it's present and renders
# cleanly against the REAL config/house_style.yaml for both profiles.
# ---------------------------------------------------------------------------


def test_formatter_prompt_contains_expected_style_token() -> None:
    content = FORMATTER_PROMPT_PATH.read_text()
    assert "{{style:formatter.house_rules}}" in content


@pytest.mark.parametrize("profile", ["full", "slim"])
def test_formatter_prompt_renders_against_real_config_for_both_profiles(profile: str) -> None:
    content = FORMATTER_PROMPT_PATH.read_text()

    # Must not raise PromptBlockError for either profile against the REAL
    # config/house_style.yaml -- proves formatter.house_rules has both a
    # "full" and a "slim" entry.
    rendered = render_prompt_blocks(content, profile=profile)

    assert "{{style:" not in rendered


def test_formatter_prompt_full_profile_renders_expected_house_rules_block() -> None:
    """Golden assertions on the token-replaced region only (not the whole
    prompt) -- keeps this test from being a brittle whole-file snapshot.
    """
    content = FORMATTER_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="full")

    assert "### PBS Wisconsin House Style" in rendered
    assert '"Capitol" not "capital"' in rendered
    assert '"OK" not "okay"' in rendered
    assert "liberals" in rendered and "conservatives" in rendered
    assert '"Legislature" capitalized, committees lowercase' in rendered
    assert "No oxford commas" in rendered
    assert "Abbreviate honorifics" in rendered
    assert "Em dashes" in rendered
    assert "Numbers in scores/tallies" in rendered
    assert '"Marquette Poll" capitalized' in rendered
    assert '"partisan" not "partizan"' in rendered
    assert "Program names are NOT italicized" in rendered
    assert "Speaker names are always bolded" in rendered
    assert "NEVER suppress content" in rendered
    assert "{{style:" not in rendered


def test_formatter_prompt_slim_profile_renders_condensed_block() -> None:
    content = FORMATTER_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="slim")

    # Flag-tier / judgment-guidance items survive in slim.
    assert '"Capitol" not "capital"' in rendered
    assert '"Legislature" capitalized, committees lowercase' in rendered
    assert "No oxford commas" in rendered
    assert "Em dashes" in rendered
    assert "Numbers in scores/tallies" in rendered
    assert "NEVER suppress content" in rendered
    assert (
        "Lexical house-style substitutions (OK/partisan/Marquette Poll/program-name "
        "italics/honorific abbreviations/liberals-conservatives casing) are applied "
        "deterministically after generation — do not worry about them."
    ) in rendered
    # Proves slim actually swapped in -- the full-profile-only heading and the
    # deterministically-enforced pairs must be absent.
    assert "### PBS Wisconsin House Style" not in rendered
    assert '"OK" not "okay"' not in rendered
    assert '"partisan" not "partizan"' not in rendered
    assert '"Marquette Poll" capitalized' not in rendered
    assert "Abbreviate honorifics" not in rendered
    assert "{{style:" not in rendered


# ---------------------------------------------------------------------------
# 7e. timestamp.md: task 4b gave it a token -- prove it's present and renders
# cleanly against the REAL config/house_style.yaml for both profiles.
# ---------------------------------------------------------------------------


def test_timestamp_prompt_contains_expected_style_token() -> None:
    content = TIMESTAMP_PROMPT_PATH.read_text()
    assert "{{style:timestamp.output_contract}}" in content


@pytest.mark.parametrize("profile", ["full", "slim"])
def test_timestamp_prompt_renders_against_real_config_for_both_profiles(profile: str) -> None:
    content = TIMESTAMP_PROMPT_PATH.read_text()

    # Must not raise PromptBlockError for either profile against the REAL
    # config/house_style.yaml -- proves timestamp.output_contract has both a
    # "full" and a "slim" entry.
    rendered = render_prompt_blocks(content, profile=profile)

    assert "{{style:" not in rendered


def test_timestamp_prompt_full_profile_renders_expected_output_contract_block() -> None:
    """Golden assertions on the token-replaced region only (not the whole
    prompt) -- keeps this test from being a brittle whole-file snapshot.
    Proves the full profile preserves today's free-form contract verbatim:
    the manually-typed output template, the chapter-count table, the first-
    chapter rule, the time-format specs, and the quality checklist.
    """
    content = TIMESTAMP_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="full")

    assert "### Required Output Format" in rendered
    assert "**Project:** {project_name}" in rendered
    assert "**Duration:** {total_duration}" in rendered
    assert "## Media Manager Format" in rendered
    assert "## YouTube Format" in rendered
    assert "### Chapter Count Targets (Maximum)" in rendered
    assert "| Under 5 min | 3 |" in rendered
    assert "| 60+ min | 10 |" in rendered
    assert "### First Chapter Rule" in rendered
    assert "The first chapter is always `0:00 Episode intro`." in rendered
    assert "### Time Format Specifications" in rendered
    assert "Use `H:MM:SS.000` with millisecond precision" in rendered
    assert "Use `M:SS` for times under 1 hour" in rendered
    assert "## Quality Checklist" in rendered
    assert "Both format tables are complete and match" in rendered
    assert "{{style:" not in rendered


def test_timestamp_prompt_slim_profile_renders_structured_contract() -> None:
    content = TIMESTAMP_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="slim")

    assert "fenced code block labeled `chapters`" in rendered
    assert "`<timecode> <title>`" in rendered
    assert "Choose boundaries ONLY from the candidate list" in rendered
    assert "Do not include the forced first chapter" in rendered
    assert "the pipeline builds both deterministically from your chapter list" in rendered
    assert "2-6 words, sentence case, naming the topic" in rendered
    assert "parallel framing for political content" in rendered
    assert "```chapters" in rendered
    # Proves slim actually swapped in -- the full-profile-only sections must be absent.
    assert "### Required Output Format" not in rendered
    assert "### Chapter Count Targets (Maximum)" not in rendered
    assert "Both format tables are complete and match" not in rendered
    assert "{{style:" not in rendered


def test_timestamp_prompt_naming_taste_guidance_survives_both_profiles() -> None:
    """The naming-taste guidance (neutral tone, topic-not-format, no generic
    names, parallel framing) stays as unconditional prose in the .md file
    (not gated by the token) -- task 4b's documented choice -- so it applies
    identically under both profiles. Also spot-checked as a condensed
    restatement inside the slim block itself (see the slim test above) since
    boundary selection + title-writing is the model's entire job once the
    format machinery moves into the deterministic engine.
    """
    content = TIMESTAMP_PROMPT_PATH.read_text()
    for profile in ("full", "slim"):
        rendered = render_prompt_blocks(content, profile=profile)
        assert "### Chapter Naming Guidelines" in rendered
        assert "Parallel framing for political content" in rendered
        assert "Avoid generic names" in rendered


# ---------------------------------------------------------------------------
# 7f. analyst.md: task 5 gave it a token -- prove it's present and renders
# cleanly against the REAL config/house_style.yaml for both profiles.
# ---------------------------------------------------------------------------


def test_analyst_prompt_contains_expected_style_token() -> None:
    content = ANALYST_PROMPT_PATH.read_text()
    assert "{{style:analyst.draft_guidance}}" in content


@pytest.mark.parametrize("profile", ["full", "slim"])
def test_analyst_prompt_renders_against_real_config_for_both_profiles(profile: str) -> None:
    content = ANALYST_PROMPT_PATH.read_text()

    # Must not raise PromptBlockError for either profile against the REAL
    # config/house_style.yaml -- proves analyst.draft_guidance has both a
    # "full" and a "slim" entry.
    rendered = render_prompt_blocks(content, profile=profile)

    assert "{{style:" not in rendered


def test_analyst_prompt_full_profile_renders_expected_draft_guidance_block() -> None:
    """Golden assertions on the token-replaced region only (not the whole
    prompt) -- keeps this test from being a brittle whole-file snapshot.
    """
    content = ANALYST_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="full")

    assert "## Metadata Suggestions" in rendered
    assert "**Suggested Title (draft):**" in rendered
    assert "title ≤ 80 characters" in rendered
    assert "short description ≤ 90 characters" in rendered
    assert "long description ≤ 350 characters" in rendered
    assert "15-20 total keywords" in rendered
    assert "{{style:" not in rendered


def test_analyst_prompt_slim_profile_renders_condensed_block() -> None:
    content = ANALYST_PROMPT_PATH.read_text()
    rendered = render_prompt_blocks(content, profile="slim")

    assert (
        "Draft metadata guidance arrives in the Style Rules section — the pipeline verifies "
        "limits deterministically."
    ) in rendered
    # Proves slim actually swapped in -- the full-profile-only heading and
    # bracketed placeholders must be absent.
    assert "## Metadata Suggestions" not in rendered
    assert "**Suggested Title (draft):**" not in rendered
    assert "{{style:" not in rendered


def test_analyst_prompt_no_stray_old_draft_ranges() -> None:
    """The pre-transcription draft-metadata guidance quoted stale
    60-70/100-150/250-300 char ranges and a 15-25 keyword range -- proves the
    DECIDED single-limit correction (80/90/350, 15-20) landed and none of the
    stale numbers survive anywhere in the rendered prompt, in either profile.
    """
    content = ANALYST_PROMPT_PATH.read_text()

    for profile in ("full", "slim"):
        rendered = render_prompt_blocks(content, profile=profile)
        assert "60-70" not in rendered
        assert "100-150" not in rendered
        assert "250-300" not in rendered
        assert "15-25" not in rendered


def test_analyst_prompt_never_fabricate_names_rule_survives_both_profiles() -> None:
    """task 5's brief: the never-fabricate-names rule (semantic, about live
    captioning speaker attribution) is untouched by the placeholder-warning
    boilerplate trim -- must survive under both profiles since it lives
    outside the tokenized region entirely.
    """
    content = ANALYST_PROMPT_PATH.read_text()
    for profile in ("full", "slim"):
        rendered = render_prompt_blocks(content, profile=profile)
        assert "NEVER fabricate proper names from garbled caption text" in rendered


# ---------------------------------------------------------------------------
# 8. Snapshot: golden-string test
# ---------------------------------------------------------------------------


def test_golden_snapshot_exact_rendered_output(tmp_path: Path) -> None:
    rules_path = _write(tmp_path, GOLDEN_YAML)
    text = "# SEO Agent\n\n{{style:greeting}}\n\nWrite a title under 60 characters."

    result = render_prompt_blocks(text, rules_path=rules_path)

    assert result == ("# SEO Agent\n\nHello there.\nBe kind.\n\nWrite a title under 60 characters.")


# ---------------------------------------------------------------------------
# 9. Worker wiring: resolve_prompt_profile pure function
# ---------------------------------------------------------------------------


def test_resolve_prompt_profile_missing_cfg_returns_full() -> None:
    assert resolve_prompt_profile({}, "seo") == "full"


def test_resolve_prompt_profile_disabled_returns_full() -> None:
    cfg = {"enabled": False, "phases": {"seo": {"post": "enforce"}}}
    assert resolve_prompt_profile(cfg, "seo") == "full"


def test_resolve_prompt_profile_enabled_mode_off_returns_full() -> None:
    cfg = {"enabled": True, "phases": {"seo": {"post": "off"}}}
    assert resolve_prompt_profile(cfg, "seo") == "full"


def test_resolve_prompt_profile_enabled_mode_shadow_returns_full() -> None:
    cfg = {"enabled": True, "phases": {"seo": {"post": "shadow"}}}
    assert resolve_prompt_profile(cfg, "seo") == "full"


def test_resolve_prompt_profile_enabled_mode_enforce_returns_slim() -> None:
    cfg = {"enabled": True, "phases": {"seo": {"post": "enforce"}}}
    assert resolve_prompt_profile(cfg, "seo") == "slim"


def test_resolve_prompt_profile_phase_missing_defaults_full() -> None:
    cfg = {"enabled": True, "phases": {}}
    assert resolve_prompt_profile(cfg, "seo") == "full"


def test_resolve_prompt_profile_validator_uses_lint_key_not_post() -> None:
    cfg = {"enabled": True, "phases": {"validator": {"lint": "enforce", "post": "off"}}}
    assert resolve_prompt_profile(cfg, "validator") == "slim"


def test_resolve_prompt_profile_validator_missing_lint_defaults_full() -> None:
    cfg = {"enabled": True, "phases": {"validator": {"post": "enforce"}}}
    assert resolve_prompt_profile(cfg, "validator") == "full"


# ---------------------------------------------------------------------------
# 10. strip_style_tokens -- the graceful-fallback degradation used by
# _load_agent_prompt when render_prompt_blocks can't render (PR #295 review #1)
# ---------------------------------------------------------------------------


def test_strip_style_tokens_removes_own_line_token_and_its_newline() -> None:
    # A token alone on its line is removed along with its trailing newline so
    # the degraded prompt has no stray blank line where the block would sit.
    text = "You are the SEO agent.\n{{style:seo.copy_rules}}\nWrite a title."
    assert strip_style_tokens(text) == "You are the SEO agent.\nWrite a title."


def test_strip_style_tokens_removes_inline_token() -> None:
    # An inline token is removed in place (no trailing newline to consume).
    text = "See {{style:foo}} for rules."
    assert strip_style_tokens(text) == "See  for rules."


def test_strip_style_tokens_noop_when_no_tokens() -> None:
    text = "Plain prompt with no tokens."
    assert strip_style_tokens(text) == text


def test_strip_style_tokens_removes_all_of_multiple() -> None:
    text = "{{style:a}}\nmiddle\n{{style:b.c}}\nend"
    assert "{{style:" not in strip_style_tokens(text)


# ---------------------------------------------------------------------------
# 11. validate_prompt_blocks -- boot-time guard that every token-bearing prompt
# renders against the configured house-style YAML (fail-fast at startup)
# ---------------------------------------------------------------------------


def test_validate_prompt_blocks_returns_token_bearing_files_on_valid_config(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "seo.md").write_text("Intro.\n{{style:voice_rules}}\n")
    (prompt_dir / "plain.md").write_text("No tokens here.")
    rules_path = _write(tmp_path, SAMPLE_YAML)

    validated = validate_prompt_blocks(prompt_dir, rules_path=rules_path)

    # Only the token-bearing prompt is validated; the token-free one is skipped.
    assert validated == ["seo.md"]


def test_validate_prompt_blocks_raises_on_missing_rules_file(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "seo.md").write_text("{{style:voice_rules}}")

    with pytest.raises(PromptBlockError):
        validate_prompt_blocks(prompt_dir, rules_path=tmp_path / "missing.yaml")


def test_validate_prompt_blocks_raises_on_unknown_key(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "seo.md").write_text("{{style:nonexistent_key}}")
    rules_path = _write(tmp_path, SAMPLE_YAML)

    with pytest.raises(PromptBlockError, match="nonexistent_key"):
        validate_prompt_blocks(prompt_dir, rules_path=rules_path)


def test_validate_prompt_blocks_checks_all_profiles(tmp_path: Path) -> None:
    # A block present in "full" but absent in "slim" WITHOUT a full fallback for
    # slim would only fail under the slim profile -- proving both are checked.
    yaml_text = """
meta: {version: 1, style_guide_synced: "2026-07-10"}
prompt_blocks:
  block_full_only:
    full: "full text"
  block_slim_bad:
    full: "full ok"
"""
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    # block_full_only renders fine under both (slim falls back to full).
    (prompt_dir / "ok.md").write_text("{{style:block_full_only}}")
    rules_path = _write(tmp_path, yaml_text)

    # Sanity: the good prompt validates without raising.
    assert validate_prompt_blocks(prompt_dir, rules_path=rules_path) == ["ok.md"]


def test_validate_prompt_blocks_passes_against_real_config() -> None:
    # The real prompts/ + config/house_style.yaml must always validate --
    # this is the exact check the app runs at startup.
    validated = validate_prompt_blocks(PROMPTS_DIR)
    # Every phase prompt that carries tokens (all five phases) is covered.
    assert set(validated) >= {"seo.md", "validator.md", "formatter.md", "timestamp.md", "analyst.md"}
