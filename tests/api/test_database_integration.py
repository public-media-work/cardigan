"""Integration tests to verify exit criteria for database service."""

import os
import tempfile

import pytest
import pytest_asyncio

from api.models.job import JobCreate, JobStatus, JobUpdate
from api.services.database import (
    close_db,
    create_job,
    get_job,
    init_db,
    update_job,
)


@pytest_asyncio.fixture
async def integration_db():
    """Create a temporary test database for integration tests."""
    import api.services.database as db_mod

    # Save original engine state so we can restore after this test
    orig_engine = db_mod._engine
    orig_factory = db_mod._async_session_factory
    orig_db_path = os.environ.get("DATABASE_PATH")

    # Reset globals so init_db() creates a fresh engine
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

    # Restore original engine
    db_mod._engine = orig_engine
    db_mod._async_session_factory = orig_factory
    if orig_db_path is not None:
        os.environ["DATABASE_PATH"] = orig_db_path

    try:
        os.unlink(db_path)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_exit_criteria_thread_safe_connections(integration_db):
    """Verify thread-safe connections work (exit criteria 1)."""
    import asyncio

    async def create_test_job(i):
        job = await create_job(
            JobCreate(
                project_path=f"/projects/test{i}",
                project_name=f"test{i}",
                transcript_file=f"/transcripts/test{i}.txt",
                priority=i,
            )
        )
        return job

    # Create 20 jobs concurrently to test thread safety
    jobs = await asyncio.gather(*[create_test_job(i) for i in range(20)])

    # All jobs should be created successfully
    assert len(jobs) == 20
    assert all(job.id is not None for job in jobs)
    assert all(job.status == JobStatus.pending for job in jobs)

    print("PASS: Thread-safe connections work correctly")


@pytest.mark.asyncio
async def test_exit_criteria_basic_crud(integration_db):
    """Verify basic job CRUD operations work (exit criteria 2)."""
    # Create
    job = await create_job(
        JobCreate(
            project_path="/projects/test",
            project_name="test",
            transcript_file="/transcripts/test.txt",
            priority=5,
        )
    )
    assert job.id is not None
    assert job.status == JobStatus.pending
    print(f"CREATE: Job {job.id} created successfully")

    # Read
    retrieved = await get_job(job.id)
    assert retrieved is not None
    assert retrieved.id == job.id
    assert retrieved.project_path == "/projects/test"
    print(f"READ: Job {job.id} retrieved successfully")

    # Update
    updated = await update_job(
        job.id,
        JobUpdate(
            status=JobStatus.in_progress,
            current_phase="analyst",
        ),
    )
    assert updated.status == JobStatus.in_progress
    assert updated.current_phase == "analyst"
    assert updated.started_at is not None
    print(f"UPDATE: Job {job.id} updated successfully")

    print("PASS: Basic job CRUD operations work correctly")


@pytest.mark.asyncio
async def test_exit_criteria_create_and_retrieve(integration_db):
    """Verify can create and retrieve jobs (exit criteria 3)."""
    # Create multiple jobs
    job1 = await create_job(
        JobCreate(
            project_path="/projects/episode1",
            project_name="episode1",
            transcript_file="/transcripts/ep1.txt",
            priority=10,
        )
    )

    job2 = await create_job(
        JobCreate(
            project_path="/projects/episode2",
            project_name="episode2",
            transcript_file="/transcripts/ep2.txt",
            priority=5,
        )
    )

    job3 = await create_job(
        JobCreate(
            project_path="/projects/episode3",
            project_name="episode3",
            transcript_file="/transcripts/ep3.txt",
            priority=1,
        )
    )

    # Retrieve all jobs
    retrieved1 = await get_job(job1.id)
    retrieved2 = await get_job(job2.id)
    retrieved3 = await get_job(job3.id)

    # Verify all retrieved correctly
    assert retrieved1.project_path == "/projects/episode1"
    assert retrieved2.project_path == "/projects/episode2"
    assert retrieved3.project_path == "/projects/episode3"

    assert retrieved1.priority == 10
    assert retrieved2.priority == 5
    assert retrieved3.priority == 1

    # Verify defaults are set correctly
    assert all(job.status == JobStatus.pending for job in [retrieved1, retrieved2, retrieved3])
    assert all(
        job.agent_phases == ["analyst", "formatter", "seo", "validator"] for job in [retrieved1, retrieved2, retrieved3]
    )
    assert all(job.retry_count == 0 for job in [retrieved1, retrieved2, retrieved3])
    assert all(job.max_retries == 3 for job in [retrieved1, retrieved2, retrieved3])

    print(f"PASS: Created and retrieved {len([job1, job2, job3])} jobs successfully")
    print("All exit criteria met!")


@pytest.mark.asyncio
async def test_started_at_survives_later_in_progress_updates(integration_db):
    """The worker re-sends in_progress at every phase; started_at must keep the first stamp.

    On cardigan02, job 27's started_at landed after its analyst phase had
    finished because each phase's update overwrote it.
    """
    job = await create_job(JobCreate(project_name="t", transcript_file="t.srt"))

    first = await update_job(job.id, JobUpdate(status=JobStatus.in_progress, current_phase="analyst"))
    later = await update_job(job.id, JobUpdate(status=JobStatus.in_progress, current_phase="formatter"))

    assert first.started_at is not None
    assert later.started_at == first.started_at
    assert later.current_phase == "formatter"
