"""Tests for the JobWorker class.

Tests job claiming, phase processing, heartbeat updates, error handling,
and recovery analysis.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from api.models.job import JobStatus
from api.services.worker import JobWorker, WorkerConfig


@pytest.fixture
def mock_llm_client():
    """Create a mock LLM client."""
    client = MagicMock()
    client.config = {
        "routing": {
            "long_form_threshold_minutes": 15,
        }
    }
    client.get_backend_for_phase.return_value = "openrouter-cheapskate"
    return client


@pytest.fixture
def mock_llm_response():
    """Create a mock LLM response."""
    response = MagicMock()
    response.content = "Test output content"
    response.cost = 0.001
    response.total_tokens = 500
    response.model = "test-model"
    return response


@pytest.fixture
def worker_config():
    """Create a test worker configuration."""
    return WorkerConfig(
        poll_interval=1,
        heartbeat_interval=5,
        max_retries=3,
        max_concurrent_jobs=1,
        worker_id="test-worker",
    )


@pytest.fixture
def sample_job():
    """Create a sample job dict."""
    return {
        "id": 1,
        "project_name": "Test Project",
        "transcript_file": "test_transcript.txt",
        "status": "in_progress",
        "priority": 10,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "phases": [],
    }


class TestWorkerConfig:
    """Tests for WorkerConfig class."""

    def test_default_config(self):
        """Test default configuration values."""
        config = WorkerConfig()
        assert config.poll_interval == 30
        assert config.heartbeat_interval == 60
        assert config.max_retries == 3
        assert config.max_concurrent_jobs == 1
        assert config.worker_id.startswith("worker-")

    def test_custom_config(self):
        """Test custom configuration values."""
        config = WorkerConfig(
            poll_interval=10,
            heartbeat_interval=30,
            max_retries=5,
            max_concurrent_jobs=3,
            worker_id="custom-worker",
        )
        assert config.poll_interval == 10
        assert config.heartbeat_interval == 30
        assert config.max_retries == 5
        assert config.max_concurrent_jobs == 3
        assert config.worker_id == "custom-worker"


class TestJobWorker:
    """Tests for JobWorker class."""

    @patch("api.services.worker.get_llm_client")
    def test_worker_initialization(self, mock_get_llm, mock_llm_client):
        """Test worker initializes correctly."""
        mock_get_llm.return_value = mock_llm_client

        worker = JobWorker()
        assert worker.config is not None
        assert worker.llm is not None
        assert worker.running is False
        assert worker.PHASES == ["analyst", "formatter", "seo", "validator"]

    @patch("api.services.worker.get_llm_client")
    def test_worker_with_custom_config(self, mock_get_llm, mock_llm_client, worker_config):
        """Test worker with custom configuration."""
        mock_get_llm.return_value = mock_llm_client

        worker = JobWorker(config=worker_config)
        assert worker.config.poll_interval == 1
        assert worker.config.worker_id == "test-worker"


class TestAllPhasesComplete:
    """Tests for _all_phases_complete method."""

    @patch("api.services.worker.get_llm_client")
    def test_empty_phases_returns_false(self, mock_get_llm, mock_llm_client, tmp_path):
        """Empty phases list should return False."""
        mock_get_llm.return_value = mock_llm_client
        worker = JobWorker()

        result = worker._all_phases_complete([], tmp_path)
        assert result is False

    @patch("api.services.worker.get_llm_client")
    def test_incomplete_phases_returns_false(self, mock_get_llm, mock_llm_client, tmp_path):
        """Incomplete phases should return False."""
        mock_get_llm.return_value = mock_llm_client
        worker = JobWorker()

        phases = [
            {"name": "analyst", "status": "completed"},
            {"name": "formatter", "status": "pending"},
            {"name": "seo", "status": "pending"},
            {"name": "validator", "status": "pending"},
        ]
        result = worker._all_phases_complete(phases, tmp_path)
        assert result is False

    @patch("api.services.worker.get_llm_client")
    def test_all_completed_but_missing_files(self, mock_get_llm, mock_llm_client, tmp_path):
        """All phases completed but missing output files should return False."""
        mock_get_llm.return_value = mock_llm_client
        worker = JobWorker()

        phases = [
            {"name": "analyst", "status": "completed"},
            {"name": "formatter", "status": "completed"},
            {"name": "seo", "status": "completed"},
            {"name": "validator", "status": "completed"},
        ]
        result = worker._all_phases_complete(phases, tmp_path)
        assert result is False

    @patch("api.services.worker.get_llm_client")
    def test_all_completed_with_files(self, mock_get_llm, mock_llm_client, tmp_path):
        """All phases completed with output files should return True."""
        mock_get_llm.return_value = mock_llm_client
        worker = JobWorker()

        # Create output files
        for phase in ["analyst", "formatter", "seo", "validator"]:
            (tmp_path / f"{phase}_output.md").write_text(f"Output for {phase}")

        phases = [
            {"name": "analyst", "status": "completed"},
            {"name": "formatter", "status": "completed"},
            {"name": "seo", "status": "completed"},
            {"name": "validator", "status": "completed"},
        ]
        result = worker._all_phases_complete(phases, tmp_path)
        assert result is True


class TestSetupProjectDir:
    """Tests for _setup_project_dir method."""

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.OUTPUT_DIR")
    def test_creates_directory(self, mock_output_dir, mock_get_llm, mock_llm_client, tmp_path):
        """Should create project directory."""
        mock_get_llm.return_value = mock_llm_client
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        job = {"id": 1, "project_name": "Test Project"}

        result = worker._setup_project_dir(job)
        assert result.exists()
        assert result.name == "Test_Project"

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.OUTPUT_DIR")
    def test_sanitizes_project_name(self, mock_output_dir, mock_get_llm, mock_llm_client, tmp_path):
        """Should sanitize special characters in project name."""
        mock_get_llm.return_value = mock_llm_client
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        job = {"id": 1, "project_name": "Test/Project:Name!"}

        result = worker._setup_project_dir(job)
        assert "/" not in result.name
        assert ":" not in result.name
        assert "!" not in result.name


class TestLoadTranscript:
    """Tests for _load_transcript method."""

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    def test_loads_transcript_from_transcripts_dir(self, mock_transcripts_dir, mock_get_llm, mock_llm_client, tmp_path):
        """Should load transcript from transcripts directory."""
        mock_get_llm.return_value = mock_llm_client
        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        type(mock_transcripts_dir).parent = PropertyMock(return_value=tmp_path)

        # Create transcript file
        (tmp_path / "test.txt").write_text("Transcript content")

        worker = JobWorker()
        job = {"id": 1, "transcript_file": "test.txt"}

        result = worker._load_transcript(job)
        assert result == "Transcript content"

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    def test_raises_on_missing_file(self, mock_transcripts_dir, mock_get_llm, mock_llm_client, tmp_path):
        """Should raise FileNotFoundError for missing transcript."""
        mock_get_llm.return_value = mock_llm_client
        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        job = {"id": 1, "transcript_file": "missing.txt"}

        with pytest.raises(FileNotFoundError):
            worker._load_transcript(job)


class TestLoadAgentPrompt:
    """Tests for _load_agent_prompt method."""

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.AGENTS_DIR")
    def test_loads_prompt_from_file(self, mock_agents_dir, mock_get_llm, mock_llm_client, tmp_path):
        """Should load agent prompt from file."""
        mock_get_llm.return_value = mock_llm_client
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        # Create prompt file
        (tmp_path / "analyst.md").write_text("Custom analyst prompt")

        worker = JobWorker()
        result = worker._load_agent_prompt("analyst")
        assert result == "Custom analyst prompt"

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.AGENTS_DIR")
    def test_uses_fallback_for_missing_file(self, mock_agents_dir, mock_get_llm, mock_llm_client, tmp_path):
        """Should use fallback prompt if file is missing."""
        mock_get_llm.return_value = mock_llm_client
        # Make the path check return False (file doesn't exist)
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        result = worker._load_agent_prompt("analyst")
        assert "transcript analyst" in result.lower()

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.AGENTS_DIR")
    def test_substitutes_today_date_placeholder(self, mock_agents_dir, mock_get_llm, mock_llm_client, tmp_path):
        """The literal {TODAY'S DATE in YYYY-MM-DD format} placeholder must be replaced
        with today's actual date so the LLM doesn't hallucinate a date near its
        training cutoff."""
        from datetime import datetime, timezone

        mock_get_llm.return_value = mock_llm_client
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        (tmp_path / "analyst.md").write_text("**Date Processed:** {TODAY'S DATE in YYYY-MM-DD format}\n")

        worker = JobWorker()
        result = worker._load_agent_prompt("analyst")
        expected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert expected_date in result
        assert "{TODAY'S DATE" not in result

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.AGENTS_DIR")
    def test_substitutes_model_placeholder_when_provided(
        self, mock_agents_dir, mock_get_llm, mock_llm_client, tmp_path
    ):
        """When a model identifier is provided, both placeholder spellings should be
        substituted so the artifact header reflects the real model rather than an
        LLM guess."""
        mock_get_llm.return_value = mock_llm_client
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        (tmp_path / "seo.md").write_text(
            "**Model:** {model name you are running as}\n" "Other line: {the model you are running as}\n"
        )

        worker = JobWorker()
        result = worker._load_agent_prompt("seo", model="anthropic/claude-4.5-haiku-20251001")
        assert "anthropic/claude-4.5-haiku-20251001" in result
        assert "{model name" not in result
        assert "{the model" not in result

    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.AGENTS_DIR")
    def test_leaves_model_placeholder_when_none(self, mock_agents_dir, mock_get_llm, mock_llm_client, tmp_path):
        """When no model is provided, the model placeholder is left alone (the LLM
        will then hallucinate, but date substitution still happens)."""
        mock_get_llm.return_value = mock_llm_client
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        (tmp_path / "analyst.md").write_text("**Model:** {model name you are running as}\n")

        worker = JobWorker()
        result = worker._load_agent_prompt("analyst")
        assert "{model name you are running as}" in result


class TestBuildPhasePrompt:
    """Tests for _build_phase_prompt method."""

    @patch("api.services.worker.get_llm_client")
    def test_analyst_prompt(self, mock_get_llm, mock_llm_client):
        """Should build analyst phase prompt."""
        mock_get_llm.return_value = mock_llm_client

        worker = JobWorker()
        context = {"transcript": "Test transcript content"}

        result = worker._build_phase_prompt("analyst", context)
        assert "Test transcript content" in result
        assert "analyze" in result.lower()

    @patch("api.services.worker.get_llm_client")
    def test_formatter_prompt_includes_analysis(self, mock_get_llm, mock_llm_client):
        """Should include analysis in formatter prompt."""
        mock_get_llm.return_value = mock_llm_client

        worker = JobWorker()
        context = {
            "transcript": "Test transcript",
            "analyst_output": "Analysis output",
        }

        result = worker._build_phase_prompt("formatter", context)
        assert "Analysis output" in result
        assert "Test transcript" in result

    @patch("api.services.worker.get_llm_client")
    def test_seo_prompt_includes_formatted(self, mock_get_llm, mock_llm_client):
        """Should include formatted transcript in SEO prompt."""
        mock_get_llm.return_value = mock_llm_client

        worker = JobWorker()
        context = {
            "transcript": "Test transcript",
            "analyst_output": "Analysis",
            "formatter_output": "Formatted transcript",
        }

        result = worker._build_phase_prompt("seo", context)
        assert "Formatted transcript" in result
        assert "SEO" in result or "metadata" in result.lower()

    @patch("api.services.worker.get_llm_client")
    def test_validator_prompt_includes_all_outputs(self, mock_get_llm, mock_llm_client):
        """Should include all phase outputs in validator prompt."""
        mock_get_llm.return_value = mock_llm_client

        worker = JobWorker()
        context = {
            "transcript": "Test transcript",
            "analyst_output": "Analysis",
            "formatter_output": "Formatted",
            "seo_output": '{"title": "Test"}',
        }

        result = worker._build_phase_prompt("validator", context)
        assert "Analysis" in result
        assert "Formatted" in result
        assert "Test" in result


class TestRunPhase:
    """Tests for _run_phase method."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_successful_phase_execution(
        self, mock_agents_dir, mock_log_event, mock_get_llm, mock_llm_client, mock_llm_response, tmp_path
    ):
        """Should successfully execute a phase."""
        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(return_value=mock_llm_response)
        mock_log_event.return_value = None
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        context = {"transcript": "Test transcript"}

        result = await worker._run_phase(
            job_id=1,
            phase_name="analyst",
            context=context,
            project_path=tmp_path,
        )

        assert result["success"] is True
        assert result["output"] == "Test output content"
        assert result["cost"] == 0.001
        assert result["tokens"] == 500
        assert (tmp_path / "analyst_output.md").exists()


