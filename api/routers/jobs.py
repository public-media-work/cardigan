"""Jobs router for Cardigan API.

Provides endpoints for job detail retrieval, updates, and control operations.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.models.events import EventCreate, EventData, EventType, SessionEvent
from api.models.item import Item, ItemCategory, ItemList, ItemStatus, ItemUpdate
from api.models.job import Job, JobStatus, JobUpdate, PhaseStatus
from api.services.airtable import AirtableClient
from api.services.database import (
    get_events_for_job,
    get_job,
    get_job_item,
    get_job_items,
    log_event,
    update_job,
    update_job_item,
)

logger = logging.getLogger(__name__)


class SSTMetadata(BaseModel):
    """SST (Single Source of Truth) metadata from Airtable."""

    media_id: Optional[str] = None
    release_title: Optional[str] = None
    short_description: Optional[str] = None
    media_manager_url: Optional[str] = None
    youtube_url: Optional[str] = None
    airtable_url: Optional[str] = None


router = APIRouter()


# Valid state transitions for job control operations
PAUSEABLE_STATES = {JobStatus.pending, JobStatus.in_progress}
RESUMABLE_STATES = {JobStatus.paused}
RETRYABLE_STATES = {JobStatus.failed, JobStatus.paused}
CANCELLABLE_STATES = {
    JobStatus.pending,
    JobStatus.in_progress,
    JobStatus.paused,
    JobStatus.awaiting_review,
}


@router.get("/{job_id}", response_model=Job)
async def get_job_detail(job_id: int):
    """Retrieve full details for a specific job.

    Args:
        job_id: Job ID to retrieve

    Returns:
        Complete job record with all fields

    Raises:
        HTTPException: 404 if job not found
    """
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return job


@router.patch("/{job_id}", response_model=Job)
async def update_job_fields(job_id: int, job_update: JobUpdate):
    """Update job fields with partial data.

    Accepts any subset of updateable fields and applies them to the job.

    Args:
        job_id: Job ID to update
        job_update: Partial update schema with optional fields

    Returns:
        Updated job record

    Raises:
        HTTPException: 404 if job not found
    """
    updated_job = await update_job(job_id, job_update)

    if updated_job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return updated_job


@router.post("/{job_id}/pause", response_model=Job)
async def pause_job(job_id: int):
    """Pause a running or pending job.

    Sets job status to 'paused'. Only valid for pending or in_progress jobs.

    Args:
        job_id: Job ID to pause

    Returns:
        Updated job record

    Raises:
        HTTPException: 404 if job not found, 400 if invalid state transition
    """
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status not in PAUSEABLE_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot pause job in status '{job.status}'. "
            f"Only {', '.join(s.value for s in PAUSEABLE_STATES)} jobs can be paused.",
        )

    job_update = JobUpdate(status=JobStatus.paused)
    updated_job = await update_job(job_id, job_update)

    return updated_job


@router.post("/{job_id}/resume", response_model=Job)
async def resume_job(job_id: int):
    """Resume a paused job.

    Sets job status back to 'pending' so it can be picked up by the worker.
    Only valid for paused jobs.

    Args:
        job_id: Job ID to resume

    Returns:
        Updated job record

    Raises:
        HTTPException: 404 if job not found, 400 if invalid state transition
    """
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status not in RESUMABLE_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resume job in status '{job.status}'. "
            f"Only {', '.join(s.value for s in RESUMABLE_STATES)} jobs can be resumed.",
        )

    job_update = JobUpdate(status=JobStatus.pending)
    updated_job = await update_job(job_id, job_update)

    return updated_job


@router.post("/{job_id}/retry", response_model=Job)
async def retry_job(job_id: int):
    """Retry a failed or paused job.

    Resets job status to 'pending' and clears error message. For truncation-paused
    jobs, resets the formatter and downstream phases while preserving analyst output.

    Args:
        job_id: Job ID to retry

    Returns:
        Updated job record

    Raises:
        HTTPException: 404 if job not found, 400 if invalid state transition
    """
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status not in RETRYABLE_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry job in status '{job.status}'. "
            f"Only {', '.join(s.value for s in RETRYABLE_STATES)} jobs can be retried.",
        )

    # Determine which phases to reset based on failure type
    phases = job.phases or []
    is_truncation = job.error_message and "TRUNCATION" in job.error_message
    if is_truncation:
        phases_to_reset = {"formatter", "seo", "timestamp"}
    else:
        # Find the first non-completed phase and reset from there forward
        # This preserves completed phases (e.g., analyst) when a later phase fails
        phase_names = [p.name for p in phases]
        first_incomplete_idx = None
        for i, phase in enumerate(phases):
            if phase.status != PhaseStatus.completed:
                first_incomplete_idx = i
                break
        if first_incomplete_idx is not None:
            phases_to_reset = set(phase_names[first_incomplete_idx:])
        else:
            phases_to_reset = None  # All completed — reset everything as fallback

    # Reset phase statuses, archiving previous run data
    updated_phases = []
    for phase in phases:
        phase_dict = phase.model_dump()
        if phases_to_reset is None or phase.name in phases_to_reset:
            # Archive current run before resetting (if it had results)
            if phase_dict.get("model"):
                prev_run = {
                    "model": phase_dict.get("model"),
                    "cost": phase_dict.get("cost", 0),
                    "tokens": phase_dict.get("tokens", 0),
                    "completed_at": phase_dict.get("completed_at"),
                }
                previous_runs = phase_dict.get("previous_runs") or []
                previous_runs.append(prev_run)
                phase_dict["previous_runs"] = previous_runs
                phase_dict["retry_count"] = (phase_dict.get("retry_count") or 0) + 1

            # Reset this phase to pending
            phase_dict["status"] = "pending"
            phase_dict["completed_at"] = None
            phase_dict["error_message"] = None
            phase_dict["cost"] = 0
            phase_dict["tokens"] = 0
            phase_dict["model"] = None
            phase_dict["metadata"] = None
        updated_phases.append(phase_dict)

    from api.models.job import JobPhase

    job_update = JobUpdate(
        status=JobStatus.pending,
        error_message="",
        current_phase=None,
        phases=[JobPhase(**p) for p in updated_phases],
    )
    updated_job = await update_job(job_id, job_update)

    # Log user retry event
    await log_event(
        EventCreate(
            job_id=job_id,
            event_type=EventType.user_action,
            data=EventData(
                extra={
                    "action": "job_retry",
                    "is_truncation_retry": is_truncation,
                    "phases_reset": list(phases_to_reset) if phases_to_reset else "all",
                }
            ),
        )
    )

    return updated_job


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: int):
    """Cancel a job.

    Sets job status to 'cancelled'. Only valid for pending, in_progress, or paused jobs.
    Cannot cancel completed or failed jobs.

    Args:
        job_id: Job ID to cancel

    Returns:
        Updated job record

    Raises:
        HTTPException: 404 if job not found, 400 if invalid state transition
    """
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status not in CANCELLABLE_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel job in status '{job.status}'. "
            f"Only {', '.join(s.value for s in CANCELLABLE_STATES)} jobs can be cancelled.",
        )

    job_update = JobUpdate(status=JobStatus.cancelled)
    updated_job = await update_job(job_id, job_update)

    return updated_job


TRANSCRIPT_REPLACEABLE_STATES = {
    JobStatus.failed,
    JobStatus.paused,
    JobStatus.pending,
    JobStatus.completed,
    JobStatus.cancelled,
    JobStatus.awaiting_review,
}

TRANSCRIPTS_DIR = Path(os.getenv("TRANSCRIPTS_DIR", "transcripts"))
ALLOWED_EXTENSIONS = {".txt", ".srt"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post("/{job_id}/replace-transcript", response_model=Job)
async def replace_transcript(
    job_id: int,
    file: UploadFile = File(..., description="Replacement transcript file (.txt or .srt)"),
):
    """Replace a job's transcript file and reset for reprocessing.

    Accepts a new transcript upload for an existing job. The old transcript
    is kept (not deleted). The job is reset to pending with all phases cleared.

    Useful when a corrected transcript is available and you want to reprocess
    without creating a new job.

    Args:
        job_id: Job ID to update
        file: New transcript file

    Returns:
        Updated job record reset to pending

    Raises:
        HTTPException: 404 if job not found, 400 if invalid file or state
    """
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status not in TRANSCRIPT_REPLACEABLE_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot replace transcript for job in status '{job.status}'. "
            f"Only {', '.join(s.value for s in TRANSCRIPT_REPLACEABLE_STATES)} jobs are eligible.",
        )

    # Validate file
    file_ext = Path(file.filename or "").suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB",
        )

    # Save new transcript
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    file_path = TRANSCRIPTS_DIR / (file.filename or "")
    file_path.write_bytes(content)
    logger.info(f"Job {job_id}: Saved replacement transcript: {file_path}")

    # Recalculate metadata
    from api.routers.queue import calculate_transcript_metrics_from_file
    from api.services.utils import extract_media_id

    new_media_id = extract_media_id(file.filename or "")
    duration_minutes, word_count = calculate_transcript_metrics_from_file(file.filename or "")

    old_transcript = job.transcript_file
    old_media_id = job.media_id

    # Reset all phases to pending (archives completed runs into previous_runs)
    from api.models.job import JobPhase
    from api.services.utils import reset_phases_for_reprocess

    reset_phases = reset_phases_for_reprocess(job.phases)

    # Build update
    project_name = Path(file.filename or "").stem
    for suffix in ["_ForClaude", "_forclaude", "_transcript"]:
        if project_name.endswith(suffix):
            project_name = project_name[: -len(suffix)]

    job_update = JobUpdate(
        status=JobStatus.pending,
        transcript_file=file.filename or "",
        project_name=project_name,
        project_path=f"/data/output/{project_name}",
        media_id=new_media_id,
        duration_minutes=duration_minutes,
        word_count=word_count,
        error_message="",
        current_phase=None,
        actual_cost=0.0,
        phases=[JobPhase(**p) for p in reset_phases],
    )
    updated_job = await update_job(job_id, job_update)

    # Re-link Airtable if media_id changed
    if new_media_id and new_media_id != old_media_id:
        try:
            airtable_client = AirtableClient()
            record = await airtable_client.search_sst_by_media_id(new_media_id)
            if record:
                record_id = record["id"]
                airtable_url = airtable_client.get_sst_url(record_id)
                link_update = JobUpdate(
                    airtable_record_id=record_id,
                    airtable_url=airtable_url,
                )
                updated_job = await update_job(job_id, link_update)
                logger.info(f"Job {job_id}: Re-linked to SST record {record_id}")
        except Exception as e:
            logger.warning(f"Job {job_id}: Airtable re-link failed - {e}")

    # Log the replacement event
    await log_event(
        EventCreate(
            job_id=job_id,
            event_type=EventType.user_action,
            data=EventData(
                extra={
                    "action": "transcript_replaced",
                    "old_transcript": old_transcript,
                    "new_transcript": file.filename,
                    "old_media_id": old_media_id,
                    "new_media_id": new_media_id,
                }
            ),
        )
    )

    logger.info(
        f"Job {job_id}: Transcript replaced {old_transcript} -> {file.filename}, "
        f"media_id {old_media_id} -> {new_media_id}"
    )
    return updated_job


@router.get("/{job_id}/events", response_model=List[SessionEvent])
async def get_job_events(job_id: int):
    """Retrieve all events for a specific job.

    Returns chronologically ordered list of events logged during job execution.
    Useful for debugging, monitoring, and audit trails.

    Args:
        job_id: Job ID to get events for

    Returns:
        List of SessionEvent records ordered by timestamp

    Raises:
        HTTPException: 404 if job not found
    """
    # Verify job exists
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    events = await get_events_for_job(job_id)

    return events


def _make_download_filename(project_name: str, filename: str) -> str:
    """Create a download filename prefixed with sanitized project name.

    Example: "Wisconsin Life / 2WLI1209HD" + "analyst_output.md"
             → "Wisconsin-Life-2WLI1209HD-analyst_output.md"
    """
    sanitized = re.sub(r"[^a-zA-Z0-9._-]+", "-", project_name)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-")
    return f"{sanitized}-{filename}"


@router.get("/{job_id}/outputs/{filename}")
async def get_job_output(job_id: int, filename: str, download: bool = Query(default=False)):
    """Retrieve an output file for a specific job.

    Returns the contents of a generated output file (markdown, json, etc.).
    File must exist in the job's output directory.

    Args:
        job_id: Job ID to get output for
        filename: Name of the output file (e.g., analyst_output.md)
        download: If True, set Content-Disposition header to trigger browser download

    Returns:
        File contents as plain text

    Raises:
        HTTPException: 404 if job or file not found, 400 if invalid filename
    """
    # Verify job exists
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Security: only allow specific safe filenames
    allowed_files = {
        "analyst_output.md",
        "formatter_output.md",
        "seo_output.md",
        "validator_output.md",
        "timestamp_output.md",
        "copy_editor_output.md",
        "recovery_analysis.md",
        "manifest.json",
    }

    # Also allow versioned revision and keyword report files
    is_revision_file = bool(re.match(r"^copy_revision_v\d+\.md$", filename))
    is_keyword_report = bool(re.match(r"^keyword_report_v\d+\.md$", filename))

    if filename not in allowed_files and not is_revision_file and not is_keyword_report:
        raise HTTPException(
            status_code=400, detail=f"Invalid filename. Allowed files: {', '.join(sorted(allowed_files))}"
        )

    # Build path to output file
    if not job.project_path:
        raise HTTPException(status_code=404, detail="Job has no output directory configured")

    # Security: Resolve paths and validate within OUTPUT directory
    output_dir = Path(os.getenv("OUTPUT_DIR", "OUTPUT")).resolve()
    file_path = (Path(job.project_path) / filename).resolve()

    # Prevent path traversal attacks
    if not file_path.is_relative_to(output_dir):
        raise HTTPException(status_code=400, detail="Invalid project path - outside output directory")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Output file '{filename}' not found for job {job_id}")

    # Read and return file contents
    content = file_path.read_text(encoding="utf-8")

    # Determine content type
    media_type = "application/json" if filename.endswith(".json") else "text/markdown"
    headers = {}
    if download:
        download_name = _make_download_filename(job.project_name or "", filename)
        headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return PlainTextResponse(content, media_type=media_type, headers=headers)


@router.get("/{job_id}/sst-metadata", response_model=SSTMetadata)
async def get_sst_metadata(job_id: int):
    """Retrieve SST (Single Source of Truth) metadata from Airtable for a job.

    Returns contextual metadata from PBS Wisconsin's Airtable SST table,
    including release title, descriptions, and external links.

    This is a READ-ONLY operation against Airtable.

    Args:
        job_id: Job ID to get SST metadata for

    Returns:
        SSTMetadata with available fields from Airtable

    Raises:
        HTTPException: 404 if job not found or no Airtable record linked
    """
    job = await get_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not job.airtable_record_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} has no linked Airtable record")

    try:
        client = AirtableClient()
        record = await client.get_sst_record(job.airtable_record_id)

        if record is None:
            raise HTTPException(status_code=404, detail=f"Airtable record {job.airtable_record_id} not found")

        fields = record.get("fields", {})

        return SSTMetadata(
            media_id=fields.get("Media ID"),
            release_title=fields.get("Release Title"),
            short_description=fields.get("Short Description"),
            media_manager_url=fields.get("Final Website Link"),  # PBS Wisconsin website URL
            youtube_url=fields.get("YouTube Link"),
            airtable_url=client.get_sst_url(job.airtable_record_id),
        )

    except ValueError as e:
        # Airtable API key not configured
        logger.warning(f"Airtable not configured: {e}")
        raise HTTPException(status_code=503, detail="Airtable integration not configured")
    except Exception as e:
        logger.error(f"Failed to fetch SST metadata: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch metadata from Airtable")


class KeywordReportUploadResponse(BaseModel):
    """Response for keyword report upload."""

    filename: str
    version: int
    message: str


class KeywordReportInfo(BaseModel):
    """Info about a single keyword report file."""

    filename: str
    version: int
    uploaded_at: Optional[str] = None


class KeywordReportsListResponse(BaseModel):
    """Response for listing keyword reports."""

    reports: List[KeywordReportInfo]


@router.post("/{job_id}/keyword-report", response_model=KeywordReportUploadResponse)
async def upload_keyword_report(
    job_id: int,
    file: UploadFile = File(..., description="SEMRush keyword export CSV or text file"),
):
    """Upload a SEMRush keyword report CSV for a job.

    Saves the file as keyword_report_v{N}.md in the job's project directory,
    prepending a markdown header with upload timestamp and source filename.
    The version number auto-increments from existing files.

    Args:
        job_id: Job ID to attach the report to
        file: CSV or text file from SEMRush keyword export

    Returns:
        Filename and version number of the saved report

    Raises:
        HTTPException: 404 if job not found or no project path configured,
                       400 if file type not allowed
    """
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not job.project_path:
        raise HTTPException(status_code=404, detail="Job has no output directory configured")

    # Validate file type
    allowed_content_types = {"text/csv", "text/plain", "application/csv", "application/octet-stream"}
    original_filename = file.filename or "keyword_report.csv"
    ext = Path(original_filename).suffix.lower()
    if ext not in {".csv", ".txt", ".tsv"} and (file.content_type or "") not in allowed_content_types:
        raise HTTPException(
            status_code=400,
            detail="Only CSV or text files are accepted for keyword reports.",
        )

    # Security: resolve project path within OUTPUT directory
    output_dir = Path(os.getenv("OUTPUT_DIR", "OUTPUT")).resolve()
    project_path = Path(job.project_path).resolve()
    if not project_path.is_relative_to(output_dir):
        raise HTTPException(status_code=400, detail="Invalid project path - outside output directory")

    # Determine next version number
    existing = sorted(project_path.glob("keyword_report_v*.md"))
    next_version = len(existing) + 1

    # Read uploaded content
    content_bytes = await file.read()
    csv_content = content_bytes.decode("utf-8", errors="replace")

    # Build markdown document
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    md_content = f"""# SEMRush Keyword Report

