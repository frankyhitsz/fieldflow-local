"""Build wheel + sdist, install away from the checkout, exercise the HTTP API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"{command}:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def smoke(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    built = run(
        [
            sys.executable,
            "-c",
            "import json; from setuptools.build_meta import build_sdist, build_wheel; "
            f"source = build_sdist({str(output)!r}); wheel = build_wheel({str(output)!r}); "
            "print(json.dumps({'sdist': source, 'wheel': wheel}))",
        ],
        cwd=ROOT,
    )
    filenames = json.loads(built.splitlines()[-1])
    wheel = output / filenames["wheel"]
    source = output / filenames["sdist"]
    with tempfile.TemporaryDirectory(prefix="fieldflow-installed-") as directory:
        root = Path(directory)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("FIELDFLOW_DB", None)
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = root / "venv/bin/python"
        run(
            [str(python), "-m", "pip", "install", "--require-hashes", "-r", str(ROOT / "requirements-runtime.lock")],
            cwd=root,
            env=environment,
        )
        run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], cwd=root, env=environment)
        run([str(python), "-m", "pip", "check"], cwd=root, env=environment)
        manifest = json.loads(
            run(
                [
                    str(python),
                    "-c",
                    'import json, backend; from backend.provenance import decision_runtime_manifest,release_manifest; print(json.dumps({"path":backend.__file__,"decision":decision_runtime_manifest().model_dump(),"release":release_manifest().model_dump()}))',
                ],
                cwd=root,
                env=environment,
            )
        )
        assert str(ROOT) not in manifest["path"], manifest["path"]
        assert manifest["decision"]["runtime_distributions"]
        assert "not-installed" not in manifest["decision"]["runtime_distributions"].values()
        assert manifest["release"]["frontend_dependency_lock_hash"] != "missing"
        run(
            [str(root / "venv/bin/fieldflow"), "--database", str(root / "app.db"), "migrate", "inspect"],
            cwd=root,
            env=environment,
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        environment["FIELDFLOW_DB"] = str(root / "app.db")
        with (root / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [str(python), "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=root,
                env=environment,
                stdout=log,
                stderr=log,
            )
            base = f"http://127.0.0.1:{port}"

            def request(path, body=None):
                data = json.dumps(body).encode() if body is not None else None
                with urllib.request.urlopen(
                    urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"}),
                    timeout=30,
                ) as response:
                    return json.load(response)

            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        scenario = request("/api/scenarios/main")
                        break
                    except OSError:
                        if process.poll() is not None:
                            raise RuntimeError((root / "server.log").read_text()) from None
                        time.sleep(0.1)
                else:
                    raise RuntimeError("installed application did not start")
                with urllib.request.urlopen(base, timeout=5) as response:
                    assert '<div id="root">' in response.read().decode()
                baseline = request("/api/scenarios/main/baseline", {})
                assignment = next(a for a in baseline["assignments"] if a["sequence"] == 1)
                order_id = assignment["work_order_id"]
                started = request(
                    f"/api/scenarios/main/work-orders/{order_id}/start",
                    {
                        "technician_id": assignment["technician_id"],
                        "occurred_at": assignment["start_time"],
                        "expected_revision": scenario["revision"],
                        "idempotency_key": "wheel-smoke-start",
                    },
                )
                completed = request(
                    f"/api/scenarios/main/work-orders/{order_id}/complete",
                    {
                        "technician_id": assignment["technician_id"],
                        "occurred_at": assignment["finish_time"],
                        "expected_revision": started["scenario"]["revision"],
                        "idempotency_key": "wheel-smoke-complete",
                    },
                )
                assert completed["event"]["booking_id"] == started["event"]["booking_id"]
                replanned = request(
                    "/api/scenarios/main/replan", {"planning_time": assignment["finish_time"], "time_limit_seconds": 1}
                )
                assert replanned["version"] == 2
                plan = request("/api/scenarios/main/plan-versions")[-1]
                # Exercise packaged decision source + lock manifests through a real calculation.
                analysis = request(
                    f"/api/scenarios/main/plan-versions/{plan['id']}/analysis-runs", {"analysis_type": "COST"}
                )
                assert analysis["effective_integrity"] == "VERIFIED"
                assert analysis["status"] == "COMPLETED"
                with urllib.request.urlopen(
                    base + f"/api/scenarios/main/plan-versions/{plan['id']}/report", timeout=10
                ) as response:
                    assert response.status == 200
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        # A source distribution must rebuild without access to frontend sources
        # or the checkout: its already-built UI and build inputs are included.
        unpacked = root / "sdist"
        unpacked.mkdir()
        with tarfile.open(source) as archive:
            archive.extractall(unpacked, filter="data")
        source_root = next(unpacked.iterdir())
        run(
            [
                sys.executable,
                "-c",
                f"from setuptools.build_meta import build_wheel; build_wheel({str(root / 'rebuilt')!r})",
            ],
            cwd=source_root,
            env=environment,
        )
        # Development setup must not require a pre-existing npm build.
        editable = root / "editable-without-ui"
        editable.mkdir()
        for name in ("pyproject.toml", "setup.py"):
            shutil.copy2(ROOT / name, editable / name)
        shutil.copytree(ROOT / "backend", editable / "backend", ignore=shutil.ignore_patterns("__pycache__"))
        run(
            [str(python), "-m", "pip", "install", "--require-hashes", "-r", str(ROOT / "requirements-build.lock")],
            cwd=root,
            env=environment,
        )
        run(
            [str(python), "-m", "pip", "install", "-e", str(editable), "--no-deps", "--no-build-isolation"],
            cwd=root,
            env=environment,
        )
    artifacts = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in (wheel, source)}
    report = {
        "artifacts": artifacts,
        "decision_manifest": manifest["decision"],
        "service_flow": "baseline → start → complete → replan → COST → report",
        "sdist_rebuild": "passed",
        "editable_without_ui": "passed",
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("\n".join(f"{digest}  {name}" for name, digest in artifacts.items()) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