class TestHeartbeatLoop:
    """Tests for _heartbeat_loop method."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_heartbeat")
    async def test_heartbeat_updates(self, mock_update_heartbeat, mock_get_llm, mock_llm_client):
        """Should update heartbeat periodically."""
        mock_get_llm.return_value = mock_llm_client
        mock_update_heartbeat.return_value = None

        config = WorkerConfig(heartbeat_interval=0.1)  # 100ms for test
        worker = JobWorker(config=config)

        # Run heartbeat for short time then cancel
        task = asyncio.create_task(worker._heartbeat_loop(1))
        await asyncio.sleep(0.25)  # Let it run for ~2 heartbeats
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_update_heartbeat.call_count >= 1


class TestProcessJob:
    """Tests for process_job method."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_successful_job_processing(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_get_llm,
        mock_llm_client,
        mock_llm_response,
        tmp_path,
        sample_job,
    ):
        """Should successfully process a job through all phases."""
        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(return_value=mock_llm_response)
        mock_update_status.return_value = None
        mock_update_phase.return_value = None
        mock_update_heartbeat.return_value = None
        mock_log_event.return_value = None
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.01, "total_tokens": 2000}

        # Set up paths
        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        # Create transcript
        (tmp_path / sample_job["transcript_file"]).write_text("Test transcript content")

        worker = JobWorker()
        await worker.process_job(sample_job)

        # Verify job was marked completed
        calls = mock_update_status.call_args_list
        # The final status should be completed
        assert (
            any(
                call.args[1].value == "completed" if hasattr(call.args[1], "value") else call.args[1] == "completed"
                for call in calls
            )
            or mock_update_status.called
        )


