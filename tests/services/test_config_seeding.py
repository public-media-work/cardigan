"""Additive config seeding and drift reporting (#440).

The live llm-config.json is seeded once from the packaged default; after that,
new keys must still arrive (at any depth) while existing values — possibly set
through the Settings page — are never overwritten.
"""

import json

import pytest

from api.services.config_path import config_drift, merge_missing, resolve_config_path

# Shape of #409's additions: a new block and new leaves, both nested under an
# existing top-level "routing" object.
DEFAULT = {
    "backends": {"openrouter": {"max_tokens": 16384, "model": "m"}},
    "routing": {
        "chunking": {"threshold_words": 3000, "tokens_per_word": 2.0, "safety_factor": 0.8},
        "reasoning": {"disabled_phases": ["formatter"]},
    },
    "worker": {"poll_interval": 5},
}


def _live():
    """A July-era live file: older values, missing the newer keys."""
    return {
        "backends": {"openrouter": {"max_tokens": 4096, "model": "m"}},
        "routing": {"chunking": {"threshold_words": 3000}},
        "worker": {"poll_interval": 5},
    }


class TestMergeMissing:
    def test_adds_new_block_under_existing_parent(self):
        live = _live()
        added = merge_missing(live, DEFAULT)
        assert live["routing"]["reasoning"] == {"disabled_phases": ["formatter"]}
        assert "routing.reasoning" in added

    def test_adds_new_leaf_inside_existing_block(self):
        live = _live()
        added = merge_missing(live, DEFAULT)
        assert live["routing"]["chunking"]["tokens_per_word"] == 2.0
        assert live["routing"]["chunking"]["safety_factor"] == 0.8
        assert {"routing.chunking.tokens_per_word", "routing.chunking.safety_factor"} <= set(added)

    def test_never_overwrites_existing_value(self):
        live = _live()
        merge_missing(live, DEFAULT)
        assert live["backends"]["openrouter"]["max_tokens"] == 4096

    def test_lists_are_leaves_not_merged(self):
        live = {"routing": {"reasoning": {"disabled_phases": []}}}
        merge_missing(live, DEFAULT)
        assert live["routing"]["reasoning"]["disabled_phases"] == []

    def test_type_mismatch_keeps_live_value(self):
        live = {"routing": "legacy-string"}
        merge_missing(live, DEFAULT)
        assert live["routing"] == "legacy-string"

    def test_noop_when_nothing_missing(self):
        live = json.loads(json.dumps(DEFAULT))
        assert merge_missing(live, DEFAULT) == []
        assert live == DEFAULT


class TestConfigDrift:
    def test_reports_changed_leaf_with_both_values(self):
        assert ("backends.openrouter.max_tokens", 4096, 16384) in config_drift(_live(), DEFAULT)

    def test_ignores_missing_keys(self):
        paths = [p for p, _, _ in config_drift(_live(), DEFAULT)]
        assert "routing.reasoning" not in paths
        assert not any(p.startswith("routing.chunking.tokens") for p in paths)

    def test_no_drift_when_equal(self):
        assert config_drift(DEFAULT, DEFAULT) == []


