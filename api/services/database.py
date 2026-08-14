"""Database service layer for Cardigan.

Provides async database operations using SQLAlchemy 2.0+ with aiosqlite.
Thread-safe connection pool and CRUD operations for jobs, events, and config.
"""

import glob
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    and_,
    delete,
    desc,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _default_app_version() -> str:
    """Derive the cost-epoch tag from the package version's major.minor.

    Examples:
        "4.1.0"               → "v4.1"
        "4.1.1.dev3+g8a1b2c3" → "v4.1"
        "0.0.0+unknown"       → "v0.0"  (build without git access; sentinel)

    The CARDIGAN_VERSION env var (set in docker-compose.yml) overrides
    this for short-lived experiments — useful when testing a model
    routing change without wanting the rows tagged with the next epoch.
    """
    from api import __version__  # local import to avoid circular at module load

    parts = __version__.split(".")[:2]
    if len(parts) < 2:
        parts = ["0", "0"]
    return f"v{'.'.join(parts)}"


# App version tag stamped on all rows produced by this process.
# Source of truth: git tag → setuptools_scm → api/__version__ → here.
# Override at runtime via CARDIGAN_VERSION env var (set in docker-compose.yml).
# See docs/VERSIONING.md for release/bump policy.
#
# Read at call time (not captured as a module constant) so tests can vary it with
# monkeypatch.setenv alone, without importlib.reload + manual engine save/restore (#119).
def get_app_version() -> str:
    """Return the cost-epoch tag for rows written by this process.

    CARDIGAN_VERSION env override, else derived from the package version.
    """
    return os.getenv("CARDIGAN_VERSION") or _default_app_version()


