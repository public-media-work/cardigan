"""End-to-end integration tests for the #243 QA-fail escalation gate.

These tests drive the REAL process_job + REAL SQLite with ONLY the LLM HTTP
boundary controlled (a local mock OpenRouter server). No stubbing of
_run_phase, update_job, or the gate itself.

Offline/CI-safe: fetch_openrouter_models is monkeypatched to return a
local catalog so the model-roster lookup never hits openrouter.ai.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Catalog returned by the monkeypatched fetch_openrouter_models
# ---------------------------------------------------------------------------
_MOCK_CATALOG = [
    {"id": "anthropic/claude-haiku-4.5-20251001", "created": 100},
    {"id": "anthropic/claude-sonnet-4.6", "created": 200},
    {"id": "anthropic/claude-opus-4-8", "created": 300},
]


# ---------------------------------------------------------------------------
# Per-test mock OpenRouter HTTP server
# ---------------------------------------------------------------------------


class _MockState:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.validator_calls = 0
        self.all_calls = 0
        self.phase_log: list = []


def _make_handler(state: _MockState):
    class MockOpenRouter(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if "models" in self.path:
                body = json.dumps({"data": _MOCK_CATALOG}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode() if n else "{}"
            req = json.loads(raw or "{}")
            msgs = " ".join(m.get("content", "") for m in req.get("messages", []))
            is_validator = "phase_results" in msgs
            state.all_calls += 1
            state.phase_log.append("validator" if is_validator else "phase")

            # Trigger B: 402 credit exhausted on first call
            if state.scenario == "credit" and state.all_calls == 1:
                body = json.dumps({"error": {"message": "Insufficient credits — your balance is exhausted"}}).encode()
                self.send_response(402)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if is_validator:
                state.validator_calls += 1
                if state.scenario in ("persistfail", "nonfixable"):
                    overall = "fail"
                else:
                    overall = "fail" if state.validator_calls == 1 else "pass"
                if state.scenario == "nonfixable":
                    phase_results = {
                        "analyst": {"status": "pass", "flags": []},
                        "formatter": {"status": "fail", "flags": ["Review notes appear in transcript body"]},
                        "seo": {"status": "pass", "flags": []},
                    }
                else:
                    phase_results = {
                        "analyst": {"status": "pass", "flags": []},
                        "formatter": {"status": "pass", "flags": []},
                        "seo": {
                            "status": "fail" if overall == "fail" else "pass",
                            "flags": ["weak keyword density"] if overall == "fail" else [],
                        },
                    }
                content = json.dumps({"overall": overall, "phase_results": phase_results})
            else:
                content = "Mock phase output. The meeting covered budget and policy in full."
                # The nonfixable scenario must be non-fixable on the EVIDENCE, not on
                # the validator's wording: since QA-1, routing reads the artifact for
                # contract markers rather than substring-matching flag prose.
                if state.scenario == "nonfixable":
                    content += "\n\n<!-- REVIEW NOTES:\n- verify proper-noun spelling\n-->\n"

            resp = {
                "model": req.get("model") or "anthropic/claude-haiku-4.5-20251001",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "total_cost": 0.01,
                },
            }
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return MockOpenRouter


@pytest.fixture()
def mock_server(request) -> Generator[tuple, None, None]:
    """Bind a mock OpenRouter HTTP server on an ephemeral port.

    Yields (state, base_url) and shuts down after the test.
    The scenario name is taken from an indirect param or defaults to 'pass'.
    """
    scenario = getattr(request, "param", "pass")
    state = _MockState(scenario)
    srv = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    yield state, base
    srv.shutdown()
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# Per-test DB fixture (mirrors tests/api/test_database.py::test_db)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def e2e_env(monkeypatch, tmp_path):
    """Isolated environment for one e2e test.

    - Fresh SQLite DB in tmp_path
    - LLM config file with openrouter* endpoints rewritten
    - Env vars: LLM_CONFIG_PATH, DATABASE_PATH, OPENROUTER_API_KEY, LLM_ENFORCE_GUARDS
    - Saves/restores DB engine globals and _llm_client global
    - Monkeypatches fetch_openrouter_models -> offline catalog
    - Resets model_roster cache
    """
    import api.services.database as db_mod
    import api.services.llm as llm_mod
    from api.services import model_roster

    # --- Save originals ---
    orig_engine = db_mod._engine
    orig_factory = db_mod._async_session_factory
    orig_db_path = os.environ.get("DATABASE_PATH")
    orig_llm_client = llm_mod._llm_client
    orig_cache = dict(model_roster._cache)

    # --- Reset DB globals ---
    db_mod._engine = None
    db_mod._async_session_factory = None

    # --- Build patched LLM config ---
    # Derive the repo root from this file's location so the test is portable
    # (CI checks out to a different path than any local worktree).
    repo_root = Path(__file__).resolve().parents[2]
    # process_job loads prompts/ and archives to transcripts/ relative to cwd, so
    # run from the repo root regardless of where pytest was invoked (monkeypatch
    # restores the original cwd on teardown).
    monkeypatch.chdir(repo_root)
    with open(repo_root / "config" / "llm-config.json") as f:
        cfg = json.load(f)

    # Caller will patch endpoint; use a placeholder URL that will be replaced
    cfg.setdefault("routing", {}).setdefault("completeness", {})["enabled"] = False
    cfg.setdefault("routing", {}).setdefault("chunking", {})["enabled"] = False
    cfg_path = tmp_path / "llm-config.json"
    cfg["_mock_base"] = "PLACEHOLDER"  # marker; replaced per-call below
    cfg_path.write_text(json.dumps(cfg))

    # --- DB path ---
    db_path = tmp_path / "e2e.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("LLM_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setenv("LLM_ENFORCE_GUARDS", "false")

    # --- Reset LLM client global (force re-init with new config) ---
    llm_mod._llm_client = None

    # --- Offline catalog: monkeypatch fetch_openrouter_models ---
    async def _mock_fetch():
        return list(_MOCK_CATALOG)

    monkeypatch.setattr(model_roster, "fetch_openrouter_models", _mock_fetch)

    # --- Reset model roster cache ---
    model_roster._cache["models"] = None
    model_roster._cache["expires"] = 0.0

    yield tmp_path, cfg, cfg_path

    # --- Teardown ---
    from api.services.database import close_db

    await close_db()
    db_mod._engine = orig_engine
    db_mod._async_session_factory = orig_factory
    if orig_db_path is not None:
        os.environ["DATABASE_PATH"] = orig_db_path
    elif "DATABASE_PATH" in os.environ:
        del os.environ["DATABASE_PATH"]
    llm_mod._llm_client = orig_llm_client
    model_roster._cache.update(orig_cache)


async def _run_scenario(scenario: str, tmp_path, cfg: dict, cfg_path):
    """Start mock server, patch config, init DB, run process_job, return job."""
    state = _MockState(scenario)
    srv = HTTPServer(("127.0.0.1", 0), _make_handler(state))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    mock_chat = f"http://127.0.0.1:{port}/api/v1/chat/completions"
    try:
        # Patch all openrouter* backend endpoints in config
        for name, b in cfg.get("backends", {}).items():
            if "openrouter" in name:
                b["endpoint"] = mock_chat
        cfg.pop("_mock_base", None)
        cfg_path.write_text(json.dumps(cfg))

        # Transcript
        tdir = tmp_path / "transcripts"
        tdir.mkdir(exist_ok=True)
        odir = tmp_path / "output"
        odir.mkdir(exist_ok=True)
        transcript = tdir / "e2e.txt"
        transcript.write_text(
            "Speaker 1: We discussed the budget and the new policy at length today. "
            "Speaker 2: Yes, the full council reviewed every line item before the vote.\n"
        )

        import api.services.llm as llm_mod
        from api.models.job import JobCreate
        from api.services.database import create_job, get_job, init_db
        from api.services.worker import JobWorker

        # Reset LLM client so it picks up the just-written config
        llm_mod._llm_client = None

        await init_db()
        from api.services.database import _engine, metadata

        async with _engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        job = await create_job(
            JobCreate(project_name="e2e243", project_path=str(odir), transcript_file=str(transcript))
        )
        worker = JobWorker()
        job_dict = job.model_dump()
        job_dict["status"] = "in_progress"

        await worker.process_job(job_dict)

        j = await get_job(job.id)
        return j, state
    finally:
        srv.shutdown()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Three e2e tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_escalate_pass(e2e_env):
    """fail -> escalate -> pass: completed, validation='pass', auto_escalated_at set."""
    tmp_path, cfg, cfg_path = e2e_env
    j, state = await _run_scenario("pass", tmp_path, cfg, cfg_path)

    assert j.status.value == "completed", f"expected completed, got {j.status!r} / {j.error_message!r}"
    vr = j.validation_result
    assert vr is not None, "validation_result must be persisted"
    assert vr.get("overall") == "pass", f"expected pass, got {vr}"
    # 4 phase calls + 2 validator calls (fail then pass) = 6 LLM calls -> cost > 0.05
    assert j.actual_cost > 0.05, f"expected > 0.05 actual_cost, got {j.actual_cost}"
    assert state.all_calls >= 6, f"expected ≥6 LLM calls, got {state.all_calls}: {state.phase_log}"
    assert j.auto_escalated_at is not None, "auto_escalated_at must be stamped after escalated-then-completed job"


@pytest.mark.asyncio
async def test_persistent_fail(e2e_env):
    """persistent fail -> paused with [qa_fail] error, auto_escalated_at set."""
    tmp_path, cfg, cfg_path = e2e_env
    j, state = await _run_scenario("persistfail", tmp_path, cfg, cfg_path)

    assert j.status.value == "paused", f"expected paused, got {j.status!r} / {j.error_message!r}"
    assert j.error_message is not None and j.error_message.startswith(
        "[qa_fail]"
    ), f"error_message must start with [qa_fail], got {j.error_message!r}"
    assert j.auto_escalated_at is not None, "auto_escalated_at must be stamped after persistent fail"
    vr = j.validation_result
    assert vr is not None and vr.get("overall") == "fail", f"persisted overall must be 'fail', got {vr}"
    assert j.retry_count == 0, f"retry_count must stay 0 (pause doesn't consume a retry), got {j.retry_count}"


@pytest.mark.asyncio
async def test_nonfixable_skips_escalation(e2e_env):
    """Review-notes-only QA fail -> paused (qa_review) WITHOUT an escalation pass."""
    tmp_path, cfg, cfg_path = e2e_env
    j, state = await _run_scenario("nonfixable", tmp_path, cfg, cfg_path)

    assert j.status.value == "paused", f"expected paused, got {j.status!r} / {j.error_message!r}"
    assert j.error_message is not None and j.error_message.startswith(
        "[qa_review]"
    ), f"error_message must start with [qa_review], got {j.error_message!r}"
    # No escalation: validator ran exactly once (no re-validation); single pipeline pass.
    assert state.validator_calls == 1, f"expected 1 validator call, got {state.validator_calls}: {state.phase_log}"
    assert state.all_calls < 6, f"expected single-pass (<6 calls), got {state.all_calls}: {state.phase_log}"
    assert j.auto_escalated_at is not None, "mark_escalated must stamp the marker (prevents resume re-loop)"


@pytest.mark.asyncio
async def test_credit_402(e2e_env):
    """402 credit exhaustion -> paused with [credit] error, retry_count=0."""
    tmp_path, cfg, cfg_path = e2e_env
    j, state = await _run_scenario("credit", tmp_path, cfg, cfg_path)

    assert j.status.value == "paused", f"expected paused, got {j.status!r} / {j.error_message!r}"
    assert j.error_message is not None and j.error_message.startswith(
        "[credit]"
    ), f"error_message must start with [credit], got {j.error_message!r}"
    assert j.retry_count == 0, f"retry_count must stay 0 (credit pause is not a retry), got {j.retry_count}"