class TestWorkerStart:
    """Tests for worker start method."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.claim_next_job")
    async def test_worker_polls_for_jobs(self, mock_claim_job, mock_get_llm, mock_llm_client):
        """Should poll for jobs when started."""
        mock_get_llm.return_value = mock_llm_client
        mock_claim_job.return_value = None  # No jobs available

        config = WorkerConfig(poll_interval=0.1)
        worker = JobWorker(config=config)

        # Start worker in background
        task = asyncio.create_task(worker.start())

        # Let it poll a few times
        await asyncio.sleep(0.3)
        await worker.stop()

        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()

        assert mock_claim_job.call_count >= 1


class TestRunPhaseDeferral:
    """_run_phase converts a BackendUnavailableError into a deferred result
    instead of a failed one, so the pipeline can requeue the job."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_backend_unavailable_returns_deferred(
        self, mock_agents_dir, mock_log_event, mock_get_llm, mock_llm_client, tmp_path
    ):
        from api.services.llm import BackendUnavailableError

        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(
            side_effect=BackendUnavailableError("memory pressure 69%", backend="local-llm", retry_after_s=30)
        )
        mock_log_event.return_value = None
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        result = await worker._run_phase(
            job_id=1, phase_name="analyst", context={"transcript": "x"}, project_path=tmp_path
        )

        assert result["success"] is False
        assert result["deferred"] is True
        assert "memory pressure" in result["detail"]
        assert result["retry_after_s"] == 30


