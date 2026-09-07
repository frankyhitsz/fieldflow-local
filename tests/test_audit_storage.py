import json
import shutil
import sqlite3
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event

import pytest

from backend import provenance
from backend.cli import timestamped_backup_path
from backend.hashing import content_hash
from backend.storage import DecisionAnalysisIntegrityError, Store, _read_artifact_blob, _store_artifact_blob


def test_dispatch_checkpoint_is_invalidated_by_external_history_change(tmp_path, monkeypatch):
    database = tmp_path / "checkpoint.db"
    store = Store(database)
    scenario = store.get_scenario("main")
    scenario.revision = 1
    store.save_scenario(scenario, "checkpoint", expected_revision=0)
    calls = []
    original = store._load_revision_row

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_load_revision_row", counted)
    assert store.dispatch_snapshot("main").revision_history_integrity.value == "VERIFIED"
    assert len(calls) >= 2
    calls.clear()
    assert store.dispatch_snapshot("main").revision_history_integrity.value == "VERIFIED"
    assert not calls
    with closing(sqlite3.connect(database)) as con, con:
        con.execute("UPDATE scenario_revisions SET reason='external corruption' WHERE scenario_id='main' AND number=0")
    snapshot = store.dispatch_snapshot("main")
    assert calls and snapshot.revision_history_integrity.value == "FAILED"
    assert snapshot.revision_head_integrity.value == "VERIFIED"


def test_worker_open_never_seeds_or_scans_history(tmp_path, monkeypatch):
    database = tmp_path / "open.db"
    Store(database)

    def forbidden(*_args, **_kwargs):
        pytest.fail("ordinary worker open must not run a history scan")

    monkeypatch.setattr(Store, "_scan_revision_integrity", forbidden)
    monkeypatch.setattr(Store, "_scan_execution_status_integrity", forbidden)
    monkeypatch.setattr("backend.storage.all_fixtures", forbidden)
    assert Store(database, allow_migration=False).get_scenario("main") is not None


def test_backup_names_in_same_second_are_unique(tmp_path):
    paths = {timestamped_backup_path(tmp_path / "app.db", "backup") for _ in range(100)}
    assert len(paths) == 100


def test_store_and_read_share_blob_limits(tmp_path, monkeypatch):
    import backend.storage as storage

    store = Store(tmp_path / "blobs.db")
    raw = json.dumps({"payload": "x" * 300})
    with store._connect() as con:
        identifier = _store_artifact_blob(con, raw)
        con.execute("UPDATE artifact_blobs SET uncompressed_size=12 WHERE content_hash=?", (identifier,))
        with pytest.raises(ValueError, match="integrity"):
            _store_artifact_blob(con, raw)
        with pytest.raises(ValueError, match="integrity"):
            _read_artifact_blob(con, identifier)
        monkeypatch.setattr(storage, "_MAX_ARTIFACT_BLOB_BYTES", 128)
        with pytest.raises(ValueError, match="limit"):
            _store_artifact_blob(con, raw)
        malicious = b"[" * 129 + b"0" + b"]" * 129
        monkeypatch.setattr(storage, "_MAX_ARTIFACT_BLOB_BYTES", 1024)
        con.execute(
            "INSERT INTO artifact_blobs VALUES (?, ?, ?, ?, ?)",
            ("deep", "ZLIB_JSON_V1", zlib.compress(malicious), len(malicious), "2000-01-01"),
        )
        with pytest.raises(ValueError, match="JSON"):
            _read_artifact_blob(con, "deep")


def test_blob_prune_keeps_referenced_rows(tmp_path):
    store = Store(tmp_path / "gc.db")
    with store._connect() as con:
        referenced = _store_artifact_blob(con, "{}")
        orphan = _store_artifact_blob(con, '{"orphan":true}')
        con.execute("UPDATE artifact_blobs SET created_at='2000-01-01T00:00:00+00:00'")
        con.execute(
            "INSERT INTO strategy_experiments(id,scenario_id,payload,created_at) VALUES ('EXP-GC','main','{}','2000-01-01')"
        )
        con.execute(
            "INSERT INTO schedule_artifacts(id, experiment_id, role, payload, artifact_blob_hash, created_at) VALUES ('ref','EXP-GC','candidate','{}',?,'2000-01-01')",
            (referenced,),
        )
    assert store.prune_artifact_blobs(retention_days=1, apply=True) == [orphan]
    with store._connect() as con:
        assert _read_artifact_blob(con, referenced) == b"{}"