from api.models.config import ConfigItem, ConfigValueType
from api.models.events import EventCreate, EventData, EventType, SessionEvent
from api.models.item import Item, ItemFlag, ItemStatus, ItemUpdate
from api.models.job import Job, JobCreate, JobOutputs, JobPhase, JobStatus, JobUpdate, PhaseStatus


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime and enum objects."""

    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


# Global engine and session factory
_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None

# SQLAlchemy metadata and table definitions
metadata = MetaData()

# Define jobs table
jobs_table = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_path", Text, nullable=False),
    Column("transcript_file", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("queued_at", DateTime, server_default=func.current_timestamp()),
    Column("started_at", DateTime, nullable=True),
    Column("completed_at", DateTime, nullable=True),
    Column("estimated_cost", Float, server_default="0.0"),
    Column("actual_cost", Float, server_default="0.0"),
    Column("agent_phases", Text, server_default='["analyst", "formatter"]'),
    Column("current_phase", Text, nullable=True),
    Column("retry_count", Integer, server_default="0"),
    Column("max_retries", Integer, server_default="3"),
    Column("error_message", Text, nullable=True),
    Column("error_timestamp", DateTime, nullable=True),
    Column("auto_escalated_at", DateTime, nullable=True),
    Column("manifest_path", Text, nullable=True),
    Column("logs_path", Text, nullable=True),
    Column("last_heartbeat", DateTime, nullable=True),
    Column("phases", Text, nullable=True),  # JSON array of JobPhase objects
    Column("airtable_record_id", Text, nullable=True),
    Column("airtable_url", Text, nullable=True),
    Column("media_id", Text, nullable=True),
    Column("duration_minutes", Float, nullable=True),
    Column("word_count", Integer, nullable=True),
    Column("content_type", Text, nullable=True),  # 'full', 'short', or 'clip'
    Column("app_version", Text, nullable=True),
    Column("validation_result", Text, nullable=True),  # JSON validation result
    # Defer-and-requeue (busy local backend). Timestamps are ISO-8601 UTC
    # strings so claim_next_job's raw-SQL gate compares them lexicographically.
    Column("retry_after", Text, nullable=True),  # don't reclaim before this time
    Column("defer_count", Integer, nullable=False, server_default="0"),
    Column("first_deferred_at", Text, nullable=True),  # anchors the give-up ceiling
    # Audio upload mode (migration 022)
    Column("job_type", Text, nullable=False, server_default="transcript"),  # 'transcript' | 'media'
    Column("media_file", Text, nullable=True),  # extracted audio, relative to MEDIA_DIR
    Column("intake", Text, nullable=True),  # JSON: speakers, context_terms, language
)

# Define session_stats table
session_stats_table = Table(
    "session_stats",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=True),
    Column("timestamp", DateTime, server_default=func.current_timestamp()),
    Column("event_type", Text, nullable=False),
    Column("data", Text, nullable=True),
    Column("app_version", Text, nullable=True),
)

# Define config table
config_table = Table(
    "config",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("value_type", Text, server_default="string"),
    Column("description", Text, nullable=True),
    Column("updated_at", DateTime, server_default=func.current_timestamp()),
)


# Define available_files table for tracking remote ingest server files
# Schema from migrations 006 + 007
available_files_table = Table(
    "available_files",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("remote_url", Text, nullable=False),
    Column("filename", Text, nullable=False),
    Column("directory_path", Text, nullable=True),
    Column("file_type", Text, nullable=False),
    Column("media_id", Text, nullable=True),
    Column("file_size_bytes", Integer, nullable=True),
    Column("remote_modified_at", DateTime, nullable=True),
    Column("first_seen_at", DateTime, server_default=func.current_timestamp()),
    Column("last_seen_at", DateTime, server_default=func.current_timestamp()),
    Column("status", Text, nullable=False, server_default="new"),
    Column("status_changed_at", DateTime, nullable=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=True),
    Column("airtable_record_id", Text, nullable=True),
    Column("attached_at", DateTime, nullable=True),
    # Added in migration 007
    Column("local_path", Text, nullable=True),
    Column("downloaded_at", DateTime, nullable=True),
)

# Define screengrab_attachments audit log table (migration 006)
screengrab_attachments_table = Table(
    "screengrab_attachments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("available_file_id", Integer, ForeignKey("available_files.id"), nullable=True),
    Column("sst_record_id", Text, nullable=False),
    Column("media_id", Text, nullable=False),
    Column("filename", Text, nullable=False),
    Column("remote_url", Text, nullable=False),
    Column("attached_at", DateTime, server_default=func.current_timestamp()),
    Column("attachments_before", Integer, nullable=True),
    Column("attachments_after", Integer, nullable=True),
    Column("success", Boolean, server_default="1"),
    Column("error_message", Text, nullable=True),
)

# Define job_items table (migration 023) — the editor-facing primitive (#329).
# Items are authoritative once extracted; the *_output.md phase documents stay
# on disk as frozen provenance and are never rewritten by an edit or approval.
job_items_table = Table(
    "job_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("key", Text, nullable=False),
    Column("layer", Text, nullable=False),  # context | deliverable
    Column("category", Text, nullable=False),  # context | metadata | copy | transcript
    Column("source", Text, nullable=False),  # llm | deterministic | human
    Column("proposed_value", Text, nullable=True),
    Column("current_value", Text, nullable=True),
    Column("current_state", Text, nullable=False, server_default="unknown"),
    Column("current_fetched_at", DateTime, nullable=True),
    Column("status", Text, nullable=False, server_default="pending_review"),
    Column("source_blocked_on", Text, nullable=True),
    Column("flags", Text, nullable=True),  # JSON array of ItemFlag
    Column("kickback_note", Text, nullable=True),
    Column("kicked_back_at", DateTime, nullable=True),
    Column("phase", Text, nullable=True),
    Column("model", Text, nullable=True),
    Column("char_limit", Integer, nullable=True),
    Column("airtable_field", Text, nullable=True),
    Column("airtable_field_id", Text, nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    Column("updated_at", DateTime, nullable=False, server_default=func.current_timestamp()),
    UniqueConstraint("job_id", "key", name="uq_job_items_job_key"),
)


def get_db_url() -> str:
    """Return SQLite database URL from environment or default."""
    db_path = os.getenv("DATABASE_PATH", "./dashboard.db")
    return f"sqlite+aiosqlite:///{db_path}"


async def init_db() -> None:
    """Initialize database connection pool.

    Creates async engine and session factory.
    Should be called once at application startup.
    """
    global _engine, _async_session_factory

    if _engine is not None:
        # Already initialized
        return

    db_url = get_db_url()

    # Create async engine with connection pooling
    _engine = create_async_engine(
        db_url,
        echo=False,  # Set to True for SQL debug logging
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # Verify connections before use
        connect_args={"check_same_thread": False},  # SQLite specific
    )

    # Create session factory
    _async_session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Create tables if they don't exist (fresh database)
    async with _engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # Defense-in-depth: backfill columns added to the ORM schema that may be
        # absent on a pre-existing / hand-maintained SQLite DB. metadata.create_all
        # NEVER ALTERs an existing table, so a deploy whose DB predates a newly
        # added column would raise OperationalError on first write to it (#243:
        # auto_escalated_at). Idempotent — skips any column already present.
        await conn.run_sync(_ensure_jobs_columns)


# Columns added to the ``jobs`` ORM schema after the table's original creation.
# name -> SQLite column type (kept in sync with the Column() declarations above;
# SQLAlchemy's DateTime renders as DATETIME in the SQLite dialect).
_JOBS_BACKFILL_COLUMNS: dict[str, str] = {
    "auto_escalated_at": "DATETIME",
    "job_type": "TEXT NOT NULL DEFAULT 'transcript'",
    "media_file": "TEXT",
    "intake": "TEXT",
}


def _ensure_jobs_columns(sync_conn) -> None:
    """Add any missing ``jobs`` columns from ``_JOBS_BACKFILL_COLUMNS`` (idempotent).

    Safe on both fresh DBs (column already created by ``metadata.create_all`` ->
    skipped) and pre-existing DBs (column missing -> added via ALTER TABLE).
    """
    existing = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()}
    for column, ddl_type in _JOBS_BACKFILL_COLUMNS.items():
        if column not in existing:
            sync_conn.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {column} {ddl_type}")


async def close_db() -> None:
    """Close database connections and cleanup resources.

    Should be called at application shutdown.
    """
    global _engine, _async_session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None


@asynccontextmanager
async def get_session():
    """Get async database session context manager.

    Usage:
        async with get_session() as session:
            result = await session.execute(...)
    """
    if _async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    async with _async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


# ============================================================================
# Helper Functions for Path Sanitization
# ============================================================================


def sanitize_path_component(name: str) -> str:
    """Sanitize a string for safe use as a filesystem path component.

    Removes or replaces characters that are invalid in file paths across
    different operating systems (/, \\, :, *, ?, ", <>, |).

    Args:
        name: The string to sanitize (e.g., project name)

    Returns:
        Sanitized string safe for use as a path component

    Examples:
        >>> sanitize_path_component("My Project: Part 1")
        'My_Project_Part_1'
        >>> sanitize_path_component("test/file\\name")
        'test_file_name'
        >>> sanitize_path_component("project<2024>")
        'project_2024_'
    """
    if not name:
        return "unnamed"

    # Replace invalid characters with underscore
    sanitized = "".join(c if c.isalnum() or c in "-_. " else "_" for c in name)

    # Replace multiple consecutive underscores with single underscore
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")

    # Remove leading/trailing underscores and spaces
    sanitized = sanitized.strip("_ ")

    # Ensure result is not empty
    if not sanitized:
        return "unnamed"

    # Limit length to avoid filesystem issues (most systems support 255 chars)
    max_length = 200  # Conservative limit
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip("_ ")

    return sanitized


# ============================================================================
# Job CRUD Operations
# ============================================================================


async def create_job(job: JobCreate, app_version: Optional[str] = None) -> Job:
    """Create a new job in the database.

    Args:
        job: Job creation schema with required fields

    Returns:
        Complete Job record with generated ID and defaults
    """
    async with get_session() as session:
        # Initialize phases - automated pipeline phases (validator is QA, copy_editor is interactive)
        default_phases = ["analyst", "formatter", "seo", "validator"]
        # Media jobs get a transcription pre-stage; the job pauses in
        # awaiting_review after it, before the LLM phases run.
        if getattr(job, "job_type", "transcript") == "media":
            default_phases = ["transcription"] + default_phases
        initial_phases = [JobPhase(name=name, status=PhaseStatus.pending).model_dump() for name in default_phases]

        # Derive project_path from project_name if not provided
        project_path = job.project_path
        if project_path is None:
            # Sanitize project name for filesystem using helper function
            safe_name = sanitize_path_component(job.project_name)
            output_dir = os.getenv("OUTPUT_DIR", "OUTPUT")
            project_path = f"{output_dir}/{safe_name}"

        # Prepare values
        # Note: agent_phases is a legacy field, phases is the new structured format
        values = {
            "project_path": project_path,
            "transcript_file": job.transcript_file,
            "priority": job.priority or 0,
            "status": JobStatus.pending.value,
            "queued_at": datetime.now(timezone.utc),
            "estimated_cost": 0.0,
            "actual_cost": 0.0,
            "agent_phases": json.dumps(default_phases),  # Legacy field
            "phases": json.dumps(initial_phases),
            "retry_count": 0,
            "max_retries": 3,
            "app_version": app_version if app_version is not None else get_app_version(),
            "job_type": getattr(job, "job_type", "transcript") or "transcript",
            "media_file": getattr(job, "media_file", None),
            "intake": json.dumps(job.intake) if getattr(job, "intake", None) else None,
        }

        # Insert job
        stmt = jobs_table.insert().values(**values)
        result = await session.execute(stmt)
        job_id = result.inserted_primary_key[0]

        # Fetch and return complete job (within same session)
        stmt = select(jobs_table).where(jobs_table.c.id == job_id)
        result = await session.execute(stmt)
        row = result.fetchone()

        job = _row_to_job(row)

        # Broadcast job creation to WebSocket clients
        try:
            from api.routers.websocket import broadcast_job_update

            await broadcast_job_update(job, event_type="job_created")
        except Exception:
            # Don't fail job creation if broadcast fails
            pass

        return job


async def get_job(job_id: int) -> Optional[Job]:
    """Retrieve a job by ID.

    Args:
        job_id: Job ID to retrieve

    Returns:
        Job record or None if not found
    """
    async with get_session() as session:
        stmt = select(jobs_table).where(jobs_table.c.id == job_id)
        result = await session.execute(stmt)
        row = result.fetchone()

        if row is None:
            return None

        return _row_to_job(row)


async def find_jobs_by_transcript(
    transcript_file: str,
    exclude_cancelled: bool = True,
) -> List[Job]:
    """Find existing jobs for a transcript file.

    Used for duplicate detection - checks if a transcript has already been
    processed or is currently in queue.

    Args:
        transcript_file: The transcript filename to search for
        exclude_cancelled: Whether to exclude cancelled jobs (default: True)

    Returns:
        List of Job records matching the transcript file
    """
    async with get_session() as session:
        stmt = select(jobs_table).where(jobs_table.c.transcript_file == transcript_file)

        if exclude_cancelled:
            stmt = stmt.where(jobs_table.c.status != JobStatus.cancelled.value)

        # Order by newest first
        stmt = stmt.order_by(jobs_table.c.queued_at.desc())

        result = await session.execute(stmt)
        rows = result.fetchall()

        return [_row_to_job(row) for row in rows]


async def find_jobs_by_media_id(
    media_id: str,
    exclude_cancelled: bool = True,
) -> List[Job]:
    """Find existing jobs for a media ID.

    Used for duplicate detection - checks if content with this media ID
    has already been processed or is currently in queue.

    Args:
        media_id: The media ID to search for (e.g., "2WLI1209HD")
        exclude_cancelled: Whether to exclude cancelled jobs (default: True)

    Returns:
        List of Job records matching the media ID
    """
    async with get_session() as session:
        stmt = select(jobs_table).where(jobs_table.c.media_id == media_id)

        if exclude_cancelled:
            stmt = stmt.where(jobs_table.c.status != JobStatus.cancelled.value)

        # Order by newest first
        stmt = stmt.order_by(jobs_table.c.queued_at.desc())

        result = await session.execute(stmt)
        rows = result.fetchall()

        return [_row_to_job(row) for row in rows]


async def list_jobs(
    status: Optional[JobStatus] = None,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    sort_order: str = "newest",
) -> List[Job]:
    """List jobs with optional filtering, search, and pagination.

    Args:
        status: Filter by job status (None = all statuses)
        limit: Maximum number of jobs to return
        offset: Number of jobs to skip
        search: Filter by transcript_file or project_path (case-insensitive contains)
        sort_order: "newest" (default) or "oldest" - by queued_at timestamp

    Returns:
        List of Job records
    """
    async with get_session() as session:
        stmt = select(jobs_table)

        # Apply status filter
        if status is not None:
            stmt = stmt.where(jobs_table.c.status == status.value)

        # Apply search filter (case-insensitive)
        if search:
            search_pattern = f"%{search}%"
            stmt = stmt.where(
                (jobs_table.c.transcript_file.ilike(search_pattern)) | (jobs_table.c.project_path.ilike(search_pattern))
            )

        # Order by queued_at (newest or oldest first)
        if sort_order == "oldest":
            stmt = stmt.order_by(jobs_table.c.queued_at.asc(), jobs_table.c.id.asc())
        else:  # newest first (default)
            stmt = stmt.order_by(jobs_table.c.queued_at.desc(), jobs_table.c.id.desc())

        # Apply pagination
        stmt = stmt.limit(limit).offset(offset)

        result = await session.execute(stmt)
        rows = result.fetchall()

        return [_row_to_job(row) for row in rows]


async def count_jobs(
    status: Optional[JobStatus] = None,
    search: Optional[str] = None,
) -> int:
    """Count jobs matching filter criteria.

    Args:
        status: Filter by job status (None = all statuses)
        search: Filter by transcript_file or project_path

    Returns:
        Count of matching jobs
    """
    async with get_session() as session:
        stmt = select(func.count()).select_from(jobs_table)

        if status is not None:
            stmt = stmt.where(jobs_table.c.status == status.value)

        if search:
            search_pattern = f"%{search}%"
            stmt = stmt.where(
                (jobs_table.c.transcript_file.ilike(search_pattern)) | (jobs_table.c.project_path.ilike(search_pattern))
            )

        result = await session.execute(stmt)
        return result.scalar() or 0


async def update_job(job_id: int, job_update: JobUpdate) -> Optional[Job]:
    """Update a job with partial fields.

    Args:
        job_id: Job ID to update
        job_update: Partial update schema with optional fields

    Returns:
        Updated Job record or None if not found
    """
    async with get_session() as session:
        # Build update dict from non-None fields
        update_values = {}

        if job_update.status is not None:
            update_values["status"] = job_update.status.value

            # Auto-set timestamps based on status
            if job_update.status == JobStatus.in_progress and "started_at" not in update_values:
                update_values["started_at"] = datetime.now(timezone.utc)
            elif job_update.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
                if "completed_at" not in update_values:
                    update_values["completed_at"] = datetime.now(timezone.utc)

        if job_update.priority is not None:
            update_values["priority"] = job_update.priority

        if job_update.transcript_file is not None:
            update_values["transcript_file"] = job_update.transcript_file

        if job_update.project_name is not None:
            update_values["project_name"] = job_update.project_name

        if job_update.project_path is not None:
            update_values["project_path"] = job_update.project_path

        if job_update.current_phase is not None:
            update_values["current_phase"] = job_update.current_phase

        if job_update.error_message is not None:
            update_values["error_message"] = job_update.error_message
            # An empty error_message clears the error state (e.g. on job retry),
            # so the stale failure timestamp must be cleared too — not re-stamped.
            if job_update.error_message == "":
                update_values["error_timestamp"] = None
            else:
                update_values["error_timestamp"] = datetime.now(timezone.utc)

        if job_update.auto_escalated_at is not None:
            update_values["auto_escalated_at"] = job_update.auto_escalated_at

        if job_update.estimated_cost is not None:
            update_values["estimated_cost"] = job_update.estimated_cost

        if job_update.actual_cost is not None:
            update_values["actual_cost"] = job_update.actual_cost

        if job_update.manifest_path is not None:
            update_values["manifest_path"] = job_update.manifest_path

        if job_update.logs_path is not None:
            update_values["logs_path"] = job_update.logs_path

        if job_update.last_heartbeat is not None:
            update_values["last_heartbeat"] = job_update.last_heartbeat

        if job_update.airtable_record_id is not None:
            update_values["airtable_record_id"] = job_update.airtable_record_id

        if job_update.airtable_url is not None:
            update_values["airtable_url"] = job_update.airtable_url

        if job_update.media_id is not None:
            update_values["media_id"] = job_update.media_id

        if job_update.duration_minutes is not None:
            update_values["duration_minutes"] = job_update.duration_minutes

        if job_update.word_count is not None:
            update_values["word_count"] = job_update.word_count

        if job_update.content_type is not None:
            update_values["content_type"] = job_update.content_type

        if job_update.validation_result is not None:
            update_values["validation_result"] = json.dumps(job_update.validation_result)

        if job_update.media_file is not None:
            update_values["media_file"] = job_update.media_file

        if job_update.intake is not None:
            update_values["intake"] = json.dumps(job_update.intake)

        # Handle phases update (replaces all phases)
        if job_update.phases is not None:
            # Use mode='json' to serialize datetime objects to ISO strings
            phases_json = json.dumps([p.model_dump(mode="json") for p in job_update.phases])
            update_values["phases"] = phases_json

        # Handle single phase update
        if job_update.phase_update is not None:
            # First fetch current phases
            stmt = select(jobs_table.c.phases).where(jobs_table.c.id == job_id)
            result = await session.execute(stmt)
            row = result.fetchone()
            if row and row.phases:
                current_phases = json.loads(row.phases)
                # Find and update the specific phase
                for i, phase in enumerate(current_phases):
                    if phase.get("name") == job_update.phase_update.name:
                        # Update only provided fields
                        if job_update.phase_update.status is not None:
                            current_phases[i]["status"] = job_update.phase_update.status.value
                        if job_update.phase_update.started_at is not None:
                            current_phases[i]["started_at"] = job_update.phase_update.started_at.isoformat()
                        if job_update.phase_update.completed_at is not None:
                            current_phases[i]["completed_at"] = job_update.phase_update.completed_at.isoformat()
                        if job_update.phase_update.cost is not None:
                            current_phases[i]["cost"] = job_update.phase_update.cost
                        if job_update.phase_update.tokens is not None:
                            current_phases[i]["tokens"] = job_update.phase_update.tokens
                        if job_update.phase_update.error_message is not None:
                            current_phases[i]["error_message"] = job_update.phase_update.error_message
                        if job_update.phase_update.output_path is not None:
                            current_phases[i]["output_path"] = job_update.phase_update.output_path
                        if job_update.phase_update.metadata is not None:
                            current_phases[i]["metadata"] = job_update.phase_update.metadata
                        break
                update_values["phases"] = json.dumps(current_phases)

        if not update_values:
            # No fields to update, just fetch and return current state
            stmt = select(jobs_table).where(jobs_table.c.id == job_id)
            result = await session.execute(stmt)
            row = result.fetchone()
            return _row_to_job(row) if row else None

        # Execute update
        stmt = update(jobs_table).where(jobs_table.c.id == job_id).values(**update_values)
        result = await session.execute(stmt)

        if result.rowcount == 0:
            return None

        # Fetch and return updated job (within same session)
        stmt = select(jobs_table).where(jobs_table.c.id == job_id)
        result = await session.execute(stmt)
        row = result.fetchone()

        job = _row_to_job(row)

        # Broadcast job update to WebSocket clients
        try:
            from api.routers.websocket import broadcast_job_update

            # Determine event type based on status
            event_type = "job_updated"
            if job.status == JobStatus.completed:
                event_type = "job_completed"
            elif job.status == JobStatus.failed:
                event_type = "job_failed"
            elif job.status == JobStatus.in_progress:
                event_type = "job_started"

            await broadcast_job_update(job, event_type=event_type)
        except Exception:
            # Don't fail job update if broadcast fails
            pass

        return job


async def delete_job(job_id: int) -> bool:
    """Delete a job from the database.

    Args:
        job_id: Job ID to delete

    Returns:
        True if deleted, False if not found
    """
    async with get_session() as session:
        stmt = delete(jobs_table).where(jobs_table.c.id == job_id)
        result = await session.execute(stmt)
        return result.rowcount > 0


async def bulk_delete_jobs_by_status(statuses: List[JobStatus]) -> int:
    """Delete all jobs with the given statuses.

    Args:
        statuses: List of job statuses to delete

    Returns:
        Number of jobs deleted
    """
    if not statuses:
        return 0

    async with get_session() as session:
        status_values = [s.value for s in statuses]
        stmt = delete(jobs_table).where(jobs_table.c.status.in_(status_values))
        result = await session.execute(stmt)
        return result.rowcount


async def update_job_status(
    job_id: int,
    status: JobStatus,
    project_path: Optional[str] = None,
    current_phase: Optional[str] = None,
    error_message: Optional[str] = None,
    actual_cost: Optional[float] = None,
) -> Optional[Job]:
    """Convenience function to update job status with common fields.

    Args:
        job_id: Job ID to update
        status: New job status
        project_path: Optional project output path
        current_phase: Optional current phase name
        error_message: Optional error message (for failed status)
        actual_cost: Optional actual cost

    Returns:
        Updated Job record or None if not found
    """
    update_data = JobUpdate(status=status)

    if current_phase is not None:
        update_data.current_phase = current_phase
    if error_message is not None:
        update_data.error_message = error_message
    if actual_cost is not None:
        update_data.actual_cost = actual_cost

    job = await update_job(job_id, update_data)

    # Handle project_path separately (not in JobUpdate model)
    if project_path is not None and job is not None:
        async with get_session() as session:
            stmt = update(jobs_table).where(jobs_table.c.id == job_id).values(project_path=project_path)
            await session.execute(stmt)

    return job


async def update_job_phase(job_id: int, phases: list) -> Optional[Job]:
    """Update the phases array for a job.

    Args:
        job_id: Job ID to update
        phases: List of phase dictionaries

    Returns:
        Updated Job or None if not found
    """
    async with get_session() as session:
        phases_json = json.dumps(phases, cls=_SafeEncoder)
        stmt = update(jobs_table).where(jobs_table.c.id == job_id).values(phases=phases_json)
        result = await session.execute(stmt)

        if result.rowcount == 0:
            return None

        # Fetch and return updated job
        stmt = select(jobs_table).where(jobs_table.c.id == job_id)
        result = await session.execute(stmt)
        row = result.fetchone()
        return _row_to_job(row) if row else None


async def get_next_pending_job() -> Optional[Job]:
    """Get the next pending job to process (non-atomic, for read-only queries).

    Returns highest priority pending job, or earliest queued if priorities equal.
    WARNING: Use claim_next_job() for worker processing to avoid race conditions.

    Returns:
        Next job to process or None if queue is empty
    """
    async with get_session() as session:
        stmt = (
            select(jobs_table)
            .where(jobs_table.c.status == JobStatus.pending.value)
            .order_by(desc(jobs_table.c.priority), jobs_table.c.queued_at)
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.fetchone()

        if row is None:
            return None

        return _row_to_job(row)


async def claim_next_job(worker_id: Optional[str] = None) -> Optional[Job]:
    """Atomically claim the next pending job for processing.

    Uses a single UPDATE statement to prevent race conditions when multiple
    workers are running. The job is marked as in_progress and started_at
    is set in one atomic operation.

    Args:
        worker_id: Optional identifier for the worker claiming the job.
                   Useful for debugging and monitoring which worker has which job.

    Returns:
        The claimed job (now in_progress) or None if no pending jobs.
    """
    from sqlalchemy import text

    async with get_session() as session:
        now = datetime.now(timezone.utc)

        # SQLite-compatible atomic claim using UPDATE with subquery
        # This finds the next job and claims it in a single statement
        # A deferred job carries a future retry_after; skip it until that time so
        # a busy local backend isn't re-hit before its backoff elapses.
        claim_sql = text("""
            UPDATE jobs
            SET status = :new_status,
                started_at = :started_at,
                last_heartbeat = :heartbeat
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = :pending_status
                  AND (retry_after IS NULL OR retry_after <= :now)
                ORDER BY priority DESC, queued_at ASC
                LIMIT 1
            )
            RETURNING *
        """)

        result = await session.execute(
            claim_sql,
            {
                "new_status": JobStatus.in_progress.value,
                "pending_status": JobStatus.pending.value,
                "started_at": now.isoformat(),
                "heartbeat": now.isoformat(),
                "now": now.isoformat(),
            },
        )

        row = result.fetchone()
        if row is None:
            return None

        # Convert the raw row to a Job model
        # RETURNING * gives us columns in table definition order
        return _row_to_job(row)


async def update_heartbeat(job_id: int) -> bool:
    """Update the last_heartbeat timestamp for a job.

    Should be called periodically during long processing operations.

    Args:
        job_id: Job ID to update heartbeat for

    Returns:
        True if updated, False if job not found
    """
    async with get_session() as session:
        stmt = update(jobs_table).where(jobs_table.c.id == job_id).values(last_heartbeat=datetime.now(timezone.utc))
        result = await session.execute(stmt)
        return result.rowcount > 0


async def defer_job(
    job_id: int,
    *,
    backoff_minutes: List[int],
    ceiling_hours: float,
    retry_after_s: Optional[int] = None,
    detail: Optional[str] = None,
) -> Optional[Job]:
    """Requeue a job whose local backend was busy — or pause it past the ceiling.

    Under the ceiling: status -> pending with a future ``retry_after`` (backoff)
    and an incremented ``defer_count``. ``defer_count`` is deliberately separate
    from ``retry_count``/``max_retries`` — a busy backend is not a failure, so it
    must not burn the failure-retry budget.

    Past the give-up ceiling (wall-clock since ``first_deferred_at``): status ->
    paused with a clear, human-actionable message.

    Args:
        backoff_minutes: backoff schedule, indexed by current defer_count (plateaus
            at the last entry).
        ceiling_hours: max wall-clock to keep deferring before pausing for a human.
        retry_after_s: backend-supplied hint; overrides the schedule when present.
        detail: upstream "why" (e.g. "memory pressure 69%…") for the event log.
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    async with get_session() as session:
        row = (await session.execute(select(jobs_table).where(jobs_table.c.id == job_id))).fetchone()
        if row is None:
            return None

        defer_count = row.defer_count or 0
        first_deferred_at = row.first_deferred_at
        first_dt = datetime.fromisoformat(first_deferred_at) if first_deferred_at else None

        # Give-up ceiling: pause for a human after waiting too long.
        if first_dt is not None and (now - first_dt) >= timedelta(hours=ceiling_hours):
            message = (
                f"Waited {ceiling_hours:g}h for local capacity; backend still busy. "
                "Retry, or switch this job to a cloud backend."
            )
            await session.execute(
                update(jobs_table)
                .where(jobs_table.c.id == job_id)
                .values(
                    status=JobStatus.paused.value,
                    error_message=message,
                    error_timestamp=now,
                    current_phase=None,
                )
            )
            await _insert_event(
                session,
                job_id,
                EventType.system_pause,
                {
                    "reason": "deferral_ceiling_exceeded",
                    "detail": detail,
                    "defer_count": defer_count,
                    "ceiling_hours": ceiling_hours,
                },
                now,
            )
            await session.commit()
        else:
            if retry_after_s is not None:
                backoff_s = int(retry_after_s)
            elif backoff_minutes:
                idx = min(defer_count, len(backoff_minutes) - 1)
                backoff_s = backoff_minutes[idx] * 60
            else:
                backoff_s = 5 * 60
            retry_after = now + timedelta(seconds=backoff_s)

            await session.execute(
                update(jobs_table)
                .where(jobs_table.c.id == job_id)
                .values(
                    status=JobStatus.pending.value,
                    retry_after=retry_after.isoformat(),
                    defer_count=defer_count + 1,
                    first_deferred_at=first_deferred_at or now.isoformat(),
                    current_phase=None,
                )
            )
            await _insert_event(
                session,
                job_id,
                EventType.job_deferred,
                {
                    "reason": "backend_unavailable",
                    "detail": detail,
                    "defer_count": defer_count + 1,
                    "retry_after": retry_after.isoformat(),
                },
                now,
            )
            await session.commit()

    return await get_job(job_id)