**Uploaded:** {timestamp}
**Source file:** {original_filename}

---

{csv_content}
"""

    filename = f"keyword_report_v{next_version}.md"
    file_path = project_path / filename
    file_path.write_text(md_content, encoding="utf-8")

    logger.info(
        "Keyword report uploaded",
        extra={"job_id": job_id, "filename": filename, "version": next_version},
    )

    return KeywordReportUploadResponse(
        filename=filename,
        version=next_version,
        message=f"Keyword report uploaded as {filename}. It will be included in the next SEO phase run.",
    )


@router.get("/{job_id}/keyword-reports", response_model=KeywordReportsListResponse)
async def list_keyword_reports(job_id: int):
    """List all uploaded keyword reports for a job.

    Returns version numbers and upload timestamps for each report file.

    Args:
        job_id: Job ID to list reports for

    Returns:
        List of keyword report metadata

    Raises:
        HTTPException: 404 if job not found
    """
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not job.project_path:
        return KeywordReportsListResponse(reports=[])

    output_dir = Path(os.getenv("OUTPUT_DIR", "OUTPUT")).resolve()
    project_path = Path(job.project_path).resolve()
    if not project_path.is_relative_to(output_dir) or not project_path.exists():
        return KeywordReportsListResponse(reports=[])

    reports = []
    for f in sorted(project_path.glob("keyword_report_v*.md")):
        match = re.match(r"^keyword_report_v(\d+)\.md$", f.name)
        version = int(match.group(1)) if match else 0
        # Extract upload timestamp from file header if present
        uploaded_at = None
        try:
            first_lines = f.read_text(encoding="utf-8").splitlines()
            for line in first_lines[:6]:
                if line.startswith("**Uploaded:**"):
                    uploaded_at = line.replace("**Uploaded:**", "").strip()
                    break
        except Exception:
            pass
        reports.append(KeywordReportInfo(filename=f.name, version=version, uploaded_at=uploaded_at))

    return KeywordReportsListResponse(reports=reports)


class PhaseRetryRequest(BaseModel):
    """Request body for phase retry with optional editorial feedback."""

    feedback: Optional[str] = Field(
        None,
        description="Editorial feedback to guide the retry (e.g., 'add a chapter for topic X', "
        "'merge the first two chapters'). Injected into the agent prompt.",
    )
    model: Optional[str] = Field(
        None,
        description="Model ID to use for this retry (e.g., 'anthropic/claude-sonnet-4.6'). "
        "Defaults to the phase's configured model if not specified.",
    )


class PhaseRetryResponse(BaseModel):
    """Response for phase retry request."""

    success: bool
    phase: Optional[str] = None
    message: str
    cost: Optional[float] = None
    tokens: Optional[int] = None


# Map output keys to phase names
OUTPUT_TO_PHASE = {
    "analysis": "analyst",
    "formatted_transcript": "formatter",
    "seo_metadata": "seo",
    "timestamp_report": "timestamp",
}


@router.post("/{job_id}/phases/{phase_name}/retry", response_model=PhaseRetryResponse)
async def retry_phase(
    job_id: int,
    phase_name: str,
    background_tasks: BackgroundTasks,
    body: PhaseRetryRequest = None,
):
    """Retry a single phase for a job with optional editorial feedback.

    Re-runs one output (e.g., timestamp) without re-running the entire pipeline.

    Accepts an optional JSON body with:
      - feedback: editorial guidance injected into the agent prompt

    Args:
        job_id: Job ID to retry a phase for
        phase_name: Phase name (analyst, formatter, seo, timestamp,
                    copy_editor) OR output key (analysis, seo_metadata,
                    timestamp_report, etc.)
        body: Optional JSON body with feedback field

    Returns:
        PhaseRetryResponse with status

    Raises:
        HTTPException: 404 if job not found, 400 if invalid phase
    """
    if body is None:
        body = PhaseRetryRequest()
    feedback = body.feedback
    # Map output key to phase name if needed
    if phase_name in OUTPUT_TO_PHASE:
        phase_name = OUTPUT_TO_PHASE[phase_name]

    # Validate phase name
    valid_phases = {"analyst", "formatter", "seo", "validator", "timestamp"}
    if phase_name not in valid_phases:
        raise HTTPException(
            status_code=400, detail=f"Invalid phase: {phase_name}. Valid phases: {', '.join(sorted(valid_phases))}"
        )

    # Verify job exists
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Capture original model for event logging
    original_model = None
    for phase in job.phases or []:
        if phase.name == phase_name and phase.model:
            original_model = phase.model
            break

    # Log user retry event
    await log_event(
        EventCreate(
            job_id=job_id,
            event_type=EventType.user_action,
            data=EventData(
                phase=phase_name,
                model=original_model,
                extra={
                    "action": "phase_retry",
                    "original_model": original_model,
                    "model_override": body.model,
                    "has_feedback": feedback is not None,
                },
            ),
        )
    )

    # Set current_phase so UI shows retry in progress
    await update_job(job_id, JobUpdate(current_phase=phase_name))

    # Run the phase retry in the background
    async def run_retry():
        from api.services.worker import JobWorker

        worker = JobWorker()
        result = await worker.retry_single_phase(job_id, phase_name, feedback=feedback, model_override=body.model)
        if not result.get("success"):
            logger.error(
                "Phase retry failed",
                extra={"job_id": job_id, "phase": phase_name, "error": result.get("error")},
            )
        # Clear current_phase after retry completes
        await update_job(job_id, JobUpdate(current_phase=None))

    background_tasks.add_task(run_retry)

    return PhaseRetryResponse(
        success=True,
        phase=phase_name,
        message=f"Phase '{phase_name}' retry started for job {job_id}. Refresh to see results.",
    )


# ============================================================================
# Items (#329)
# ============================================================================


@router.get("/{job_id}/items", response_model=ItemList)
async def list_job_items(
    job_id: int,
    include: Optional[str] = Query(
        None,
        description="Pass 'values' to hydrate large values (the formatted transcript is 50-150KB)",
    ),
    category: Optional[ItemCategory] = Query(None, description="Filter to one category"),
    status: Optional[ItemStatus] = Query(None, description="Filter to one review status"),
):
    """List a job's items — the editor-facing view of its output.

    Items replace the agent-shaped ``*_output.md`` documents as the unit an
    editor reviews. Each carries a proposed value, the current Airtable value
    it would replace, and a status.

    Large values are omitted by default; pass ``?include=values`` to hydrate
    them. Nothing in this response blocks: flags are advisory, and an item
    carrying errors is still approvable.
    """
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    include_values = include == "values"
    items = await get_job_items(job_id, include_values=include_values)

    if category is not None:
        items = [item for item in items if item.category == category]
    if status is not None:
        items = [item for item in items if item.status == status]

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for item in items:
        by_status[item.status.value] = by_status.get(item.status.value, 0) + 1
        by_category[item.category.value] = by_category.get(item.category.value, 0) + 1

    return ItemList(
        items=items,
        total=len(items),
        job_id=job_id,
        values_included=include_values,
        summary={
            "by_status": by_status,
            "by_category": by_category,
            "needs_confirmation": sum(1 for item in items if item.needs_overwrite_confirmation()),
            "flagged": sum(1 for item in items if item.has_errors()),
        },
    )


@router.get("/{job_id}/items/{key}", response_model=Item)
async def get_single_job_item(job_id: int, key: str):
    """Retrieve one item by key, always with its full value."""
    item = await get_job_item(job_id, key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Item '{key}' not found on job {job_id}")
    return item


@router.patch("/{job_id}/items/{key}", response_model=Item)
async def patch_job_item(job_id: int, key: str, body: ItemUpdate):
    """Apply an editor action to one item: approve, kick back, or edit in place.

    An in-place value edit re-sources the item to ``human``. A kickback records
    the note and stamps the time but does not regenerate anything — that is a
    separate action which re-runs each affected phase once, carrying all of
    that phase's notes.
    """
    item = await update_job_item(job_id, key, body)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Item '{key}' not found on job {job_id}")

    logger.info(
        "Item updated",
        extra={"job_id": job_id, "item_key": key, "status": item.status.value, "source": item.source.value},
    )
    return item
