"""Persistence and API tests for job items (#329)."""

import os
import tempfile

import pytest
import pytest_asyncio

from api.models.item import ItemStatus, ItemUpdate
from api.models.job import JobCreate
from api.services.database import (
    close_db,
    create_job,
    delete_job,
    delete_job_items,
    get_job_item,
    get_job_items,
    init_db,
    update_job_item,
    upsert_job_items,
)
from api.services.items import ITEM_SPECS, LARGE_VALUE_CHARS, build_items


@pytest_asyncio.fixture
async def test_db():
    """Temporary database with the full schema."""
    import api.services.database as db_mod

    orig_engine = db_mod._engine
    orig_factory = db_mod._async_session_factory
    orig_db_path = os.environ.get("DATABASE_PATH")

    db_mod._engine = None
    db_mod._async_session_factory = None

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = db_path

    await init_db()

    from api.services.database import _engine, metadata

    async with _engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    yield db_path

    await close_db()
    db_mod._engine = orig_engine
    db_mod._async_session_factory = orig_factory
    if orig_db_path is not None:
        os.environ["DATABASE_PATH"] = orig_db_path
    try:
        os.unlink(db_path)
    except Exception:
        pass


@pytest_asyncio.fixture
async def job(test_db):
    """A job to hang items off."""
    return await create_job(
        JobCreate(project_name="6POL0111", transcript_file="6POL0111.txt"),
    )


SEO_MD = """# SEO Report

### Title (Final Recommendation)

**Recommended:**
Polling the failed surplus deal | Inside Wisconsin Politics

### Short Description (90 chars max)

**Recommended:**
Zac Schultz breaks down the collapsed surplus negotiation.
"""


@pytest.mark.asyncio
async def test_upsert_then_read_back(job):
    rows = build_items(job_id=job.id, seo_md=SEO_MD)

    written = await upsert_job_items(job.id, rows)
    items = await get_job_items(job.id)

    assert written == len(ITEM_SPECS)
    assert len(items) == len(ITEM_SPECS)
    title = next(item for item in items if item.key == "title")
    assert title.proposed_value.startswith("Polling the failed surplus deal")
    assert title.airtable_field == "Release Title"
    assert title.char_limit == 80


@pytest.mark.asyncio
async def test_items_are_returned_in_registry_order(job):
    await upsert_job_items(job.id, build_items(job_id=job.id, seo_md=SEO_MD))

    keys = [item.key for item in await get_job_items(job.id)]

    assert keys == [spec.key for spec in ITEM_SPECS]


@pytest.mark.asyncio
async def test_upsert_is_idempotent(job):
    rows = build_items(job_id=job.id, seo_md=SEO_MD)

    await upsert_job_items(job.id, rows)
    await upsert_job_items(job.id, rows)

    assert len(await get_job_items(job.id)) == len(ITEM_SPECS)


@pytest.mark.asyncio
async def test_reextraction_preserves_an_editor_verdict(job):
    """A phase retry must not silently un-approve work the editor signed off."""
    await upsert_job_items(job.id, build_items(job_id=job.id, seo_md=SEO_MD))
    await update_job_item(job.id, "title", ItemUpdate(status=ItemStatus.approved))

    # A later phase run proposes something new for the same key.
    revised = build_items(job_id=job.id, seo_md=SEO_MD.replace("Polling", "Revised"))
    await upsert_job_items(job.id, revised)

    title = await get_job_item(job.id, "title")
    assert title.status == ItemStatus.approved
    assert title.proposed_value.startswith("Revised")


@pytest.mark.asyncio
async def test_kickback_records_note_and_timestamp(job):
    await upsert_job_items(job.id, build_items(job_id=job.id, seo_md=SEO_MD))

    item = await update_job_item(
        job.id,
        "title",
        ItemUpdate(status=ItemStatus.kicked_back, kickback_note="too clickbaity"),
    )

    assert item.status == ItemStatus.kicked_back
    assert item.kickback_note == "too clickbaity"
    assert item.kicked_back_at is not None