def test_prune_racing_new_reference_keeps_blob(tmp_path, monkeypatch):
    store = Store(tmp_path / "gc-race.db")
    collector = Store(store.path, allow_migration=False)
    attempted = Event()
    original = collector._unlocked_connect

    @contextmanager
    def traced():
        with original() as con:
            con.set_trace_callback(lambda sql: attempted.set() if sql.startswith("BEGIN IMMEDIATE") else None)
            yield con

    monkeypatch.setattr(collector, "_unlocked_connect", traced)
    with store._connect() as con:
        identifier = _store_artifact_blob(con, '{"race":true}')
        con.execute("UPDATE artifact_blobs SET created_at='2000-01-01T00:00:00+00:00'")
        con.execute("INSERT INTO strategy_experiments VALUES ('EXP-RACE','main','{}','2000-01-01')")
    with ThreadPoolExecutor() as executor:
        with store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO schedule_artifacts(id,experiment_id,role,payload,artifact_blob_hash,created_at) "
                "VALUES ('race-ref','EXP-RACE','candidate','{}',?,'2000-01-01')",
                (identifier,),
            )
            pruning = executor.submit(collector.prune_artifact_blobs, retention_days=1, apply=True)
            try:
                assert attempted.wait(10)
            finally:
                con.commit()
        assert pruning.result(10) == []
    with store._connect() as con:
        assert _read_artifact_blob(con, identifier) == b'{"race":true}'


def test_development_configuration_does_not_change_runtime_dependency_hash(tmp_path):
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nrequires-python=">=3.11"\ndependencies=["pydantic==2.13.4"]\n[tool.pyright]\npythonVersion="3.11"\n'
    )
    before = content_hash(provenance.runtime_dependency_inputs(tmp_path))
    project.write_text(
        project.read_text().replace('pythonVersion="3.11"', 'pythonVersion="3.13"')
        + '\n[project.optional-dependencies]\ndev=["pytest==9.1.1"]\n'
    )
    assert content_hash(provenance.runtime_dependency_inputs(tmp_path)) == before
    project.write_text(project.read_text().replace("pydantic==2.13.4", "pydantic==2.13.5"))
    assert content_hash(provenance.runtime_dependency_inputs(tmp_path)) != before


@pytest.mark.parametrize("filename", ["normalization.py", "execution.py", "main.py"])
def test_real_decision_source_changes_invalidate_build_identity(tmp_path, filename):
    root = Path(provenance.__file__).parent
    shutil.copytree(root, tmp_path / "backend", ignore=shutil.ignore_patterns("__pycache__", "web", "_runtime"))
    copy = tmp_path / "backend"
    before = content_hash(provenance.decision_source_closure(copy))
    file = copy / filename
    if filename == "main.py":
        file.write_text(
            file.read_text().replace(
                "    plan = require_plan_for_use(scenario_id, plan.id, PlanUseCase.analyze)",
                "    # changed decision execution policy\n    plan = require_plan_for_use(scenario_id, plan.id, PlanUseCase.analyze)",
            )
        )
    else:
        file.write_text(file.read_text() + "\nDECISION_POLICY_CHANGE = True\n")
    assert content_hash(provenance.decision_source_closure(copy)) != before


@pytest.mark.parametrize(
    "field,value",
    [
        ("termination_reason", "FORGED"),
        ("solution_found", False),
        ("solver_version", "forged"),
        ("solver_policy_fingerprint", "forged"),
        ("planning_context_hash", "forged"),
    ],
)
def test_result_manifest_must_match_run_semantics(tmp_path, field, value):
    from fastapi.testclient import TestClient

    from backend.main import create_app

    database = tmp_path / "run-proof.db"
    with TestClient(create_app(db_path=database)) as client:
        assert client.post("/api/scenarios/main/baseline").status_code == 200
    store = Store(database)
    with closing(sqlite3.connect(database)) as con, con:
        identifier, payload = con.execute("SELECT id,payload FROM schedule_runs LIMIT 1").fetchone()
        changed = json.loads(payload)
        changed[field] = value
        with pytest.raises(sqlite3.IntegrityError, match="terminal schedule run is immutable"):
            con.execute("UPDATE schedule_runs SET payload=? WHERE id=?", (json.dumps(changed), identifier))
        triggers = con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='schedule_runs'"
        ).fetchall()
        for (name,) in triggers:
            con.execute(f'DROP TRIGGER "{name}"')
        con.execute("UPDATE schedule_runs SET payload=? WHERE id=?", (json.dumps(changed), identifier))
    with pytest.raises(DecisionAnalysisIntegrityError):
        store.get_schedule_run(identifier)
