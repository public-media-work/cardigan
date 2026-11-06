"""Job models for Cardigan API."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from api.models.item import CopyMode


class JobStatus(str, Enum):
    """Valid job status values (status column is plain Text, no CHECK constraint)."""

    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    paused = "paused"
    investigating = "investigating"  # Manager agent diagnosing failure
    awaiting_review = "awaiting_review"  # Media job transcribed; editor must approve transcript


class PhaseStatus(str, Enum):
    """Status for individual processing phases."""

    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class JobPhase(BaseModel):
    """Represents an individual processing phase within a job.

    Tracks completion status of each phase (analyst, formatter, etc.)
    to enable resuming from the last successful phase.
    """

    name: str = Field(..., description="Phase identifier (e.g., 'analyst', 'formatter')")
    status: PhaseStatus = Field(default=PhaseStatus.pending, description="Current phase status")
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cost: float = Field(default=0.0, description="Cost incurred during this phase")
    tokens: int = Field(default=0, description="Tokens used during this phase")
    input_tokens: int = Field(default=0, description="Input/prompt tokens used")
    output_tokens: int = Field(default=0, description="Output/completion tokens used")
    error_message: Optional[str] = None
    output_path: Optional[str] = Field(None, description="Path to phase output file if applicable")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Phase-specific metadata")
    model: Optional[str] = Field(None, description="Model used for this phase")
    # Retry tracking fields
    retry_count: int = Field(default=0, description="Times this phase has been manually retried")
    previous_runs: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="History of previous runs [{model, cost, tokens, completed_at}]"
    )

    def is_complete(self) -> bool:
        """Check if phase completed successfully."""
        return self.status == PhaseStatus.completed

    def is_failed(self) -> bool:
        """Check if phase failed."""
        return self.status == PhaseStatus.failed

    def can_resume(self) -> bool:
        """Check if phase can be resumed (failed or pending)."""
        return self.status in (PhaseStatus.pending, PhaseStatus.failed)


class JobBase(BaseModel):
    """Base job schema with common fields."""

    project_path: str = Field(..., description="Path to project directory")
    transcript_file: str = Field(..., description="Path to transcript file")
    priority: int = Field(default=0, description="Job priority (higher = sooner)")
    max_retries: int = Field(default=3, description="Maximum retry attempts")


class JobCreate(BaseModel):
    """Schema for creating a new job (POST /queue)."""

    project_name: str = Field(..., description="Name for this project (used for output folder)")
    transcript_file: str = Field(..., description="Path to transcript file (relative to transcripts/)")
    project_path: Optional[str] = Field(None, description="Output path (auto-generated if not provided)")
    priority: Optional[int] = Field(default=0, description="Job priority (higher = sooner)")
    job_type: str = Field(default="transcript", description="'transcript' (classic) or 'media' (audio upload)")
    media_file: Optional[str] = Field(None, description="Extracted-audio filename relative to MEDIA_DIR (media jobs)")
    intake: Optional[Dict[str, Any]] = Field(
        None, description="Upload-form intake for media jobs: speakers, context_terms, language"
    )
    copy_generation: Optional[CopyMode] = Field(
        None, description="Override the instance copy mode for this job: off | review | suggest (#427)"
    )


class PhaseUpdate(BaseModel):
    """Schema for updating a specific phase within a job."""

    name: str = Field(..., description="Phase name to update")
    status: Optional[PhaseStatus] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cost: Optional[float] = None
    tokens: Optional[int] = None
    error_message: Optional[str] = None
    output_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class JobUpdate(BaseModel):
    """Schema for partial job updates (PATCH /jobs/{id})."""

    status: Optional[JobStatus] = None
    priority: Optional[int] = None
    transcript_file: Optional[str] = None
    project_name: Optional[str] = None
    project_path: Optional[str] = None
    current_phase: Optional[str] = None
    error_message: Optional[str] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    manifest_path: Optional[str] = None
    logs_path: Optional[str] = None
    last_heartbeat: Optional[datetime] = None
    airtable_record_id: Optional[str] = None
    airtable_url: Optional[str] = None
    media_id: Optional[str] = None
    duration_minutes: Optional[float] = Field(None, description="Transcript duration in minutes")
    word_count: Optional[int] = Field(None, description="Transcript word count")
    content_type: Optional[str] = Field(None, description="Detected content type: 'full', 'short', or 'clip'")
    phases: Optional[List[JobPhase]] = Field(None, description="Replace all phases")
    phase_update: Optional[PhaseUpdate] = Field(None, description="Update a single phase")
    validation_result: Optional[Dict[str, Any]] = Field(
        None, description="Structured validation result from validator phase"
    )
    auto_escalated_at: Optional[datetime] = None
    media_file: Optional[str] = None
    intake: Optional[Dict[str, Any]] = None


class JobOutputs(BaseModel):
    """Output file references from job manifest."""

    analysis: Optional[str] = None
    formatted_transcript: Optional[str] = None
    seo_metadata: Optional[str] = None
    qa_review: Optional[str] = None
    timestamp_report: Optional[str] = None
    copy_edited: Optional[str] = None
    recovery_analysis: Optional[str] = None


class Job(BaseModel):
    """Complete job record including all database fields."""

    id: int
    project_path: str
    transcript_file: str
    project_name: Optional[str] = Field(None, description="Computed from project_path")
    status: JobStatus
    priority: int
    queued_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    estimated_cost: float
    actual_cost: float
    agent_phases: List[str] = Field(default_factory=lambda: ["analyst", "formatter"])
    current_phase: Optional[str] = None
    phases: List[JobPhase] = Field(default_factory=list, description="Detailed status of each processing phase")
    retry_count: int
    max_retries: int
    error_message: Optional[str] = None
    error_timestamp: Optional[datetime] = None
    manifest_path: Optional[str] = None
    logs_path: Optional[str] = None
    last_heartbeat: Optional[datetime] = None
    airtable_record_id: Optional[str] = Field(None, description="Airtable record ID (e.g., 'recXXXXXXXXXXXXXX')")
    airtable_url: Optional[str] = Field(None, description="Full URL to the Airtable record")
    media_id: Optional[str] = Field(None, description="Extracted media ID from filename (e.g., '2WLI1209HD')")
    duration_minutes: Optional[float] = Field(
        None, description="Transcript duration in minutes (from SRT or estimated)"
    )
    word_count: Optional[int] = Field(None, description="Transcript word count")
    content_type: Optional[str] = Field(None, description="Detected content type: 'full', 'short', or 'clip'")
    outputs: Optional[JobOutputs] = Field(None, description="Output files from manifest")
    app_version: Optional[str] = None
    validation_result: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Structured validation result from validator phase: {phase_results: {phase: {status, flags}}, overall}",
    )
    auto_escalated_at: Optional[datetime] = None
    job_type: str = Field(default="transcript", description="'transcript' (classic) or 'media' (audio upload)")
    media_file: Optional[str] = Field(None, description="Extracted-audio filename relative to MEDIA_DIR")
    intake: Optional[Dict[str, Any]] = Field(None, description="Upload-form intake for media jobs")
    copy_generation: Optional[CopyMode] = Field(
        None, description="Per-job copy mode override; null inherits the instance setting (#427)"
    )

    class Config:
        from_attributes = True

    def get_resume_phase(self) -> Optional[str]:
        """Get the phase name to resume from.

        Returns the first phase that is not completed, or None if all complete.
        """
        for phase in self.phases:
            if not phase.is_complete():
                return phase.name
        return None

    def get_completed_phases(self) -> List[str]:
        """Get list of completed phase names."""
        return [p.name for p in self.phases if p.is_complete()]

    def get_phase(self, name: str) -> Optional[JobPhase]:
        """Get a specific phase by name."""
        for phase in self.phases:
            if phase.name == name:
                return phase
        return None

    def all_phases_complete(self) -> bool:
        """Check if all phases are complete."""
        return all(p.is_complete() for p in self.phases)


class JobList(BaseModel):
    """Paginated job list response."""

    jobs: List[Job]
    total: int
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)
    total_pages: int