class TestResolveConfigPath:
    @pytest.fixture
    def paths(self, monkeypatch, tmp_path):
        default = tmp_path / "default.json"
        default.write_text(json.dumps(DEFAULT))
        target = tmp_path / "state" / "llm-config.json"
        monkeypatch.setattr("api.services.config_path.DEFAULT_CONFIG", default)
        monkeypatch.setenv("LLM_CONFIG_PATH", str(target))
        return default, target

    def test_seeds_when_absent(self, paths):
        _, target = paths
        assert resolve_config_path() == target
        assert json.loads(target.read_text()) == DEFAULT

    def test_merges_new_keys_into_existing_file(self, paths):
        _, target = paths
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(_live()))

        resolve_config_path()

        live = json.loads(target.read_text())
        assert live["routing"]["reasoning"] == {"disabled_phases": ["formatter"]}
        assert live["routing"]["chunking"]["tokens_per_word"] == 2.0
        assert live["backends"]["openrouter"]["max_tokens"] == 4096, "operator value must survive"

    def test_untouched_when_nothing_to_add(self, paths):
        _, target = paths
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(DEFAULT))
        before = target.stat().st_mtime_ns

        resolve_config_path()

        assert target.stat().st_mtime_ns == before
        assert not list(target.parent.glob(".*.tmp")), "no temp file left behind"

    def test_unreadable_live_file_left_alone(self, paths):
        _, target = paths
        target.parent.mkdir(parents=True)
        target.write_text("{not json")

        assert resolve_config_path() == target
        assert target.read_text() == "{not json"

    @pytest.mark.parametrize("content", ["[]", '"a string"', "null", "42"])
    def test_non_object_live_file_left_alone(self, paths, content):
        _, target = paths
        target.parent.mkdir(parents=True)
        target.write_text(content)

        assert resolve_config_path() == target
        assert target.read_text() == content

    def test_first_seed_leaves_no_temp_file(self, paths):
        _, target = paths
        resolve_config_path()
        assert not list(target.parent.glob(".*.tmp"))


def test_roster_reads_live_path_without_seeding(monkeypatch, tmp_path):
    """Reading the roster config must not create or rewrite the live file."""
    from api.services import model_roster

    default = tmp_path / "default.json"
    default.write_text(json.dumps(DEFAULT))
    target = tmp_path / "state" / "llm-config.json"
    target.parent.mkdir()
    target.write_text(json.dumps(_live()))
    before = target.read_text()
    monkeypatch.setattr("api.services.config_path.DEFAULT_CONFIG", default)
    monkeypatch.setenv("LLM_CONFIG_PATH", str(target))

    assert model_roster._load_config() == _live()
    assert target.read_text() == before


def test_failed_write_leaves_no_temp_file(monkeypatch, tmp_path):
    """A write that raises must not strand its .tmp file."""
    from api.services import config_path

    target = tmp_path / "llm-config.json"

    def boom(tmp):
        tmp.write_text("partial")
        raise OSError("disk full")

    with pytest.raises(OSError):
        config_path._replace_atomically(target, boom)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


class TestDriftScript:
    """scripts/config_drift.py runs on every cardigan-deploy: it must report, not crash."""

    @pytest.fixture
    def run(self, monkeypatch, tmp_path):
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "config_drift_script", Path(__file__).resolve().parents[2] / "scripts" / "config_drift.py"
        )
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)

        default = tmp_path / "default.json"
        default.write_text(json.dumps(DEFAULT))
        target = tmp_path / "llm-config.json"
        monkeypatch.setattr("api.services.config_path.DEFAULT_CONFIG", default)
        monkeypatch.setenv("LLM_CONFIG_PATH", str(target))

        def _run(content, *argv):
            target.write_text(content)
            monkeypatch.setattr("sys.argv", ["config_drift.py", *argv])
            return script.main()

        return _run

    def test_reports_drift_and_exits_zero(self, run, capsys):
        assert run(json.dumps(_live())) == 0
        assert "backends.openrouter.max_tokens" in capsys.readouterr().out

    def test_strict_exits_one_on_drift(self, run):
        assert run(json.dumps(_live()), "--strict") == 1

    def test_unreadable_live_file_is_reported_not_raised(self, run, capsys):
        assert run("{not json") == 0
        assert "cannot compare" in capsys.readouterr().out

    @pytest.mark.parametrize("content", ["[]", "null", "42"])
    def test_non_object_live_file_is_reported_not_raised(self, run, capsys, content):
        assert run(content) == 0
        assert "not a JSON object" in capsys.readouterr().out

    def test_strict_exits_one_when_it_cannot_compare(self, run):
        assert run("{not json", "--strict") == 1