class TestProcessJobDeferral:
    """A phase that defers (busy local backend) requeues the job via defer_job
    instead of failing it."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.defer_job", new_callable=AsyncMock)
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_deferred_phase_requeues_not_fails(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_defer_job,
        mock_get_llm,
        mock_llm_client,
        tmp_path,
        sample_job,
    ):
        from api.services.llm import BackendUnavailableError

        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(
            side_effect=BackendUnavailableError("memory pressure 69%", backend="local-llm", retry_after_s=30)
        )
        mock_update_status.return_value = None
        mock_update_phase.return_value = None
        mock_update_heartbeat.return_value = None
        mock_log_event.return_value = None
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.0, "total_tokens": 0}
        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name
        (tmp_path / sample_job["transcript_file"]).write_text("Test transcript content")

        worker = JobWorker()
        await worker.process_job(sample_job)

        # The job was requeued, not failed.
        assert mock_defer_job.called
        statuses = [
            (c.args[1].value if hasattr(c.args[1], "value") else c.args[1])
            for c in mock_update_status.call_args_list
            if len(c.args) > 1
        ]
        assert "failed" not in statuses
        assert "completed" not in statuses


class TestResolveDurationIntoMetrics:
    """#126: the SRT-parsed duration must override the word-count estimate so
    every downstream consumer (routing, prompt context, persisted
    duration_minutes) reports the same value."""

    @patch("api.services.worker.get_llm_client")
    def test_srt_duration_overrides_word_count_estimate(self, mock_get_llm, mock_llm_client, tmp_path):
        mock_get_llm.return_value = mock_llm_client
        worker = JobWorker()

        # SRT whose last caption ends at 00:18:34 (~18.57 min) — like job 168.
        srt = tmp_path / "6POL0108.srt"
        srt.write_text(
            "1\n00:00:01,000 --> 00:00:04,000\nHello.\n\n" "2\n00:18:30,000 --> 00:18:34,000\nGoodbye.\n",
            encoding="utf-8",
        )
        # Word-count estimate is wrong (38.5 min), like the SEO agent saw.
        metrics = {"estimated_duration_minutes": 38.5, "word_count": 5000}

        result = worker._resolve_duration_into_metrics(metrics, srt)

        assert abs(result["estimated_duration_minutes"] - 18.57) < 0.2
        assert metrics["estimated_duration_minutes"] != 38.5

    @patch("api.services.worker.get_llm_client")
    def test_no_srt_keeps_estimate(self, mock_get_llm, mock_llm_client, tmp_path):
        mock_get_llm.return_value = mock_llm_client
        worker = JobWorker()
        metrics = {"estimated_duration_minutes": 25.0, "word_count": 4000}

        result = worker._resolve_duration_into_metrics(metrics, None)

        assert result["estimated_duration_minutes"] == 25.0


class TestCreditExhaustionAndTruncation:
    """Triggers B (credit) + C (truncation) routing onto pause_and_suggest (#243)."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_run_phase_tags_credit_exhausted(
        self, mock_agents_dir, mock_log_event, mock_get_llm, mock_llm_client, tmp_path
    ):
        """_run_phase must catch CreditExhaustedError BEFORE the generic handler
        and return a tagged dict (not a flattened {'success': False, 'error': ...})."""
        from api.services.llm import CreditExhaustedError

        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(
            side_effect=CreditExhaustedError("OpenRouter credit exhausted", backend="openrouter")
        )
        mock_log_event.return_value = None
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        worker = JobWorker()
        result = await worker._run_phase(
            job_id=1, phase_name="analyst", context={"transcript": "x"}, project_path=tmp_path
        )

        assert result["success"] is False
        assert result["credit_exhausted"] is True
        assert "credit" in result["error"].lower()

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.pause_and_suggest")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_process_job_credit_exhausted_pauses_not_failed(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_pause,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_get_llm,
        mock_llm_client,
        tmp_path,
        sample_job,
    ):
        """Credit exhaustion on the first phase routes to pause_and_suggest(trigger='credit')
        and the job is NEVER marked failed (no raise, no consumed retry)."""
        from api.services.llm import CreditExhaustedError

        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(
            side_effect=CreditExhaustedError("OpenRouter credit exhausted", backend="openrouter")
        )
        mock_pause.return_value = None
        mock_update_status.return_value = None
        mock_update_phase.return_value = None
        mock_update_heartbeat.return_value = None
        mock_log_event.return_value = None
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.0, "total_tokens": 0}

        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name
        (tmp_path / sample_job["transcript_file"]).write_text("Test transcript content")

        worker = JobWorker()
        await worker.process_job(sample_job)

        mock_pause.assert_awaited_once()
        assert mock_pause.await_args.kwargs["trigger"] == "credit"

        statuses = []
        for c in mock_update_status.call_args_list:
            s = c.args[1] if len(c.args) > 1 else c.kwargs.get("status")
            statuses.append(getattr(s, "value", s))
        assert "failed" not in statuses

    @pytest.mark.asyncio
    @patch("api.services.completeness.check_completeness")
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.pause_and_suggest")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_process_job_truncation_routes_through_pause_and_suggest(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_pause,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_get_llm,
        mock_check_completeness,
        mock_llm_client,
        mock_llm_response,
        tmp_path,
        sample_job,
    ):
        """Truncation detection pauses via pause_and_suggest(trigger='truncation')."""
        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(return_value=mock_llm_response)
        mock_pause.return_value = None
        mock_update_status.return_value = None
        mock_update_phase.return_value = None
        mock_update_heartbeat.return_value = None
        mock_log_event.return_value = None
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.02, "total_tokens": 1000}

        truncated = MagicMock()
        truncated.is_complete = False
        truncated.skipped = False
        truncated.coverage_ratio = 0.4
        truncated.output_word_count = 100
        truncated.source_word_count = 250
        truncated.to_dict.return_value = {"coverage_ratio": 0.4}
        mock_check_completeness.return_value = truncated

        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name
        (tmp_path / sample_job["transcript_file"]).write_text("Test transcript content " * 50)

        worker = JobWorker()
        await worker.process_job(sample_job)

        mock_pause.assert_awaited_once()
        assert mock_pause.await_args.kwargs["trigger"] == "truncation"

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.pause_and_suggest")
    async def test_pause_for_credit_helper(
        self, mock_pause, mock_end_tracking, mock_update_status, mock_get_llm, mock_llm_client
    ):
        """Shared terminal helper (used by main loop AND optional-phase path) pauses
        with trigger='credit' and records cost without marking the job failed."""
        mock_get_llm.return_value = mock_llm_client
        mock_pause.return_value = None
        mock_end_tracking.return_value = {"total_cost": 0.05}
        mock_update_status.return_value = None

        worker = JobWorker()
        await worker._pause_for_credit(job_id=7)

        mock_pause.assert_awaited_once()
        assert mock_pause.await_args.kwargs["trigger"] == "credit"
        assert "credit" in mock_pause.await_args.kwargs["message"].lower()
        # Cost recorded as a paused partial write, never failed.
        assert mock_update_status.await_args.args[1] == JobStatus.paused


