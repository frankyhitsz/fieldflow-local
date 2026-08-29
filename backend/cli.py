from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .storage import SCHEMA_VERSION, Store

ROOT = Path(__file__).resolve().parents[1]


def default_database() -> Path:
    return Path(os.getenv("FIELDFLOW_DB", ROOT / "fieldflow.db")).resolve()


def database_status(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"database": str(path), "exists": False, "target_schema_version": SCHEMA_VERSION}
    with closing(sqlite3.connect(path)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
    return {
        "database": str(path),
        "exists": True,
        "schema_version": version,
        "target_schema_version": SCHEMA_VERSION,
        "migration_required": version < SCHEMA_VERSION,
        "integrity_check": integrity,
        "foreign_key_issue_count": len(foreign_key_issues),
        "table_count": tables,
        "size_bytes": path.stat().st_size,
    }


def verified(path: Path, *, require_current_schema: bool) -> dict[str, object]:
    status = database_status(path)
    if not status["exists"]:
        raise RuntimeError(f"数据库不存在：{path}")
    if status["integrity_check"] != "ok" or status["foreign_key_issue_count"] != 0:
        raise RuntimeError(f"数据库完整性检查失败：{json.dumps(status, ensure_ascii=False)}")
    if require_current_schema and status["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError(f"数据库 schema v{status['schema_version']}，应用要求 v{SCHEMA_VERSION}")
    return status


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_connection:
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            source_connection.backup(destination_connection)


def timestamped_backup_path(database: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return database.with_name(f"{database.stem}.{label}-{stamp}{database.suffix}")


def migrate_copy(source: Path, destination: Path) -> dict[str, object]:
    sqlite_backup(source, destination)
    Store(destination)
    return verified(destination, require_current_schema=True)


def atomic_replace(database: Path, migrated: Path) -> None:
    os.replace(migrated, database)
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def command_inspect(database: Path) -> int:
    print(json.dumps(database_status(database), ensure_ascii=False, indent=2))
    return 0


def command_backup(database: Path, output: Path | None) -> int:
    verified(database, require_current_schema=False)
    destination = output.resolve() if output else timestamped_backup_path(database, "backup")
    sqlite_backup(database, destination)
    verified(destination, require_current_schema=False)
    print(destination)
    return 0


def command_dry_run(database: Path) -> int:
    verified(database, require_current_schema=False)
    with tempfile.TemporaryDirectory(prefix="fieldflow-migrate-") as directory:
        candidate = Path(directory) / database.name
        status = migrate_copy(database, candidate)
        status["dry_run"] = True
        status["source_unchanged"] = True
        print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def command_apply(database: Path) -> int:
    before = verified(database, require_current_schema=False)
    if before["schema_version"] == SCHEMA_VERSION:
        print(json.dumps({**before, "changed": False}, ensure_ascii=False, indent=2))
        return 0
    backup = timestamped_backup_path(database, "pre-migrate")
    sqlite_backup(database, backup)
    candidate_file = tempfile.NamedTemporaryFile(
        prefix=f".{database.name}.migrating-",
        suffix=".db",
        dir=database.parent,
        delete=False,
    )
    candidate = Path(candidate_file.name)
    candidate_file.close()
    candidate.unlink()
    try:
        after = migrate_copy(database, candidate)
        atomic_replace(database, candidate)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    print(json.dumps({**after, "changed": True, "backup": str(backup)}, ensure_ascii=False, indent=2))
    return 0


def command_verify(database: Path) -> int:
    print(json.dumps(verified(database, require_current_schema=True), ensure_ascii=False, indent=2))
    return 0


def command_restore(database: Path, backup: Path, force: bool) -> int:
    if not force:
        raise RuntimeError("restore 会替换当前数据库；请在确认停机和备份路径后添加 --force")
    backup = backup.resolve()
    verified(backup, require_current_schema=False)
    if database.exists():
        safety_backup = timestamped_backup_path(database, "pre-restore")
        sqlite_backup(database, safety_backup)
    else:
        safety_backup = None
    candidate_file = tempfile.NamedTemporaryFile(
        prefix=f".{database.name}.restoring-",
        suffix=".db",
        dir=database.parent,
        delete=False,
    )
    candidate = Path(candidate_file.name)
    candidate_file.close()
    try:
        shutil.copy2(backup, candidate)
        verified(candidate, require_current_schema=False)
        atomic_replace(database, candidate)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    print(
        json.dumps(
            {"restored_from": str(backup), "database": str(database), "safety_backup": str(safety_backup)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def artifact_store(database: Path) -> Store:
    verified(database, require_current_schema=True)
    return Store(database)


def command_artifact_inspect(database: Path) -> int:
    print(json.dumps(artifact_store(database).artifact_blob_stats(), ensure_ascii=False, indent=2))
    return 0


def command_artifact_export(database: Path, blob_hash: str, output: Path) -> int:
    payload = artifact_store(database).export_artifact_blob(blob_hash)
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    print(destination)
    return 0


def command_artifact_prune(database: Path, retention_days: int, apply: bool) -> int:
    pruned = artifact_store(database).prune_artifact_blobs(retention_days=retention_days, apply=apply)
    print(
        json.dumps(
            {"apply": apply, "retention_days": retention_days, "eligible_count": len(pruned), "content_hashes": pruned},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_artifact_vacuum(database: Path) -> int:
    store = artifact_store(database)
    before = database.stat().st_size
    store.vacuum_artifact_storage()
    print(json.dumps({"size_before": before, "size_after": database.stat().st_size}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="fieldflow")
    root.add_argument("--database", type=Path, default=default_database())
    commands = root.add_subparsers(dest="command", required=True)
    migration = commands.add_parser("migrate", help="inspect and migrate a FieldFlow SQLite database")
    actions = migration.add_subparsers(dest="migration_command", required=True)
    actions.add_parser("inspect")
    backup = actions.add_parser("backup")
    backup.add_argument("--output", type=Path)
    actions.add_parser("dry-run")
    actions.add_parser("apply")
    actions.add_parser("verify")
    restore = actions.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--force", action="store_true")
    artifacts = commands.add_parser("artifacts", help="inspect and maintain content-addressed artifacts")
    artifact_actions = artifacts.add_subparsers(dest="artifact_command", required=True)
    artifact_actions.add_parser("inspect")
    export = artifact_actions.add_parser("export")
    export.add_argument("content_hash")
    export.add_argument("output", type=Path)
    prune = artifact_actions.add_parser("prune")
    prune.add_argument("--retention-days", type=int, default=30)
    prune.add_argument("--apply", action="store_true")
    artifact_actions.add_parser("vacuum")
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    database = arguments.database.resolve()
    try:
        if arguments.command == "migrate":
            match arguments.migration_command:
                case "inspect":
                    return command_inspect(database)
                case "backup":
                    return command_backup(database, arguments.output)
                case "dry-run":
                    return command_dry_run(database)
                case "apply":
                    return command_apply(database)
                case "verify":
                    return command_verify(database)
                case "restore":
                    return command_restore(database, arguments.backup, arguments.force)
        if arguments.command == "artifacts":
            match arguments.artifact_command:
                case "inspect":
                    return command_artifact_inspect(database)
                case "export":
                    return command_artifact_export(database, arguments.content_hash, arguments.output)
                case "prune":
                    return command_artifact_prune(database, arguments.retention_days, arguments.apply)
                case "vacuum":
                    return command_artifact_vacuum(database)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"fieldflow: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
