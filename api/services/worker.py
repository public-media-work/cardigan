"""Job processing worker for Cardigan.

Polls the queue for pending jobs and processes them through agent phases.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from api.models.events import EventCreate, EventData, EventType
from api.models.job import JobStatus
from api.services import glossary as glossary_service
from api.services.airtable import get_airtable_client
from api.services.database import (
    claim_next_job,
    clear_defer_state,
    defer_job,
    get_job,
    log_event,
    record_heartbeat,
    set_config,
    update_job,
    update_job_heartbeat,
    update_job_phase,
    update_job_status,
)
from api.services.escalation import (
    classify_qa_failure,
    nonfixable_review_message,
    pause_and_suggest,
    resolve_escalated_model,
    select_escalation_phases,
)
from api.services.llm import (
    BackendUnavailableError,
    CreditExhaustedError,
    LLMResponse,
    end_run_tracking,
    get_llm_client,
    start_run_tracking,
)
from api.services.logging import get_logger, setup_logging
from api.services.restart_signal import get_restart_requested_at, should_restart
from api.services.style_engine import (
    PostStageResult,
    PromptBlockError,
    render_prompt_blocks,
    strip_style_tokens,
)
from api.services.style_engine.prompt_blocks import resolve_prompt_profile
from api.services.utils import calculate_transcript_metrics

# Initialize logging for worker
setup_logging(log_file="worker.log")
logger = get_logger(__name__)

# Default paths
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "OUTPUT"))
TRANSCRIPTS_DIR = Path(os.getenv("TRANSCRIPTS_DIR", "transcripts"))
TRANSCRIPTS_ARCHIVE_DIR = TRANSCRIPTS_DIR / "archive"
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
AGENTS_DIR = Path("prompts")
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR", "knowledge"))

# How long to let in-flight jobs drain on a restart before force-exiting.
# A wedged job is reclaimed afterward by database.reset_stale_jobs().
RESTART_DRAIN_TIMEOUT_SECONDS = 60


def _extract_speakers_from_sst(sst_context: Dict[str, Any]) -> Dict[str, Any]:
    """Extract host and panelist names from SST text fields.

    Parses Social Media Description and Project Notes to find speaker names
    when the dedicated Host/Presenter fields aren't populated.

    Returns dict with optional 'host' and 'panelists' keys.
    """
    result: Dict[str, Any] = {}

    # Title/role words that precede names but aren't names themselves
    _TITLE_PREFIX = r"(?:(?:Chief|Senior|Political|Reporter|Reporters|Bureau|Capitol|News|Wisconsin|PBS|WPR|County|District|Court|Assembly|State|Department|Justice|General)\s+)*"
    # Proper name: First [van/de/von] Last — after stripping title prefixes
    _NAME = rf"{_TITLE_PREFIX}([A-Z][a-z]+(?:\s+van)?\s+[A-Z][a-z]+)"

    # Try Project Notes first (series-level, more structured)
    project_notes = sst_context.get("project_notes", "")
    if project_notes:
        # Find host: "led by ... [Name]" up to "and include"
        led_match = re.search(r"(?:led|hosted)\s+by\s+(.+?)(?:\s+and\s+include|\.\s|$)", project_notes)
        if led_match:
            names = re.findall(_NAME, led_match.group(1))
            if names:
                result["host"] = names[0]

        # Find panelists: "include ... [names]"
        include_match = re.search(r"include\s+(.+?)(?:\.\s|\.$|$)", project_notes)
        if include_match:
            panelists = re.findall(_NAME, include_match.group(1))
            if panelists:
                result["panelists"] = panelists

    # Try Social Media Description (per-episode, names reporters)
    social_desc = sst_context.get("social_media_description", "")
    if social_desc:
        reporters_match = re.search(
            rf"reporters?\s+{_NAME}\s+and\s+{_NAME}",
            social_desc,
            re.IGNORECASE,
        )
        if reporters_match:
            name1, name2 = reporters_match.group(1), reporters_match.group(2)
            if result.get("host"):
                # Add episode-specific names as panelists if not already listed
                existing = result.get("panelists", [])
                for name in [name1, name2]:
                    if name != result["host"] and name not in existing:
                        existing.append(name)
                if existing:
                    result["panelists"] = existing
            else:
                # First name is likely the host
                result["host"] = name1
                if name2 != name1:
                    result["panelists"] = [name2]

    return result


class WorkerConfig:
    """Configuration for the job worker."""

    def __init__(
        self,
        poll_interval: int = 30,
        heartbeat_interval: int = 60,
        max_retries: int = 3,
        max_concurrent_jobs: int = 1,
        worker_id: Optional[str] = None,
    ):
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.max_retries = max_retries
        self.max_concurrent_jobs = max_concurrent_jobs
        # Generate worker_id if not provided
        self.worker_id = worker_id or f"worker-{os.getpid()}"


def apply_validator_model(phases: list, model: Optional[str]) -> list:
    """Set the validator phase's recorded model to the model that just re-judged."""
    for p in phases:
        if p.get("name") == "validator" and model:
            p["model"] = model
    return phases


def apply_escalated_phase_models(phases: Optional[list], escalated: Dict[str, dict]) -> Optional[list]:
    """Refresh persisted ``phases[]`` with the model/cost from escalated re-runs (#243).

    The escalated ``_run_phase`` calls in the QA gate don't update the job's
    persisted ``phases[]`` (only the main loop does), so per-phase attribution
    would otherwise stay stale at the pre-escalation model. ``escalated`` maps
    ``phase_name -> {"model": ..., "cost": ...}``. Handles both ``JobPhase``
    objects and dict entries. Returns the (mutated) ``phases`` list.
    """
    if not phases:
        return phases
    for p in phases:
        name = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
        info = escalated.get(name) if name else None
        if not info:
            continue
        model = info.get("model")
        cost = info.get("cost")
        if isinstance(p, dict):
            if model:
                p["model"] = model
            if cost is not None:
                p["cost"] = cost
        else:
            if model:
                p.model = model
            if cost is not None:
                p.cost = cost
    return phases