class TestProcessJobSeamGap:
    """Worker-level test for the seam-gap pause path wired into the formatter phase."""

    # 14 distinctive captions; the formatter "drops" the contiguous block 6-9.
    _CAPTIONS = [
        "Madison reporters gathered outside the Capitol rotunda",
        "Governor Evers proposed expanding rural broadband funding",
        "Republicans criticized the broadband subsidy proposal sharply",
        "Senator Johnson defended existing telecommunications infrastructure spending",
        "Advocates demanded faster fiber deployment across northern counties",
        "Treasurer Godlewski announced surprising pension investment returns",  # DROP
        "Auditors questioned the pension accounting methodology thoroughly",  # DROP
        "Lawmakers debated transparency reforms during heated committee hearings",  # DROP
        "Witnesses testified about contractor billing irregularities extensively",  # DROP
        "Mayor Rhodes welcomed the downtown revitalization grant enthusiastically",
        "Developers unveiled ambitious waterfront housing density plans",
        "Neighbors objected to increased traffic congestion concerns",
        "Council postponed the zoning variance vote indefinitely",
        "Anchors thanked viewers before the evening broadcast concluded",
    ]
    _DROPPED = {5, 6, 7, 8}  # 0-indexed → SRT captions 6,7,8,9 (a 4-block)

    def _source_srt(self) -> str:
        blocks = []
        for i, text in enumerate(self._CAPTIONS, start=1):
            blocks.append(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{text}\n\n")
        return "".join(blocks)

    def _formatter_output_with_gap(self) -> str:
        kept = [t for i, t in enumerate(self._CAPTIONS) if i not in self._DROPPED]
        body = "\n\n".join(f"**Speaker:** {t}." for t in kept)
        return (
            "<!-- model: test -->\n# Formatted Transcript\n**Project:** Test\n---\n\n"
            f"{body}\n\n**Status:** ready_for_editing"
        )

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_seam_gap_pauses_job_before_optional_phases(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_get_llm,
        mock_llm_client,
        tmp_path,
    ):
        """A formatter output missing a contiguous caption block pauses the job
        with a SEAM GAP message. Kept short so the global completeness check
        skips (under its 500-word floor) and the seam check is what fires."""
        mock_get_llm.return_value = mock_llm_client

        # Every phase returns the gapped formatter output; the formatter phase's
        # output is what the seam check reads.
        response = MagicMock()
        response.content = self._formatter_output_with_gap()
        response.cost = 0.001
        response.total_tokens = 500
        response.model = "test-model"
        mock_llm_client.chat = AsyncMock(return_value=response)

        mock_update_status.return_value = None
        mock_update_phase.return_value = None
        mock_update_heartbeat.return_value = None
        mock_log_event.return_value = None
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.01, "total_tokens": 2000}

        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        job = {
            "id": 99,
            "project_name": "Seam Test",
            "transcript_file": "seamtest.srt",
            "status": "in_progress",
            "priority": 10,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "phases": [],
        }
        (tmp_path / job["transcript_file"]).write_text(self._source_srt(), encoding="utf-8")

        worker = JobWorker()
        await worker.process_job(job)

        # A paused status update carrying the seam message must have been issued.
        paused = [c for c in mock_update_status.call_args_list if getattr(c.args[1], "value", c.args[1]) == "paused"]
        assert paused, "expected the job to be paused on a seam gap"
        assert any(
            "SEAM GAP DETECTED" in str(c.kwargs.get("error_message", "")) for c in paused
        ), "paused status should carry the SEAM GAP message"

    def _formatter_output_gap_no_net_loss(self) -> str:
        """Same dropped block, but padded so total content words exceed the
        source — a reconstruction/expansion, not a real loss."""
        kept = [t for i, t in enumerate(self._CAPTIONS) if i not in self._DROPPED]
        body = "\n\n".join(f"**Speaker:** {t}." for t in kept)
        padding = " ".join(["reconstructed"] * 60)
        return (
            "<!-- model: test -->\n# Formatted Transcript\n**Project:** Test\n---\n\n"
            f"{body}\n\n**Speaker:** {padding}.\n\n**Status:** ready_for_editing"
        )

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_seam_gap_without_net_loss_does_not_pause(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_get_llm,
        mock_llm_client,
        tmp_path,
    ):
        """A detected seam gap with NO net content loss (the garbled-ASR
        reconstruction false positive) is non-blocking: the job must NOT be
        paused with a SEAM GAP message. This is the exact behavior the blocking
        guard adds — the unit-level coverage is in test_seam_coverage.py; this
        asserts it end-to-end at the worker."""
        mock_get_llm.return_value = mock_llm_client

        response = MagicMock()
        response.content = self._formatter_output_gap_no_net_loss()
        response.cost = 0.001
        response.total_tokens = 500
        response.model = "test-model"
        mock_llm_client.chat = AsyncMock(return_value=response)

        mock_update_status.return_value = None
        mock_update_phase.return_value = None
        mock_update_heartbeat.return_value = None
        mock_log_event.return_value = None
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.01, "total_tokens": 2000}

        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        job = {
            "id": 100,
            "project_name": "Seam Nonblock",
            "transcript_file": "seamtest.srt",
            "status": "in_progress",
            "priority": 10,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "phases": [],
        }
        (tmp_path / job["transcript_file"]).write_text(self._source_srt(), encoding="utf-8")

        worker = JobWorker()
        await worker.process_job(job)

        seam_pauses = [
            c
            for c in mock_update_status.call_args_list
            if getattr(c.args[1], "value", c.args[1]) == "paused"
            and "SEAM GAP" in str(c.kwargs.get("error_message", ""))
        ]
        assert not seam_pauses, "a corroboration-free seam gap must not pause the job"


