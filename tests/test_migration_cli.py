import json
import sqlite3
from contextlib import closing

import pytest

from backend.cli import main
from backend.storage import SCHEMA_VERSION, Store


def test_migration_cli_dry_run_apply_verify_and_restore(tmp_path, capsys):
    database = tmp_path / "fieldflow.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")

    assert main(["--database", str(database), "migrate", "dry-run"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["schema_version"] == SCHEMA_VERSION
    assert dry_run["source_unchanged"] is True
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1
    assert main(["--database", str(database), "migrate", "apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["schema_version"] == SCHEMA_VERSION
    backup = applied["backup"]
    assert main(["--database", str(database), "migrate", "verify"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["integrity_check"] == "ok"

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE scenarios SET updated_at='mutated-after-backup' WHERE id='main'")
    assert main(["--database", str(database), "migrate", "restore", backup]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(["--database", str(database), "migrate", "restore", backup, "--force"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["restored_from"] == backup
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1


def test_application_store_rejects_implicit_upgrade(tmp_path):
    database = tmp_path / "explicit-upgrade.db"
    Store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    with pytest.raises(RuntimeError, match="explicit migration"):
        Store(database, allow_migration=False)
