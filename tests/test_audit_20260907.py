import gzip
import json
import sqlite3
import subprocess
import sys
import zlib
from contextlib import closing
from pathlib import Path

import pytest

from backend.cli import main as cli_main
from backend.cli import sqlite_backup, verified
from backend.database_lock import database_access
from backend.storage import Store
from scripts.mutation_smoke import classify_pytest_result


@pytest.mark.parametrize("fixture", ["v0510", "v0511"])
def test_real_legacy_artifacts_and_comparison_survive_upgrade(tmp_path, fixture):
    database = tmp_path / "legacy.db"
    database.write_bytes(gzip.decompress((Path(__file__).parent / f"fixtures/{fixture}.sqlite.gz").read_bytes()))
    with closing(sqlite3.connect(database)) as con:
        original_schema = con.execute("PRAGMA user_version").fetchone()[0]
        original = {
            row[0]: json.loads(row[1]) for row in con.execute("SELECT id, payload FROM decision_analysis_artifacts")
        }
        for identifier, payload in original.items():
            if "artifact_blob_hash" in payload:
                blob = con.execute(
                    "SELECT compressed_payload FROM artifact_blobs WHERE content_hash=?",
                    (payload["artifact_blob_hash"],),
                ).fetchone()
                original[identifier] = json.loads(zlib.decompress(blob[0]))
        comparison_id = con.execute("SELECT id FROM risk_comparison_runs").fetchone()[0]
        event_payloads = dict(con.execute("SELECT id,payload FROM work_order_execution_events").fetchall())
    command = ["--database", str(database), "migrate"]
    assert cli_main([*command, "dry-run"]) == 0
    assert cli_main([*command, "apply"]) == 0
    assert cli_main([*command, "verify"]) == 0
    backup = next(tmp_path.glob("legacy.pre-migrate-*.db"))
    assert cli_main([*command, "restore", str(backup), "--force"]) == 0
    with closing(sqlite3.connect(database)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == original_schema
        assert dict(con.execute("SELECT id,payload FROM work_order_execution_events").fetchall()) == event_payloads
    assert cli_main([*command, "apply"]) == 0
    assert cli_main([*command, "verify"]) == 0
    assert len(list(tmp_path.glob("legacy.pre-migrate-*.db"))) == 2
    store = Store(database, allow_migration=False, recover_runtime=False)
    with store._connect() as con:
        analyses = con.execute("SELECT id FROM decision_analysis_runs").fetchall()
    for row in analyses:
        run = store.get_decision_analysis_run("main", row["id"])
        assert run.effective_integrity.value == "VERIFIED", (run.id, run.error)
        for artifact in store.list_decision_analysis_artifacts("main", run.id):
            assert artifact.effective_integrity.value == "VERIFIED", artifact.id
            assert artifact.artifact_hash == original[artifact.id]["artifact_hash"]
    assert store.get_risk_comparison("main", comparison_id).effective_integrity.value == "VERIFIED"
    with store._connect() as con:
        assert dict(con.execute("SELECT id,payload FROM work_order_execution_events").fetchall()) == event_payloads
    statuses = {order.status.value for order in store.get_scenario("main").work_orders}
    assert {"completed", "started"} <= statuses
    assert store.dispatch_snapshot("main").operational_view.execution_integrity.value == "VERIFIED"


def test_restore_preserves_committed_wal_rows(tmp_path, capsys):
    source, destination = tmp_path / "source.db", tmp_path / "restored.db"
    Store(source)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE wal_probe(value TEXT)")
        connection.execute("INSERT INTO wal_probe VALUES ('committed in WAL')")
        connection.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        assert cli_main(["--database", str(destination), "migrate", "restore", str(source), "--force"]) == 0, (
            capsys.readouterr()
        )
        with closing(sqlite3.connect(destination)) as restored:
            assert restored.execute("SELECT value FROM wal_probe").fetchone()[0] == "committed in WAL"


def test_apply_refuses_live_database_owner_in_another_process(tmp_path):
    database = tmp_path / "live.db"
    Store(database)
    with database_access(database):
        result = subprocess.run(
            [sys.executable, "-m", "backend.cli", "--database", str(database), "migrate", "apply"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 2
    assert "DATABASE_IN_USE" in result.stderr


def test_backup_never_overwrites_or_accepts_same_source(tmp_path):
    database, destination = tmp_path / "source.db", tmp_path / "backup.db"
    Store(database)
    sqlite_backup(database, destination)
    original = destination.read_bytes()
    with pytest.raises(FileExistsError):
        sqlite_backup(database, destination)
    assert destination.read_bytes() == original
    with pytest.raises(ValueError):
        sqlite_backup(database, database)


def test_domain_verify_detects_corruption_with_valid_sqlite(tmp_path):
    database = tmp_path / "broken.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE scenarios SET current_snapshot_hash='forged' WHERE id='main'")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(RuntimeError, match="领域完整性"):
        verified(database, require_current_schema=True)


def test_current_schema_missing_command_manifest_is_not_backfilled(tmp_path):
    database = tmp_path / "missing-proof.db"
    store = Store(database)
    store.begin_command_record(
        "main:test", "missing-proof", "fingerprint", status="RUNNING", resource_type=None, resource_id=None, payload={}
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE command_keys SET command_manifest_payload=NULL, command_manifest_hash=NULL")
    reopened = Store(database)
    with pytest.raises(RuntimeError, match="完整性"):
        reopened.update_command_record(
            "main:test",
            "missing-proof",
            "fingerprint",
            status="COMPLETED",
            resource_type=None,
            resource_id=None,
            payload={},
        )
    assert reopened.audit_domain_integrity() != "VERIFIED"


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5, -9])
def test_mutation_infrastructure_exit_is_not_killed(tmp_path, exit_code):
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite tests="1"><testcase><failure message="AssertionError"/></testcase></testsuite></testsuites>'
    )
    assert classify_pytest_result(exit_code, report)[0] == "HARNESS_ERROR"


def test_mutation_no_tests_is_harness_error(tmp_path):
    report = tmp_path / "result.xml"
    report.write_text('<testsuites><testsuite tests="0"/></testsuites>')
    assert classify_pytest_result(5, report)[0] == "HARNESS_ERROR"


def test_mutation_import_failure_is_not_killed(tmp_path):
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite tests="1"><testcase><error message="ImportError"/></testcase></testsuite></testsuites>'
    )
    assert classify_pytest_result(1, report)[0] == "HARNESS_ERROR"


def test_mutation_requires_assertion_evidence(tmp_path):
    report: Path = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite tests="1"><testcase><failure message="AssertionError: invariant violated"/></testcase></testsuite></testsuites>'
    )
    assert classify_pytest_result(1, report) == ("KILLED", 1)
    report.write_text(
        '<testsuites><testsuite tests="1"><testcase><failure message="RuntimeError: broken environment"/></testcase></testsuite></testsuites>'
    )
    assert classify_pytest_result(1, report)[0] == "HARNESS_ERROR"