class TestProcessJobSpeakerSegmentation:
    """Worker-level test that interior `>>` markers are split before the formatter
    LLM sees them (issue #269)."""

    @pytest.mark.asyncio
    @patch("api.services.worker.get_llm_client")
    @patch("api.services.worker.update_job_status")
    @patch("api.services.worker.update_job_phase")
    @patch("api.services.worker.update_job_heartbeat")
    @patch("api.services.worker.log_event")
    @patch("api.services.worker.start_run_tracking")
    @patch("api.services.worker.end_run_tracking")
    @patch("api.services.worker.TRANSCRIPTS_DIR")
    @patch("api.services.worker.OUTPUT_DIR")
    @patch("api.services.worker.AGENTS_DIR")
    async def test_formatter_receives_split_captions(
        self,
        mock_agents_dir,
        mock_output_dir,
        mock_transcripts_dir,
        mock_end_tracking,
        mock_start_tracking,
        mock_log_event,
        mock_update_heartbeat,
        mock_update_phase,
        mock_update_status,
        mock_get_llm,
        mock_llm_client,
        mock_llm_response,
        tmp_path,
    ):
        """The formatter phase's LLM input must contain the split form of an
        interior-`>>` caption, not the original joined caption."""
        mock_get_llm.return_value = mock_llm_client
        mock_llm_client.chat = AsyncMock(return_value=mock_llm_response)
        mock_start_tracking.return_value = MagicMock(total_cost=0, total_tokens=0)
        mock_end_tracking.return_value = {"total_cost": 0.01, "total_tokens": 2000}

        mock_transcripts_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_output_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_agents_dir.__truediv__ = lambda self, name: tmp_path / name

        srt = (
            "1\n00:00:01,000 --> 00:00:02,000\nPlain opener line here.\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nAlpha speaks now. >> Beta replies instead.\n\n"
        )
        job = {
            "id": 100,
            "project_name": "Seg Test",
            "transcript_file": "segtest.srt",
            "status": "in_progress",
            "priority": 10,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "phases": [],
        }
        (tmp_path / job["transcript_file"]).write_text(srt, encoding="utf-8")

        worker = JobWorker()
        await worker.process_job(job)

        # Find the formatter phase's LLM call and read its user message.
        formatter_msgs = [
            c.kwargs["messages"] for c in mock_llm_client.chat.call_args_list if c.kwargs.get("phase") == "formatter"
        ]
        assert formatter_msgs, "formatter phase should have called the LLM"
        user_text = "\n".join(m["content"] for m in formatter_msgs[0] if m["role"] == "user")

        # Split form present; original joined caption absent.
        assert ">> Beta replies instead." in user_text
        assert "Alpha speaks now. >> Beta replies instead." not in user_text


