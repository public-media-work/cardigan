"""Tests for the SST context path: field names, batch lookup errors, the
media_id fallback, and validator SST context.

Covers the fixes for #382, #331 and #373, found during the 2MSY editorial
run (evidence on #328):

- #382: ``batch_search_sst_by_media_ids`` requested a ``Title`` field that
  does not exist on the SST table, so Airtable 422'd every request and the
  handler swallowed the error into ``{}`` — indistinguishable from "no
  match". It had presumably never returned a record.
- #331: ``_fetch_sst_context`` returned ``None`` immediately when a job had
  no ``airtable_record_id``, never falling back to
  ``search_sst_by_media_id`` — six 2MSY jobs ran with no SST context.
- #373: ``_build_phase_prompt``'s validator branch got no ``sst_section``,
  so the validator could neither catch factual errors (job 32 shipped
  "Milwaukee" Symphony against an SST that says "Madison") nor recognize
  SST-sourced facts as grounded.

The schema-contract tests pin every field name the code requests from the
SST table against a snapshot of the live schema
(tests/fixtures/sst_schema_fields.json). Regenerate the snapshot from the
Airtable metadata API (GET /v0/meta/bases/{base_id}/tables) if the schema
drifts; set CARDIGAN_LIVE_AIRTABLE_TESTS=1 to verify against the live
schema directly.
"""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from api.services.airtable import AirtableClient
from api.services.worker import (
    SST_CONTEXT_AIRTABLE_FIELDS,
    SST_CONTEXT_FIELD_MAP,
    SST_PROJECT_FIELD_MAP,
    JobWorker,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sst_schema_fields.json"


def _snapshot_field_names() -> set[str]:
    return set(json.loads(FIXTURE.read_text())["field_names"])


def _snapshot_projects_field_names() -> set[str]:
    return set(json.loads(FIXTURE.read_text())["projects_field_names"])


# ---------------------------------------------------------------------------
# Schema contract (#382 root cause: a requested field name that doesn't exist)
# ---------------------------------------------------------------------------


def test_batch_search_field_names_exist_in_schema():
    """Every field the batch lookup requests must exist on the SST table.

    Requesting a nonexistent field makes Airtable reject the entire request
    with 422 — this is exactly how #382 made the lookup return empty forever.
    """
    missing = set(AirtableClient.SST_BATCH_FIELDS) - _snapshot_field_names()
    assert not missing, f"batch lookup requests fields not on the SST table: {sorted(missing)}"


def test_sst_context_field_names_exist_in_schema():
    """Every field _fetch_sst_context reads must exist on the SST table.

    A ``fields.get()`` on a name that isn't real returns None silently, so
    the context entry simply never appears (the old mapping asked for
    'Title', 'Program', 'Keywords' and 'Tags' — none exist).
    """
    missing = set(SST_CONTEXT_AIRTABLE_FIELDS) - _snapshot_field_names()
    assert not missing, f"_fetch_sst_context reads fields not on the SST table: {sorted(missing)}"


def test_project_field_names_exist_in_schema():
    """Every field read off the linked Project record must exist there too.

    Same defect class as #382, one table over — program identity comes from
    the Projects table's 'Project Name' primary field.
    """
    missing = set(SST_PROJECT_FIELD_MAP.values()) - _snapshot_projects_field_names()
    assert not missing, f"_fetch_sst_context reads fields not on the Projects table: {sorted(missing)}"


@pytest.mark.skipif(
    os.environ.get("CARDIGAN_LIVE_AIRTABLE_TESTS") != "1",
    reason="live Airtable schema check; set CARDIGAN_LIVE_AIRTABLE_TESTS=1 to run",
)
def test_requested_field_names_exist_in_live_schema():
    """Verify the snapshot (and thus the code) against the live schema."""
    from api.services.secrets import get_secret

    key = get_secret("AIRTABLE_API_KEY")
    assert key, "AIRTABLE_API_KEY required for live schema test"
    resp = httpx.get(
        "https://api.airtable.com/v0/meta/bases/appZ2HGwhiifQToB6/tables",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    resp.raise_for_status()
    tables = {t["id"]: t for t in resp.json()["tables"]}
    live_sst = {f["name"] for f in tables[AirtableClient.TABLE_ID]["fields"]}
    live_projects = {f["name"] for f in tables[AirtableClient.PROJECTS_TABLE_ID]["fields"]}

    requested = set(AirtableClient.SST_BATCH_FIELDS) | set(SST_CONTEXT_AIRTABLE_FIELDS)
    missing = requested - live_sst
    assert not missing, f"code requests fields not on the live SST table: {sorted(missing)}"

    missing_projects = set(SST_PROJECT_FIELD_MAP.values()) - live_projects
    assert not missing_projects, f"code requests fields not on the live Projects table: {sorted(missing_projects)}"


# ---------------------------------------------------------------------------
# batch_search_sst_by_media_ids error handling (#382)
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """Minimal async context manager standing in for httpx.AsyncClient."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.requests.append({"url": url, "params": params})
        return self._response


def _client() -> AirtableClient:
    return AirtableClient(api_key="pat-test-not-real")


def test_batch_search_raises_on_422():
    """A 422 (e.g. bad field name) must raise, not degrade to an empty dict.

    The old handler logged the error and returned {}, which every caller
    read as "no matching records" — a total failure disguised as a miss.
    """
    request = httpx.Request("GET", "https://api.airtable.com/v0/x/y")
    response = httpx.Response(422, json={"error": {"type": "UNKNOWN_FIELD_NAME"}}, request=request)
    fake = _FakeAsyncClient(response)

    with patch("api.services.airtable.httpx.AsyncClient", return_value=fake):
        with pytest.raises(httpx.HTTPStatusError):
            import asyncio

            asyncio.run(_client().batch_search_sst_by_media_ids(["2MSY0000HD"]))


def test_batch_search_returns_matches():
    """Happy path: records come back keyed by media_id."""
    request = httpx.Request("GET", "https://api.airtable.com/v0/x/y")
    response = httpx.Response(
        200,
        json={
            "records": [
                {
                    "id": "recKOHEyjglPEvf0a",
                    "fields": {"Media ID": "2MSY0000HD", "Release Title": "Madison Symphony at 100"},
                }
            ]
        },
        request=request,
    )
    fake = _FakeAsyncClient(response)

    with patch("api.services.airtable.httpx.AsyncClient", return_value=fake):
        import asyncio

        results = asyncio.run(_client().batch_search_sst_by_media_ids(["2MSY0000HD"]))

    assert results["2MSY0000HD"]["id"] == "recKOHEyjglPEvf0a"
    # The request must ask for the class-level field list (schema-checked above)
    assert fake.requests[0]["params"]["fields[]"] == list(AirtableClient.SST_BATCH_FIELDS)


# ---------------------------------------------------------------------------
# _fetch_sst_context media_id fallback (#331)
# ---------------------------------------------------------------------------

_SST_RECORD = {
    "id": "recKOHEyjglPEvf0a",
    "fields": {
        "Media ID": "2MSY0000HD",
        "Release Title": "Madison Symphony at 100",
        "Short Description": "A century of the MSO.",
        "General Keywords/Tags": "Madison Symphony Orchestra, classical music",
        "Project": ["recPROJECT0000001"],
    },
}

_PROJECT_RECORD = {
    "id": "recPROJECT0000001",
    "fields": {
        "Project Name": "Madison Symphony at 100",
        "Notes": "Finale event of MSO's 100th anniversary season.",
    },
}


def _worker() -> JobWorker:
    return JobWorker.__new__(JobWorker)


def _mock_airtable(search_result=None, get_result=None):
    client = MagicMock()
    client.search_sst_by_media_id = AsyncMock(return_value=search_result)
    client.get_sst_record = AsyncMock(return_value=get_result)
    client.get_project_record = AsyncMock(return_value=_PROJECT_RECORD)
    return client


@pytest.mark.asyncio
async def test_fetch_sst_context_falls_back_to_media_id_search():
    """No airtable_record_id + a media_id must resolve via search, not bail.

    This is the #331 core: six 2MSY jobs had a media_id whose SST record
    existed, and every one ran with no context because the fallback was
    missing.
    """
    client = _mock_airtable(search_result=_SST_RECORD)
    with patch("api.services.worker.get_airtable_client", return_value=client):
        ctx = await _worker()._fetch_sst_context({"id": 32, "media_id": "2MSY0000HD"})

    assert ctx is not None
    assert ctx["title"] == "Madison Symphony at 100"
    client.search_sst_by_media_id.assert_awaited_once_with("2MSY0000HD")
    client.get_sst_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_sst_context_prefers_record_id_when_present():
    client = _mock_airtable(get_result=_SST_RECORD)
    with patch("api.services.worker.get_airtable_client", return_value=client):
        ctx = await _worker()._fetch_sst_context(
            {"id": 1, "media_id": "2MSY0000HD", "airtable_record_id": "recKOHEyjglPEvf0a"}
        )

    assert ctx is not None
    client.get_sst_record.assert_awaited_once_with("recKOHEyjglPEvf0a")
    client.search_sst_by_media_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_sst_context_returns_none_without_any_identifier():
    client = _mock_airtable()
    with patch("api.services.worker.get_airtable_client", return_value=client):
        ctx = await _worker()._fetch_sst_context({"id": 2})

    assert ctx is None
    client.search_sst_by_media_id.assert_not_awaited()
    client.get_sst_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_sst_context_maps_real_field_names():
    """The context must be built from fields that exist on the SST table.

    The old mapping read 'Title', 'Program', 'Keywords' and 'Tags' — none of
    which exist — so title/program/keywords were silently absent from every
    prompt even when the record was fetched.
    """
    client = _mock_airtable(search_result=_SST_RECORD)
    with patch("api.services.worker.get_airtable_client", return_value=client):
        ctx = await _worker()._fetch_sst_context({"id": 32, "media_id": "2MSY0000HD"})

    assert ctx["title"] == "Madison Symphony at 100"
    assert ctx["keywords"] == "Madison Symphony Orchestra, classical music"
    # Program identity lives on the linked Project record (SST has no
    # 'Program' field), keyed off its primary field.
    assert ctx["program"] == "Madison Symphony at 100"
    assert ctx["project_notes"] == "Finale event of MSO's 100th anniversary season."
    # Every schema-pinned mapping key must round-trip into the context when
    # the record carries a value for it — this catches an inline literal
    # drifting away from SST_CONTEXT_FIELD_MAP.
    for key, field_name in SST_CONTEXT_FIELD_MAP.items():
        if field_name in _SST_RECORD["fields"]:
            assert ctx[key] == _SST_RECORD["fields"][field_name], key


@pytest.mark.asyncio
async def test_fetch_sst_context_title_falls_back_to_batch_episode():
    """Pre-release records have no Release Title; Batch-Episode fills in."""
    record = {
        "id": "recWORKINGTITLE01",
        "fields": {"Media ID": "2MSY0000HD", "Batch-Episode": "MSO Centennial (working)"},
    }
    client = _mock_airtable(search_result=record)
    with patch("api.services.worker.get_airtable_client", return_value=client):
        ctx = await _worker()._fetch_sst_context({"id": 40, "media_id": "2MSY0000HD"})

    assert ctx["title"] == "MSO Centennial (working)"


# ---------------------------------------------------------------------------
# Validator gets SST context (#373)
# ---------------------------------------------------------------------------


def test_validator_prompt_includes_sst_section():
    """The validator branch must receive the same SST section other phases do.

    Without it the validator passed job 32's "Milwaukee Symphony" output
    clean against an SST that says Madison, and flagged SST-sourced facts
    as fabrications (#373).
    """
    worker = _worker()
    worker.llm = MagicMock()
    worker.llm.config = {}

    context = {
        "analyst_output": "analysis",
        "formatter_output": "formatted",
        "seo_output": "seo",
        "sst_context": {"title": "Madison Symphony at 100", "program": "Madison Symphony at 100"},
    }
    prompt = worker._build_phase_prompt("validator", context)
    assert "Single Source of Truth (SST) Context" in prompt
    assert "Madison Symphony at 100" in prompt


def test_validator_prompt_without_sst_context_unchanged():
    worker = _worker()
    worker.llm = MagicMock()
    worker.llm.config = {}

    context = {"analyst_output": "a", "formatter_output": "f", "seo_output": "s"}
    prompt = worker._build_phase_prompt("validator", context)
    assert "Single Source of Truth" not in prompt


def test_validator_checklist_has_factual_consistency_item():
    """The checklist must carry a factual-consistency item scoped to SST.

    Removing #369's false positives is only safe if the validator gains a
    real check for the factual error those false positives were
    accidentally catching.
    """
    import yaml

    config = yaml.safe_load((Path(__file__).parent.parent / "config" / "house_style.yaml").read_text())
    checklist = config["prompt_blocks"]["validator.checklist"]

    for profile in ("full", "slim"):
        text = checklist[profile].lower()
        assert "sst" in text, f"validator.checklist.{profile} never mentions SST context"
        assert (
            "factual" in text or "consistent" in text
        ), f"validator.checklist.{profile} has no factual-consistency item"
