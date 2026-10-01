"""Tests for MCP server tool handlers.

Tests the tool handler functions directly (not via MCP protocol),
using a temporary OUTPUT directory to isolate filesystem operations.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.server import (
    WRITABLE_FIELDS,
    _extract_sst_fields,
    handle_save_keyword_report,
    handle_save_revision,
)


@pytest.fixture
def output_dir(tmp_path):
    """Provide a temporary OUTPUT directory and patch the module global."""
    with patch("mcp_server.server.OUTPUT_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def project_with_manifest(output_dir):
    """Create a project folder with a minimal manifest for testing."""
    project_name = "2WLITestProjectSM"
    project_path = output_dir / project_name
    project_path.mkdir()
    manifest = {
        "project_name": project_name,
        "phases": [
            {"name": "analyst", "status": "completed"},
            {"name": "formatter", "status": "completed"},
            {"name": "seo", "status": "completed"},
        ],
        "outputs": {
            "analysis": "analyst_output.md",
            "formatted_transcript": "formatter_output.md",
            "seo_metadata": "seo_output.md",
        },
        "revisions": [],
        "keyword_reports": [],
    }
    (project_path / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (project_path / "analyst_output.md").write_text("# Analysis\nTest analysis content.")
    (project_path / "formatter_output.md").write_text("# Formatted Transcript\nTest transcript.")
    (project_path / "seo_output.md").write_text("# SEO\nTest SEO content.")
    return project_name, project_path


@pytest.mark.asyncio
async def test_save_revision_succeeds(project_with_manifest):
    """save_revision should write a file and update manifest."""
    project_name, project_path = project_with_manifest
    result = await handle_save_revision(
        {
            "project_name": project_name,
            "content": "# Revision\nTest revision content.",
        }
    )
    assert len(result) == 1
    assert "✅" in result[0].text
    assert "copy_revision_v1.md" in result[0].text

    assert (project_path / "copy_revision_v1.md").exists()
    assert (project_path / "copy_revision_v1.md").read_text() == "# Revision\nTest revision content."

    manifest = json.loads((project_path / "manifest.json").read_text())
    assert len(manifest["revisions"]) == 1
    assert manifest["revisions"][0]["version"] == 1


@pytest.mark.asyncio
async def test_save_revision_auto_versions(project_with_manifest):
    """save_revision should auto-increment version numbers."""
    project_name, project_path = project_with_manifest

    await handle_save_revision({"project_name": project_name, "content": "Version 1"})
    result = await handle_save_revision({"project_name": project_name, "content": "Version 2"})
    assert "copy_revision_v2.md" in result[0].text
    assert (project_path / "copy_revision_v2.md").read_text() == "Version 2"


@pytest.mark.asyncio
async def test_save_revision_missing_args(output_dir):
    """save_revision should return error for missing arguments."""
    result = await handle_save_revision({"project_name": "test"})
    assert "Error" in result[0].text

    result = await handle_save_revision({"content": "test"})
    assert "Error" in result[0].text


@pytest.mark.asyncio
async def test_save_revision_completes_within_timeout(project_with_manifest):
    """save_revision should complete within a reasonable time, not hang."""
    project_name, _ = project_with_manifest
    result = await asyncio.wait_for(
        handle_save_revision(
            {
                "project_name": project_name,
                "content": "Timeout test content",
            }
        ),
        timeout=5.0,
    )
    assert "✅" in result[0].text


@pytest.mark.asyncio
async def test_save_keyword_report_succeeds(project_with_manifest):
    """save_keyword_report should write a file and update manifest."""
    project_name, project_path = project_with_manifest
    result = await handle_save_keyword_report(
        {
            "project_name": project_name,
            "content": "# Keywords\nTest keyword report.",
        }
    )
    assert "✅" in result[0].text
    assert "keyword_report_v1.md" in result[0].text

    assert (project_path / "keyword_report_v1.md").exists()
    manifest = json.loads((project_path / "manifest.json").read_text())
    assert len(manifest["keyword_reports"]) == 1


@pytest.mark.asyncio
async def test_save_keyword_report_auto_versions(project_with_manifest):
    """save_keyword_report should auto-increment version numbers."""
    project_name, project_path = project_with_manifest

    await handle_save_keyword_report({"project_name": project_name, "content": "Report 1"})
    result = await handle_save_keyword_report({"project_name": project_name, "content": "Report 2"})
    assert "keyword_report_v2.md" in result[0].text


@pytest.mark.asyncio
async def test_save_keyword_report_completes_within_timeout(project_with_manifest):
    """save_keyword_report should complete within a reasonable time."""
    project_name, _ = project_with_manifest
    result = await asyncio.wait_for(
        handle_save_keyword_report({"project_name": project_name, "content": "Timeout test"}),
        timeout=5.0,
    )
    assert "✅" in result[0].text


@pytest.mark.asyncio
async def test_validate_copy_all_valid(output_dir):
    """validate_copy should report all fields valid when under limits."""
    from mcp_server.server import handle_validate_copy

    result = await handle_validate_copy(
        {
            "title": "Wisconsin Life | Alice Good Café in Verona",
            "short_description": "In Verona, Alice Good brews Colombian coffee and community.",
            "long_description": "Alice Good Café in Verona is more than a coffee shop.",
        }
    )
    text = result[0].text
    assert "✅" in text
    assert "Yes" in text  # "All valid: ✅ Yes"


@pytest.mark.asyncio
async def test_validate_copy_over_limit(output_dir):
    """validate_copy should flag fields that exceed character limits."""
    from mcp_server.server import handle_validate_copy

    result = await handle_validate_copy(
        {
            "title": "X" * 85,
            "short_description": "X" * 105,
            "long_description": "OK",
        }
    )
    text = result[0].text
    assert "❌" in text
    assert "OVER LIMIT" in text
    assert "85" in text


@pytest.mark.asyncio
async def test_validate_copy_partial_fields(output_dir):
    """validate_copy should work with only some fields provided."""
    from mcp_server.server import handle_validate_copy

    result = await handle_validate_copy(
        {
            "title": "Just a title",
        }
    )
    text = result[0].text
    assert "12" in text
    assert "Error" not in text


@pytest.mark.asyncio
async def test_validate_copy_with_keywords(output_dir):
    """validate_copy should count keywords when provided."""
    from mcp_server.server import handle_validate_copy

    result = await handle_validate_copy(
        {
            "title": "Test Title",
            "keywords": "coffee, Verona, Wisconsin, fair trade, community",
        }
    )
    text = result[0].text
    assert "5" in text  # 5 keywords


@pytest.mark.asyncio
async def test_validate_copy_empty(output_dir):
    """validate_copy should handle no fields gracefully."""
    from mcp_server.server import handle_validate_copy

    result = await handle_validate_copy({})
    text = result[0].text
    assert "Error" in text or "at least one" in text.lower()


@pytest.mark.asyncio
async def test_list_project_files_shows_all_files(project_with_manifest):
    """list_project_files should return all files in the project folder."""
    from mcp_server.server import handle_list_project_files

    project_name, project_path = project_with_manifest

    # Add some extra files to simulate a real project
    (project_path / "copy_revision_v1.md").write_text("Revision 1")
    (project_path / "keyword_report_v1.md").write_text("Keywords")
    semrush_dir = project_path / "semrush"
    semrush_dir.mkdir()
    (semrush_dir / "export_2026-04-16.csv").write_text("keyword,volume\ncoffee,1000")

    result = await handle_list_project_files({"project_name": project_name})
    text = result[0].text

    assert "analyst_output.md" in text
    assert "copy_revision_v1.md" in text
    assert "keyword_report_v1.md" in text
    assert "export_2026-04-16.csv" in text
    assert "manifest.json" in text


@pytest.mark.asyncio
async def test_list_project_files_missing_project(output_dir):
    """list_project_files should return error for nonexistent project."""
    from mcp_server.server import handle_list_project_files

    result = await handle_list_project_files({"project_name": "nonexistent"})
    text = result[0].text
    assert "not found" in text.lower() or "Error" in text


@pytest.mark.asyncio
async def test_list_project_files_empty_project(output_dir):
    """list_project_files should handle a project with only a manifest."""
    from mcp_server.server import handle_list_project_files

    project_name = "2WLIEmptyProject"
    project_path = output_dir / project_name
    project_path.mkdir()
    (project_path / "manifest.json").write_text('{"project_name": "2WLIEmptyProject"}')

    result = await handle_list_project_files({"project_name": project_name})
    text = result[0].text
    assert "manifest.json" in text


@pytest.mark.asyncio
async def test_list_revisions_with_history(project_with_manifest):
    """list_revisions should show revision and keyword report history."""
    from mcp_server.server import handle_list_revisions

    project_name, project_path = project_with_manifest

    # Create some revisions via the actual save tool
    await handle_save_revision({"project_name": project_name, "content": "Rev 1"})
    await handle_save_revision({"project_name": project_name, "content": "Rev 2"})
    await handle_save_keyword_report({"project_name": project_name, "content": "KW 1"})

    result = await handle_list_revisions({"project_name": project_name})
    text = result[0].text

    assert "v1" in text
    assert "v2" in text
    assert "copy_revision" in text
    assert "keyword_report" in text


@pytest.mark.asyncio
async def test_list_revisions_empty_project(project_with_manifest):
    """list_revisions should report no revisions for a fresh project."""
    from mcp_server.server import handle_list_revisions

    project_name, _ = project_with_manifest

    result = await handle_list_revisions({"project_name": project_name})
    text = result[0].text
    assert "no revisions" in text.lower() or "No revisions" in text


@pytest.mark.asyncio
async def test_list_revisions_missing_project(output_dir):
    """list_revisions should return error for nonexistent project."""
    from mcp_server.server import handle_list_revisions

    result = await handle_list_revisions({"project_name": "nonexistent"})
    text = result[0].text
    assert "not found" in text.lower() or "Error" in text


def test_writable_fields_allowlist():
    """WRITABLE_FIELDS should contain exactly the approved fields."""
    assert "title" in WRITABLE_FIELDS
    assert "short_description" in WRITABLE_FIELDS
    assert "long_description" in WRITABLE_FIELDS
    assert "keywords" in WRITABLE_FIELDS
    assert "social_description" in WRITABLE_FIELDS
    assert "social_tags" in WRITABLE_FIELDS
    assert "facebook_description" in WRITABLE_FIELDS
    assert "hashtags" in WRITABLE_FIELDS
    assert "status" not in WRITABLE_FIELDS
    assert "producer" not in WRITABLE_FIELDS
    assert "media_id" not in WRITABLE_FIELDS


def test_extract_sst_fields_includes_social():
    """_extract_sst_fields should extract social media fields."""
    record = {
        "id": "recTEST123",
        "fields": {
            "Media ID": "2WLITestSM",
            "Release Title": "Test Title",
            "Short Description": "Test short",
            "Long Description": "Test long",
            "General Keywords/Tags": "kw1, kw2",
            "Social Media Description": "Social desc",
            "Social Media Tags": "social tags",
            "Facebook Description": "FB desc",
            "Hashtags": "#test #hashtag",
        },
    }
    result = _extract_sst_fields(record)
    assert result["social_description"] == "Social desc"
    assert result["social_tags"] == "social tags"
    assert result["facebook_description"] == "FB desc"
    assert result["hashtags"] == "#test #hashtag"


@pytest.mark.asyncio
async def test_patch_sst_record_success(output_dir, monkeypatch):
    """patch_sst_record should PATCH the Airtable record and return True."""
    from mcp_server.server import patch_sst_record

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "recTEST123", "fields": {"Release Title": "New Title"}}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_response)

    monkeypatch.setattr("mcp_server.server.AIRTABLE_API_KEY", "fake-key")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: mock_client)

    success, result = await patch_sst_record("recTEST123", {"Release Title": "New Title"})
    assert success is True
    mock_client.patch.assert_called_once()


@pytest.mark.asyncio
async def test_patch_sst_record_no_api_key(output_dir, monkeypatch):
    """patch_sst_record should fail gracefully without an API key."""
    from mcp_server.server import patch_sst_record

    monkeypatch.setattr("mcp_server.server.AIRTABLE_API_KEY", None)

    success, result = await patch_sst_record("recTEST123", {"Release Title": "New"})
    assert success is False
    assert "not configured" in result.lower()


@pytest.mark.asyncio
async def test_post_sst_comment_success(output_dir, monkeypatch):
    """post_sst_comment should POST a comment to the Airtable record."""
    from mcp_server.server import post_sst_comment

    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr("mcp_server.server.AIRTABLE_API_KEY", "fake-key")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: mock_client)

    success = await post_sst_comment("recTEST123", "Test comment")
    assert success is True


@pytest.mark.asyncio
async def test_propose_sst_edit_stages_in_manifest(project_with_manifest, monkeypatch):
    """propose_sst_edit should store the proposal in manifest.json."""
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title", "short_description": "Old short"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, project_path = project_with_manifest
    result = await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "Wisconsin Life | New Title Here",
            "reason": "SEO improvement",
        }
    )
    text = result[0].text
    assert "Wisconsin Life | New Title Here" in text
    assert "✅" in text  # under 80 char limit

    manifest = json.loads((project_path / "manifest.json").read_text())
    assert "proposed_edits" in manifest
    assert "title" in manifest["proposed_edits"]
    assert manifest["proposed_edits"]["title"]["proposed_value"] == "Wisconsin Life | New Title Here"
    assert manifest["proposed_edits"]["title"]["current_value"] == "Old Title"
    assert manifest["proposed_edits"]["title"]["record_id"] == "recTEST123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    [
        "```\nMadison Symphony Orchestra, classical music\n```",
        "`Madison Symphony Orchestra, classical music`",
        "```Madison Symphony Orchestra, classical music",
        "~~~\nMadison Symphony Orchestra, classical music\n~~~",
        "​```\nMadison Symphony Orchestra\n```﻿",
    ],
    ids=["fenced-block", "inline-backticks", "leading-fence-only", "tilde-fence", "invisible-wrapped-fence"],
)
async def test_propose_sst_edit_rejects_code_fenced_value(project_with_manifest, monkeypatch, bad_value):
    """A proposed value carrying a boundary code fence must be rejected (#380).

    Pasted fences render invisibly in richText fields while corrupting the
    first and last keywords — it happened twice to the same record.
    """
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "keywords": "old, keywords"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, project_path = project_with_manifest
    result = await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "keywords",
            "proposed_value": bad_value,
            "reason": "SEO refresh",
        }
    )
    assert "fence" in result[0].text.lower()

    manifest = json.loads((project_path / "manifest.json").read_text())
    assert "keywords" not in manifest.get("proposed_edits", {})


@pytest.mark.asyncio
async def test_propose_sst_edit_rejects_non_string_value(project_with_manifest, monkeypatch):
    """A non-string proposed_value gets a clean error, not an AttributeError."""
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    result = await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "keywords",
            "proposed_value": ["a", "list", "of", "tags"],
            "reason": "SEO refresh",
        }
    )
    assert "must be a string" in result[0].text


@pytest.mark.asyncio
async def test_commit_sst_edits_blocks_fenced_staged_value(project_with_manifest, monkeypatch):
    """Commit re-checks for fences: a manifest staged before the propose-time
    guard (or hand-edited) must not write a fence to Airtable."""
    from mcp_server.server import handle_commit_sst_edits, load_manifest, save_manifest

    async def mock_fetch(record_id):
        return {"keywords": "old, keywords"}

    monkeypatch.setattr("mcp_server.server.fetch_sst_context", mock_fetch)

    project_name, _ = project_with_manifest
    manifest = load_manifest(project_name) or {"project_name": project_name}
    manifest["proposed_edits"] = {
        "keywords": {
            "airtable_column": "General Keywords/Tags",
            "current_value": "old, keywords",
            "proposed_value": "```\nMadison Symphony Orchestra, classical music\n```",
            "reason": "staged before the guard existed",
            "record_id": "recTEST123",
        }
    }
    save_manifest(project_name, manifest)

    result = await handle_commit_sst_edits({"media_id": project_name})
    assert "Commit blocked" in result[0].text
    assert "fence" in result[0].text.lower()


@pytest.mark.asyncio
async def test_propose_sst_edit_allows_interior_backticks(project_with_manifest, monkeypatch):
    """Only leading/trailing backticks are paste artifacts; interior ones pass."""
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "long_description": "old"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    result = await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "long_description",
            "proposed_value": "A look at the `pipeline` behind the music.",
            "reason": "editorial",
        }
    )
    assert "Proposed Edit" in result[0].text


@pytest.mark.asyncio
async def test_propose_sst_edit_rejects_disallowed_field(project_with_manifest, monkeypatch):
    """propose_sst_edit should reject fields not in the allowlist."""
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    result = await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "status",
            "proposed_value": "Complete",
            "reason": "Done",
        }
    )
    assert (
        "not writable" in result[0].text.lower()
        or "not allowed" in result[0].text.lower()
        or "allowed fields" in result[0].text.lower()
    )


@pytest.mark.asyncio
async def test_propose_sst_edit_warns_over_limit(project_with_manifest, monkeypatch):
    """propose_sst_edit should warn when proposed value exceeds character limit."""
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    result = await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "X" * 85,
            "reason": "Testing",
        }
    )
    text = result[0].text
    assert "❌" in text or "OVER" in text


@pytest.mark.asyncio
async def test_propose_sst_edit_multiple_fields(project_with_manifest, monkeypatch):
    """propose_sst_edit should allow staging multiple fields."""
    from mcp_server.server import handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title", "short_description": "Old short"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, project_path = project_with_manifest

    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "Better",
        }
    )
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "short_description",
            "proposed_value": "New short desc",
            "reason": "Clearer",
        }
    )

    manifest = json.loads((project_path / "manifest.json").read_text())
    assert "title" in manifest["proposed_edits"]
    assert "short_description" in manifest["proposed_edits"]


@pytest.mark.asyncio
async def test_review_proposed_edits_shows_diff(project_with_manifest, monkeypatch):
    """review_proposed_edits should show current vs proposed for all staged edits."""
    from mcp_server.server import handle_propose_sst_edit, handle_review_proposed_edits

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title", "short_description": "Old short"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest

    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "Better SEO",
        }
    )
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "short_description",
            "proposed_value": "New short",
            "reason": "Clearer",
        }
    )

    result = await handle_review_proposed_edits({"media_id": project_name})
    text = result[0].text

    assert "Old Title" in text
    assert "New Title" in text
    assert "Old short" in text
    assert "New short" in text
    assert "Better SEO" in text
    assert "2" in text  # 2 edits


@pytest.mark.asyncio
async def test_review_proposed_edits_empty(project_with_manifest):
    """review_proposed_edits should report no pending edits."""
    from mcp_server.server import handle_review_proposed_edits

    project_name, _ = project_with_manifest
    result = await handle_review_proposed_edits({"media_id": project_name})
    text = result[0].text
    assert "no" in text.lower() or "No" in text


@pytest.mark.asyncio
async def test_commit_sst_edits_writes_and_comments(project_with_manifest, monkeypatch):
    """commit_sst_edits should PATCH Airtable and post an audit comment."""
    from mcp_server.server import handle_commit_sst_edits, handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, project_path = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "SEO",
        }
    )

    # Mock concurrency re-fetch (returns same value as when proposed — no conflict)
    async def mock_fetch(record_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.fetch_sst_context", mock_fetch)

    # Mock the write
    async def mock_patch(record_id, fields):
        return True, {"id": record_id, "fields": fields}

    monkeypatch.setattr("mcp_server.server.patch_sst_record", mock_patch)

    # Mock the comment
    comment_posted = []

    async def mock_comment(record_id, text):
        comment_posted.append(text)
        return True

    monkeypatch.setattr("mcp_server.server.post_sst_comment", mock_comment)

    result = await handle_commit_sst_edits({"media_id": project_name})
    text = result[0].text

    assert "✅" in text
    assert "New Title" in text
    assert len(comment_posted) == 1
    assert "Old Title" in comment_posted[0]
    assert "New Title" in comment_posted[0]

    # Verify proposed_edits cleared from manifest
    manifest = json.loads((project_path / "manifest.json").read_text())
    assert manifest.get("proposed_edits", {}) == {}


@pytest.mark.asyncio
async def test_commit_sst_edits_concurrency_conflict(project_with_manifest, monkeypatch):
    """commit_sst_edits should refuse if Airtable values changed since proposal."""
    from mcp_server.server import handle_commit_sst_edits, handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, project_path = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "SEO",
        }
    )

    # Mock concurrency re-fetch — returns DIFFERENT value (someone edited Airtable)
    async def mock_fetch(record_id):
        return {"record_id": "recTEST123", "title": "Manually Edited Title"}

    monkeypatch.setattr("mcp_server.server.fetch_sst_context", mock_fetch)

    result = await handle_commit_sst_edits({"media_id": project_name})
    text = result[0].text

    assert "conflict" in text.lower() or "changed" in text.lower()
    assert "Manually Edited Title" in text

    # Verify proposed_edits NOT cleared (user can re-propose)
    manifest = json.loads((project_path / "manifest.json").read_text())
    assert "title" in manifest.get("proposed_edits", {})


@pytest.mark.asyncio
async def test_commit_sst_edits_no_pending(project_with_manifest):
    """commit_sst_edits should report no pending edits."""
    from mcp_server.server import handle_commit_sst_edits

    project_name, _ = project_with_manifest
    result = await handle_commit_sst_edits({"media_id": project_name})
    assert "no" in result[0].text.lower() or "No" in result[0].text


@pytest.mark.asyncio
async def test_patch_sst_record_http_error(output_dir, monkeypatch):
    """patch_sst_record should return failure on non-200 responses."""
    from mcp_server.server import patch_sst_record

    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.text = "INVALID_VALUE"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_response)

    monkeypatch.setattr("mcp_server.server.AIRTABLE_API_KEY", "fake-key")
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: mock_client)

    success, result = await patch_sst_record("recTEST123", {"Release Title": "X"})
    assert success is False
    assert "422" in result


@pytest.mark.asyncio
async def test_commit_sst_edits_patch_failure(project_with_manifest, monkeypatch):
    """commit_sst_edits should report error when PATCH fails after concurrency check passes."""
    from mcp_server.server import handle_commit_sst_edits, handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, project_path = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "SEO",
        }
    )

    # Concurrency check passes
    async def mock_fetch(record_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.fetch_sst_context", mock_fetch)

    # But PATCH fails
    async def mock_patch(record_id, fields):
        return False, "Airtable returned 500: Internal Server Error"

    monkeypatch.setattr("mcp_server.server.patch_sst_record", mock_patch)

    result = await handle_commit_sst_edits({"media_id": project_name})
    text = result[0].text

    assert "Error" in text
    assert "500" in text
    # Staged edits should still be preserved for retry
    manifest = json.loads((project_path / "manifest.json").read_text())
    assert "title" in manifest.get("proposed_edits", {})


# =============================================================================
# Task 6a: YAML-sourced WRITABLE_FIELDS limits
# =============================================================================


def test_writable_fields_char_limits_sources_from_yaml(monkeypatch):
    """_writable_fields_char_limits() should read title/short/long maxes from
    load_rules() -- proven by feeding it fake rule data distinguishable from
    both the real YAML and _FALLBACK_CHAR_LIMITS, not just by re-checking
    against the real config (which the fallback would also coincidentally
    match today).
    """
    import mcp_server.server as mcp_server_module

    class _FakeRules:
        def limits_for(self):
            return {
                "title": {"max": 42},
                "short_description": {"max": 43},
                "long_description": {"max": 44},
            }

    monkeypatch.setattr(mcp_server_module, "load_rules", lambda: _FakeRules())

    limits = mcp_server_module._writable_fields_char_limits()
    assert limits["title"] == 42
    assert limits["short_description"] == 43
    assert limits["long_description"] == 44
    # Fields with no limits.fields max entry keep their fallback (None).
    assert limits["keywords"] is None
    assert limits["hashtags"] is None


def test_writable_fields_char_limits_falls_back_on_yaml_failure(monkeypatch):
    """A load_rules() failure (missing/bad YAML) must fall back to the exact
    hardcoded _FALLBACK_CHAR_LIMITS, not raise -- the SST write path must
    never break because a config file is bad.
    """
    import mcp_server.server as mcp_server_module

    def _raise():
        raise RuntimeError("YAML exploded")

    monkeypatch.setattr(mcp_server_module, "load_rules", _raise)

    limits = mcp_server_module._writable_fields_char_limits()
    assert limits == mcp_server_module._FALLBACK_CHAR_LIMITS
    assert limits is not mcp_server_module._FALLBACK_CHAR_LIMITS  # a copy, not the shared dict


# =============================================================================
# Task 6a: review_proposed_edits inline style check (informational, fail-open)
# =============================================================================


@pytest.mark.asyncio
async def test_review_proposed_edits_shows_clean_style_check(project_with_manifest, monkeypatch):
    """A proposed value with no violations should show 'clean'."""
    from mcp_server.server import handle_propose_sst_edit, handle_review_proposed_edits

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "short_description": "Old short"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "short_description",
            "proposed_value": "A perfectly ordinary short description.",
            "reason": "Clarity",
        }
    )

    result = await handle_review_proposed_edits({"media_id": project_name})
    text = result[0].text
    assert "**Style check:** clean" in text


@pytest.mark.asyncio
async def test_review_proposed_edits_flags_forbidden_phrase(project_with_manifest, monkeypatch):
    """A proposed value containing a forbidden viewer-directive phrase
    ("discover") should surface its rule id in the Style check line. The rule
    id derives from the entry's `id` in house_style.yaml (voice.forbidden.discover),
    not the shared category, so same-category phrases stay distinguishable.
    """
    from mcp_server.server import handle_propose_sst_edit, handle_review_proposed_edits

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "short_description": "Old short"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "short_description",
            "proposed_value": "Discover the Wisconsin River with local paddlers.",
            "reason": "Engagement",
        }
    )

    result = await handle_review_proposed_edits({"media_id": project_name})
    text = result[0].text
    assert "Style check:" in text
    assert "voice.forbidden.discover" in text


@pytest.mark.asyncio
async def test_review_proposed_edits_suggests_title_casing(project_with_manifest, monkeypatch):
    """An all-caps title should get a 'suggested casing' note comparing
    against the down-styled/canonical form.
    """
    from mcp_server.server import handle_propose_sst_edit, handle_review_proposed_edits

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "WISCONSIN LIFE UPDATE",
            "reason": "Testing casing",
        }
    )

    result = await handle_review_proposed_edits({"media_id": project_name})
    text = result[0].text
    assert "suggested casing:" in text
    assert "Wisconsin Life update" in text


@pytest.mark.asyncio
async def test_review_proposed_edits_style_check_fails_open(project_with_manifest, monkeypatch):
    """If the style engine blows up (bad/missing YAML), the preview must
    still render exactly as it did before this feature, plus one
    explanatory note -- never blocking or altering propose/review/commit.
    """
    from mcp_server.server import handle_propose_sst_edit, handle_review_proposed_edits

    async def mock_search(media_id):
        return {"record_id": "recTEST123", "title": "Old Title"}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    project_name, _ = project_with_manifest
    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "SEO",
        }
    )

    def _raise(*args, **kwargs):
        raise RuntimeError("style engine exploded")

    monkeypatch.setattr("mcp_server.server._build_style_notes", _raise)

    result = await handle_review_proposed_edits({"media_id": project_name})
    text = result[0].text

    # Preview still renders the diff normally.
    assert "Old Title" in text
    assert "New Title" in text
    # No per-edit style section, but the fail-open note is present.
    assert "**Style check:**" not in text
    assert "Style check unavailable" in text


# =============================================================================
# Task 6a: commit_sst_edits editor_correction capture (fail-open)
# =============================================================================


def _make_editor_correction_project(output_dir, project_name: str):
    """Build a minimal project folder for the editor_correction tests below.

    Uses a project name UNIQUE to each test (rather than the shared
    `project_with_manifest` fixture's fixed "2WLITestProjectSM") because
    these tests query the session-scoped shared test DB (see
    tests/conftest.py's `_init_test_db`) for editor_correction rows by
    media_id -- reusing a fixed name across tests in the same pytest
    session would make earlier tests' committed rows indistinguishable from
    the row under test.
    """
    project_path = output_dir / project_name
    project_path.mkdir()
    manifest = {
        "project_name": project_name,
        "phases": [
            {"name": "analyst", "status": "completed"},
            {"name": "formatter", "status": "completed"},
            {"name": "seo", "status": "completed"},
        ],
        "outputs": {
            "analysis": "analyst_output.md",
            "formatted_transcript": "formatter_output.md",
            "seo_metadata": "seo_output.md",
        },
        "revisions": [],
        "keyword_reports": [],
    }
    (project_path / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (project_path / "seo_output.md").write_text("# SEO\nTest SEO content.")
    return project_path


async def _commit_with_mocks(monkeypatch, project_name, *, sst_snapshot, patch_result=None):
    """Shared plumbing for the editor_correction tests below: stages a
    title edit and mocks the Airtable round-trip so commit succeeds.
    """
    from mcp_server.server import handle_commit_sst_edits, handle_propose_sst_edit

    async def mock_search(media_id):
        return {"record_id": "recTEST123", **sst_snapshot}

    monkeypatch.setattr("mcp_server.server.search_sst_by_media_id", mock_search)

    await handle_propose_sst_edit(
        {
            "media_id": project_name,
            "field": "title",
            "proposed_value": "New Title",
            "reason": "SEO",
        }
    )

    async def mock_fetch(record_id):
        return {"record_id": "recTEST123", **sst_snapshot}

    monkeypatch.setattr("mcp_server.server.fetch_sst_context", mock_fetch)

    async def mock_patch(record_id, fields):
        return patch_result or (True, {"id": record_id, "fields": fields})

    monkeypatch.setattr("mcp_server.server.patch_sst_record", mock_patch)

    async def mock_comment(record_id, text):
        return True

    monkeypatch.setattr("mcp_server.server.post_sst_comment", mock_comment)

    return await handle_commit_sst_edits({"media_id": project_name})


@pytest.mark.asyncio
async def test_commit_sst_edits_logs_editor_correction_event(output_dir, monkeypatch):
    """A successful commit should log one editor_correction event per
    committed field, with committed_value + original_value populated.
    Direct DB access -- see mcp_server.server._log_editor_corrections'
    docstring for why this reuses api.services.database directly.
    """
    from sqlalchemy import select

    from api.services.database import get_session, session_stats_table

    project_name = "2WLIEditorCorrectionBasic"
    _make_editor_correction_project(output_dir, project_name)
    result = await _commit_with_mocks(monkeypatch, project_name, sst_snapshot={"title": "Old Title"})
    assert "✅" in result[0].text

    async with get_session() as session:
        rows = (
            await session.execute(
                select(session_stats_table).where(session_stats_table.c.event_type == "editor_correction")
            )
        ).fetchall()

    matching = [json.loads(row.data) for row in rows if json.loads(row.data)["extra"].get("media_id") == project_name]
    assert len(matching) == 1
    extra = matching[0]["extra"]
    assert extra["field"] == "title"
    assert extra["committed_value"] == "New Title"
    assert extra["original_value"] == "Old Title"
    # The fixture's seo_output.md has no structured "### Title" section, so
    # pipeline_value is not cleanly recoverable here -- documented v1 gap.
    assert extra["pipeline_value"] is None


@pytest.mark.asyncio
async def test_commit_sst_edits_recovers_pipeline_value_from_seo_output(output_dir, monkeypatch):
    """When seo_output.md HAS a structured '### Title' / '**Recommended:**'
    section, pipeline_value should be recovered from it.
    """
    from sqlalchemy import select

    from api.services.database import get_session, session_stats_table

    project_name = "2WLIEditorCorrectionSeo"
    project_path = _make_editor_correction_project(output_dir, project_name)
    (project_path / "seo_output.md").write_text(
        "### Title\n**Recommended:**\nWisconsin Life | Pipeline Recommended Title\n"
    )

    result = await _commit_with_mocks(monkeypatch, project_name, sst_snapshot={"title": "Old Title"})
    assert "✅" in result[0].text

    async with get_session() as session:
        rows = (
            await session.execute(
                select(session_stats_table).where(session_stats_table.c.event_type == "editor_correction")
            )
        ).fetchall()

    matching = [json.loads(row.data) for row in rows if json.loads(row.data)["extra"].get("media_id") == project_name]
    assert len(matching) == 1
    assert matching[0]["extra"]["pipeline_value"] == "Wisconsin Life | Pipeline Recommended Title"


@pytest.mark.asyncio
async def test_commit_sst_edits_succeeds_when_event_logging_fails(output_dir, monkeypatch):
    """Event-logging failure must never fail (or even blemish) the commit --
    the Airtable write has already happened by the time this runs.
    """
    project_name = "2WLIEditorCorrectionFailOpen"
    _make_editor_correction_project(output_dir, project_name)

    async def _raise_init_db():
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr("api.services.database.init_db", _raise_init_db)

    result = await _commit_with_mocks(monkeypatch, project_name, sst_snapshot={"title": "Old Title"})
    text = result[0].text

    assert "✅" in text
    assert "New Title" in text
    assert "Error" not in text