class TestWorkerRestartSignal:
    """JobWorker exits when a Settings restart request postdates its start (#304)."""

    @pytest.mark.asyncio
    async def test_should_stop_for_restart_true_when_request_is_newer(self):
        with patch("api.services.worker.get_llm_client"):
            worker = JobWorker(WorkerConfig())
        worker._start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch(
            "api.services.worker.get_restart_requested_at",
            new_callable=AsyncMock,
            return_value=datetime(2026, 1, 2, tzinfo=timezone.utc),
        ):
            assert await worker._should_stop_for_restart() is True

    @pytest.mark.asyncio
    async def test_should_stop_for_restart_false_when_no_request(self):
        with patch("api.services.worker.get_llm_client"):
            worker = JobWorker(WorkerConfig())
        with patch(
            "api.services.worker.get_restart_requested_at",
            new_callable=AsyncMock,
            return_value=None,
        ):
            assert await worker._should_stop_for_restart() is False

    @pytest.mark.asyncio
    async def test_should_stop_for_restart_false_when_request_predates_start(self):
        with patch("api.services.worker.get_llm_client"):
            worker = JobWorker(WorkerConfig())
        worker._start_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with patch(
            "api.services.worker.get_restart_requested_at",
            new_callable=AsyncMock,
            return_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ):
            assert await worker._should_stop_for_restart() is False