async def clear_defer_state(job_id: int) -> None:
    """Reset deferral bookkeeping so a later, unrelated busy spell starts fresh.

    Called when a job makes forward progress (a phase succeeds) — the backend
    served the request, so the prior busy spell is over and its ceiling is moot.
    """
    async with get_session() as session:
        await session.execute(
            update(jobs_table)
            .where(jobs_table.c.id == job_id)
            .values(retry_after=None, defer_count=0, first_deferred_at=None)
        )
        await session.commit()


async def _insert_event(session, job_id: int, event_type: EventType, data: dict, now: datetime) -> None:
    """Insert a session_stats event within an existing session (caller commits)."""
    await session.execute(
        session_stats_table.insert().values(
            job_id=job_id,
            timestamp=now,
            event_type=event_type.value,
            data=json.dumps(data),
        )
    )


async def get_stale_jobs(threshold_minutes: int = 10) -> List[Job]:
    """Get jobs that are in_progress but haven't had a heartbeat update within threshold_minutes.

    Returns list of jobs that may be stuck.

    Args:
        threshold_minutes: Minutes since last heartbeat to consider job stale

    Returns:
        List of jobs with stale heartbeats
    """
    async with get_session() as session:
        # Calculate cutoff time
        from datetime import timedelta

        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

        # Find in_progress jobs with stale or null heartbeat
        stmt = (
            select(jobs_table)
            .where(
                and_(
                    jobs_table.c.status == JobStatus.in_progress.value,
                    func.coalesce(jobs_table.c.last_heartbeat, datetime.min.replace(tzinfo=timezone.utc)) < cutoff_time,
                )
            )
            .order_by(jobs_table.c.id)
        )
        result = await session.execute(stmt)
        rows = result.fetchall()

        return [_row_to_job(row) for row in rows]