@pytest.mark.asyncio
async def test_in_place_edit_resources_the_item_to_human(job):
    await upsert_job_items(job.id, build_items(job_id=job.id, seo_md=SEO_MD))

    item = await update_job_item(job.id, "title", ItemUpdate(proposed_value="A human wrote this"))

    assert item.proposed_value == "A human wrote this"
    assert item.source.value == "human"


@pytest.mark.asyncio
async def test_large_values_are_omitted_unless_requested(job):
    body = "x" * (LARGE_VALUE_CHARS + 1)
    await upsert_job_items(job.id, build_items(job_id=job.id, formatter_md=body))

    omitted = await get_job_items(job.id, include_values=False)
    hydrated = await get_job_items(job.id, include_values=True)

    transcript_omitted = next(i for i in omitted if i.key == "formatted_transcript")
    transcript_full = next(i for i in hydrated if i.key == "formatted_transcript")
    assert transcript_omitted.proposed_value is None
    assert transcript_full.proposed_value == body
    # Small values survive the omission pass.
    assert next(i for i in omitted if i.key == "title") is not None


@pytest.mark.asyncio
async def test_update_unknown_item_returns_none(job):
    assert await update_job_item(job.id, "nope", ItemUpdate(status=ItemStatus.approved)) is None


@pytest.mark.asyncio
async def test_delete_job_items(job):
    await upsert_job_items(job.id, build_items(job_id=job.id, seo_md=SEO_MD))

    removed = await delete_job_items(job.id)

    assert removed == len(ITEM_SPECS)
    assert await get_job_items(job.id) == []


@pytest.mark.asyncio
async def test_items_are_scoped_to_their_job(test_db):
    job_a = await create_job(JobCreate(project_name="a", transcript_file="a.txt"))
    job_b = await create_job(JobCreate(project_name="b", transcript_file="b.txt"))

    await upsert_job_items(job_a.id, build_items(job_id=job_a.id, seo_md=SEO_MD))

    assert len(await get_job_items(job_a.id)) == len(ITEM_SPECS)
    assert await get_job_items(job_b.id) == []

    await delete_job(job_b.id)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoints_round_trip(job):
    """GET the list, GET one, PATCH a verdict — over real HTTP."""
    from fastapi.testclient import TestClient

    from api.main import app

    await upsert_job_items(job.id, build_items(job_id=job.id, seo_md=SEO_MD))

    with TestClient(app) as client:
        listed = client.get(f"/api/jobs/{job.id}/items")
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["total"] == len(ITEM_SPECS)
        assert payload["job_id"] == job.id
        assert payload["values_included"] is False
        # 9 specs are blocked on an integration or on structured output, and
        # with no SST context the 4 deterministic dates have no Digital
        # Premiere to derive from, so they wait too.
        blocked_specs = sum(1 for spec in ITEM_SPECS if spec.is_blocked)
        assert payload["summary"]["by_status"]["awaiting_source"] == blocked_specs + 4
        # Nothing was fetched, so every item is honestly 'unknown'.
        assert all(item["current_state"] == "unknown" for item in payload["items"])

        single = client.get(f"/api/jobs/{job.id}/items/title")
        assert single.status_code == 200
        assert single.json()["proposed_value"].startswith("Polling the failed surplus deal")

        patched = client.patch(
            f"/api/jobs/{job.id}/items/title",
            json={"status": "approved"},
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "approved"

        filtered = client.get(f"/api/jobs/{job.id}/items", params={"category": "context"})
        assert filtered.status_code == 200
        assert {item["key"] for item in filtered.json()["items"]} == {
            "context.themes",
            "context.speakers",
            "context.place_names",
            "context.editorial_angles",
        }


@pytest.mark.asyncio
async def test_endpoints_404_cleanly(job):
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        assert client.get("/api/jobs/999999/items").status_code == 404
        assert client.get(f"/api/jobs/{job.id}/items/nope").status_code == 404
        assert client.patch(f"/api/jobs/{job.id}/items/nope", json={"status": "approved"}).status_code == 404
