#!/usr/bin/env python3
"""Minimal concurrent batch driver for Pi worker subtasks.

Runs a manifest of pre-declared, mutually disjoint tasks, each as a fixed
single-run child ``run_pi_worker.py`` (module-level ``RUNNER_PATH``, patchable by
tests only), up to ``--max-concurrency``. The manifest declares *conflicts*, not
a sandbox: the driver refuses to launch a batch whose declared scopes or
exclusive resources overlap, but it does not confine a worker's filesystem
access. There is no DAG dependency graph, no automatic retry, no resource
serialization, no raw shell execution, and no ``pi`` spawn from this driver.

Safety posture:

* Exactly one child runner per task, each with a distinct ``delegation_id``.
* Only independent/disjoint packages; write/write and write/read overlaps are
  rejected after canonical absolute resolution, including ancestor/descendant
  and symlink aliases across workdirs. Read/read overlaps are allowed.
* Default routing is standard with automatic profile selection; unsupported
  routing classes are rejected rather than silently rerouted.
* Every child is started in its own session (``start_new_session=True``) so an
  interrupt can terminate only the owned process group, descendants included.
* Pending tasks stay ``not_started``; a successful receipt only records
  execution completion pending the main agent's review, never acceptance.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from itertools import product
from pathlib import Path, PurePosixPath
from typing import Any

RUNNER_PATH = Path(__file__).resolve().with_name("run_pi_worker.py")
WATCHDOG_GRACE_SECONDS = 15.0
SHUTDOWN_WAIT_SECONDS = 2.0
KILL_WAIT_SECONDS = 2.0
POLL_SECONDS = 0.05
SUMMARY_NAME = "batch-summary.json"
DEFAULT_MAX_CONCURRENCY = 3

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MANIFEST_FIELDS = frozenset({"version", "tasks"})
_TASK_FIELDS = frozenset({"id", "delegation_id", "workdir", "contract_file",
                          "routing_class", "profile", "thinking", "tools",
                          "scope", "resources", "timeout",
                          # Optional; validated and forwarded as the child's
                          # --task-group-id only when present.
                          "task_group_id"})
_TASK_REQUIRED = frozenset({"id", "delegation_id", "workdir", "contract_file",
                            "tools", "scope"})
_ENUMS = {"routing_class": frozenset({"standard"}),
          "tools": frozenset({"write", "read-only"})}
_TASK_DEFAULTS = {"routing_class": "standard", "profile": "auto",
                  "thinking": "medium", "resources": [], "timeout": 900.0,
                  # Optional opaque caller-supplied group label forwarded to the
                  # child as --task-group-id only when present in the manifest.
                  # It never participates in conflict detection, delegation
                  # identity, or child routing/model selection.
                  "task_group_id": None}


class BatchError(RuntimeError):
    def __init__(self, message: str, *, code: str = "batch_error",
                 task_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.task_id = task_id


class BatchInterrupted(RuntimeError):
    pass


def _runner_module():
    spec = importlib.util.spec_from_file_location("pi_worker_runner",
                                                  str(RUNNER_PATH))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exc_message(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__


def _resolve(path: Path, *, strict: bool, label: str, task_id: str) -> Path:
    """Canonicalize a path, turning every filesystem failure into a sanitized BatchError.

    No traceback and no raw OS text escapes the batch driver: callers get a
    ``BatchError`` naming the offending task and field only.
    """
    try:
        return path.resolve(strict=strict)
    except (OSError, ValueError, RuntimeError) as exc:
        raise BatchError(f"task {label} could not be resolved: {_exc_message(exc)}",
                         code="invalid_path", task_id=task_id) from exc


def _load_manifest(path: str) -> list[dict[str, Any]]:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        raise BatchError("--manifest must be an absolute path", code="absolute_path_required")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise BatchError(f"manifest unreadable: {_exc_message(exc)}", code="manifest_unreadable")
    except ValueError as exc:
        raise BatchError(f"manifest JSON invalid: {_exc_message(exc)}", code="invalid_manifest_json")
    if not isinstance(data, dict):
        raise BatchError("manifest must be a JSON object", code="invalid_manifest")
    unknown = set(data) - _MANIFEST_FIELDS
    if unknown:
        raise BatchError(f"unknown manifest fields: {sorted(unknown)}",
                         code="unknown_manifest_field")
    if data.get("version") != 1:
        raise BatchError("manifest version must be 1", code="invalid_manifest_version")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise BatchError("manifest tasks must be a non-empty list", code="invalid_manifest")
    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_delegations: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise BatchError("every manifest task must be an object", code="invalid_task")
        unknown = set(raw) - _TASK_FIELDS
        if unknown:
            raise BatchError(f"unknown task fields: {sorted(unknown)}",
                             code="unknown_task_field")
        missing = _TASK_REQUIRED - set(raw)
        if missing:
            raise BatchError(f"missing task fields: {sorted(missing)}", code="invalid_task")
        task = dict(_TASK_DEFAULTS)
        task.update(raw)
        tid = task["id"]
        if not isinstance(tid, str) or not _ID_RE.fullmatch(tid):
            raise BatchError(f"task id must match [a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}: {tid!r}",
                             code="invalid_id", task_id=str(tid)[:64])
        if tid in seen_ids:
            raise BatchError(f"duplicate task id: {tid}", code="duplicate_id", task_id=tid)
        seen_ids.add(tid)
        delegation = task["delegation_id"]
        if not isinstance(delegation, str) or not delegation:
            raise BatchError("delegation_id must be a non-empty string", task_id=tid)
        if delegation in seen_delegations:
            raise BatchError(f"duplicate delegation_id: {delegation}",
                             code="duplicate_delegation", task_id=tid)
        seen_delegations.add(delegation)
        # Same bounded id shape as the runner's --task-group-id; None (absent)
        # keeps the legacy manifest and child argv shape unchanged.
        group = task["task_group_id"]
        if group is not None and (not isinstance(group, str)
                                  or not _ID_RE.fullmatch(group)):
            raise BatchError(
                f"task_group_id must match [a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}: {group!r}",
                code="invalid_task_group_id", task_id=tid)
        for enum_field, allowed in _ENUMS.items():
            value = task[enum_field]
            if value not in allowed:
                raise BatchError(f"invalid task {enum_field}: {value!r}",
                                 code="invalid_enum", task_id=tid)
        for free_field in ("workdir", "contract_file", "profile", "thinking"):
            if not isinstance(task[free_field], str) or not task[free_field]:
                raise BatchError(f"invalid task {free_field}", task_id=tid)
        timeout = task["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not math.isfinite(timeout) or timeout <= 0:
            raise BatchError("task timeout must be a positive finite number",
                             code="invalid_timeout", task_id=tid)
        tasks.append(task)
    _validate_paths(tasks)
    _validate_conflicts(tasks)
    return tasks


def _validate_paths(tasks: list[dict[str, Any]]) -> None:
    for task in tasks:
        tid = task["id"]
        for label, field in (("workdir", "workdir"), ("contract_file", "contract_file")):
            path = Path(task[field])
            if not path.is_absolute():
                raise BatchError(f"task {label} must be absolute",
                                 code="absolute_path_required", task_id=tid)
        workdir = _resolve(Path(task["workdir"]), strict=True, label="workdir", task_id=tid)
        if not workdir.is_dir():
            raise BatchError("task workdir must be an existing directory",
                             code="invalid_workdir", task_id=tid)
        try:
            contract_ok = Path(task["contract_file"]).is_file()
        except OSError as exc:
            raise BatchError(f"task contract_file is not readable: {_exc_message(exc)}",
                             code="invalid_contract_file", task_id=tid) from exc
        if not contract_ok:
            raise BatchError("task contract_file must be an existing file",
                             code="invalid_contract_file", task_id=tid)
        task["workdir_resolved"] = str(workdir)
        seen_keys: set[str] = set()
        scope_paths: list[Path] = []
        scope = task["scope"]
        if not isinstance(scope, list) or not scope:
            raise BatchError("task scope must be a non-empty list",
                             code="invalid_scope", task_id=tid)
        for entry in scope:
            if not isinstance(entry, str) or not entry or not entry.strip():
                raise BatchError("scope entries must be non-empty strings",
                                 code="invalid_scope", task_id=tid)
            if set(entry) & set("*?[]\\") or entry.startswith("/") \
                    or ".." in PurePosixPath(entry).parts or "." in PurePosixPath(entry).parts:
                raise BatchError(f"invalid scope entry: {entry!r}",
                                 code="invalid_scope", task_id=tid)
            resolved = _resolve(workdir / entry, strict=False, label=f"scope {entry!r}",
                                task_id=tid)
            if not resolved.is_relative_to(workdir):
                raise BatchError(f"scope escapes workdir: {entry!r}",
                                 code="scope_escape", task_id=tid)
            key = resolved.relative_to(workdir).as_posix()
            if key in seen_keys:
                raise BatchError(f"duplicate scope entry resolves to {key}",
                                 code="duplicate_scope", task_id=tid)
            seen_keys.add(key)
            scope_paths.append(resolved)
        task["scope_keys"] = sorted(seen_keys)
        task["scope_paths"] = scope_paths
        resources = task["resources"]
        if not isinstance(resources, list):
            raise BatchError("task resources must be a list", task_id=tid)
        for resource in resources:
            if not isinstance(resource, str) or not _ID_RE.fullmatch(resource):
                raise BatchError(f"invalid resource key: {resource!r}",
                                 code="invalid_resource", task_id=tid)
        if len(set(resources)) != len(resources):
            raise BatchError("duplicate resource key within task",
                             code="duplicate_resource", task_id=tid)


def _overlaps(left: Path, right: Path) -> bool:
    """True when two canonical paths are equal or one is an ancestor of the other."""
    return left == right or left in right.parents or right in left.parents


def _validate_conflicts(tasks: list[dict[str, Any]]) -> None:
    """Reject cross-task write/write and write/read scope overlaps and shared resources.

    Scopes are compared as canonical absolute paths, so directory vs file,
    symlink aliases, nested workdirs, and same-name entries in unrelated
    workdirs are all handled correctly. Read/read overlaps are allowed.
    """
    problems: list[str] = []
    for first, second in product(tasks, repeat=2):
        if first["id"] >= second["id"]:
            continue
        if first["tools"] == "read-only" and second["tools"] == "read-only":
            scope_pairs: list[tuple[Path, Path]] = []
        else:
            scope_pairs = list(product(first["scope_paths"], second["scope_paths"]))
        for left, right in scope_pairs:
            if _overlaps(left, right):
                problems.append(
                    f"scope overlap between {first['id']} and {second['id']}")
                break
    seen_resource: dict[str, str] = {}
    for task in tasks:
        for resource in task["resources"]:
            owner = seen_resource.get(resource)
            if owner is not None:
                problems.append(
                    f"exclusive resource {resource!r} held by {owner} and {task['id']}")
            else:
                seen_resource[resource] = task["id"]
    if problems:
        # De-duplicate while keeping deterministic order; keep messages compact.
        unique = list(dict.fromkeys(problems))
        raise BatchError("; ".join(unique[:5]), code="task_conflict")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-concurrency", default=None,
                        help="Positive integer cap on simultaneously running "
                             "children. Defaults to min(3, task count).")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--transport", choices=("rpc", "intercom"), default="rpc")
    parser.add_argument("--intercom-extension", default=None)
    parser.add_argument("--supervisor", default=None)
    parser.add_argument("--intercom-cli", default=None)
    parser.add_argument("--pi", default=None)
    # Optional global profiles override forwarded AS-IS to every child (same
    # path for dry validation and spawned children: one argv builder). When
    # unset, children resolve PI_WORKER_PROFILES_FILE / the default location
    # themselves; there is no per-task profiles configuration.
    parser.add_argument("--profiles-file", default=None)
    args = parser.parse_args(argv)
    if not args.output_dir:
        raise BatchError("--output-dir is required")
    output = Path(args.output_dir)
    if not output.is_absolute():
        raise BatchError("--output-dir must be an absolute path",
                         code="absolute_path_required")
    args.output_dir_path = output
    raw_limit = args.max_concurrency
    if raw_limit is None:
        args.limit = None
    elif isinstance(raw_limit, bool) or not re.fullmatch(r"[0-9]+", str(raw_limit)):
        raise BatchError("--max-concurrency must be a positive integer",
                         code="invalid_concurrency")
    else:
        limit = int(raw_limit)
        if limit <= 0:
            raise BatchError("--max-concurrency must be a positive integer",
                             code="invalid_concurrency")
        args.limit = limit
    return args


def _validate_output_dir(args: argparse.Namespace, runner_mod: Any) -> None:
    output = args.output_dir_path
    if output.exists() and not output.is_dir():
        raise BatchError("--output-dir exists and is not a directory",
                         code="invalid_output_dir")
    if output.is_dir() and any(output.iterdir()):
        raise BatchError("--output-dir must not be an existing non-empty directory",
                         code="invalid_output_dir")
    try:
        runner_mod.validate_receipt_file(str(output / SUMMARY_NAME))
    except Exception as exc:
        raise BatchError(f"output dir receipt target rejected: {_exc_message(exc)}",
                         code="invalid_output_dir")


def _task_argv(task: dict[str, Any], receipt: Path,
               args: argparse.Namespace) -> list[str]:
    """Build the full child argv.

    The exact same builder feeds dry validation and the launched child, so the
    transport/intercom/supervisor/intercom-cli/pi flags can never diverge
    between the validated invocation and the executed one.
    """
    argv = ["--workdir", task["workdir_resolved"],
            "--contract-file", task["contract_file"],
            "--routing-class", task["routing_class"],
            "--profile", task["profile"],
            "--thinking", task["thinking"],
            "--tools", task["tools"],
            "--delegation-id", task["delegation_id"],
            "--timeout", repr(float(task["timeout"]))]
    for scope_key in task["scope_keys"]:
        # The runner's --scope is action="append": one flag per entry.
        argv += ["--scope", scope_key]
    argv += ["--receipt-file", str(receipt)]
    if task.get("task_group_id"):
        argv += ["--task-group-id", task["task_group_id"]]
    if args.transport == "intercom":
        argv += ["--transport", "intercom",
                 "--intercom-extension", args.intercom_extension or "",
                 "--supervisor", args.supervisor or ""]
        if args.intercom_cli:
            argv += ["--intercom-cli", args.intercom_cli]
    if args.pi:
        argv += ["--pi", args.pi]
    if args.profiles_file:
        argv += ["--profiles-file", args.profiles_file]
    return argv


def _validate_task(args: argparse.Namespace, task: dict[str, Any],
                   receipt: Path, runner_mod: Any) -> None:
    if args.transport == "rpc" and (args.intercom_extension or args.supervisor
                                    or args.intercom_cli):
        raise BatchError("--transport rpc rejects intercom flags",
                         code="transport_validation")
    argv = _task_argv(task, receipt, args)
    try:
        parsed = runner_mod.parse_args(argv)
    except SystemExit as exc:  # argparse failures: invalid enum values etc.
        raise BatchError(f"task arguments rejected: exit {exc.code}",
                         code="invalid_task_args", task_id=task["id"]) from exc
    if runner_mod.harness_is_pi() and parsed.transport != "intercom":
        raise BatchError("pi-main batch runner must use --transport intercom",
                         code="invalid_transport_mode", task_id=task["id"])
    try:
        runner_mod.validate_launch_inputs(parsed)
    except Exception as exc:
        raise BatchError(_exc_message(exc), code="invalid_task_inputs",
                         task_id=task["id"]) from exc


def _atomic_write(path: Path, record: dict[str, Any]) -> None:
    temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(record, out, ensure_ascii=False, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                os.unlink(temp)
            except OSError:
                pass


def _write_summary(path: Path, statuses: dict[str, dict[str, Any]],
                   tasks: list[dict[str, Any]], limit: int) -> None:
    entries = []
    for task in tasks:
        status = statuses[task["id"]]
        entry: dict[str, Any] = {"id": task["id"],
                                 "delegation_id": task["delegation_id"],
                                 "status": status["status"]}
        for key in ("run_id", "error_code", "review_status"):
            if status.get(key):
                entry[key] = status[key]
        # Coarse correlation label only: surfaced when the manifest supplied it,
        # omitted otherwise so absence keeps the historical summary shape.
        if task.get("task_group_id"):
            entry["task_group_id"] = task["task_group_id"]
        entries.append(entry)
    _atomic_write(path, {"version": 1, "max_concurrency": limit, "tasks": entries})


def _read_receipt(path: Path) -> Any:
    """Return parsed JSON, None when absent, or the string ``"malformed"``."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        return "malformed"