async def reset_stuck_jobs(threshold_minutes: int = 10) -> List[Job]:
    """Find and reset jobs that have been in_progress for longer than
    threshold_minutes without a heartbeat update.

    For each stuck job:
    - Set status back to 'pending'
    - Increment retry_count
    - Clear started_at and current_phase
    - Log the reset event to session_stats

    Returns list of jobs that were reset.
    """
    from datetime import timedelta

    async with get_session() as session:
        # Calculate threshold timestamp
        threshold_time = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

        # Find stuck jobs - in_progress with old heartbeat or no heartbeat
        stmt = select(jobs_table).where(
            and_(
                jobs_table.c.status == JobStatus.in_progress.value,
                func.coalesce(jobs_table.c.last_heartbeat, jobs_table.c.started_at) < threshold_time,
            )
        )
        result = await session.execute(stmt)
        stuck_rows = result.fetchall()

        reset_jobs = []

        for row in stuck_rows:
            job_id = row.id
            new_retry_count = row.retry_count + 1

            # Determine new status based on retry count
            if new_retry_count >= row.max_retries:
                # Max retries exceeded - mark as failed
                update_values = {
                    "status": JobStatus.failed.value,
                    "error_message": "Max retries exceeded after stuck job reset",
                    "error_timestamp": datetime.now(timezone.utc),
                    "retry_count": new_retry_count,
                    "completed_at": datetime.now(timezone.utc),
                }

                # Log job_failed event
                event_data_dict = {
                    "job_id": job_id,
                    "reason": "stuck_job_reset_max_retries",
                    "threshold_minutes": threshold_minutes,
                    "retry_count": new_retry_count,
                    "max_retries": row.max_retries,
                }
                event_values = {
                    "job_id": job_id,
                    "timestamp": datetime.now(timezone.utc),
                    "event_type": EventType.job_failed.value,
                    "data": json.dumps(event_data_dict),
                }
                stmt_event = session_stats_table.insert().values(**event_values)
                await session.execute(stmt_event)
            else:
                # Reset to pending
                update_values = {
                    "status": JobStatus.pending.value,
                    "started_at": None,
                    "current_phase": None,
                    "retry_count": new_retry_count,
                }

                # Log system_error event
                event_data_dict = {
                    "job_id": job_id,
                    "reason": "stuck_job_reset",
                    "threshold_minutes": threshold_minutes,
                    "retry_count": new_retry_count,
                }
                event_values = {
                    "job_id": job_id,
                    "timestamp": datetime.now(timezone.utc),
                    "event_type": EventType.system_error.value,
                    "data": json.dumps(event_data_dict),
                }
                stmt_event = session_stats_table.insert().values(**event_values)
                await session.execute(stmt_event)

            # Update job
            stmt_update = update(jobs_table).where(jobs_table.c.id == job_id).values(**update_values)
            await session.execute(stmt_update)

            # Fetch updated job
            stmt_fetch = select(jobs_table).where(jobs_table.c.id == job_id)
            result_fetch = await session.execute(stmt_fetch)
            updated_row = result_fetch.fetchone()
            reset_jobs.append(_row_to_job(updated_row))

        return reset_jobs


