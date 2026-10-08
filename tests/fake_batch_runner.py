#!/usr/bin/env python3
"""Fake stand-in for run_pi_worker.py used ONLY by tests/test_batch.py.

The production batch driver never references this file: the test entrypoint
imports the batch module, patches its module-level ``RUNNER_PATH`` to point
here, and the driver launches it exactly like the real single-worker runner.
This fake reads the same child argv the driver forwards and its own optional
``BATCH_FAKE_*`` environment hooks. It never talks to pi, providers, brokers,
or any production record store.

When *imported* (as the batch driver does for dry validation), this module only
defines the runner interface — ``parse_args``, ``validate_launch_inputs``,
``harness_is_pi``, ``validate_receipt_file`` — mirroring ``run_pi_worker.py``
without side effects. The fake child behavior runs only when the module is
executed as a script (``__main__``), exactly how the driver spawns it.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Production PURE profile loader, imported directly (never the runner) so this
# fake's no-enabled-profile gate agrees exactly with the real configuration
# schema, including the version-2 roles array. Importing only defines the
# module; load_profile_config reads the file lazily inside validation.
_PROFILE_CONFIG_PATH = (Path(__file__).resolve().parent.parent
                        / "scripts" / "profile_config.py")
_profile_config_spec = importlib.util.spec_from_file_location(
    "fake_batch_profile_config", _PROFILE_CONFIG_PATH)
assert _profile_config_spec and _profile_config_spec.loader
_profile_config = importlib.util.module_from_spec(_profile_config_spec)
_profile_config_spec.loader.exec_module(_profile_config)

# Same bounded shape as the real runner's TASK_GROUP_ID_RE.
_TASK_GROUP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# ---------------------------------------------------------------------------
# Runner interface used by the batch driver's validation-only path
# (mirrors run_pi_worker.py; raises ValueError instead of exiting so the batch
# driver can attach a task id to the failure).
# ---------------------------------------------------------------------------


def harness_is_pi() -> bool:
    """Same harness check as the real runner, read from the live environment."""
    if os.environ.get("PI_CODING_AGENT", "").lower() == "true":
        return True
    return os.environ.get("AI_AGENT", "").lower() == "pi"


def validate_receipt_file(value: str | None) -> None:
    """Read-only validation of a receipt destination (real semantics subset)."""
    if value is None:
        return
    if not value:
        raise ValueError("--receipt-file must not be empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("--receipt-file must be an absolute path")
    if path.is_symlink():
        raise ValueError("--receipt-file must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("--receipt-file must name a regular file")
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("--receipt-file must not traverse a symlink parent")
        if not parent.exists():
            # Missing parents are allowed; the driver/writer creates them 0700.
            continue
        if not parent.is_dir():
            raise ValueError("--receipt-file parent is not a directory")
        mode = parent.stat().st_mode
        if (mode & 0o022) and not (mode & 0o1000):
            raise ValueError("--receipt-file parent must not be group/other writable")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse exactly the flags the batch driver forwards to a child."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workdir")
    parser.add_argument("--contract-file")
    parser.add_argument("--routing-class", choices=("standard",))
    parser.add_argument("--profile", default="auto")
    parser.add_argument("--thinking", default="medium")
    parser.add_argument("--tools", choices=("read-only", "write"))
    parser.add_argument("--delegation-id")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--pi", default=None)
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--receipt-file")
    parser.add_argument("--transport", choices=("rpc", "intercom"), default="rpc")
    parser.add_argument("--intercom-extension", default=None)
    parser.add_argument("--supervisor", default=None)
    parser.add_argument("--intercom-cli", default=None)
    parser.add_argument("--task-group-id", default=None)
    # Same global flag the real runner accepts: forwarded only when the batch
    # driver was given --profiles-file.
    parser.add_argument("--profiles-file", default=None)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def validate_launch_inputs(args: argparse.Namespace) -> dict[str, str]:
    """Validate the required child inputs without writing anything.

    Mirrors the real runner's required-field and filesystem checks closely
    enough that a batch the fake accepts is one the real runner would also
    accept structurally: workdir/contract/tools/delegation presence, an
    existing workdir, and an existing UTF-8 contract file.
    """
    if not args.workdir:
        raise ValueError("--workdir is required")
    if not args.contract_file:
        raise ValueError("--contract-file is required")
    if not args.tools:
        raise ValueError("--tools is required")
    if not args.delegation_id:
        raise ValueError("--delegation-id is required")
    workdir = Path(args.workdir)
    if not workdir.is_absolute():
        raise ValueError("--workdir must be an absolute path")
    if not workdir.is_dir():
        raise ValueError(f"not a directory: {workdir}")
    contract = Path(args.contract_file)
    if not contract.is_file():
        raise ValueError(f"contract file not found: {contract}")
    try:
        contract.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"contract file unreadable: {exc}") from exc
    if args.transport == "intercom":
        if not args.intercom_extension:
            raise ValueError("--transport intercom requires --intercom-extension")
        if not args.supervisor:
            raise ValueError("--transport intercom requires --supervisor")
    if getattr(args, "task_group_id", None) is not None \
            and not _TASK_GROUP_ID_RE.fullmatch(args.task_group_id):
        raise ValueError("--task-group-id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    # Mirrored no-enabled-profile gate: the real runner resolves the same
    # precedence from the production pure loader (explicit --profiles-file >
    # PI_WORKER_PROFILES_FILE > ~/.config/pi-worker/profiles.json) and needs at
    # least one enabled profile assigned to the sub role. An absent env/default
    # file is an empty configuration; a malformed explicit file raises the
    # loader's actionable error. Nothing is written on either path.
    if getattr(args, "profile", "auto") == "auto":
        config = _profile_config.load_profile_config(
            getattr(args, "profiles_file", None))
        if not config.profiles_for_role("sub", enabled_only=True):
            raise ValueError("no enabled standard profile: configure "
                             "profiles via --profiles-file or "
                             "PI_WORKER_PROFILES_FILE")
    return {"workdir": str(workdir), "tools": args.tools,
            "routing_class": args.routing_class or "standard"}


# ---------------------------------------------------------------------------
# Fake child behavior: runs ONLY when executed as a script, never on import.
# ---------------------------------------------------------------------------

_CHILD_ARGPARSE = argparse.ArgumentParser(add_help=False)
_CHILD_ARGPARSE.add_argument("--receipt-file")
_CHILD_ARGPARSE.add_argument("--delegation-id")


def _child_body() -> None:
    LONG_SLEEP = 120.0

    args, _ = _CHILD_ARGPARSE.parse_known_args()
    receipt = args.receipt_file
    delegation = args.delegation_id
    task_id = Path(receipt).parent.name if receipt else (delegation or "task")
    markers = Path(receipt).parent / "markers" if receipt else Path("markers")

    def _mark(name: str, payload: str) -> None:
        markers.mkdir(parents=True, exist_ok=True)
        with open(markers / name, "a", encoding="utf-8") as out:
            out.write(payload + "\n")

    def _record_argv() -> None:
        if not receipt:
            return
        markers.mkdir(parents=True, exist_ok=True)
        (markers / f"argv-{task_id}.json").write_text(
            json.dumps(sys.argv[1:]), encoding="utf-8")

    def _barrier_wait() -> bool:
        path, threshold = os.environ.get("BATCH_FAKE_BARRIER"), \
            os.environ.get("BATCH_FAKE_BARRIER_THRESHOLD")
        if not path or not threshold:
            return False
        deadline = time.time() + 5.0
        joined = False
        while time.time() < deadline:
            with open(path, "a+", encoding="utf-8") as gate:
                fcntl.flock(gate, fcntl.LOCK_EX)
                gate.seek(0)
                arrivals = [line for line in gate.read().splitlines() if line]
                if len(arrivals) >= int(threshold):
                    joined = True
                gate.write(task_id + "\n")
                fcntl.flock(gate, fcntl.LOCK_UN)
            if joined:
                break
            time.sleep(0.02)
        if joined:
            _mark("concurrent-" + task_id, "joined")
        return joined

    def _record_timing() -> None:
        path = os.environ.get("BATCH_FAKE_TIMINGS")
        if not path:
            return
        with open(path, "a", encoding="utf-8") as out:
            fcntl.flock(out, fcntl.LOCK_EX)
            out.write(json.dumps({"task_id": task_id, "start": time.time()}) + "\n")
            fcntl.flock(out, fcntl.LOCK_UN)

    def _write_receipt(payload: dict) -> None:
        with open(receipt, "w", encoding="utf-8") as out:
            json.dump(payload, out)
            out.write("\n")

    def _valid_completed() -> dict:
        return {"run_id": os.urandom(16).hex(), "delegation_id": delegation,
                "outcome": {"status": "completed"}}

    def _spawn_descendant() -> None:
        child = subprocess.Popen([sys.executable, "-c",
                                  f"import time; time.sleep({LONG_SLEEP})"],
                                 start_new_session=False)
        _mark(f"descendant-{task_id}.pid", str(child.pid))

    def _env(name: str, default: str) -> str:
        return os.environ.get(f"{name}_{task_id}", os.environ.get(name, default))

    mode = _env("BATCH_FAKE_MODE", "ok")
    sleep = float(_env("BATCH_FAKE_SLEEP", "0.15"))

    _record_argv()
    _mark("start-" + task_id, str(time.time()))
    _barrier_wait()
    _record_timing()

    if mode == "spawn_descendant":
        _spawn_descendant()
        time.sleep(LONG_SLEEP)
    elif mode == "slow":
        time.sleep(sleep)
        _write_receipt(_valid_completed())
    elif mode == "ok":
        time.sleep(sleep)
        _write_receipt(_valid_completed())
    elif mode == "fail":
        time.sleep(sleep)
        _write_receipt({"run_id": os.urandom(16).hex(), "delegation_id": delegation,
                        "outcome": {"status": "failed"}})
    elif mode == "nonzero_ok":
        time.sleep(sleep)
        _write_receipt(_valid_completed())
    elif mode == "bad":
        time.sleep(sleep)
        with open(receipt, "w", encoding="utf-8") as out:
            out.write("{not valid json")
    elif mode == "wrong_delegation":
        time.sleep(sleep)
        payload = _valid_completed()
        payload["delegation_id"] = "someone-else"
        _write_receipt(payload)
    elif mode == "duplicate_run":
        time.sleep(sleep)
        payload = _valid_completed()
        payload["run_id"] = "duplicate-run-id"
        _write_receipt(payload)
    # mode "none": leave the receipt unwritten.

    _mark("end-" + task_id, str(time.time()))
    exit_code = {"ok": 0, "slow": 0, "bad": 0, "none": 0, "wrong_delegation": 0,
                 "duplicate_run": 0, "nonzero_ok": 3, "fail": 2}.get(mode, 0)
    sys.exit(exit_code)


if __name__ == "__main__":
    _child_body()