def _receipt_problem(data: Any, task: dict[str, Any],
                     run_ids: set[str]) -> str | None:
    """Classify a zero-exit child's receipt; ``None`` means a valid completion."""
    if data is None:
        return "receipt_absent"
    if data == "malformed" or not isinstance(data, dict):
        return "receipt_malformed"
    if data.get("delegation_id") != task["delegation_id"]:
        return "receipt_wrong_delegation"
    outcome = data.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("status") != "completed":
        return "receipt_not_completed"
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return "receipt_missing_run_id"
    if run_id in run_ids:
        return "duplicate_run_id"
    return None


def _spawn(task: dict[str, Any], args: argparse.Namespace,
           output: Path) -> dict[str, Any]:
    """Start one owned child process group.

    Log file descriptors are closed on every path, including a failed spawn, so
    a batch that fails to launch one child cannot leak fds or block later
    cleanup of the children that did start.
    """
    task_dir = output / task["id"]
    os.mkdir(task_dir, 0o700)
    receipt_path = task_dir / "terminal.json"
    argv = [sys.executable, str(RUNNER_PATH), *_task_argv(task, receipt_path, args)]
    out_fd = err_fd = None
    proc: subprocess.Popen[bytes] | None = None
    try:
        out_fd = os.open(task_dir / "stdout.log",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        err_fd = os.open(task_dir / "stderr.log",
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        proc = subprocess.Popen(argv, stdout=out_fd, stderr=err_fd,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        for fd in (out_fd, err_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    assert proc is not None
    return {"task": task, "proc": proc, "receipt_path": receipt_path,
            "pgid": proc.pid,
            "deadline": time.monotonic() + float(task["timeout"])
            + WATCHDOG_GRACE_SECONDS}


def _send_group(entry: dict[str, Any], sig: int) -> None:
    """Signal only the child's owned process group, never the batch's own group."""
    pgid = entry.get("pgid")
    if pgid is not None and pgid > 0:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    proc = entry["proc"]
    try:
        if proc.poll() is None:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _wait_bounded(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _terminate(entry: dict[str, Any]) -> None:
    """Bounded TERM then KILL of the owned process group, descendants included."""
    _send_group(entry, signal.SIGTERM)
    if not _wait_bounded(entry["proc"], SHUTDOWN_WAIT_SECONDS):
        _send_group(entry, signal.SIGKILL)
        _wait_bounded(entry["proc"], KILL_WAIT_SECONDS)
        return
    # The direct child exited; still KILL the group so a descendant that ignored
    # TERM cannot outlive the batch.
    _send_group(entry, signal.SIGKILL)


def _harvest(entry: dict[str, Any], returncode: int, run_ids: set[str],
             statuses: dict[str, dict[str, Any]],
             running: list[dict[str, Any]]) -> None:
    task = entry["task"]
    if returncode == 0:
        data = _read_receipt(entry["receipt_path"])
        problem = _receipt_problem(data, task, run_ids)
        if problem is None:
            run_ids.add(data["run_id"])
            statuses[task["id"]] = {"status": "completed", "run_id": data["run_id"],
                                    "review_status": "pending_main_review"}
        else:
            statuses[task["id"]] = {"status": "unconfirmed", "error_code": problem}
    else:
        # A nonzero child is a failure even if it left a receipt claiming
        # success; the batch never upgrades a failed execution.
        statuses[task["id"]] = {"status": "failed", "error_code": "nonzero_exit"}
    if entry in running:
        running.remove(entry)


def _harvest_watchdog(entry: dict[str, Any],
                      statuses: dict[str, dict[str, Any]],
                      running: list[dict[str, Any]]) -> None:
    statuses[entry["task"]["id"]] = {"status": "failed", "error_code": "watchdog_timeout"}
    if entry in running:
        running.remove(entry)


def _launch(tasks: list[dict[str, Any]], args: argparse.Namespace, limit: int,
            output: Path) -> tuple[int, dict[str, dict[str, Any]]]:
    summary_path = output / SUMMARY_NAME
    statuses: dict[str, dict[str, Any]] = {
        t["id"]: {"status": "not_started"} for t in tasks}
    _write_summary(summary_path, statuses, tasks, limit)
    running: list[dict[str, Any]] = []
    pending = list(tasks)
    run_ids: set[str] = set()

    old_umask = os.umask(0o077)
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)

    def _interrupt(signum: int, frame: Any) -> None:
        raise BatchInterrupted(f"signal {signum}")

    def _install(signum: int, handler: Any) -> None:
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            # Not on the main thread (in-process test embedding): the deadline
            # watchdog and normal harvest still bound the run.
            pass

    _install(signal.SIGINT, _interrupt)
    _install(signal.SIGTERM, _interrupt)
    try:
        while pending or running:
            while pending and len(running) < limit:
                task = pending.pop(0)
                try:
                    entry = _spawn(task, args, output)
                except Exception as exc:
                    if isinstance(exc, BatchError):
                        raise
                    raise BatchError(
                        f"failed to launch task {task['id']}: {_exc_message(exc)}",
                        code="spawn_failed", task_id=task["id"]) from exc
                running.append(entry)
                statuses[task["id"]] = {"status": "running"}
                _write_summary(summary_path, statuses, tasks, limit)
            if not running:
                break
            changed = False
            for entry in list(running):
                proc = entry["proc"]
                returncode = proc.poll()
                if returncode is not None:
                    _harvest(entry, returncode, run_ids, statuses, running)
                    changed = True
                elif time.monotonic() >= entry["deadline"]:
                    _terminate(entry)
                    _harvest_watchdog(entry, statuses, running)
                    changed = True
            if changed:
                _write_summary(summary_path, statuses, tasks, limit)
            else:
                time.sleep(POLL_SECONDS)
        success = all(s["status"] == "completed" for s in statuses.values())
        return (0 if success else 1), statuses
    except BatchInterrupted:
        for entry in list(running):
            _terminate(entry)
            statuses[entry["task"]["id"]] = {"status": "interrupted"}
        for task in pending:
            statuses[task["id"]] = {"status": "not_started"}
        running.clear()
        _write_summary(summary_path, statuses, tasks, limit)
        return 130, statuses
    finally:
        # Safety net for an unexpected exception: never orphan an owned child.
        for entry in list(running):
            try:
                _terminate(entry)
            except Exception:
                pass
        os.umask(old_umask)
        try:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        tasks = _load_manifest(args.manifest)
        runner_mod = _runner_module()
        limit = args.limit if args.limit is not None else min(
            DEFAULT_MAX_CONCURRENCY, len(tasks))
        limit = min(limit, len(tasks))
        _validate_output_dir(args, runner_mod)
        output = args.output_dir_path
        for task in tasks:
            receipt = output / task["id"] / "terminal.json"
            _validate_task(args, task, receipt, runner_mod)
        if args.validate_only:
            print(json.dumps({"status": "validated", "tasks": [t["id"] for t in tasks],
                              "max_concurrency": limit}, sort_keys=True))
            return 0
        if not output.exists():
            output.mkdir(mode=0o700)
        code, statuses = _launch(tasks, args, limit, output)
        return code
    except BatchInterrupted:
        print("batch: interrupted; cleaned up owned children", file=sys.stderr)
        return 130
    except BatchError as exc:
        prefix = f"task {exc.task_id}: " if exc.task_id else ""
        print(f"batch: {prefix}{exc.code}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("batch: interrupted; cleaned up owned children", file=sys.stderr)
        return 130
    except Exception as exc:  # defensive: never leak a traceback to the caller
        print(f"batch: internal_error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