async def run_stuck_job_cleanup(threshold_minutes: int = 10) -> dict:
    """Run the stuck job cleanup routine.

    Returns summary dict with:
    - reset_count: Number of jobs reset to pending
    - failed_count: Number of jobs that exceeded max retries
    - job_ids: List of affected job IDs
    """
    reset_jobs = await reset_stuck_jobs(threshold_minutes)

    reset_count = sum(1 for job in reset_jobs if job.status == JobStatus.pending)
    failed_count = sum(1 for job in reset_jobs if job.status == JobStatus.failed)
    job_ids = [job.id for job in reset_jobs]

    return {
        "reset_count": reset_count,
        "failed_count": failed_count,
        "job_ids": job_ids,
    }


# ============================================================================
# Event Logging Operations
# ============================================================================


async def log_event(event: EventCreate, app_version: Optional[str] = None) -> SessionEvent:
    """Log a session event to the database.

    Args:
        event: Event creation schema

    Returns:
        Complete SessionEvent record with generated ID
    """
    async with get_session() as session:
        # Serialize event data to JSON
        data_json = None
        if event.data is not None:
            data_json = event.data.model_dump_json(exclude_none=True)

        values = {
            "job_id": event.job_id,
            "timestamp": datetime.now(timezone.utc),
            "event_type": event.event_type.value,
            "data": data_json,
            "app_version": app_version if app_version is not None else get_app_version(),
        }

        stmt = session_stats_table.insert().values(**values)
        result = await session.execute(stmt)
        event_id = result.inserted_primary_key[0]

        # Fetch and return complete event
        stmt = select(session_stats_table).where(session_stats_table.c.id == event_id)
        result = await session.execute(stmt)
        row = result.fetchone()

        return _row_to_event(row)