class JobWorker:
    """Processes jobs from the queue through agent phases.

    Note: copy_editor is intentionally excluded from automatic processing.
    It's designed to be run interactively via Claude Desktop/MCP for
    human-in-the-loop editing workflow.

    The validator phase runs last to check all outputs.
    """

    # Required phases that always run
    PHASES = ["analyst", "formatter", "seo", "validator"]

    # Optional phases with trigger conditions
    # timestamp: Runs automatically for 10+ minute content, or when requested
    OPTIONAL_PHASES = ["timestamp"]

    # Duration threshold (in minutes) for auto-triggering timestamp phase
    TIMESTAMP_AUTO_THRESHOLD_MINUTES = 10

    @staticmethod
    def _parse_validation_result(raw_output: str) -> dict:
        """Parse validator JSON output, handling markdown fences.

        Args:
            raw_output: Raw LLM response (may include markdown code fences)

        Returns:
            Parsed validation result dict

        Raises:
            json.JSONDecodeError: If output is not valid JSON after cleaning
        """
        cleaned = raw_output.strip()
        # Strip markdown code fences if present
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        return json.loads(cleaned.strip())

    def __init__(self, config: Optional[WorkerConfig] = None):
        self.config = config or WorkerConfig()
        self.llm = get_llm_client()
        self.running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._current_job_id: Optional[int] = None
        self._start_time = datetime.now(timezone.utc)

    async def start(self):
        """Start the worker polling loop with concurrent job processing."""
        self.running = True
        worker_id = self.config.worker_id
        max_concurrent = self.config.max_concurrent_jobs

        logger.info(
            "Worker starting",
            extra={
                "worker_id": worker_id,
                "poll_interval": self.config.poll_interval,
                "max_concurrent": max_concurrent,
            },
        )

        # Track active job tasks and their job_ids for cleanup on failure
        active_tasks: set = set()
        task_to_job_id: Dict[asyncio.Task, int] = {}

        while self.running:
            try:
                # Publish liveness + LLM runtime status to the shared DB so the
                # API container can observe this worker across container boundaries
                # (#179 worker detection, #158 active backend/model/last_run).
                # Best-effort: never let observability break the polling loop.
                try:
                    await record_heartbeat("worker")
                    await set_config(
                        "llm_runtime_status",
                        json.dumps(self.llm.get_status(), default=str),
                        value_type="json",
                        description="Last-known LLM backend/model/run totals, published by the worker",
                    )
                except Exception as hb_error:
                    logger.debug(
                        "Heartbeat/status publish failed",
                        extra={"worker_id": worker_id, "error": str(hb_error)},
                    )

                # Exit for a Settings-requested restart; the supervisor/Docker
                # restart policy brings us back with the fresh config.
                if await self._should_stop_for_restart():
                    logger.info(
                        "Restart requested via Settings; draining in-flight jobs and exiting",
                        extra={"worker_id": worker_id},
                    )
                    self.running = False
                    continue

                # Clean up completed tasks
                done_tasks = {t for t in active_tasks if t.done()}
                for task in done_tasks:
                    job_id = task_to_job_id.pop(task, None)
                    # Check for exceptions
                    try:
                        task.result()
                    except Exception as e:
                        logger.error(
                            "Task error",
                            extra={"worker_id": worker_id, "job_id": job_id, "error": str(e)},
                            exc_info=True,
                        )
                        # Mark job as failed if we have job_id and it escaped normal handling
                        if job_id is not None:
                            try:
                                await update_job_status(job_id, JobStatus.failed)
                                await log_event(
                                    EventCreate(
                                        job_id=job_id,
                                        event_type=EventType.job_failed,
                                        data=EventData(
                                            extra={
                                                "error": f"Unhandled task exception: {str(e)}",
                                                "worker_id": worker_id,
                                            }
                                        ),
                                    )
                                )
                            except Exception as cleanup_error:
                                logger.error(
                                    "Failed to mark job as failed during cleanup",
                                    extra={"job_id": job_id, "error": str(cleanup_error)},
                                )
                active_tasks -= done_tasks

                # Claim more jobs if we have capacity
                while len(active_tasks) < max_concurrent:
                    job = await claim_next_job(worker_id=worker_id)
                    if job:
                        # Convert Job model to dict for processing
                        job_dict = job.model_dump() if hasattr(job, "model_dump") else dict(job)
                        # Start processing as a task
                        task = asyncio.create_task(self.process_job(job_dict))
                        active_tasks.add(task)
                        task_to_job_id[task] = job.id
                        logger.info(
                            "Job claimed",
                            extra={
                                "worker_id": worker_id,
                                "job_id": job.id,
                                "active_jobs": len(active_tasks),
                                "max_concurrent": max_concurrent,
                            },
                        )
                    else:
                        # No more pending jobs
                        break

                # Wait before next poll
                await asyncio.sleep(self.config.poll_interval)

            except Exception as e:
                logger.error(
                    "Error in polling loop",
                    extra={"worker_id": worker_id, "error": str(e)},
                    exc_info=True,
                )
                await asyncio.sleep(self.config.poll_interval)

        # Wait for active tasks on shutdown, bounded so a wedged job can't block restart.
        if active_tasks:
            logger.info(
                "Waiting for active jobs to complete on shutdown",
                extra={"worker_id": worker_id, "active_jobs": len(active_tasks)},
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=RESTART_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # Name the jobs, not just the count -- the whole point of the
                # bounded drain is that the reclaim by reset_stale_jobs isn't
                # silent about which work was abandoned mid-flight.
                abandoned = sorted(
                    job_id for task, job_id in task_to_job_id.items() if not task.done() and job_id is not None
                )
                logger.warning(
                    "Drain timeout after %ss; abandoning %d in-flight job(s) %s for reclaim by reset_stale_jobs",
                    RESTART_DRAIN_TIMEOUT_SECONDS,
                    len(abandoned),
                    abandoned,
                    extra={"worker_id": worker_id, "abandoned_job_ids": abandoned},
                )

    async def _should_stop_for_restart(self) -> bool:
        """True if a Settings 'Restart Components' request postdates this worker's start."""
        return should_restart(self._start_time, await get_restart_requested_at())

    async def stop(self):
        """Stop the worker."""
        self.running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

    async def retry_single_phase(
        self,
        job_id: int,
        phase_name: str,
        feedback: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Retry a single phase for a completed job.

        This allows regenerating one output (e.g., timestamp) without
        re-running the entire pipeline.

        Args:
            job_id: The job ID to retry a phase for
            phase_name: The phase to retry (e.g., 'timestamp', 'seo', 'analyst')
            feedback: Optional editorial feedback to guide the retry
            model_override: Optional model ID to use instead of the phase default

        Returns:
            Dict with status, phase result, and any errors
        """
        from api.services.database import get_job, update_job

        # Validate phase name
        valid_phases = set(self.PHASES + self.OPTIONAL_PHASES)
        if phase_name not in valid_phases:
            return {"success": False, "error": f"Invalid phase: {phase_name}. Valid: {valid_phases}"}

        # Load job
        job = await get_job(job_id)
        if not job:
            return {"success": False, "error": f"Job {job_id} not found"}

        job_dict = job.model_dump() if hasattr(job, "model_dump") else dict(job)

        # Get project path
        project_path = Path(job_dict.get("project_path", ""))
        if not project_path.exists():
            return {"success": False, "error": f"Project path not found: {project_path}"}

        # Build context from existing outputs
        # Note: Use `or 0` pattern because .get() returns None for NULL db values
        context = {
            "project_name": job_dict.get("project_name") or "Unknown",
            "transcript_file": job_dict.get("transcript_file", ""),
            "project_path": project_path,
            "transcript_metrics": {
                "word_count": job_dict.get("word_count") or 0,
                "estimated_duration_minutes": job_dict.get("duration_minutes") or 0,
            },
        }

        # Load existing phase outputs for context
        for existing_phase in self.PHASES:
            output_file = project_path / f"{existing_phase}_output.md"
            if output_file.exists():
                context[f"{existing_phase}_output"] = output_file.read_text()

        # Load transcript
        try:
            transcript_content = self._load_transcript(job_dict)
            context["transcript"] = transcript_content
        except Exception as e:
            logger.warning(f"Could not load transcript for phase retry: {e}")

        # For timestamp phase, load SRT content
        if phase_name == "timestamp":
            srt_path = self._find_srt_file(job_dict)
            if srt_path and srt_path.exists():
                try:
                    context["srt_content"] = srt_path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Could not load SRT for timestamp retry: {e}")

        # Fetch SST context if available
        sst_context = await self._fetch_sst_context(job_dict)
        if sst_context:
            context["sst_context"] = sst_context

        # Load validation flags for context enrichment
        validation_flags = []
        if job_dict.get("validation_result"):
            vr = job_dict["validation_result"]
            if isinstance(vr, str):
                vr = json.loads(vr)
            phase_validation = vr.get("phase_results", {}).get(phase_name, {})
            validation_flags = phase_validation.get("flags", [])

        if validation_flags:
            context["_validation_flags"] = validation_flags
            logger.info(
                "Including validation flags in retry context",
                extra={"job_id": job_id, "phase": phase_name, "flag_count": len(validation_flags)},
            )

        # Load previous output for context feed-forward
        prev_output_file = project_path / f"{phase_name}_output.md"
        if prev_output_file.exists():
            context["_previous_output"] = prev_output_file.read_text()

        # Inject editorial feedback if provided
        if feedback:
            context["_editorial_feedback"] = feedback
            logger.info(
                "Including editorial feedback in retry",
                extra={"job_id": job_id, "phase": phase_name, "feedback_length": len(feedback)},
            )

        # Run the phase
        try:
            logger.info(
                "Retrying single phase",
                extra={"job_id": job_id, "phase": phase_name, "model_override": model_override},
            )

            phase_result = await self._run_phase(
                job_id=job_id,
                phase_name=phase_name,
                context=context,
                project_path=project_path,
                model_override=model_override,
            )

            # Update phase status in job record
            phases = job_dict.get("phases", [])
            if isinstance(phases, str):
                phases = json.loads(phases)

            # Convert any datetime objects to ISO strings for JSON serialization
            for p in phases:
                for key in ["started_at", "completed_at"]:
                    if key in p and p[key] is not None:
                        if hasattr(p[key], "isoformat"):
                            p[key] = p[key].isoformat()

            # Find and update the phase, archiving the previous run
            phase_updated = False
            for p in phases:
                if p.get("name") == phase_name:
                    # Archive the current run before overwriting
                    if p.get("model"):
                        prev_run = {
                            "model": p.get("model"),
                            "cost": p.get("cost", 0),
                            "tokens": p.get("tokens", 0),
                            "completed_at": p.get("completed_at"),
                        }
                        if feedback:
                            prev_run["feedback"] = feedback
                        previous_runs = p.get("previous_runs") or []
                        previous_runs.append(prev_run)
                        p["previous_runs"] = previous_runs
                    p["retry_count"] = (p.get("retry_count") or 0) + 1

                    # Update with new results
                    p["status"] = phase_result.get("status", "completed")
                    p["cost"] = phase_result.get("cost", 0)
                    p["tokens"] = phase_result.get("tokens", 0)
                    p["input_tokens"] = phase_result.get("input_tokens", 0)
                    p["output_tokens"] = phase_result.get("output_tokens", 0)
                    p["model"] = phase_result.get("model")
                    p["completed_at"] = datetime.now(timezone.utc).isoformat()
                    phase_updated = True
                    break

            if not phase_updated and phase_result.get("status") == "completed":
                # Add the phase if it didn't exist
                phases.append(
                    {
                        "name": phase_name,
                        "status": "completed",
                        "cost": phase_result.get("cost", 0),
                        "tokens": phase_result.get("tokens", 0),
                        "input_tokens": phase_result.get("input_tokens", 0),
                        "output_tokens": phase_result.get("output_tokens", 0),
                        "model": phase_result.get("model"),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

            # Save updated phases
            from api.models.job import JobUpdate

            await update_job(job_id, JobUpdate(phases=phases))

            # Extract glossary entries from editorial feedback (fire-and-forget)
            if feedback and phase_result.get("success"):
                try:
                    added = await self._extract_glossary_entries(feedback, job_id)
                    if added > 0:
                        logger.info(
                            "Glossary updated from editorial feedback",
                            extra={"job_id": job_id, "entries_added": added},
                        )
                except Exception as e:
                    logger.warning(
                        "Glossary extraction failed (non-fatal)",
                        extra={"job_id": job_id, "error": str(e)},
                    )

            # Re-run validator after successful retry (unless we just retried the validator)
            if phase_name != "validator" and phase_result.get("success"):
                try:
                    logger.info("Re-running validator after retry", extra={"job_id": job_id})
                    # Update context with the new output
                    context[f"{phase_name}_output"] = phase_result.get("output", "")
                    validator_result = await self._run_phase(
                        job_id=job_id,
                        phase_name="validator",
                        context=context,
                        project_path=project_path,
                    )
                    if validator_result.get("output"):
                        try:
                            validation_data = self._parse_validation_result(validator_result["output"])
                            validation_data = await self._apply_style_lint(job_id, context, validation_data)
                            refreshed = await get_job(job_id)
                            phases = refreshed.phases or [] if refreshed else []
                            phases = apply_validator_model(phases, validator_result.get("model"))
                            await update_job(
                                job_id,
                                JobUpdate(validation_result=validation_data, phases=phases),
                            )
                            logger.info(
                                "Post-retry validation complete",
                                extra={
                                    "job_id": job_id,
                                    "overall": validation_data.get("overall"),
                                },
                            )
                        except json.JSONDecodeError:
                            logger.warning(
                                "Post-retry validator returned invalid JSON",
                                extra={"job_id": job_id},
                            )
                except Exception as e:
                    logger.warning(
                        f"Post-retry validation failed (non-fatal): {e}",
                        extra={"job_id": job_id},
                    )

            return {
                "success": True,
                "phase": phase_name,
                "result": phase_result,
            }

        except Exception as e:
            logger.error(
                "Phase retry failed", extra={"job_id": job_id, "phase": phase_name, "error": str(e)}, exc_info=True
            )
            return {"success": False, "error": str(e)}

    async def _extract_glossary_entries(self, feedback: str, job_id: int) -> int:
        """Extract glossary-worthy corrections from editorial feedback.

        Sends feedback through a lightweight LLM call to identify name
        corrections, spelling fixes, and terms that should be added to
        knowledge/glossary.md. Returns the number of entries added.
        """
        glossary_path = KNOWLEDGE_DIR / "glossary.md"
        if not glossary_path.exists():
            return 0

        current_glossary = glossary_path.read_text()

        system_prompt = """You extract name corrections and spelling fixes from editorial feedback on transcripts.

Given editorial feedback, identify any corrections that should be added to a glossary for future transcripts. Focus on:
- Name spelling corrections (e.g., "used Shawn instead of Sean")
- Proper noun fixes
- Recurring terms that were wrong

For each correction, output ONE line in this exact format:
CORRECTION: wrong_form -> correct_form | context

If there are no glossary-worthy corrections in the feedback, output exactly:
NO_CORRECTIONS

Do NOT include general formatting feedback, style notes, or paraphrasing observations — only specific spelling/naming corrections."""

        user_prompt = f"""Editorial feedback:
---
{feedback}
---

Current glossary (to avoid duplicates):
---
{current_glossary}
---

Extract any name or spelling corrections that should be added to the glossary. Skip anything already covered."""

        # Use cheapest tier for this lightweight extraction
        backend = self.llm.get_backend_for_phase("analyst")

        try:
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                backend=backend,
                job_id=job_id,
                phase="glossary_extraction",
            )
            content = response.content.strip()
        except Exception as e:
            logger.warning(
                "Glossary extraction LLM call failed",
                extra={"job_id": job_id, "error": str(e)},
            )
            return 0

        if "NO_CORRECTIONS" in content or not content:
            return 0

        # Parse corrections and append to glossary
        new_entries = []
        for line in content.split("\n"):
            line = line.strip()
            if not line.startswith("CORRECTION:"):
                continue
            try:
                correction_part = line[len("CORRECTION:") :].strip()
                if " -> " not in correction_part:
                    continue
                forms, _, context_note = correction_part.partition("|")
                wrong, _, correct = forms.partition("->")
                wrong = wrong.strip()
                correct = correct.strip()
                context_note = context_note.strip() if context_note else ""

                if not wrong or not correct:
                    continue

                # Skip if already in glossary
                if wrong.lower() in current_glossary.lower() and correct.lower() in current_glossary.lower():
                    continue

                new_entries.append((correct, wrong, context_note))
            except Exception:
                continue

        if not new_entries:
            return 0

        added = glossary_service.add_corrections(new_entries, glossary_path)

        if added:
            logger.info(
                "Appended glossary entries from editorial feedback",
                extra={
                    "job_id": job_id,
                    "entries": [f"{w} -> {c}" for c, w, _ in new_entries],
                },
            )

        return added

    async def process_job(self, job: Dict[str, Any]):
        """Process a single job through all phases."""
        job_id = job["id"]
        self._current_job_id = job_id
        # Pick up any model/config changes made via the Settings API since
        # this worker process started (api and worker are separate containers).
        self.llm.reload_config()
        project_name = job.get("project_name") or "Unknown"

        logger.info("Processing job", extra={"job_id": job_id, "project_name": project_name})

        # Start cost tracking for this run
        tracker = start_run_tracking(job_id)

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id))

        try:
            # Status already set to in_progress by claim_next_job()

            # Log job started event
            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.job_started,
                    data=EventData(extra={"project_name": job.get("project_name")}),
                )
            )

            # Set up project directory
            project_path = self._setup_project_dir(job)
            await update_job_status(job_id, JobStatus.in_progress, project_path=str(project_path))

            # Check if all phases are already complete (recovery case)
            phases = job.get("phases") or []
            if isinstance(phases, str):
                phases = json.loads(phases)

            if self._all_phases_complete(phases, project_path):
                logger.info(
                    "All phases already complete, marking job done",
                    extra={"job_id": job_id, "project_name": project_name},
                )
                await update_job_status(
                    job_id,
                    JobStatus.completed,
                    actual_cost=job.get("actual_cost", 0),
                )
                return

            # Media jobs (audio upload mode): run the transcription pre-stage,
            # then stop — the job waits in awaiting_review until the editor
            # approves the corrected transcript, which resets it to pending
            # with transcript_file set and the LLM phases still pending.
            if job.get("job_type") == "media":
                transcription_phase = next((p for p in phases if p.get("name") == "transcription"), None)
                if not transcription_phase or transcription_phase.get("status") != "completed":
                    await self._run_transcription_stage(job, project_path, phases)
                    return
                if not job.get("transcript_file"):
                    # Reclaimed (e.g. manual requeue) before the review was
                    # approved — park it back in the review gate, don't fail.
                    logger.info(
                        "Media job has no approved transcript yet; returning to review",
                        extra={"job_id": job_id},
                    )
                    await update_job_status(job_id, JobStatus.awaiting_review)
                    return

            # Load transcript
            transcript_content = self._load_transcript(job)

            # Calculate transcript metrics for routing decisions
            routing_config = self.llm.config.get("routing", {})
            threshold_minutes = routing_config.get("long_form_threshold_minutes", 15)
            transcript_metrics = calculate_transcript_metrics(
                transcript_content, long_form_threshold_minutes=threshold_minutes
            )
            # Prefer SRT-parsed duration over the word-count estimate so every
            # downstream consumer (routing, prompt context, persisted
            # duration_minutes) reports the same, accurate value (#126).
            transcript_metrics = self._resolve_duration_into_metrics(transcript_metrics, self._find_srt_file(job))
            logger.info(
                "Transcript metrics calculated",
                extra={
                    "job_id": job_id,
                    "word_count": transcript_metrics["word_count"],
                    "estimated_duration_minutes": transcript_metrics["estimated_duration_minutes"],
                    "is_long_form": transcript_metrics["is_long_form"],
                },
            )

            # Persist metrics to database (backfills on retry if missing)
            if not job.get("word_count") or not job.get("duration_minutes"):
                try:
                    from api.models.job import JobUpdate
                    from api.services.database import update_job

                    await update_job(
                        job_id,
                        JobUpdate(
                            word_count=transcript_metrics["word_count"],
                            duration_minutes=transcript_metrics["estimated_duration_minutes"],
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to persist transcript metrics (non-fatal)",
                        extra={"job_id": job_id, "error": str(e)},
                    )

            # Fetch SST context if linked (Task 6.2.1)
            sst_context = await self._fetch_sst_context(job)
            if sst_context:
                logger.info(
                    "SST context loaded", extra={"job_id": job_id, "sst_title": sst_context.get("title", "Unknown")}
                )

            # Detect content type (short/clip/full) for routing decisions
            srt_path_for_detection = self._find_srt_file(job)
            content_type = self._detect_content_type(transcript_metrics, srt_path_for_detection, sst_context)
            logger.info(
                "Content type detected",
                extra={"job_id": job_id, "content_type": content_type},
            )

            # Persist content_type to the database
            try:
                from api.models.job import JobUpdate
                from api.services.database import update_job

                await update_job(job_id, JobUpdate(content_type=content_type))
                # Also propagate into job dict so _should_run_timestamp_phase can read it
                job["content_type"] = content_type
            except Exception as e:
                logger.warning(
                    "Failed to persist content_type (non-fatal)",
                    extra={"job_id": job_id, "error": str(e)},
                )

            # Attempt diarization if service is available and media file exists
            diarization_result = None
            media_path = self._find_media_file(job)
            if media_path:
                try:
                    from api.services.diarization_client import DiarizationClient

                    diarization_client = DiarizationClient()
                    if await diarization_client.is_available():
                        logger.info(
                            "Diarization service available, processing media file",
                            extra={"job_id": job_id, "media_file": media_path.name},
                        )
                        diarization_result = await diarization_client.diarize(str(media_path))
                        if diarization_result:
                            logger.info(
                                "Diarization complete",
                                extra={
                                    "job_id": job_id,
                                    "speakers": len(diarization_result.get("speakers", [])),
                                    "segments": len(diarization_result.get("segments", [])),
                                },
                            )
                    else:
                        logger.debug("Diarization service not available", extra={"job_id": job_id})
                    await diarization_client.close()
                except Exception as e:
                    logger.warning(
                        "Diarization failed (non-fatal, continuing without it)",
                        extra={"job_id": job_id, "error": str(e)},
                    )

            # Get existing phases or initialize
            phases = job.get("phases") or []
            if isinstance(phases, str):
                phases = json.loads(phases)

            # Process each phase
            context = {
                "project_name": project_name,  # Expose project name for style-engine emitters (e.g., timestamp post-stage)
                "transcript": transcript_content,
                "transcript_file": job.get("transcript_file", ""),
                "project_path": project_path,
                "transcript_metrics": transcript_metrics,
                "sst_context": sst_context,  # Add SST context to processing context
                "content_type": content_type,  # Expose content_type for prompt building
                "diarization_result": diarization_result,  # Speaker diarization (may be None)
            }

            truncation_paused = False
            # Holds the parsed validator verdict (set when the validator phase runs);
            # consumed by the QA-fail escalation gate at completion time.
            validation_data: Optional[dict] = None

            for phase_name in self.PHASES:
                # Check if phase already completed
                existing_phase = next((p for p in phases if p["name"] == phase_name), None)
                if existing_phase and existing_phase.get("status") == "completed":
                    logger.debug("Skipping completed phase", extra={"job_id": job_id, "phase": phase_name})
                    # Load previous output for context
                    output_file = project_path / f"{phase_name}_output.md"
                    if output_file.exists():
                        context[f"{phase_name}_output"] = output_file.read_text()
                    continue

                # Update current phase
                await update_job_status(job_id, JobStatus.in_progress, current_phase=phase_name)

                # Process phase
                logger.info("Running phase", extra={"job_id": job_id, "phase": phase_name})
                phase_result = await self._run_phase(job_id, phase_name, context, project_path)

                # Update phases list with model/tier info
                phase_data = {
                    "name": phase_name,
                    "status": "completed" if phase_result["success"] else "failed",
                    "cost": phase_result.get("cost", 0),
                    "tokens": phase_result.get("tokens", 0),
                    "input_tokens": phase_result.get("input_tokens", 0),
                    "output_tokens": phase_result.get("output_tokens", 0),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "model": phase_result.get("model"),
                    "attempts": phase_result.get("attempts", 1),
                }

                # Update or add phase, preserving retry history fields
                phase_updated = False
                for i, p in enumerate(phases):
                    if p["name"] == phase_name:
                        # Preserve retry history from previous runs
                        phase_data["retry_count"] = p.get("retry_count", 0)
                        phase_data["previous_runs"] = p.get("previous_runs")
                        phase_data["metadata"] = p.get("metadata")
                        phases[i] = phase_data
                        phase_updated = True
                        break
                if not phase_updated:
                    phases.append(phase_data)

                await update_job_phase(job_id, phases)

                if phase_result.get("deferred"):
                    # Backend busy, not broken: requeue with backoff (or pause past
                    # the ceiling) and stop here. The worker reclaims the job when
                    # retry_after elapses, resuming at this still-unfinished phase.
                    backoff_minutes, ceiling_hours = self._deferral_policy()
                    await defer_job(
                        job_id,
                        backoff_minutes=backoff_minutes,
                        ceiling_hours=ceiling_hours,
                        retry_after_s=phase_result.get("retry_after_s"),
                        detail=phase_result.get("detail"),
                    )
                    logger.info(
                        "Job requeued — local backend busy",
                        extra={"job_id": job_id, "phase": phase_name, "detail": phase_result.get("detail")},
                    )
                    return

                if phase_result.get("credit_exhausted"):
                    # OpenRouter is out of credit (Trigger B, #243). Pause the job
                    # with an actionable message instead of raising (which would
                    # mark it failed and burn a retry). The user adds credit and
                    # retries from where it stopped.
                    logger.warning(
                        "Job paused — OpenRouter credit exhausted",
                        extra={"job_id": job_id, "phase": phase_name, "project_name": project_name},
                    )
                    await self._pause_for_credit(job_id)
                    return

                if not phase_result["success"]:
                    raise Exception(f"Phase {phase_name} failed: {phase_result.get('error')}")

                # Add output to context for next phase
                context[f"{phase_name}_output"] = phase_result.get("output", "")

                # Parse and store validation result
                if phase_name == "validator" and phase_result.get("output"):
                    try:
                        validation_data = self._parse_validation_result(phase_result["output"])
                        validation_data = await self._apply_style_lint(job_id, context, validation_data)
                        from api.models.job import JobUpdate as JU
                        from api.services.database import update_job as db_update_job

                        await db_update_job(job_id, JU(validation_result=validation_data))
                        logger.info(
                            "Validation complete",
                            extra={
                                "job_id": job_id,
                                "overall": validation_data.get("overall"),
                                "flags": sum(
                                    len(p.get("flags", [])) for p in validation_data.get("phase_results", {}).values()
                                ),
                            },
                        )
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "Validator returned invalid JSON, storing raw output",
                            extra={"job_id": job_id, "error": str(e)},
                        )

                # === Completeness check after formatter phase ===
                if phase_name == "formatter":
                    from api.services.completeness import check_completeness

                    completeness_config = self.llm.config.get("routing", {}).get("completeness", {})
                    if completeness_config.get("enabled", True):
                        formatter_output = phase_result.get("output", "")
                        transcript_file = job.get("transcript_file", "")
                        is_srt = transcript_file.lower().endswith(".srt")

                        completeness = check_completeness(
                            formatter_output=formatter_output,
                            source_transcript=transcript_content,
                            is_srt=is_srt,
                            duration_minutes=job.get("duration_minutes"),
                            threshold=completeness_config.get("coverage_threshold", 0.70),
                            min_source_words=completeness_config.get("min_source_words", 500),
                        )

                        # Store result in context for Manager phase
                        context["completeness_check"] = completeness.to_dict()

                        if not completeness.is_complete and not completeness.skipped:
                            logger.warning(
                                "Transcript truncation detected",
                                extra={
                                    "job_id": job_id,
                                    "coverage_ratio": completeness.coverage_ratio,
                                    "source_words": completeness.source_word_count,
                                    "output_words": completeness.output_word_count,
                                },
                            )

                            await log_event(
                                EventCreate(
                                    job_id=job_id,
                                    event_type=EventType.phase_failed,
                                    data=EventData(
                                        phase="completeness_check",
                                        extra=completeness.to_dict(),
                                    ),
                                )
                            )

                            if completeness_config.get("pause_on_truncation", True):
                                truncation_msg = (
                                    f"TRUNCATION DETECTED: Formatter output covers only "
                                    f"{completeness.coverage_ratio:.0%} of source transcript "
                                    f"({completeness.output_word_count:,} / "
                                    f"{completeness.source_word_count:,} words). "
                                    f"Retry to escalate to a more capable model."
                                )
                                # Route truncation through the shared terminal
                                # helper (Trigger C, #243) for a consistent
                                # ``[truncation] ...`` paused message. The later
                                # ``if truncation_paused`` block records the cost.
                                await pause_and_suggest(
                                    job_id,
                                    trigger="truncation",
                                    message=truncation_msg,
                                )
                                truncation_paused = True
                                break  # Exit phase loop cleanly (no exception)

                # === Seam-gap check after formatter phase ===
                # Catches localized chunk-boundary drops that the global coverage
                # ratio can't see (issue #269). Runs only if the completeness
                # check above didn't already pause the job.
                if phase_name == "formatter" and not truncation_paused:
                    from api.services.seam_coverage import (
                        DEFAULT_BLOCKING_RATIO,
                        find_dropped_spans,
                        format_gap_message,
                    )

                    seam_config = self.llm.config.get("routing", {}).get("seam_coverage", {})
                    if seam_config.get("enabled", True):
                        formatter_output = phase_result.get("output", "")
                        transcript_file = job.get("transcript_file", "")
                        is_srt = transcript_file.lower().endswith(".srt")

                        seam = find_dropped_spans(
                            source_transcript=transcript_content,
                            formatter_output=formatter_output,
                            is_srt=is_srt,
                            min_run=seam_config.get("min_run", 4),
                            per_caption_floor=seam_config.get("per_caption_floor", 0.5),
                            blocking_ratio=seam_config.get("blocking_ratio", DEFAULT_BLOCKING_RATIO),
                        )

                        context["seam_coverage"] = seam.to_dict()

                        if seam.has_gap:
                            logger.warning(
                                "Seam gap detected in formatter output",
                                extra={
                                    "job_id": job_id,
                                    "blocking": seam.blocking,
                                    "net_coverage_ratio": seam.net_coverage_ratio,
                                    "dropped_spans": seam.to_dict()["dropped_spans"],
                                },
                            )
                            # Only pause on a gap corroborated by net content loss
                            # (blocking). A gap with no net loss is the formatter
                            # reconstructing garbled captions, not real content loss
                            # — record it (context["seam_coverage"]) but proceed.
                            if seam.blocking:
                                await log_event(
                                    EventCreate(
                                        job_id=job_id,
                                        event_type=EventType.phase_failed,
                                        data=EventData(
                                            phase="seam_coverage",
                                            extra=seam.to_dict(),
                                        ),
                                    )
                                )
                                if seam_config.get("pause_on_gap", True):
                                    await update_job_status(
                                        job_id,
                                        JobStatus.paused,
                                        error_message=format_gap_message(seam),
                                    )
                                    truncation_paused = True
                                    break  # Exit phase loop cleanly (no exception)

            # Handle truncation pause — exit before optional phases
            if truncation_paused:
                logger.info(
                    "Job paused due to truncation detection", extra={"job_id": job_id, "project_name": project_name}
                )
                run_summary = await end_run_tracking(job_id)
                if run_summary:
                    await update_job_status(
                        job_id,
                        JobStatus.paused,
                        actual_cost=run_summary["total_cost"],
                    )
                return

            # Process optional phases (timestamp) if conditions are met
            srt_path = self._find_srt_file(job)
            if self._should_run_timestamp_phase(job, transcript_metrics, srt_path):
                phase_name = "timestamp"

                # Check if phase already completed
                existing_phase = next((p for p in phases if p["name"] == phase_name), None)
                if not (existing_phase and existing_phase.get("status") == "completed"):
                    # Add SRT content to context
                    if srt_path:
                        try:
                            context["srt_content"] = srt_path.read_text(encoding="utf-8", errors="replace")
                            context["srt_path"] = str(srt_path)
                        except Exception as e:
                            logger.warning(
                                "Failed to read SRT file for timestamp phase", extra={"job_id": job_id, "error": str(e)}
                            )

                    # Update current phase
                    await update_job_status(job_id, JobStatus.in_progress, current_phase=phase_name)

                    # Process phase
                    logger.info("Running optional phase", extra={"job_id": job_id, "phase": phase_name})
                    phase_result = await self._run_phase(job_id, phase_name, context, project_path)

                    # Update phases list
                    phase_data = {
                        "name": phase_name,
                        "status": "completed" if phase_result["success"] else "failed",
                        "cost": phase_result.get("cost", 0),
                        "tokens": phase_result.get("tokens", 0),
                        "input_tokens": phase_result.get("input_tokens", 0),
                        "output_tokens": phase_result.get("output_tokens", 0),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "model": phase_result.get("model"),
                        "optional": True,  # Mark as optional phase
                    }

                    # Update existing phase entry or append new one
                    opt_phase_updated = False
                    for i, p in enumerate(phases):
                        if p.get("name") == phase_name:
                            phase_data["retry_count"] = p.get("retry_count", 0)
                            phase_data["previous_runs"] = p.get("previous_runs")
                            phase_data["metadata"] = p.get("metadata")
                            phases[i] = phase_data
                            opt_phase_updated = True
                            break
                    if not opt_phase_updated:
                        phases.append(phase_data)
                    await update_job_phase(job_id, phases)

                    # Credit exhaustion is NEVER non-fatal, even in an optional
                    # phase (Trigger B, #243). Pause-and-suggest before the swallow
                    # below would otherwise hide the credit problem and let the job
                    # complete on partial output.
                    if phase_result.get("credit_exhausted"):
                        logger.warning(
                            "Job paused — OpenRouter credit exhausted (optional phase)",
                            extra={"job_id": job_id, "phase": phase_name, "project_name": project_name},
                        )
                        await self._pause_for_credit(job_id)
                        return

                    # Optional phase failure is logged but doesn't fail the job
                    if not phase_result["success"]:
                        logger.warning(
                            "Optional phase failed (non-fatal)",
                            extra={"job_id": job_id, "phase": phase_name, "error": phase_result.get("error")},
                        )

                    # Add output to context
                    context[f"{phase_name}_output"] = phase_result.get("output", "")

            # Create manifest
            await self._create_manifest(job, project_path, phases, tracker)

            # QA-fail escalation gate (Trigger A, #243): a validator `overall: "fail"`
            # must NOT silently land as `completed`. The gate escalates flagged phases
            # once and re-validates; on persistent/unresolvable failure it pauses the
            # job (via pause_and_suggest) instead of completing it.
            #
            # The cost tracker must stay ALIVE across the gate so the escalated
            # `_run_phase` re-runs and the re-validation LLM call are billed into
            # this job's run total (and the cost-cap check still applies during
            # escalation). `end_run_tracking` is therefore called AFTER the gate
            # returns, not before it (#243 fix).
            outcome = await self._finalize_with_qa_gate(
                job_id,
                context,
                project_path,
                validation_data,
                [p.get("name") for p in (phases or [])],
            )

            run_summary = await end_run_tracking(job_id)

            if outcome == "completed":
                # Mark job completed
                await update_job_status(
                    job_id,
                    JobStatus.completed,
                    actual_cost=run_summary["total_cost"] if run_summary else 0,
                )
                # Job finished: clear any deferral bookkeeping so a future reprocess
                # starts a fresh capacity-wait ceiling rather than inheriting this one.
                await clear_defer_state(job_id)

                # Archive the transcript file (non-fatal if this fails). Only on
                # completion — a paused job may be reviewed/retried and still needs
                # its source transcript, so we must NOT move it away on pause.
                try:
                    self._archive_transcript(job)
                except Exception as archive_err:
                    logger.warning(
                        "Failed to archive transcript (non-fatal)", extra={"job_id": job_id, "error": str(archive_err)}
                    )

                logger.info(
                    "Job completed successfully",
                    extra={
                        "job_id": job_id,
                        "project_name": project_name,
                        "total_cost": run_summary["total_cost"] if run_summary else 0,
                    },
                )
            else:
                # Gate already paused the job with an actionable error_message;
                # do NOT mark it completed and do NOT archive the transcript.
                # Record the attempt's cost without clobbering the pause reason.
                await update_job_status(
                    job_id,
                    JobStatus.paused,
                    actual_cost=run_summary["total_cost"] if run_summary else 0,
                )
                logger.info(
                    "Job paused by QA gate (not completed)",
                    extra={
                        "job_id": job_id,
                        "project_name": project_name,
                        "total_cost": run_summary["total_cost"] if run_summary else 0,
                    },
                )

        except Exception as e:
            logger.error(
                "Job failed",
                extra={"job_id": job_id, "project_name": project_name, "error": str(e)},
                exc_info=True,
            )

            # End tracking for this attempt
            run_summary = await end_run_tracking(job_id)
            current_cost = run_summary["total_cost"] if run_summary else 0

            # Mark job as failed — user can retry individual phases from the UI
            await update_job_status(
                job_id,
                JobStatus.failed,
                error_message=str(e),
                actual_cost=current_cost,
            )

            # Log error event
            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.job_failed,
                    data=EventData(extra={"error": str(e)}),
                )
            )

        finally:
            # Ensure cost tracker is cleaned up (prevents memory leak on crashes)
            from api.services.llm import _run_trackers

            _run_trackers.pop(job_id, None)

            # Stop heartbeat - properly await cancellation
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task  # Wait for cancellation to complete
                except asyncio.CancelledError:
                    pass  # Expected when cancelling
                except Exception as e:
                    logger.warning("Heartbeat cleanup error", extra={"job_id": job_id, "error": str(e)})
                self._heartbeat_task = None
            self._current_job_id = None

    def _phase_model(self, job, phase_name: str) -> Optional[str]:
        """Return the model the named phase actually ran on, from the job's
        persisted phases. Handles both JobPhase objects and dict entries.

        Returns None if the job/phase is unknown (the caller then treats the
        family as unresolvable and skips escalation for that phase).
        """
        if job is None:
            return None
        phases = getattr(job, "phases", None) or []
        for p in phases:
            name = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
            if name == phase_name:
                return p.get("model") if isinstance(p, dict) else getattr(p, "model", None)
        return None

    async def _finalize_with_qa_gate(
        self,
        job_id: int,
        context: Dict[str, Any],
        project_path,
        validation_result: Optional[dict],
        phase_order: list,
    ) -> str:
        """Decide the terminal state after all phases run (Trigger A, #243).

        Returns 'completed' or 'paused'. On any pause path the job is left
        visibly NOT completed via ``pause_and_suggest`` (status=paused with an
        actionable error_message).

        Policy:
          - validator overall != "fail" (or gate disabled) -> 'completed'
          - "fail" + already auto-escalated once            -> 'paused'
          - "fail" (first time) -> escalate flagged phases once on a stronger
            model, re-validate, then 'completed' (pass) or 'paused' (still fail
            / no stronger model available).
        """
        from api.models.job import JobUpdate

        cfg = self.llm.config.get("qa_escalation", {})
        overall = (validation_result or {}).get("overall")

        if overall != "fail" or not cfg.get("on_validation_fail", True):
            return "completed"

        job = await get_job(job_id)
        if job is not None and getattr(job, "auto_escalated_at", None) is not None:
            await pause_and_suggest(
                job_id,
                trigger="qa_fail",
                message="QA failed again after escalation — review or retry on a stronger model.",
            )
            return "paused"

        # Non-model-fixable guard (#276): if every failing flag is something a
        # stronger model cannot fix (formatter review-notes / needs_review /
        # missing media_id), skip the futile escalation pass and route straight
        # to a cheap, honest human-review pause.
        if cfg.get("skip_escalation_when_nonfixable", True):
            classed = classify_qa_failure(validation_result, context)
            if not classed["escalate"]:
                await pause_and_suggest(
                    job_id,
                    trigger="qa_review",
                    message=nonfixable_review_message(classed["nonfixable"]),
                    mark_escalated=True,
                )
                return "paused"

        # The dedicated re-validation below re-runs the validator, so exclude it
        # from the escalation loop — otherwise the validator would run up to 3x
        # and its escalated output would be immediately overwritten (#243 fix).
        phases = [p for p in select_escalation_phases(validation_result, phase_order) if p != "validator"]
        exclude = cfg.get("exclude_variants", ["fast", "fable"])
        reran = False
        # Track escalated per-phase model/cost so we can refresh the persisted
        # phases[] attribution after the gate resolves (Minor-A, #243).
        escalated_models: Dict[str, dict] = {}
        for phase_name in phases:
            current_model = self._phase_model(job, phase_name)
            target = await resolve_escalated_model(current_model, exclude)
            if target is None:
                # Already at the strongest family / catalog unavailable for this phase.
                continue
            res = await self._run_phase(job_id, phase_name, context, project_path, model_override=target)
            if res and res.get("success"):
                # Thread the escalated output back into context so the
                # re-validation below judges the ESCALATED work, not the stale
                # pre-escalation output (mirrors the main phase loop) (#243 fix).
                context[f"{phase_name}_output"] = res.get("output", "")
                escalated_models[phase_name] = {"model": res.get("model") or target, "cost": res.get("cost")}
                reran = True

        if not reran:
            await pause_and_suggest(
                job_id,
                trigger="qa_fail",
                message="QA failed and no stronger model was available — review or retry.",
                mark_escalated=True,
            )
            return "paused"

        # Re-validate once on the configured default validator model.
        reval = await self._run_phase(job_id, "validator", context, project_path)
        if reval and reval.get("success"):
            verdict = self._parse_validation_result(reval.get("output", ""))
            verdict = await self._apply_style_lint(job_id, context, verdict)
            escalated_models["validator"] = {"model": reval.get("model"), "cost": reval.get("cost")}
        else:
            verdict = {"overall": "fail"}

        # Persist the LATEST validation verdict so a completed-after-escalation job
        # no longer reports the stale pre-escalation "fail" (MUST-FIX 1, #243). Also
        # refresh per-phase model/cost attribution for the escalated re-runs
        # (Minor-A). update_job is a partial update, so the subsequent
        # pause_and_suggest (status/error/escalation marker) won't clobber this.
        refreshed = await get_job(job_id)
        phases_update = apply_escalated_phase_models(
            list(refreshed.phases) if refreshed and refreshed.phases else None,
            escalated_models,
        )
        await update_job(
            job_id,
            JobUpdate(
                validation_result=verdict,
                phases=phases_update,
                auto_escalated_at=datetime.now(timezone.utc) if verdict.get("overall") == "pass" else None,
            ),
        )

        if verdict.get("overall") == "pass":
            return "completed"

        await pause_and_suggest(
            job_id,
            trigger="qa_fail",
            message="QA failed after escalation — review or retry on a stronger model.",
            mark_escalated=True,
        )
        return "paused"

    async def _fetch_sst_context(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fetch SST metadata from Airtable if job has linked record.

        Args:
            job: Job dict with potential airtable_record_id field

        Returns:
            Dict with SST fields if found, None if no SST link or error
        """
        airtable_record_id = job.get("airtable_record_id")
        if not airtable_record_id:
            return None

        try:
            client = get_airtable_client()
            record = await client.get_sst_record(airtable_record_id)

            if not record:
                logger.warning("SST record not found", extra={"job_id": job.get("id"), "record_id": airtable_record_id})
                return None

            # Extract relevant fields for agent context
            fields = record.get("fields", {})
            sst_context = {
                "title": fields.get("Title"),
                "short_description": fields.get("Short Description"),
                "long_description": fields.get("Long Description"),
                "keywords": fields.get("Keywords"),
                "tags": fields.get("Tags"),
                "host": fields.get("Host"),
                "presenter": fields.get("Presenter"),
                "program": fields.get("Program"),
                "media_id": fields.get("Media ID"),
                "social_media_description": fields.get("Social Media Description"),
            }

            # Follow Project linked record for series-level context
            project_ids = fields.get("Project")
            if project_ids and isinstance(project_ids, list):
                try:
                    project_record = await client.get_project_record(project_ids[0])
                    if project_record:
                        project_fields = project_record.get("fields", {})
                        sst_context["project_notes"] = project_fields.get("Notes")
                        sst_context["project_description"] = project_fields.get("Project Description")
                except Exception as e:
                    logger.debug("Failed to fetch linked Project (non-fatal)", extra={"error": str(e)})

            # Auto-extract speaker names from SST text fields when Host/Presenter aren't set
            if not sst_context.get("host"):
                extracted = _extract_speakers_from_sst(sst_context)
                if extracted.get("host"):
                    sst_context["host"] = extracted["host"]
                if extracted.get("panelists") and not sst_context.get("presenter"):
                    sst_context["presenter"] = ", ".join(extracted["panelists"])

            # Remove None values
            sst_context = {k: v for k, v in sst_context.items() if v is not None}

            return sst_context if sst_context else None

        except Exception as e:
            logger.warning("Failed to fetch SST context (non-fatal)", extra={"job_id": job.get("id"), "error": str(e)})
            return None

    async def _heartbeat_loop(self, job_id: int):
        """Send periodic heartbeats while processing."""
        while True:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)
                await update_job_heartbeat(job_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Heartbeat error", extra={"job_id": job_id, "error": str(e)})

    async def _run_transcription_stage(
        self, job: Dict[str, Any], project_path: Path, phases: List[Dict[str, Any]]
    ) -> None:
        """Run the WhisperX transcription pre-stage for a media job.

        Terminal for this claim in every branch:
        - service unavailable/busy -> defer_job (requeue with backoff)
        - transcription error      -> phase failed, job failed
        - success                  -> transcription_raw.json +
          transcription_edited.json written to the project dir, phase
          completed, job -> awaiting_review for the editor.
        """
        from api.models.job import JobUpdate
        from api.services.diarization_client import DiarizationClient
        from api.services.whisper_prompt import build_initial_prompt

        job_id = job["id"]
        intake = job.get("intake") or {}
        if isinstance(intake, str):
            try:
                intake = json.loads(intake)
            except json.JSONDecodeError:
                intake = {}

        media_file = job.get("media_file") or ""
        # media_file is client-writable via PATCH /jobs/{id}; contain the
        # resolved path to MEDIA_DIR so a crafted value can't make the worker
        # ship an arbitrary readable file to the transcription service.
        media_path = (MEDIA_DIR / media_file).resolve() if media_file else None
        if media_path is not None and not media_path.is_relative_to(MEDIA_DIR.resolve()):
            media_path = None
        if media_path is None or not media_path.exists():
            message = f"[transcription] Media file not found: {media_file or '(none)'}"
            self._mark_transcription_phase(phases, "failed", error_message=message)
            await update_job_phase(job_id, phases)
            await update_job_status(job_id, JobStatus.failed, error_message=message)
            return

        client = DiarizationClient()
        try:
            if not await client.is_available():
                backoff_minutes, ceiling_hours = self._deferral_policy()
                await defer_job(
                    job_id,
                    backoff_minutes=backoff_minutes,
                    ceiling_hours=ceiling_hours,
                    detail="diarization service unavailable",
                )
                logger.info(
                    "Media job requeued — diarization service unavailable",
                    extra={"job_id": job_id, "media_file": media_file},
                )
                return

            speakers = [s for s in (intake.get("speakers") or []) if s]
            context_terms = [t for t in (intake.get("context_terms") or []) if t]
            try:
                glossary_terms = glossary_service.get_whisper_terms()
            except Exception as e:
                logger.warning(
                    "Failed to load glossary terms for prompt (continuing)",
                    extra={"job_id": job_id, "error": str(e)},
                )
                glossary_terms = []
            initial_prompt = build_initial_prompt(speakers, context_terms, glossary_terms)

            await update_job_status(job_id, JobStatus.in_progress, current_phase="transcription")
            self._mark_transcription_phase(phases, "in_progress")
            await update_job_phase(job_id, phases)

            logger.info(
                "Running transcription stage",
                extra={
                    "job_id": job_id,
                    "media_file": media_file,
                    "speakers": len(speakers),
                    "prompt_chars": len(initial_prompt),
                },
            )
            outcome = await client.transcribe(
                str(media_path),
                initial_prompt=initial_prompt,
                language=intake.get("language") or "",
                diarize=True,
                min_speakers=len(speakers) or None,
                max_speakers=(len(speakers) + 1) if speakers else None,
            )
        finally:
            await client.close()

        if outcome.status == "busy":
            backoff_minutes, ceiling_hours = self._deferral_policy()
            self._mark_transcription_phase(phases, "pending")
            await update_job_phase(job_id, phases)
            await defer_job(
                job_id,
                backoff_minutes=backoff_minutes,
                ceiling_hours=ceiling_hours,
                retry_after_s=outcome.retry_after_s,
                detail=outcome.detail or "transcription service busy",
            )
            logger.info(
                "Media job requeued — transcription service busy",
                extra={"job_id": job_id, "retry_after_s": outcome.retry_after_s},
            )
            return

        if outcome.status != "ok" or not outcome.result:
            message = f"[transcription] {outcome.detail or 'Transcription failed'}"
            self._mark_transcription_phase(phases, "failed", error_message=message)
            await update_job_phase(job_id, phases)
            await update_job_status(job_id, JobStatus.failed, error_message=message)
            logger.error("Transcription stage failed", extra={"job_id": job_id, "detail": outcome.detail})
            return

        result = outcome.result
        raw_path = project_path / "transcription_raw.json"
        raw_path.write_text(json.dumps(result, indent=2))

        # Seed the editable working copy. Undiarized results get a single
        # speaker bucket; the speaker map is pre-filled from intake order as
        # a suggestion for the review UI.
        diarized = bool(result.get("diarized"))
        segments = []
        labels_seen = []
        for seg in result.get("segments", []):
            label = seg.get("speaker") or "SPEAKER_00"
            if label not in labels_seen:
                labels_seen.append(label)
            segments.append(
                {
                    "id": seg.get("id", len(segments)),
                    "start": seg.get("start", 0.0),
                    "end": seg.get("end", 0.0),
                    "speaker": label,
                    "text": (seg.get("text") or "").strip(),
                }
            )
        speaker_map = {label: (speakers[i] if i < len(speakers) else "") for i, label in enumerate(sorted(labels_seen))}
        edited = {
            "segments": segments,
            "speaker_map": speaker_map,
            "diarized": diarized,
            "language": result.get("language"),
            "duration_seconds": result.get("duration_seconds"),
        }
        (project_path / "transcription_edited.json").write_text(json.dumps(edited, indent=2))

        word_count = sum(len(s["text"].split()) for s in segments)
        duration_seconds = result.get("duration_seconds") or 0.0
        self._mark_transcription_phase(
            phases,
            "completed",
            metadata={
                "duration_seconds": duration_seconds,
                "language": result.get("language"),
                "diarized": diarized,
                "speakers_detected": len(labels_seen),
                "segments": len(segments),
                "initial_prompt": initial_prompt,
            },
            output_path=str(raw_path),
        )
        await update_job_phase(job_id, phases)
        try:
            await update_job(
                job_id,
                JobUpdate(
                    word_count=word_count,
                    duration_minutes=round(duration_seconds / 60, 1) if duration_seconds else None,
                ),
            )
        except Exception as e:
            logger.warning(
                "Failed to persist transcription metrics (non-fatal)",
                extra={"job_id": job_id, "error": str(e)},
            )
        await update_job_status(job_id, JobStatus.awaiting_review)
        await log_event(
            EventCreate(
                job_id=job_id,
                event_type=EventType.phase_completed,
                data=EventData(
                    phase="transcription",
                    extra={"segments": len(segments), "speakers": len(labels_seen), "diarized": diarized},
                ),
            )
        )
        logger.info(
            "Transcription complete — awaiting editor review",
            extra={"job_id": job_id, "segments": len(segments), "speakers": len(labels_seen)},
        )

    @staticmethod
    def _mark_transcription_phase(
        phases: List[Dict[str, Any]],
        status: str,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        output_path: Optional[str] = None,
    ) -> None:
        """Update the transcription entry in a phases list in place."""
        now_iso = datetime.now(timezone.utc).isoformat()
        for phase in phases:
            if phase.get("name") == "transcription":
                phase["status"] = status
                if status == "in_progress":
                    phase["started_at"] = now_iso
                if status == "completed":
                    phase["completed_at"] = now_iso
                    phase["cost"] = 0
                if error_message is not None:
                    phase["error_message"] = error_message
                if metadata is not None:
                    phase["metadata"] = metadata
                if output_path is not None:
                    phase["output_path"] = output_path
                return
        # Defensive: phases list lacked a transcription entry (hand-edited job)
        phases.insert(
            0,
            {
                "name": "transcription",
                "status": status,
                "started_at": now_iso if status == "in_progress" else None,
                "completed_at": now_iso if status == "completed" else None,
                "cost": 0,
                "tokens": 0,
                "error_message": error_message,
                "metadata": metadata,
                "output_path": output_path,
            },
        )

    def _all_phases_complete(self, phases: List[Dict[str, Any]], project_path: Path) -> bool:
        """Check if all required phases are complete and output files exist.

        This is a recovery mechanism for jobs that failed after all phases
        completed (e.g., due to archiving errors). Verifies both phase status
        and actual output file existence.
        """
        if not phases:
            return False

        # Check all required phases are in the list and completed
        completed_phases = set()
        for phase in phases:
            if phase.get("status") == "completed":
                completed_phases.add(phase.get("name"))

        required_phases = set(self.PHASES)  # analyst, formatter, seo
        if not required_phases.issubset(completed_phases):
            return False

        # Verify output files actually exist
        for phase_name in required_phases:
            output_file = project_path / f"{phase_name}_output.md"
            if not output_file.exists():
                return False

        return True

    def _setup_project_dir(self, job: Dict[str, Any]) -> Path:
        """Create and return the project output directory."""
        project_name = job.get("project_name", f"job_{job['id']}")
        # Sanitize project name for filesystem
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_name)

        project_path = OUTPUT_DIR / safe_name
        project_path.mkdir(parents=True, exist_ok=True)

        return project_path

    def _load_transcript(self, job: Dict[str, Any]) -> str:
        """Load the transcript file content.

        Handles various encodings (UTF-8, ISO-8859, etc.) gracefully.
        Also checks the archive folder as a fallback for re-processed jobs.
        """
        transcript_file = job.get("transcript_file", "")

        # Try various paths, including archive folder as fallback
        paths_to_try = [
            Path(transcript_file),
            TRANSCRIPTS_DIR / transcript_file,
            TRANSCRIPTS_DIR / Path(transcript_file).name,
            # Archive folder fallback for re-processed jobs
            TRANSCRIPTS_ARCHIVE_DIR / transcript_file,
            TRANSCRIPTS_ARCHIVE_DIR / Path(transcript_file).name,
        ]

        for path in paths_to_try:
            if path.exists():
                # Log if we're using archive fallback
                if TRANSCRIPTS_ARCHIVE_DIR in path.parents or path.parent == TRANSCRIPTS_ARCHIVE_DIR:
                    logger.info(
                        "Using archived transcript (re-processing)",
                        extra={"job_id": job.get("id"), "source": str(path)},
                    )
                # Try different encodings
                for encoding in ["utf-8", "iso-8859-1", "cp1252", "latin-1"]:
                    try:
                        return path.read_text(encoding=encoding)
                    except UnicodeDecodeError:
                        continue
                # Last resort: read with errors='replace'
                return path.read_text(encoding="utf-8", errors="replace")

        raise FileNotFoundError(f"Transcript not found: {transcript_file}")

    def _archive_transcript(self, job: Dict[str, Any]) -> None:
        """Move completed transcript to archive folder.

        Archives the original transcript file to transcripts/archive/ after
        successful job completion. This keeps the main transcripts folder
        clean and shows only unprocessed files.
        """
        transcript_file = job.get("transcript_file", "")
        if not transcript_file:
            return

        # Find the source file
        source_paths = [
            Path(transcript_file),
            TRANSCRIPTS_DIR / transcript_file,
            TRANSCRIPTS_DIR / Path(transcript_file).name,
        ]

        source = None
        for path in source_paths:
            if path.exists():
                source = path
                break

        if not source:
            logger.warning("Transcript not found for archiving", extra={"source_file": transcript_file})
            return

        # Create archive directory if needed
        TRANSCRIPTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        # Move to archive
        dest = TRANSCRIPTS_ARCHIVE_DIR / source.name
        try:
            import shutil

            shutil.move(str(source), str(dest))
            logger.info("Archived transcript", extra={"source_file": source.name, "destination": str(dest)})
        except Exception as e:
            logger.error(
                "Failed to archive transcript",
                extra={"source_file": source.name, "error": str(e)},
                exc_info=True,
            )

    def _find_srt_file(self, job: Dict[str, Any]) -> Optional[Path]:
        """Find the SRT file associated with a transcript.

        Looks for an SRT file with the same base name as the transcript
        in both the transcripts folder and archive folder.

        Returns:
            Path to SRT file if found, None otherwise.
        """
        transcript_file = job.get("transcript_file", "")
        if not transcript_file:
            return None

        # Get the base name without extension
        from api.services.utils import extract_media_id

        media_id = extract_media_id(transcript_file)
        if not media_id:
            return None

        # Look for SRT file with same base name
        search_dirs = [TRANSCRIPTS_DIR, TRANSCRIPTS_ARCHIVE_DIR]

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue

            # Try exact match first
            srt_path = search_dir / f"{media_id}.srt"
            if srt_path.exists():
                return srt_path

            # Try variations
            for srt_file in search_dir.glob("*.srt"):
                srt_media_id = extract_media_id(srt_file.name)
                if srt_media_id == media_id:
                    return srt_file

        return None

    # Media file extensions for diarization, in preference order
    MEDIA_EXTENSIONS = [".mp4", ".mkv", ".mov", ".webm", ".wav", ".mp3", ".m4a", ".flac"]

    def _find_media_file(self, job: Dict[str, Any], search_dirs: Optional[List[Path]] = None) -> Optional[Path]:
        """Find an audio/video file matching the job's media ID.

        Searches the transcripts directory (and any extra search_dirs) for files
        whose name starts with the media ID and has a recognized media extension.
        Returns the first match in MEDIA_EXTENSIONS preference order (video first).

        Args:
            job: Job dict with media_id and transcript_file fields.
            search_dirs: Override search directories (for testing). Defaults to TRANSCRIPTS_DIR.

        Returns:
            Path to the media file, or None if not found.
        """
        media_id = job.get("media_id")
        if not media_id:
            return None

        dirs_to_search = search_dirs or [Path(os.environ.get("TRANSCRIPTS_DIR", "transcripts"))]

        for ext in self.MEDIA_EXTENSIONS:
            for search_dir in dirs_to_search:
                candidate = search_dir / f"{media_id}{ext}"
                if candidate.exists():
                    return candidate

        return None

    def _get_content_duration_minutes(
        self, transcript_metrics: Dict[str, Any], srt_path: Optional[Path] = None
    ) -> float:
        """Get content duration in minutes from SRT or transcript metrics.

        Prefers SRT duration (more accurate) if available, falls back
        to estimated duration from transcript word count.

        Returns:
            Duration in minutes.
        """
        # Try to get duration from SRT file (most accurate)
        if srt_path and srt_path.exists():
            try:
                from api.services.utils import get_srt_duration, parse_srt

                srt_content = srt_path.read_text(encoding="utf-8", errors="replace")
                captions = parse_srt(srt_content)
                if captions:
                    duration_ms = get_srt_duration(captions)
                    return duration_ms / 60000  # Convert ms to minutes
            except Exception as e:
                logger.warning("Failed to parse SRT for duration", extra={"srt_file": str(srt_path), "error": str(e)})

        # Fall back to transcript metrics (estimated from word count)
        return transcript_metrics.get("estimated_duration_minutes", 0)

    def _resolve_duration_into_metrics(
        self, transcript_metrics: Dict[str, Any], srt_path: Optional[Path]
    ) -> Dict[str, Any]:
        """Override the word-count duration estimate with the SRT-parsed value.

        The five duration consumers (tier routing, prompt context, the persisted
        ``duration_minutes`` column, SEO/manager prompts) all read
        ``transcript_metrics["estimated_duration_minutes"]``. When an SRT is
        available it is far more accurate than the word-count estimate, so write
        the best-available duration back into the metrics here, once, so every
        consumer agrees (#126). No-op when no SRT is found.
        """
        best = self._get_content_duration_minutes(transcript_metrics, srt_path)
        if best:
            transcript_metrics["estimated_duration_minutes"] = best
        return transcript_metrics

    def _detect_content_type(
        self,
        transcript_metrics: Dict[str, Any],
        srt_path: Optional[Path],
        sst_context: Optional[Dict[str, Any]],
    ) -> str:
        """Detect the content type based on duration and SST metadata.

        Returns one of: 'full', 'short', 'clip'.

        Detection logic:
        - If duration < 90 seconds (1.5 minutes) -> 'short'
        - If SST context indicates Clip content type -> 'clip'
        - Otherwise -> 'full'
        """
        # Check SST context for explicit clip classification first.
        # Looks for Airtable picker-style fields like "Full-Length, Clip, Livestream"
        # that contain "Clip" as a selected value.
        if sst_context:
            for field_value in sst_context.values():
                if isinstance(field_value, str) and "Clip" in field_value:
                    return "clip"

        # Check duration threshold for Shorts.
        # 90-second threshold is intentionally higher than YouTube's 60s limit
        # to catch near-Shorts content.
        duration_minutes = self._get_content_duration_minutes(transcript_metrics, srt_path)
        if 0 < duration_minutes < 1.5:
            return "short"

        return "full"

    def _should_run_timestamp_phase(
        self, job: Dict[str, Any], transcript_metrics: Dict[str, Any], srt_path: Optional[Path]
    ) -> bool:
        """Determine if the timestamp phase should run.

        Timestamp phase runs when:
        1. An SRT file exists AND
        2. Content is not a Short (content_type != 'short') AND
        3. Content is 10+ minutes OR job explicitly requests it

        Returns:
            True if timestamp phase should run, False otherwise.
        """
        # No SRT file = no timestamp phase
        if not srt_path or not srt_path.exists():
            logger.debug("Skipping timestamp phase: no SRT file", extra={"job_id": job.get("id")})
            return False

        # Shorts never get timestamp phase — too short to need chapter markers
        content_type = job.get("content_type")
        if content_type == "short":
            logger.info(
                "Skipping timestamp phase: Short content does not need chapter markers",
                extra={"job_id": job.get("id")},
            )
            return False

        # Check for explicit request in job
        include_timestamps = job.get("include_timestamps", False)
        if include_timestamps:
            logger.info("Timestamp phase enabled by request", extra={"job_id": job.get("id")})
            return True

        # Check duration threshold
        duration_minutes = self._get_content_duration_minutes(transcript_metrics, srt_path)
        if duration_minutes >= self.TIMESTAMP_AUTO_THRESHOLD_MINUTES:
            logger.info(
                "Timestamp phase auto-triggered for long content",
                extra={
                    "job_id": job.get("id"),
                    "duration_minutes": round(duration_minutes, 1),
                    "threshold": self.TIMESTAMP_AUTO_THRESHOLD_MINUTES,
                },
            )
            return True

        logger.debug(
            "Skipping timestamp phase: below duration threshold",
            extra={
                "job_id": job.get("id"),
                "duration_minutes": round(duration_minutes, 1),
                "threshold": self.TIMESTAMP_AUTO_THRESHOLD_MINUTES,
            },
        )
        return False

    def _deferral_policy(self) -> tuple[list, float]:
        """Backoff schedule (minutes) and give-up ceiling (hours) for deferring a
        job when a local backend is busy. Read from routing.deferral, with
        sensible defaults so the feature works even without explicit config."""
        cfg = self.llm.config.get("routing", {}).get("deferral", {})
        backoff_minutes = cfg.get("backoff_minutes") or [2, 5, 10, 15]
        ceiling_hours = cfg.get("ceiling_hours", 6)
        return backoff_minutes, ceiling_hours

    async def _pause_for_credit(self, job_id: int) -> None:
        """Terminal pause for OpenRouter credit exhaustion (Trigger B, #243).

        Routes through the shared ``pause_and_suggest`` helper (status=paused,
        actionable message, retry count untouched — credit problems must never
        consume a retry) then records the attempt's cost without clobbering the
        pause reason. Mirrors the truncation and QA-gate paused terminal paths.
        """
        await pause_and_suggest(
            job_id,
            trigger="credit",
            message="OpenRouter credit exhausted — add credit, then retry.",
        )
        run_summary = await end_run_tracking(job_id)
        if run_summary:
            await update_job_status(
                job_id,
                JobStatus.paused,
                actual_cost=run_summary["total_cost"],
            )

    async def _run_phase(
        self,
        job_id: int,
        phase_name: str,
        context: Dict[str, Any],
        project_path: Path,
        model_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a single agent phase.

        Args:
            job_id: Job ID for tracking
            phase_name: Phase to run
            context: Context dict with transcript, outputs, etc.
            project_path: Path to project output directory
            model_override: Optional model ID to override the phase default
        """
        # Normalize live-caption speaker markers before formatting: split any
        # caption with an interior ">>" so the formatter sees one turn per
        # caption (issue #269 — fixes mid-cue turn-order inversion and speaker
        # misattribution). Word-preserving, so downstream checks are unaffected.
        if phase_name == "formatter":
            seg_config = self.llm.config.get("routing", {}).get("speaker_segmentation", {})
            transcript_file = context.get("transcript_file", "")
            if seg_config.get("enabled", True) and transcript_file.lower().endswith(".srt"):
                from api.services.speaker_segmentation import split_interior_speaker_changes

                context["transcript"] = split_interior_speaker_changes(context.get("transcript", ""))

        # Style-engine pre-generation hook (kill-switched by default; see
        # routing.style_engine in config/llm-config.json). Placed ahead of the
        # chunking branch so a chunked formatter path would inherit it once
        # that phase is wired (later task). Stale-state guard is unconditional
        # so a prior phase's style_pre never leaks into this phase's prompt.
        context.pop("style_pre", None)
        style_cfg = self._style_cfg()
        if style_cfg.get("enabled") and style_cfg.get("phases", {}).get(phase_name, {}).get("pre"):
            try:
                from api.services.style_engine import load_rules, run_pre_stage

                pre = run_pre_stage(
                    phase_name,
                    context,
                    load_rules(style_cfg.get("rules_file", "config/house_style.yaml")),
                )
                if pre.prompt_section:
                    context["style_pre"] = {"prompt_section": pre.prompt_section, **pre.data}
            except Exception:
                logger.warning(
                    "style pre-stage failed open",
                    extra={"job_id": job_id, "phase": phase_name},
                    exc_info=True,
                )

        # Check for chunked formatter processing
        if phase_name == "formatter":
            chunking_config = self.llm.config.get("routing", {}).get("chunking", {})
            if chunking_config.get("enabled", False):
                from api.services.chunking import split_transcript

                transcript_file = context.get("transcript_file", "")
                is_srt = transcript_file.lower().endswith(".srt")
                chunks = split_transcript(
                    context.get("transcript", ""),
                    is_srt=is_srt,
                    config=chunking_config,
                )
                if chunks is not None:
                    logger.info(
                        "Formatter using chunked processing",
                        extra={
                            "job_id": job_id,
                            "chunk_count": len(chunks),
                        },
                    )
                    return await self._run_formatter_chunked(
                        job_id=job_id,
                        chunks=chunks,
                        context=context,
                        project_path=project_path,
                        chunking_config=chunking_config,
                        model_override=model_override,
                    )

        # Load prompts
        system_prompt = self._load_agent_prompt(phase_name)
        user_message = self._build_phase_prompt(phase_name, context)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Get backend for this phase
        backend = self.llm.get_backend_for_phase(phase_name)
        logger.info(
            "Running phase",
            extra={
                "job_id": job_id,
                "phase": phase_name,
                "backend": backend,
            },
        )

        # Log phase started
        await log_event(
            EventCreate(
                job_id=job_id,
                event_type=EventType.phase_started,
                data=EventData(
                    phase=phase_name,
                    backend=backend,
                ),
            )
        )

        try:
            # Get timeout from backend config
            try:
                backend_config = self.llm.get_backend_config(backend)
                effective_timeout = backend_config.get("timeout", 120)
                if not isinstance(effective_timeout, (int, float)):
                    effective_timeout = 120
            except Exception:
                effective_timeout = 120

            # Call LLM with timeout
            response: LLMResponse = await asyncio.wait_for(
                self.llm.chat(
                    messages=messages,
                    backend=backend,
                    model=model_override,
                    job_id=job_id,
                    phase=phase_name,
                ),
                timeout=effective_timeout,
            )

            # Style-engine post-generation hook (kill-switched by default; see
            # routing.style_engine in config/llm-config.json). Shadow mode
            # records checks/events but returns the raw content unchanged;
            # enforce mode returns normalized content plus the full result so
            # the persist step below can write provenance + a pre-fix raw
            # archive. Off (default) returns (response.content, None) as a
            # pure passthrough.
            final_content, style_post = await self._apply_style_post(
                job_id, phase_name, response.content, context, project_path
            )

            # Save output
            output_file = project_path / f"{phase_name}_output.md"
            if output_file.exists():
                prev_content = output_file.read_text()
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                prev_file = project_path / f"{phase_name}_output.{timestamp}.prev.md"
                prev_file.write_text(prev_content)
                logger.info(
                    "Preserved previous output",
                    extra={"job_id": job_id, "phase": phase_name, "preserved_as": prev_file.name},
                )

            # Add provenance header
            provenance_header = (
                f"<!-- model: {response.model} | cost: ${response.cost:.4f} | tokens: {response.total_tokens} -->\n"
            )
            style_line = ""
            if style_post is not None:
                style_line = (
                    f"<!-- style-engine: fixes: {len(style_post.check.fixes)} | "
                    f"flags: {len(style_post.check.violations)} -->\n"
                )
            output_file.write_text(provenance_header + style_line + final_content)

            # Log phase completed
            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.phase_completed,
                    data=EventData(
                        phase=phase_name,
                        cost=response.cost,
                        tokens=response.total_tokens,
                        model=response.model,
                    ),
                )
            )

            return {
                "success": True,
                "output": final_content,
                "cost": response.cost,
                "tokens": response.total_tokens,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "model": response.model,
            }

        except CreditExhaustedError as e:
            # OpenRouter has no credit/quota left (Trigger B, #243). This is NOT a
            # generic phase failure — caught BEFORE the generic ``except Exception``
            # below so it isn't flattened into ``{"success": False, "error": ...}``
            # (which would mark the job failed and consume a retry). Tag the result
            # so the pipeline can route it to pause-and-suggest instead.
            logger.warning(
                "Phase halted — OpenRouter credit exhausted",
                extra={"job_id": job_id, "phase": phase_name, "backend": e.backend, "detail": e.detail},
            )
            return {
                "success": False,
                "credit_exhausted": True,
                "error": e.detail,
                "cost": 0,
                "tokens": 0,
            }

        except BackendUnavailableError as e:
            # The backend is temporarily busy (memory pressure / loading / contention),
            # not broken. Signal the pipeline to requeue rather than fail the job.
            logger.info(
                "Phase deferred — backend unavailable",
                extra={"job_id": job_id, "phase": phase_name, "backend": e.backend, "detail": e.detail},
            )
            return {
                "success": False,
                "deferred": True,
                "detail": e.detail,
                "retry_after_s": e.retry_after_s,
                "cost": 0,
                "tokens": 0,
            }

        except asyncio.TimeoutError:
            error_msg = f"Phase {phase_name} timed out after {effective_timeout}s"
            logger.error(error_msg, extra={"job_id": job_id, "phase": phase_name})
            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.phase_failed,
                    data=EventData(phase=phase_name, extra={"error": error_msg}),
                )
            )
            return {"success": False, "error": error_msg, "cost": 0, "tokens": 0}

        except Exception as e:
            error_msg = f"Phase {phase_name} failed: {e}"
            logger.error(error_msg, extra={"job_id": job_id, "phase": phase_name}, exc_info=True)
            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.phase_failed,
                    data=EventData(phase=phase_name, extra={"error": str(e)}),
                )
            )
            return {"success": False, "error": str(e), "cost": 0, "tokens": 0}

    def _style_cfg(self) -> dict:
        """The `routing.style_engine` config block (empty dict when absent).

        Absent block, or `enabled: false`, or a phase mode of "off" is the
        kill switch: every hook that reads this must degrade to a no-op.
        """
        return self.llm.config.get("routing", {}).get("style_engine", {}) or {}

    async def _apply_style_post(
        self,
        job_id: int,
        phase_name: str,
        content: str,
        context: Dict[str, Any],
        project_path: Path,
    ) -> Tuple[str, Optional[PostStageResult]]:
        """Run the style-engine post-generation stage for one phase's raw output.

        Returns ``(final_content, PostStageResult | None)``. Fail-open on any
        exception (rules load failure, engine bug, event-logging failure) --
        logs a warning and returns the original ``content`` unchanged, exactly
        as if the hook were off. The engine can never fail a job.

        Shadow mode records ``style_violation`` events but returns the raw
        ``content`` untouched -- it deliberately does NOT set
        ``context["style_checks"]`` (that write is enforce-mode-only; see the
        shadow early-return, PR #295 review #4). The ``PostStageResult`` is
        discarded -- callers use the ``None`` sentinel to know no
        provenance/raw-archive handling is needed. Enforce mode returns the
        normalized output and the full
        result so the caller can persist provenance + the pre-normalization
        raw file -- and additionally logs one ``style_violation`` event per
        ``AppliedFix`` (``action: "fixed"``), the feedback loop's signal for
        which deterministic fixes are actually firing in production (see
        ``docs/STYLE_FEEDBACK_LOOP.md``).
        """
        cfg = self._style_cfg()
        mode = cfg.get("phases", {}).get(phase_name, {}).get("post", "off")
        if not (cfg.get("enabled") and mode in ("shadow", "enforce")):
            return content, None
        try:
            from api.services.style_engine import load_rules, run_post_stage

            post = run_post_stage(
                phase_name,
                content,
                context,
                load_rules(cfg.get("rules_file", "config/house_style.yaml")),
            )
            for violation in post.check.violations:
                await log_event(
                    EventCreate(
                        job_id=job_id,
                        event_type=EventType.style_violation,
                        data=EventData(
                            phase=phase_name,
                            extra={
                                **violation.to_dict(),
                                "mode": mode,
                                "action": "flagged" if mode == "enforce" else "shadow",
                            },
                        ),
                    )
                )

            if mode == "shadow":
                # Record-only: raw content flows AND no downstream state changes.
                # Deliberately does NOT set context["style_checks"] -- that key
                # feeds the validator pre-stage's "deterministic checks already
                # performed" section, so populating it in shadow would alter the
                # validator prompt for checks that were never enforced (PR #295
                # review #4). The style_violation events above are shadow's record.
                return content, None

            # Enforce mode only, past this point. Record the check for the
            # validator pre-stage + lint merge to consume, then log one
            # style_violation event per deterministic AppliedFix so "the model
            # keeps getting X wrong (auto-fixed N times)" is visible to the
            # feedback loop (scripts/style_report.py), not just the
            # provenance comment in the persisted output file. Mirrors the
            # violations loop above exactly (same EventCreate/EventData
            # shape) and stays inside this method's fail-open try.
            context.setdefault("style_checks", {})[phase_name] = post.check.to_dict()
            for fix in post.check.fixes:
                await log_event(
                    EventCreate(
                        job_id=job_id,
                        event_type=EventType.style_violation,
                        data=EventData(
                            phase=phase_name,
                            extra={**fix.to_dict(), "mode": mode, "action": "fixed"},
                        ),
                    )
                )

            if post.changed and cfg.get("keep_raw_on_fix", True):
                (project_path / f"{phase_name}_output.raw.md").write_text(content)

            return post.normalized_output, post
        except Exception:
            logger.warning(
                "style post-stage failed open",
                extra={"job_id": job_id, "phase": phase_name},
                exc_info=True,
            )
            # Clean up partial style_checks entry on fail-open. If log_event
            # raised mid-loop, the already-written context["style_checks"][phase]
            # entry is incomplete; remove it so the QA-gate merge doesn't consume
            # a partial event trail as if the phase checked completely.
            checks = context.get("style_checks")
            if isinstance(checks, dict):
                checks.pop(phase_name, None)
            return content, None

    async def _apply_style_lint(
        self, job_id: int, context: Dict[str, Any], validation_data: Optional[dict]
    ) -> Optional[dict]:
        """Run the deterministic lint suite and merge style flags into the validator verdict.

        Returns the (possibly merged) ``validation_data``. Fail-open: any
        exception logs a warning and returns ``validation_data`` unchanged,
        exactly as if the hook were off.

        Shadow mode records ``context["lint_checks"]`` and logs
        ``style_violation`` events (``source: "lint"``) but returns
        ``validation_data`` untouched. Enforce mode combines each canonical
        phase's post-stage ``context["style_checks"]`` entry (if any, from
        ``_apply_style_post``) with its ``run_lint`` result -- post-stage
        violations first, lint violations appended after, deduped by
        ``(rule_id, message)`` -- and merges the combined result into
        ``validation_data`` via ``merge_style_flags`` so BOTH deterministic
        sources (not lint alone) reach the persisted QA verdict.
        """
        cfg = self._style_cfg()
        mode = cfg.get("phases", {}).get("validator", {}).get("lint", "off")
        if not (cfg.get("enabled") and mode in ("shadow", "enforce")):
            return validation_data
        try:
            from api.services.style_engine import load_rules, merge_style_flags, run_lint

            rules = load_rules(cfg.get("rules_file", "config/house_style.yaml"))
            lint_results = run_lint(context, rules)  # {phase: PhaseCheckResult}

            # Combine: post-stage style_checks (context) + lint results, per
            # phase -- post-stage violations first, lint appended after,
            # deduped by (rule_id, message) within a phase.
            post_checks = context.get("style_checks") or {}
            combined: Dict[str, dict] = {}
            for phase, lint_check in lint_results.items():
                lint_dict = lint_check.to_dict()
                post_violations = list((post_checks.get(phase) or {}).get("violations") or [])
                seen = {(v.get("rule_id"), v.get("message")) for v in post_violations}
                merged_violations = list(post_violations)
                for violation in lint_dict.get("violations") or []:
                    key = (violation.get("rule_id"), violation.get("message"))
                    if key in seen:
                        continue
                    seen.add(key)
                    merged_violations.append(violation)
                combined[phase] = {**lint_dict, "violations": merged_violations}

            for phase, check in lint_results.items():
                for violation in check.violations:
                    await log_event(
                        EventCreate(
                            job_id=job_id,
                            event_type=EventType.style_violation,
                            data=EventData(
                                phase=phase,
                                extra={**violation.to_dict(), "source": "lint", "mode": mode},
                            ),
                        )
                    )

            context["lint_checks"] = {p: c.to_dict() for p, c in lint_results.items()}

            if mode == "shadow":
                return validation_data  # record-only

            return merge_style_flags(validation_data, combined, cfg.get("qa_gate", {}))
        except Exception:
            logger.warning("style lint failed open", extra={"job_id": job_id}, exc_info=True)
            context.pop("lint_checks", None)  # no partial state (1b precedent)
            return validation_data

    @staticmethod
    def _section_tail(content: str, max_chars: int = 200) -> str:
        """Last line of a chunk's content, used as a 'format through to here'
        anchor. Handles SRT (last caption text) and plain text (last line)."""
        from api.services.utils import parse_srt

        caps = parse_srt(content)
        if caps:
            text = " ".join(caps[-1].text.split())
        else:
            lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
            text = lines[-1] if lines else ""
        return text[:max_chars]

    async def _run_formatter_chunked(
        self,
        job_id: int,
        chunks: list,
        context: Dict[str, Any],
        project_path: Path,
        chunking_config: Dict[str, Any],
        model_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run formatter phase with chunked parallel processing.

        Splits long transcripts into chunks, processes them concurrently
        with a semaphore for backpressure, then merges results.
        """
        from api.services.chunking import TranscriptChunk, merge_formatter_chunks

        max_parallel = chunking_config.get("max_parallel", 3)
        semaphore = asyncio.Semaphore(max_parallel)

        # Load system prompt once
        system_prompt = self._load_agent_prompt("formatter")
        analysis = context.get("analyst_output", "")
        sst_context = context.get("sst_context")

        # Load glossary for chunked formatter (mirrors _build_phase_prompt_base)
        glossary_section = ""
        glossary_path = KNOWLEDGE_DIR / "glossary.md"
        if glossary_path.exists():
            glossary_section = f"\n## Transcript Glossary\n\n{glossary_path.read_text()}\n\n"

        # Build SST section (reuse logic from _build_phase_prompt)
        sst_section = ""
        if sst_context:
            sst_section = "\n## Single Source of Truth (SST) Context\n\n"
            for key in [
                "title",
                "program",
                "short_description",
                "long_description",
                "host",
                "presenter",
                "keywords",
                "tags",
                "social_media_description",
                "project_notes",
            ]:
                if sst_context.get(key):
                    sst_section += f"**{key.replace('_', ' ').title()}:** {sst_context[key]}\n"

        # Get backend for this phase
        backend = self.llm.get_backend_for_phase("formatter")

        # Get timeout from backend config
        try:
            backend_config = self.llm.get_backend_config(backend)
            effective_timeout = backend_config.get("timeout", 120)
            if not isinstance(effective_timeout, (int, float)):
                effective_timeout = 120
        except Exception:
            effective_timeout = 120

        total_chunks = len(chunks)

        async def process_chunk(chunk: TranscriptChunk) -> str:
            """Process a single chunk through the formatter LLM."""
            async with semaphore:
                # Verbatim preservation instruction (applies to all chunks)
                verbatim_instruction = """CRITICAL: You MUST preserve ALL spoken dialogue VERBATIM. Do NOT summarize, condense, paraphrase, or reword.
Every sentence spoken in the transcript must appear in your output using the speaker's actual words.
You may remove filler words (um, uh) and fix grammar/punctuation, but do NOT rephrase, rewrite, or generate new copy.
If the speaker said it, those exact words must appear in the output. Do NOT substitute your own phrasing.
If a caption is garbled or unclear, include your best reconstruction rather than dropping it. NEVER silently omit content.
SPELLING: Always use "partisan" (not "partizan"), "bipartisan" (not "bipartisan"). Program names like "Inside Wisconsin Politics" are NOT italicized."""

                # Coverage mandate + tail anchor: the model must format the whole
                # section through to its last line. On job 12 chunk 1 silently
                # dropped ~96s from the middle of its section (#269); it knew where
                # the previous section ended (overlap) but had no target for where
                # its own section must reach.
                section_tail = self._section_tail(chunk.content)
                coverage_mandate = (
                    "COVERAGE MANDATE: Format EVERY line of the section below, in order, from its "
                    "first line through to its last. Do NOT skip, summarize, or jump over any "
                    "portion of the section.\n"
                )
                tail_anchor = (
                    "This section ENDS with the following line — your formatted output MUST reach "
                    f"and include it (do not stop before it):\n---\n{section_tail}\n---\n"
                    if section_tail
                    else ""
                )

                if chunk.index == 0:
                    # First chunk: normal formatter prompt (generates the metadata header).
                    user_message = f"{verbatim_instruction}\n\n"
                    if total_chunks > 1:
                        user_message += coverage_mandate + tail_anchor + "\n"
                    if total_chunks > 1:
                        # Chunk 0 only sees the first slice of a long transcript. Without
                        # this it concludes the transcript is truncated and emits false
                        # "incomplete / needs_review" review notes that survive the merge
                        # and trip the validator into a spurious QA failure (job 12).
                        user_message += (
                            f"IMPORTANT: This is section 1 of {total_chunks} of a long transcript "
                            "being processed in parts. Later sections cover the rest of the "
                            "transcript. Your section legitimately ends partway through — do NOT "
                            "assess overall transcript completeness, do NOT claim the transcript "
                            "is truncated, incomplete, or cut off, and do NOT set a 'needs_review' "
                            "status on that basis. Format only the dialogue in this section.\n\n"
                        )
                    user_message += "Using the following analysis as guidance:\n\n"
                    if sst_section:
                        user_message += sst_section
                    if glossary_section:
                        user_message += glossary_section
                    user_message += f"---\n{analysis}\n---\n\n"
                    user_message += f"Please format this transcript:\n\n---\n{chunk.content}\n---"
                else:
                    # Continuation chunks: skip header, start with dialogue
                    user_message = f"""{verbatim_instruction}
{glossary_section}
IMPORTANT: This is section {chunk.index + 1} of {total_chunks} of a long transcript being processed in parts.
DO NOT generate the metadata header (Project, Program, Duration, Date).
DO NOT generate "# Formatted Transcript" heading.
Begin directly with speaker attribution and dialogue.
The previous section ended with:
---
{chunk.overlap_prefix}
---
Continue formatting from where the previous section left off. Do NOT repeat content from the overlap above.
CRITICAL: Pay careful attention to which speaker is talking. Use the overlap above to identify who was speaking last and maintain correct attribution. Getting the wrong name on a statement is worse than using a generic label.

Using the following analysis as guidance:
---
{analysis}
---

{coverage_mandate}{tail_anchor}
Please format this transcript section:

---
{chunk.content}
---"""

                # Append the style-engine pre-generation prompt section (if the
                # kill-switched hook in `_run_phase` produced one before the
                # chunking branch ran). Every chunk is an independent LLM call,
                # so each one needs the rules -- mirrors `_build_phase_prompt`'s
                # append for the unchunked path.
                style_pre = context.get("style_pre") or {}
                style_section = style_pre.get("prompt_section")
                if style_section:
                    user_message = f"{user_message}\n\n{style_section}"

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]

                response = await asyncio.wait_for(
                    self.llm.chat(
                        messages=messages,
                        backend=backend,
                        job_id=job_id,
                        phase="formatter",
                        model=model_override,
                    ),
                    timeout=effective_timeout,
                )

                logger.info(
                    "Chunk processed",
                    extra={
                        "job_id": job_id,
                        "chunk_index": chunk.index,
                        "total_chunks": total_chunks,
                        "cost": response.cost,
                        "tokens": response.total_tokens,
                    },
                )

                return {
                    "content": response.content,
                    "cost": response.cost,
                    "tokens": response.total_tokens,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "model": response.model,
                }

        # Log phase start
        await log_event(
            EventCreate(
                job_id=job_id,
                event_type=EventType.phase_started,
                data=EventData(
                    phase="formatter",
                    backend=backend,
                    extra={
                        "chunked": True,
                        "chunk_count": total_chunks,
                    },
                ),
            )
        )

        try:
            # Run all chunks concurrently (semaphore limits parallelism)
            chunk_results = await asyncio.gather(
                *(process_chunk(chunk) for chunk in chunks),
                return_exceptions=True,
            )

            # Credit exhaustion anywhere in the batch must pause the job (Trigger B,
            # #243), not fail it. Detect it BEFORE the generic exception flatten below,
            # and return the same credit-tagged dict ``_run_phase`` returns for the
            # non-chunked case so the main loop routes it through ``_pause_for_credit``.
            credit_error = next(
                (r for r in chunk_results if isinstance(r, CreditExhaustedError)),
                None,
            )
            if credit_error is not None:
                # Sum cost from any chunks that completed before the batch hit the wall.
                partial_cost = sum(r["cost"] for r in chunk_results if not isinstance(r, Exception))
                logger.warning(
                    "Chunked formatter halted — OpenRouter credit exhausted",
                    extra={
                        "job_id": job_id,
                        "backend": credit_error.backend,
                        "detail": credit_error.detail,
                    },
                )
                await log_event(
                    EventCreate(
                        job_id=job_id,
                        event_type=EventType.phase_failed,
                        data=EventData(
                            phase="formatter",
                            extra={"error": credit_error.detail, "credit_exhausted": True},
                        ),
                    )
                )
                return {
                    "success": False,
                    "credit_exhausted": True,
                    "error": credit_error.detail,
                    "cost": partial_cost,
                    "tokens": 0,
                }

            # Check for failures
            for i, result in enumerate(chunk_results):
                if isinstance(result, Exception):
                    error_msg = f"Chunk {i} failed: {result}"
                    logger.error(error_msg, extra={"job_id": job_id, "chunk_index": i})
                    await log_event(
                        EventCreate(
                            job_id=job_id,
                            event_type=EventType.phase_failed,
                            data=EventData(
                                phase="formatter",
                                extra={"error": error_msg, "chunk_index": i},
                            ),
                        )
                    )
                    return {
                        "success": False,
                        "error": error_msg,
                        "attempts": 1,
                        "cost": 0,
                    }

            # Aggregate cost/tokens from all chunks
            total_cost = sum(r["cost"] for r in chunk_results)
            total_tokens = sum(r["tokens"] for r in chunk_results)
            total_input_tokens = sum(r.get("input_tokens", 0) for r in chunk_results)
            total_output_tokens = sum(r.get("output_tokens", 0) for r in chunk_results)
            actual_model = next((r.get("model") for r in chunk_results if r.get("model")), None)

            # Merge text outputs
            merged = merge_formatter_chunks([r["content"] for r in chunk_results])

            # Style-engine post-generation hook, run ONCE on the merged output
            # (not per-chunk -- see `_apply_style_post` for shadow/enforce/
            # fail-open semantics; kill-switched by default). Mirrors the
            # `_run_phase` persist site exactly, including the `.raw.md`
            # pre-normalization archive, which `_apply_style_post` handles
            # internally.
            final_content, style_post = await self._apply_style_post(job_id, "formatter", merged, context, project_path)

            # Save merged output
            output_file = project_path / "formatter_output.md"
            if output_file.exists():
                prev_content = output_file.read_text()
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                prev_file = project_path / f"formatter_output.{timestamp}.prev.md"
                prev_file.write_text(prev_content)

            provenance_header = (
                f"<!-- model: {actual_model} (chunked, {total_chunks} chunks) | "
                f"backend: {backend} | "
                f"cost: ${total_cost:.4f} | tokens: {total_tokens} -->\n"
            )
            style_line = ""
            if style_post is not None:
                style_line = (
                    f"<!-- style-engine: fixes: {len(style_post.check.fixes)} | "
                    f"flags: {len(style_post.check.violations)} -->\n"
                )
            output_file.write_text(provenance_header + style_line + final_content)

            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.phase_completed,
                    data=EventData(
                        phase="formatter",
                        cost=total_cost,
                        tokens=total_tokens,
                        extra={
                            "chunked": True,
                            "chunk_count": total_chunks,
                        },
                    ),
                )
            )

            return {
                "success": True,
                "output": final_content,
                "cost": total_cost,
                "tokens": total_tokens,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "model": actual_model or f"chunked ({total_chunks} chunks via {backend})",
            }

        except Exception as e:
            error_msg = f"Chunked formatter failed: {e}"
            logger.error(error_msg, extra={"job_id": job_id}, exc_info=True)
            await log_event(
                EventCreate(
                    job_id=job_id,
                    event_type=EventType.phase_failed,
                    data=EventData(
                        phase="formatter",
                        extra={"error": error_msg, "chunked": True},
                    ),
                )
            )
            return {
                "success": False,
                "error": error_msg,
                "attempts": 1,
                "cost": 0,
            }

    def _style_prompt_profile(self, phase_name: str) -> str:
        """Which prompt-block profile ("full" or "slim") to render for this phase.

        Reads `routing.style_engine` from the LLM config (empty dict when
        absent -- the kill-switch default, which resolves to "full"). "slim" is
        selected only when the phase's style-engine mode is "enforce".
        Delegates to the pure `resolve_prompt_profile` so the selection logic is
        unit-testable without a DB-backed worker.
        """
        cfg = self.llm.config.get("routing", {}).get("style_engine", {})
        return resolve_prompt_profile(cfg, phase_name)

    def _load_agent_prompt(self, phase_name: str, model: Optional[str] = None) -> str:
        """Load the system prompt for an agent phase.

        Substitutes runtime values into known placeholders:
        - `{TODAY'S DATE in YYYY-MM-DD format}` → current UTC date (LLM has no clock).
        - `{model name you are running as}` / `{the model you are running as}` →
          the model identifier, if provided. Without this, the LLM would
          hallucinate (typically a date near its training cutoff and a generic
          model name).

        Finally, whichever path produced the text (file or hardcoded
        fallback), it flows through a single `render_prompt_blocks` call at
        the end of this method to substitute any `{{style:KEY}}` tokens. All
        five phase prompts (`prompts/{analyst,formatter,timestamp,seo,
        validator}.md`) carry `{{style:*}}` tokens now, so this call is live
        on every phase run -- the fallback prompts above do not carry tokens
        and pass through render_prompt_blocks as a no-op.
        """
        prompt_file = AGENTS_DIR / f"{phase_name}.md"

        if prompt_file.exists():
            text = prompt_file.read_text()
            text = text.replace(
                "{TODAY'S DATE in YYYY-MM-DD format}",
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )
            if model:
                text = text.replace("{model name you are running as}", model)
                text = text.replace("{the model you are running as}", model)
        else:
            # Fallback prompts if files don't exist
            fallback_prompts = {
                "analyst": """You are a transcript analyst for PBS Wisconsin. Your role is to analyze raw video transcripts and identify:

1. Key topics and themes discussed
2. Speaker identification and roles
3. Important quotes and timestamps
4. Structural elements (segments, transitions)
5. Items that may need human review (unclear audio, names to verify)

Output a detailed analysis document in markdown format that will guide the formatting and SEO agents.""",
                "formatter": """You are a transcript formatter for PBS Wisconsin. Your role is to transform raw transcripts into clean, readable markdown documents.

CRITICAL: Preserve ALL spoken dialogue. Do NOT summarize or condense. Every sentence must appear in your output.

Guidelines:
- Use proper speaker attribution (SPEAKER NAME:)
- Create logical paragraph breaks
- Preserve important timestamps (if present)
- Fix obvious transcription errors
- Remove filler words (um, uh) but preserve all substantive content
- Maintain the original meaning and voice

Output a clean, well-formatted markdown transcript with COMPLETE content.""",
                "seo": """You are an SEO specialist for PBS Wisconsin streaming content. Your role is to generate search-optimized metadata for video content.

Generate:
1. Title (compelling, keyword-rich, under 60 chars)
2. Short description (1-2 sentences, 150 chars)
3. Long description (2-3 paragraphs, engaging)
4. Tags (10-15 relevant keywords)
5. Categories

Output as JSON with keys: title, short_description, long_description, tags, categories""",
                "copy_editor": """You are a copy editor for PBS Wisconsin. Your role is to review and refine formatted transcripts for broadcast quality.

Focus on:
- Grammar and punctuation
- Clarity and readability
- PBS style guidelines
- Consistency in formatting
- Preserving speaker voice while improving prose

Output the polished transcript with any notes on changes made.""",
                "validator": """You are a quality validation agent for Cardigan. Review all pipeline outputs for quality.

Check:
1. Formatter: Speaker labels use first+last name only (no titles like Dr./Mr./Ms.), review notes only at top
2. SEO: Title <60 chars, descriptions are engaging, tags relevant
3. Analyst: Speakers identified, topics captured

Output a structured JSON checklist with:
- overall_status: "approved" or "needs_revision"
- checks: array of {phase, criterion, passed, note}
- issues: array of {severity, phase, description}
- recommendation: string""",
            }

            text = fallback_prompts.get(
                phase_name, f"You are the {phase_name} agent. Process the input and provide appropriate output."
            )

        rules_file = (
            self.llm.config.get("routing", {}).get("style_engine", {}).get("rules_file", "config/house_style.yaml")
        )
        try:
            return render_prompt_blocks(
                text,
                profile=self._style_prompt_profile(phase_name),
                rules_path=rules_file,
            )
        except PromptBlockError:
            # Graceful degradation (PR #295 review #1): prompt-block rendering
            # is fail-fast, but a missing/corrupt house-style YAML must not fail
            # every job. Fall back to the raw prompt with {{style:*}} tokens
            # stripped (never leak literal tokens to the LLM). A boot-time
            # check (validate_prompt_blocks in main.lifespan) already refuses to
            # start on a broken file, so reaching here means the file broke
            # AFTER boot (e.g. an mtime-triggered live reload of bad YAML).
            logger.warning(
                "prompt-block rendering failed; running phase on unstyled prompt (tokens stripped)",
                extra={"phase": phase_name, "rules_file": rules_file},
                exc_info=True,
            )
            return strip_style_tokens(text)

    def _build_phase_prompt(self, phase_name: str, context: Dict[str, Any]) -> str:
        """Build the user prompt for a phase with relevant context.

        On retries, appends validation flags and previous output to help
        the model fix identified issues.
        """
        prompt = self._build_phase_prompt_base(phase_name, context)

        # Append retry context (validation flags + previous output) if present
        validation_flags = context.get("_validation_flags")
        if validation_flags:
            prompt += "\n\n## Validation Issues from Previous Attempt\n\n"
            prompt += "The previous output was flagged for these issues. Address each one:\n\n"
            for flag in validation_flags:
                prompt += f"- {flag}\n"

        previous_output = context.get("_previous_output")
        if previous_output:
            prompt += "\n\n## Previous Output (for reference)\n\n"
            prompt += "Use this as a starting point. Fix the flagged issues while preserving what worked:\n\n"
            prompt += f"---\n{previous_output}\n---"

        # Append the style-engine pre-generation prompt section (if the
        # kill-switched hook in `_run_phase` produced one for this phase).
        style_pre = context.get("style_pre") or {}
        section = style_pre.get("prompt_section")
        if section:
            prompt = f"{prompt}\n\n{section}"

        return prompt

    def _build_phase_prompt_base(self, phase_name: str, context: Dict[str, Any]) -> str:
        """Build the base user prompt for a phase (without retry context)."""
        transcript = context.get("transcript", "")
        sst_context = context.get("sst_context")

        # Build SST context section if available
        sst_section = ""
        if sst_context:
            sst_section = "\n## Single Source of Truth (SST) Context\n\n"
            sst_section += "The following metadata exists in the PBS Wisconsin Airtable SST for this project:\n\n"

            if sst_context.get("title"):
                sst_section += f"**Title:** {sst_context['title']}\n"
            if sst_context.get("program"):
                sst_section += f"**Program:** {sst_context['program']}\n"
            if sst_context.get("short_description"):
                sst_section += f"**Short Description:** {sst_context['short_description']}\n"
            if sst_context.get("long_description"):
                sst_section += f"**Long Description:** {sst_context['long_description']}\n"
            if sst_context.get("host"):
                sst_section += f"**Host:** {sst_context['host']}\n"
            if sst_context.get("presenter"):
                sst_section += f"**Presenter:** {sst_context['presenter']}\n"
            if sst_context.get("keywords"):
                sst_section += f"**Keywords:** {sst_context['keywords']}\n"
            if sst_context.get("tags"):
                sst_section += f"**Tags:** {sst_context['tags']}\n"
            if sst_context.get("social_media_description"):
                sst_section += f"**Social Media Description:** {sst_context['social_media_description']}\n"
            if sst_context.get("project_notes"):
                sst_section += f"**Project Notes (Series Info):** {sst_context['project_notes']}\n"

            sst_section += "\n*Use this context to align your analysis with existing metadata. Speaker names from SST are authoritative.*\n\n"

        # Detect content type for Shorts-specific prompt adjustments
        content_type = context.get("content_type", "full")

        # Load transcript glossary for analyst and formatter phases
        wi_reference = ""
        if phase_name in ("analyst", "formatter"):
            glossary_path = KNOWLEDGE_DIR / "glossary.md"
            if glossary_path.exists():
                wi_reference = f"\n## Transcript Glossary\n\n{glossary_path.read_text()}\n\n"

        if phase_name == "analyst":
            if content_type == "short":
                prompt = (
                    "This is a YouTube Short (under 90 seconds). Provide a brief analysis focused on the "
                    "single topic. Do not create a structural breakdown — focus on identifying the core "
                    "message, target audience, and 3-5 keywords.\n\n"
                )
            else:
                prompt = """Please analyze the following transcript:
"""
            if sst_section:
                prompt += sst_section
            if wi_reference:
                prompt += wi_reference
            prompt += f"""---
{transcript}
---

Provide a detailed analysis document."""
            editorial_feedback = context.get("_editorial_feedback")
            if editorial_feedback:
                prompt += f"""

## Editorial Feedback

The editor reviewed the previous analysis and requests these changes:

{editorial_feedback}"""
            return prompt

        elif phase_name == "formatter":
            analysis = context.get("analyst_output", "")

            # Add verbatim preservation instruction (matches chunked formatter behavior)
            verbatim_instruction = """CRITICAL: You MUST preserve ALL spoken dialogue. Do NOT summarize, condense, or paraphrase.
Every sentence spoken in the transcript must appear in your output. You may remove filler words
(um, uh) and fix grammar, but do NOT drop or merge sentences. Completeness is more important than brevity.
If a caption is garbled or unclear, include your best reconstruction rather than dropping it. NEVER silently omit content.

"""

            prompt = verbatim_instruction
            prompt += "Using the following analysis as guidance:\n\n"
            if sst_section:
                prompt += sst_section
            if wi_reference:
                prompt += wi_reference
            prompt += f"""---
{analysis}
---

Please format this transcript:

---
{transcript}
---"""

            # Inject diarization data if available
            diarization = context.get("diarization_result")
            if diarization and diarization.get("segments"):
                speakers = ", ".join(diarization["speakers"])
                # Format a compact segment list for the prompt
                seg_lines = []
                for seg in diarization["segments"][:50]:  # Cap at 50 segments to limit prompt size
                    start = seg["start"]
                    end = seg["end"]
                    speaker = seg["speaker"]
                    conf = seg.get("confidence", 0)
                    m_start, s_start = divmod(int(start), 60)
                    m_end, s_end = divmod(int(end), 60)
                    seg_lines.append(f"  {m_start}:{s_start:02d}-{m_end}:{s_end:02d}  {speaker} (conf: {conf:.0%})")

                prompt += f"""

## Speaker Diarization Analysis

The following speaker diarization was generated from the audio track. Use this to verify
and correct speaker labels in the captions. Detected speakers: {speakers}

{chr(10).join(seg_lines)}

Note: Diarization confidence scores below 70% should be treated as uncertain."""

            editorial_feedback = context.get("_editorial_feedback")
            if editorial_feedback:
                prompt += f"""

## Editorial Feedback

The editor reviewed the formatted transcript and requests these changes:

{editorial_feedback}"""
            return prompt

        elif phase_name == "seo":
            analysis = context.get("analyst_output", "")
            formatted = context.get("formatter_output", "")
            if content_type == "short":
                prompt = (
                    "This is a YouTube Short. Optimize metadata for YouTube Shorts discovery: "
                    "title under 40 characters, include #Shorts hashtag, focus on vertical video tags, "
                    "and write a description under 200 characters.\n\n"
                )
            else:
                prompt = "Based on this analysis:\n\n"
            if sst_section:
                prompt += sst_section
            prompt += f"""---
{analysis}
---

And this formatted transcript:

---
{formatted[:2000]}...
---

Generate SEO metadata as a markdown report."""
            editorial_feedback = context.get("_editorial_feedback")
            if editorial_feedback:
                prompt += f"""

## Editorial Feedback

The editor reviewed the SEO metadata and requests these changes:

{editorial_feedback}"""

            # Inject keyword report if one exists in the project directory
            project_path = context.get("project_path")
            if project_path:
                keyword_reports = sorted(Path(project_path).glob("keyword_report_v*.md"))
                if keyword_reports:
                    latest_report = keyword_reports[-1].read_text(encoding="utf-8")
                    prompt += f"""

## SEMRush Keyword Data

The following keyword report was uploaded for this project. Use this data to inform your SEO recommendations — prioritize keywords with high search volume and low competition.

---
{latest_report}
---"""

            return prompt

        elif phase_name == "copy_editor":
            formatted = context.get("formatter_output", "")
            prompt = f"""Please review and polish this formatted transcript:

---
{formatted}
---

Apply PBS style guidelines and improve readability while preserving speaker voice."""
            editorial_feedback = context.get("_editorial_feedback")
            if editorial_feedback:
                prompt += f"""

## Editorial Feedback

The editor reviewed the copy-edited transcript and requests these changes:

{editorial_feedback}"""
            return prompt

        elif phase_name == "validator":
            analysis = context.get("analyst_output", "")
            formatted = context.get("formatter_output", "")
            seo = context.get("seo_output", "")
            prompt = "Validate the following pipeline outputs and return your JSON verdict.\n\n"

            # Add completeness check results if available
            completeness = context.get("completeness_check")
            if completeness:
                status = "PASS" if completeness["is_complete"] else "FAIL - TRUNCATION DETECTED"
                prompt += f"""## Automated Completeness Check
- Coverage: {completeness['coverage_ratio']:.1%}
- Result: {status}

"""

            prompt += f"""## Analyst Output:
---
{analysis}
---

## Formatted Transcript:
---
{formatted}
---

## SEO Metadata:
---
{seo}
---"""
            return prompt

        elif phase_name == "timestamp":
            srt_content = context.get("srt_content", "")
            formatted = context.get("formatter_output", "")
            analysis = context.get("analyst_output", "")
            transcript_metrics = context.get("transcript_metrics", {})

            # Prefer SRT-derived duration over the word-count estimate. The estimate
            # has been observed to overshoot real runtime by ~2x for short-form
            # talk-show content, which causes the agent to believe the SRT is
            # truncated even when it isn't. Parse the SRT directly when available.
            duration = transcript_metrics.get("estimated_duration_minutes", 0)
            if srt_content:
                try:
                    from api.services.utils import get_srt_duration, parse_srt

                    captions = parse_srt(srt_content)
                    if captions:
                        duration = get_srt_duration(captions) / 60000
                except Exception as e:
                    logger.debug("SRT duration parse failed, falling back to estimate", extra={"error": str(e)})

            project_name = context.get("project_name", "Unknown")

            prompt = f"""Create a timestamp report with chapter markers for this video content.

## Project: {project_name}
"""
            if sst_section:
                prompt += sst_section
            prompt += f"""
**Estimated Duration:** {duration:.1f} minutes

## Original SRT Content (use these timecodes):
---
{srt_content[:500000]}{"..." if len(srt_content) > 500000 else ""}
---

## Analyst Output (use this to identify chapter boundaries):
---
{analysis[:4000]}{"..." if len(analysis) > 4000 else ""}
---

## Your Task

Identify 3-8 logical chapter breaks based on topic transitions, speaker changes, and segment markers in the content above.

"""
            if self._style_prompt_profile("timestamp") == "slim":
                # Slim profile: the system prompt's {{style:timestamp.output_contract}}
                # token already spells out the fenced ```chapters contract (and the
                # deterministic post-stage renders the Media Manager/YouTube sections
                # from it) -- a "TWO sections" instruction here would contradict that
                # contract, so keep this minimal and defer to the system instructions.
                prompt += "Select chapter boundaries and titles per the output contract in your instructions."
            else:
                prompt += """Output a timestamp report with TWO sections:
1. **Media Manager Format** - Table with Title, Start Time (H:MM:SS.000), End Time (H:MM:SS.999)
2. **YouTube Format** - Simple list like "0:00 Introduction" for video descriptions

Follow the exact format specified in your system instructions."""

            # Inject editorial feedback if present
            editorial_feedback = context.get("_editorial_feedback")
            if editorial_feedback:
                prompt += f"""

## Editorial Feedback

The editor reviewed the previous timestamp output and provided the following feedback. Incorporate these changes:

{editorial_feedback}"""
            return prompt

        return f"Process the following:\n\n{transcript}"

    async def _create_manifest(
        self,
        job: Dict[str, Any],
        project_path: Path,
        phases: List[Dict[str, Any]],
        tracker,
    ):
        """Create a manifest file for the completed project."""
        manifest = {
            "job_id": job["id"],
            "project_name": job.get("project_name"),
            "transcript_file": job.get("transcript_file"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "phases": phases,
            "total_cost": tracker.total_cost if tracker else 0,
            "total_tokens": tracker.total_tokens if tracker else 0,
            "outputs": {
                "analysis": "analyst_output.md",
                "formatted_transcript": "formatter_output.md",
                "seo_metadata": "seo_output.md",
                "qa_review": "validator_output.md",
                "copy_edited": "copy_editor_output.md",
            },
            # Airtable SST linking - enables MCP server to fetch live metadata
            "media_id": job.get("media_id"),
            "airtable_record_id": job.get("airtable_record_id"),
            "airtable_url": job.get("airtable_url"),
            "duration_minutes": job.get("duration_minutes"),
        }

        manifest_file = project_path / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, indent=2, default=str))


# CLI entry point
async def run_worker():
    """Run the worker as a standalone process."""
    worker = JobWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(run_worker())