async def get_events_for_job(job_id: int) -> List[SessionEvent]:
    """Retrieve all events for a specific job.

    Args:
        job_id: Job ID to get events for

    Returns:
        List of SessionEvent records ordered by timestamp
    """
    async with get_session() as session:
        stmt = (
            select(session_stats_table)
            .where(session_stats_table.c.job_id == job_id)
            .order_by(session_stats_table.c.timestamp)
        )
        result = await session.execute(stmt)
        rows = result.fetchall()

        return [_row_to_event(row) for row in rows]


# ============================================================================
# Config Operations
# ============================================================================


async def get_config(key: str) -> Optional[ConfigItem]:
    """Retrieve a configuration value by key.

    Args:
        key: Configuration key

    Returns:
        ConfigItem or None if not found
    """
    async with get_session() as session:
        stmt = select(config_table).where(config_table.c.key == key)
        result = await session.execute(stmt)
        row = result.fetchone()

        if row is None:
            return None

        return _row_to_config(row)


async def set_config(
    key: str,
    value: str,
    value_type: str = "string",
    description: Optional[str] = None,
) -> ConfigItem:
    """Set or update a configuration value.

    Args:
        key: Configuration key
        value: Configuration value (as string)
        value_type: Type of value (string, int, float, bool, json)
        description: Optional description of config item

    Returns:
        Updated or created ConfigItem
    """
    async with get_session() as session:
        # Check if key exists
        stmt = select(config_table).where(config_table.c.key == key)
        result = await session.execute(stmt)
        existing = result.fetchone()

        if existing is not None:
            # Update existing
            update_values = {
                "value": value,
                "value_type": value_type,
                "updated_at": datetime.now(timezone.utc),
            }
            if description is not None:
                update_values["description"] = description

            stmt = update(config_table).where(config_table.c.key == key).values(**update_values)
            await session.execute(stmt)
        else:
            # Insert new
            values = {
                "key": key,
                "value": value,
                "value_type": value_type,
                "description": description,
                "updated_at": datetime.now(timezone.utc),
            }
            stmt = config_table.insert().values(**values)
            await session.execute(stmt)

        # Fetch and return (within same session)
        stmt = select(config_table).where(config_table.c.key == key)
        result = await session.execute(stmt)
        row = result.fetchone()

        return _row_to_config(row)


# A component is considered alive if its heartbeat is newer than this. The worker
# heartbeats every poll loop (~30s) and the watcher every scan loop, so 120s gives
# ~3-4 missed beats of slack before we report it down. Cross-container safe: the
# heartbeat lives in the shared DB, unlike pgrep/lsof which can't see other
# containers (#179).
HEARTBEAT_STALE_SECONDS = 120


async def record_heartbeat(component: str) -> None:
    """Record a liveness heartbeat for a system component (e.g. 'worker', 'watcher').

    Writes the current UTC time to a `<component>_heartbeat` config key so other
    containers (notably the API serving /system/status) can observe liveness via
    shared DB state rather than same-host process probes.
    """
    await set_config(
        f"{component}_heartbeat",
        datetime.now(timezone.utc).isoformat(),
        value_type="string",
        description=f"Last liveness heartbeat for the {component} process",
    )


async def get_heartbeat_age_seconds(component: str) -> Optional[float]:
    """Return seconds since `component` last heartbeat, or None if never/unparseable."""
    item = await get_config(f"{component}_heartbeat")
    if item is None or not item.value:
        return None
    try:
        ts = datetime.fromisoformat(item.value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def heartbeat_is_fresh(age_seconds: Optional[float]) -> bool:
    """True if a heartbeat age (from get_heartbeat_age_seconds) is within the stale window."""
    return age_seconds is not None and age_seconds < HEARTBEAT_STALE_SECONDS


async def record_startup_markers(version: str) -> None:
    """Record app-lifecycle markers on startup: last restart time and last deploy time.

    - ``api_restarted_at`` is overwritten on every start — this process's boot time.
    - ``deployed_version`` / ``version_deployed_at`` are updated only when the running
      version differs from the last recorded one. That distinguishes a fresh push from a
      same-version restart, and — being in the shared DB — persists across restarts.
    """
    now = datetime.now(timezone.utc).isoformat()
    await set_config(
        "api_restarted_at",
        now,
        value_type="string",
        description="UTC time the API process last started (restart marker)",
    )
    prev = await get_config("deployed_version")
    if prev is None or not prev.value or prev.value != version:
        # Order matters: `set_config` commits per call, so these two writes are separate
        # transactions. Writing the timestamp FIRST makes a crash between them self-healing —
        # `deployed_version` still holds the old version, so the next boot sees a mismatch and
        # rewrites both. The reverse order would strand a stale `version_deployed_at`
        # permanently, since the version guard would match from then on.
        await set_config(
            "version_deployed_at",
            now,
            value_type="string",
            description="UTC time the current app version was first seen (deploy marker)",
        )
        await set_config(
            "deployed_version",
            version,
            value_type="string",
            description="App version currently deployed to this instance",
        )


async def list_config() -> List[ConfigItem]:
    """List all configuration items.

    Returns:
        List of all ConfigItem records
    """
    async with get_session() as session:
        stmt = select(config_table).order_by(config_table.c.key)
        result = await session.execute(stmt)
        rows = result.fetchall()

        return [_row_to_config(row) for row in rows]


# ============================================================================
# Helper Functions
# ============================================================================


# ============================================================================
# Job Items (#329)
# ============================================================================


async def upsert_job_items(job_id: int, rows: List[dict]) -> int:
    """Insert or update a job's items, keyed on (job_id, key).

    Regeneration updates in place — there is no per-item version history by
    design. Airtable's own revision tracking covers published values, and
    worker.py archives superseded phase output as
    ``{phase}_output.{timestamp}.prev.md``, so earlier proposals survive on
    disk.

    Editor-owned columns are preserved across re-extraction: an item the
    editor already approved or kicked back keeps its ``status``,
    ``kickback_note`` and ``kicked_back_at`` rather than being reset by a
    later phase run. Callers that want a reset should clear those fields
    explicitly.

    Returns the number of rows written.
    """
    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    written = 0

    async with get_session() as session:
        existing_result = await session.execute(
            select(job_items_table.c.key, job_items_table.c.status).where(job_items_table.c.job_id == job_id)
        )
        existing = {row.key: row.status for row in existing_result.fetchall()}

        for row in rows:
            payload = dict(row)
            payload["job_id"] = job_id
            payload["updated_at"] = now

            flags = payload.get("flags")
            if isinstance(flags, list):
                payload["flags"] = json.dumps(flags)

            key = payload["key"]
            if key in existing:
                # Never clobber a verdict the editor already gave.
                if existing[key] in (ItemStatus.approved.value, ItemStatus.kicked_back.value):
                    payload.pop("status", None)
                stmt = (
                    update(job_items_table)
                    .where(
                        and_(
                            job_items_table.c.job_id == job_id,
                            job_items_table.c.key == key,
                        )
                    )
                    .values(**payload)
                )
            else:
                payload["created_at"] = now
                stmt = job_items_table.insert().values(**payload)

            await session.execute(stmt)
            written += 1

    return written


async def get_job_items(job_id: int, include_values: bool = True) -> List[Item]:
    """Return a job's items in registry order.

    When ``include_values`` is False, values longer than
    ``items.LARGE_VALUE_CHARS`` are replaced with None — the transcript item
    alone is routinely 50-150KB and would dominate a list response.
    """
    from api.services.items import LARGE_VALUE_CHARS, SPECS_BY_KEY

    async with get_session() as session:
        stmt = select(job_items_table).where(job_items_table.c.job_id == job_id)
        result = await session.execute(stmt)
        rows = result.fetchall()

    order = list(SPECS_BY_KEY)
    items = [_row_to_item(row, include_values=include_values, large=LARGE_VALUE_CHARS) for row in rows]
    items.sort(key=lambda item: order.index(item.key) if item.key in order else len(order))
    return items


async def get_job_item(job_id: int, key: str) -> Optional[Item]:
    """Return one item by its key, or None."""
    async with get_session() as session:
        stmt = select(job_items_table).where(and_(job_items_table.c.job_id == job_id, job_items_table.c.key == key))
        result = await session.execute(stmt)
        row = result.fetchone()

    return _row_to_item(row) if row else None


async def update_job_item(job_id: int, key: str, item_update: ItemUpdate) -> Optional[Item]:
    """Apply an editor action to one item.

    An in-place value edit re-sources the item to ``human``. Setting
    ``kicked_back`` stamps ``kicked_back_at``; regeneration is a separate
    action that re-runs each affected phase once with all of its notes.
    """
    values: dict = {}

    if item_update.status is not None:
        values["status"] = item_update.status.value
        if item_update.status == ItemStatus.kicked_back:
            values["kicked_back_at"] = datetime.now(timezone.utc)

    if item_update.proposed_value is not None:
        values["proposed_value"] = item_update.proposed_value
        values["source"] = "human"

    if item_update.kickback_note is not None:
        values["kickback_note"] = item_update.kickback_note

    if not values:
        return await get_job_item(job_id, key)

    values["updated_at"] = datetime.now(timezone.utc)

    async with get_session() as session:
        stmt = (
            update(job_items_table)
            .where(and_(job_items_table.c.job_id == job_id, job_items_table.c.key == key))
            .values(**values)
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            return None

    return await get_job_item(job_id, key)


async def delete_job_items(job_id: int) -> int:
    """Delete every item for a job. Returns the count removed."""
    async with get_session() as session:
        stmt = delete(job_items_table).where(job_items_table.c.job_id == job_id)
        result = await session.execute(stmt)
        return result.rowcount or 0


def _row_to_item(row, include_values: bool = True, large: int = 4000) -> Item:
    """Convert a database row to an Item model."""
    flags: List[ItemFlag] = []
    if row.flags:
        try:
            flags = [ItemFlag(**flag) for flag in json.loads(row.flags)]
        except (json.JSONDecodeError, TypeError, ValueError):
            # A malformed flags blob must not make the item unreadable —
            # flags are advisory and never gate anything.
            flags = []

    proposed = row.proposed_value
    if not include_values and proposed is not None and len(proposed) > large:
        proposed = None

    return Item(
        id=row.id,
        job_id=row.job_id,
        key=row.key,
        layer=row.layer,
        category=row.category,
        source=row.source,
        proposed_value=proposed,
        current_value=row.current_value,
        current_state=row.current_state,
        current_fetched_at=row.current_fetched_at,
        status=row.status,
        source_blocked_on=row.source_blocked_on,
        flags=flags,
        kickback_note=row.kickback_note,
        kicked_back_at=row.kicked_back_at,
        phase=row.phase,
        model=row.model,
        char_limit=row.char_limit,
        airtable_field=row.airtable_field,
        airtable_field_id=row.airtable_field_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_job(row) -> Job:
    """Convert database row to Job model.

    Handles JSON deserialization for agent_phases and phases fields,
    derives project_name from project_path, and loads outputs from manifest.
    """
    # Parse agent_phases JSON
    agent_phases = json.loads(row.agent_phases)

    # Parse phases JSON (with fallback for existing rows without phases)
    phases = []
    if hasattr(row, "phases") and row.phases:
        phases_data = json.loads(row.phases)
        phases = [JobPhase(**p) for p in phases_data]
    else:
        # Initialize phases from agent_phases for backward compatibility
        phases = [JobPhase(name=name, status=PhaseStatus.pending) for name in agent_phases]

    # Derive project_name from project_path
    project_name = os.path.basename(row.project_path.rstrip("/"))

    # Load outputs from manifest.json if it exists, but only include files that actually exist
    outputs = None
    manifest_path = os.path.join(row.project_path, "manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
                if "outputs" in manifest:
                    # Filter to only include outputs where the file actually exists
                    manifest_outputs = manifest["outputs"]
                    filtered_outputs = {}
                    for key, filename in manifest_outputs.items():
                        if filename:
                            file_path = os.path.join(row.project_path, filename)
                            if os.path.exists(file_path):
                                filtered_outputs[key] = filename

                    # Check for revision files (created by copy editor in Claude Desktop)
                    revision_files = sorted(
                        glob.glob(os.path.join(row.project_path, "copy_revision_v*.md")), reverse=True
                    )
                    if revision_files:
                        # Use latest revision as copy_edited
                        latest_revision = os.path.basename(revision_files[0])
                        filtered_outputs["copy_edited"] = latest_revision

                    # Check for timestamp report (may not be in manifest)
                    timestamp_file = os.path.join(row.project_path, "timestamp_output.md")
                    if os.path.exists(timestamp_file):
                        filtered_outputs["timestamp_report"] = "timestamp_output.md"

                    if filtered_outputs:
                        outputs = JobOutputs(**filtered_outputs)
        except (json.JSONDecodeError, IOError):
            pass  # Ignore errors reading manifest

    return Job(
        id=row.id,
        project_path=row.project_path,
        transcript_file=row.transcript_file,
        project_name=project_name,
        status=JobStatus(row.status),
        priority=row.priority,
        queued_at=row.queued_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        estimated_cost=row.estimated_cost,
        actual_cost=row.actual_cost,
        agent_phases=agent_phases,
        current_phase=row.current_phase,
        phases=phases,
        retry_count=row.retry_count,
        max_retries=row.max_retries,
        error_message=row.error_message,
        error_timestamp=row.error_timestamp,
        auto_escalated_at=getattr(row, "auto_escalated_at", None),
        manifest_path=row.manifest_path,
        logs_path=row.logs_path,
        last_heartbeat=row.last_heartbeat,
        airtable_record_id=getattr(row, "airtable_record_id", None),
        airtable_url=getattr(row, "airtable_url", None),
        media_id=getattr(row, "media_id", None),
        duration_minutes=getattr(row, "duration_minutes", None),
        word_count=getattr(row, "word_count", None),
        content_type=getattr(row, "content_type", None),
        validation_result=json.loads(row.validation_result) if getattr(row, "validation_result", None) else None,
        outputs=outputs,
        app_version=getattr(row, "app_version", None),
        job_type=getattr(row, "job_type", None) or "transcript",
        media_file=getattr(row, "media_file", None),
        intake=json.loads(row.intake) if getattr(row, "intake", None) else None,
    )


def _row_to_event(row) -> SessionEvent:
    """Convert database row to SessionEvent model.

    Handles JSON deserialization for data field.
    """
    # Parse data JSON if present
    event_data = None
    if row.data is not None:
        event_data = EventData.model_validate_json(row.data)

    return SessionEvent(
        id=row.id,
        job_id=row.job_id,
        timestamp=row.timestamp,
        event_type=EventType(row.event_type),
        data=event_data,
        app_version=getattr(row, "app_version", None),
    )


def _row_to_config(row) -> ConfigItem:
    """Convert database row to ConfigItem model."""
    return ConfigItem(
        key=row.key,
        value=row.value,
        value_type=ConfigValueType(row.value_type),
        description=row.description,
        updated_at=row.updated_at,
    )


# ============================================================================
# Convenience Aliases for Worker
# ============================================================================

# Legacy alias - prefer claim_next_job for worker use
get_next_job = claim_next_job
update_job_heartbeat = update_heartbeat
