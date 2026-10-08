#!/usr/bin/env python3
"""Route and run exactly one Pi worker via JSONL RPC, with an opt-in Pi-main intercom transport."""

from __future__ import annotations

import argparse
import codecs
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import secrets
import subprocess
import sys
import queue
import threading
import time
from collections import deque
from typing import Any

try:  # scripts/ on sys.path (direct execution)
    from worker_progress import (
        CHECKPOINT_CODES,
        RETRY_END_EVENTS,
        CheckpointPolicy,
        ProgressObserver,
        checkpoint_message,
        time_budget_reminder,
    )
except ImportError:  # pragma: no cover - loaded by file location (tests)
    import importlib.util as _importlib_util

    _progress_spec = _importlib_util.spec_from_file_location(
        "worker_progress", Path(__file__).resolve().parent / "worker_progress.py")
    assert _progress_spec is not None and _progress_spec.loader is not None
    _progress_module = _importlib_util.module_from_spec(_progress_spec)
    _progress_spec.loader.exec_module(_progress_module)
    CHECKPOINT_CODES = _progress_module.CHECKPOINT_CODES
    RETRY_END_EVENTS = _progress_module.RETRY_END_EVENTS
    CheckpointPolicy = _progress_module.CheckpointPolicy
    ProgressObserver = _progress_module.ProgressObserver
    checkpoint_message = _progress_module.checkpoint_message
    time_budget_reminder = _progress_module.time_budget_reminder


# Worker profiles are host configuration, never shipped defaults: the runner
# sources the profile table (the SUB-role view only) and the
# record-retention period from scripts/profile_config.py (CLI --profiles-file >
# PI_WORKER_PROFILES_FILE > ~/.config/pi-worker/profiles.json). There is NO
# built-in provider, model, profile, or main-agent model anywhere in this
# production source: a fresh machine with no configuration yields an empty
# table and auto routing fails with an informative no_enabled_profile error
# before any reservation, provider preflight, Pi process, or ledger write, and
# there is no fallback provider. The table object stays stable across reloads
# (cleared and refilled in place) so captured references keep observing the
# live table. Sticky safety is unchanged: a sticky selection naming a profile
# that is no longer configured is reported as removed (profile_removed), never
# silently redrawn; when a host changes a pinned profile's provider or model it
# must register a NEW profile id per the documented sticky policy. Roles are
# eligibility only: apply_profile_config installs profiles_for_role("sub"), so
# main-only or unassigned profiles never enter worker state or draws, and the
# actual main model stays host-controlled. Auth is solely Pi's default: the
# runner spawns no login shell and manages no credential environment.
PROFILES: dict[str, dict[str, Any]] = {}
# Default until a configuration is installed: 14-day retention (0 disables
# cleanup). Neither is ever shipped as a profile/model default.
RECORD_RETENTION_DAYS = 14
try:  # scripts/ on sys.path (direct execution)
    import profile_config as _profile_config
except ImportError:  # pragma: no cover - loaded by file location (tests)
    import importlib.util as _profile_config_util

    _profile_config_spec = _profile_config_util.spec_from_file_location(
        "profile_config", Path(__file__).resolve().parent / "profile_config.py")
    assert _profile_config_spec is not None and _profile_config_spec.loader is not None
    _profile_config = _profile_config_util.module_from_spec(_profile_config_spec)
    _profile_config_spec.loader.exec_module(_profile_config)


def apply_profile_config(config) -> None:
    """Install the SUB-role view of a validated configuration in place.

    Only profiles whose ``roles`` include ``"sub"`` enter the worker table:
    main-only and unassigned (empty roles) profiles never reach worker profile
    state or draws, while disabled sub profiles stay registered so history,
    sticky pinning, and receipts keep resolving. The table object stays stable
    across reloads (cleared and refilled in place). Roles change eligibility
    only; the actual main model is host-controlled and never selected here.
    """
    global RECORD_RETENTION_DAYS
    PROFILES.clear()
    PROFILES.update(config.profiles_for_role("sub"))
    RECORD_RETENTION_DAYS = config.record_retention_days


def configure_profiles(explicit: str | None):
    """Strictly load, validate, and install the profile configuration.

    Called by ``main`` with the explicit ``--profiles-file`` value (may be
    ``None``) so precedence explicit > env > user default applies and any
    configuration problem is reported before reservation, provider checks, the
    Pi process, or ledger writes. A malformed or broken local file never
    blocks an explicit ``--profiles-file``: the best-effort import-time load
    swallows its error, and this strict reload then reports it (or, with an
    explicit override, loads the override instead).
    """
    config = _profile_config.load_profile_config(explicit)
    apply_profile_config(config)
    return config


def _initial_profile_load() -> None:
    """Best-effort import-time load of the host configuration.

    Reads the existing USER configuration: PI_WORKER_PROFILES_FILE when set,
    otherwise the default home file. This is loading host-owned configuration,
    not a built-in model default — an empty fresh home yields no profiles at
    all. The behavior is deliberate: read-only helpers (review append, record
    retention) observe the locally configured retention values without
    going through ``main``. All failures are silently ignored here and are
    re-reported (or refused) by the strict load in ``configure_profiles`` when
    ``main`` runs, so a malformed user file never blocks an explicit CLI
    override.
    """
    try:
        config = _profile_config.load_profile_config(
            os.environ.get(_profile_config.PROFILES_FILE_ENV))
    except Exception:
        return
    apply_profile_config(config)


_initial_profile_load()
THINKING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
# Supported routing classes. The retired class is gone from the table above, so
# an unsupported class is rejected the same way by the CLI, the AGENTS.md
# override path, and the internal selection/reservation helpers: there is no
# arbitrary or implicit class pool.
ROUTING_CLASSES = ("standard",)

# Record kinds: ``production`` runs feed the normal metrics; ``test`` runs are
# explicitly tagged by a caller and excluded from the default production report
# unless ``--include-tests`` is passed.
RECORD_KINDS = ("production", "test")
LIFECYCLE_RECORD_TYPE = "lifecycle"
NOTIFICATION_RECORD_TYPE = "notification"
RUN_FINISHED_EVENT = "RUN_FINISHED"
# Best-effort terminal notification budget: at most two attempts of at most five
# seconds each, then the runner stops. The stable run_id makes retries idempotent
# for the receiver.
NOTIFICATION_MAX_ATTEMPTS = 2
NOTIFICATION_TIMEOUT_SECONDS = 5.0
# Coarse runner-side checkpoint sidecar cadence (seconds between writes).
CHECKPOINT_INTERVAL_SECONDS = 2.0
# Optional opaque cross-run grouping label (--task-group-id). Purely a coarse,
# caller-supplied correlation id recorded in the assignment block when supplied:
# it never participates in sticky delegation identity, the unchanged-contract
# digest gate, routing, or model selection, and no id is ever inferred or
# auto-generated from a goal, delegation name, or project string.
TASK_GROUP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# Preflight subprocess caps. When a total run deadline is supplied, each cap is
# additionally clamped to the remaining budget; these defaults are retained for
# direct callers that do not pass a deadline.
PREFLIGHT_AUTH_TIMEOUT_SECONDS = 30.0
PREFLIGHT_CATALOG_TIMEOUT_SECONDS = 60.0
# Bounded pre-prompt session-id lookup. It is best-effort diagnostics only: an
# unsupported or slow get_session_stats falls back without failing the run.
EARLY_SESSION_TIMEOUT_SECONDS = 2.0
# Default per-running-tool bound in seconds, enforced from a tool's
# ``tool_execution_start`` to its matching ``tool_execution_end``. ``0`` explicitly
# disables it. A partial tool update or unrelated model traffic never resets it,
# and concurrent tools are tracked independently. The existing global ``--timeout``
# (which still includes preflight) is a separate, unchanged bound.
DEFAULT_TOOL_TIMEOUT_SECONDS = 180.0
# Automatic *agent-turn* retry bounds, enforced from the observed
# ``auto_retry_start``/``auto_retry_end`` events. ``--max-auto-retries`` counts
# retries globally for the run: the ``(count + 1)``-th ``auto_retry_start`` is
# refused with a sanitized ``retry_limit`` failure, so the default of 2 allows at
# most two automatic retries and ``0`` rejects every retry.
# ``--retry-budget-seconds`` bounds the *aggregate* observed retry time (busy
# time of all retries, in-flight included, so overlapping retries can exceed
# wall-clock); ``0`` explicitly disables the time bound only, never the count.
# Both are independent of the global ``--timeout``, which is unchanged.
DEFAULT_MAX_AUTO_RETRIES = 2
DEFAULT_RETRY_BUDGET_SECONDS = 120.0

INTERCOM_TOOL = "intercom"
INTERCOM_GET_COMMANDS_ID = "intercom-cmd-list"
# Strict RFC 4122 hex-only full UUID. Short roster IDs are explicitly rejected: the
# runner does not auto-resolve them and the worker contract treats any short match as
# untrusted, so the same posture belongs to the CLI.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class RunnerError(RuntimeError):
    def __init__(self, message: str, *, code: str = "runner_error", stage: str = "runner"):
        super().__init__(message)
        self.code = code
        self.stage = stage


class RunnerTerminated(Exception):
    """Raised from the SIGTERM handler so the runner can finalize a receipt.

    A SIGKILL cannot be caught; the reconciliation report surfaces those runs as
    stale/unconfirmed instead of fabricating a terminal outcome.
    """


class _Wake(Exception):
    """Internal: an idle read woke early on a per-tool bound or checkpoint.

    Raised instead of ``TimeoutError`` so a shorter-than-deadline wake is never
    misreported as the global run timeout. An exhausted global budget still
    raises ``TimeoutError`` unchanged.
    """


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def harness_is_pi() -> bool:
    """True when this runner was started from a Pi session rather than Codex."""
    if os.environ.get("PI_CODING_AGENT", "").lower() == "true":
        return True
    return os.environ.get("AI_AGENT", "").lower() == "pi"


def record_dir() -> Path:
    """Where run receipts, reviews and routing state are written.

    PI_WORKER_RECORD_DIR wins outright. Otherwise the directory follows the harness that
    launched the runner, so the two sides keep separate ledgers: a Pi session writes to
    ~/.pi/agent/worker-run-records, Codex (and anything else) keeps the original
    ~/.codex/worker-run-records. Both default resolutions share the same file layout.
    """
    override = os.environ.get("PI_WORKER_RECORD_DIR")
    if override:
        return Path(override)
    if harness_is_pi():
        return Path.home() / ".pi" / "agent" / "worker-run-records"
    return Path.home() / ".codex" / "worker-run-records"


def local_day(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now().astimezone()).astimezone().date().isoformat()


# Run-record retention: record files are kept for 14 local calendar days
# (today plus the previous 13). A file is eligible for deletion when the date
# encoded in its exact ``YYYY-MM-DD.jsonl`` name is older than
# ``today - 13 days`` (age >= 14 days). File mtime is never consulted.
# Retention is automatic and lock-serialized: every successful ``append_record``
# (run, review, lifecycle, notification) prunes once, under the same exclusive
# ``.append.lock`` flock as the append itself, so a concurrent writer is never
# deleted mid-append. There is no scheduler and no midnight daemon: the next
# write after a file ages out removes it. Retained records stay immutable —
# pruning never rewrites run/review IDs or routing state.
# The period is host configuration (record_retention_days, default 14; 0
# disables cleanup) installed by apply_profile_config above.
_RECORD_FILE_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl")


def _parse_record_day(name: str) -> dt.date | None:
    """Strictly parse an exact ``YYYY-MM-DD`` token, else ``None`` (retain)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        return None
    try:
        return dt.date.fromisoformat(name)
    except ValueError:
        return None


def _retention_reference_day() -> dt.date:
    """Reference 'today' for retention, from the same local-day source appends
    use, so tests can mock ``local_day`` consistently."""
    parsed = _parse_record_day(local_day())
    if parsed is not None:
        return parsed
    return dt.datetime.now().astimezone().date()


def _retention_scan(directory: Path, *, today: dt.date, protect: set[str],
                    delete: bool, retention_days: int | None = None) -> dict[str, Any]:
    """Single-directory (non-recursive) retention scan of ``directory``.

    Eligible: regular non-symlink direct child files named exactly
    ``YYYY-MM-DD.jsonl`` whose encoded date is at least the configured local
    calendar window old (default ``RECORD_RETENTION_DAYS``). Everything else —
    state files, lock files, ``.checkpoints``, other extensions, directories,
    symlinks, malformed or future-dated names — is untouched. Callers hold the
    exclusive ``.append.lock`` when ``delete`` is true; ``delete=False`` (dry
    run) never mutates anything. Deletion is best-effort: failures are counted
    coarsely and never fabricated as successes. A window of ``0`` disables
    cleanup: nothing is eligible and nothing is deleted.
    """
    days = RECORD_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return {"removed": [], "eligible": [], "failed_count": 0}
    cutoff = today - dt.timedelta(days=days - 1)
    eligible: list[str] = []
    removed: list[str] = []
    failed = 0
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return {"removed": [], "eligible": [], "failed_count": 0}
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        match = _RECORD_FILE_NAME_RE.fullmatch(entry.name)
        if match is None or entry.name in protect:
            continue
        day = _parse_record_day(match.group(1))
        if day is None or day >= cutoff:
            continue
        eligible.append(entry.name)
        if not delete:
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            failed += 1
        else:
            removed.append(entry.name)
    return {"removed": removed, "eligible": eligible, "failed_count": failed}


def prune_record_files(directory: Path | None = None, *,
                       today: dt.date | None = None,
                       dry_run: bool = False,
                       retention_days: int | None = None) -> dict[str, Any]:
    """Delete run-record files aged past the configured retention window.

    Resolves the active ``record_dir()`` (``PI_WORKER_RECORD_DIR``, Pi, or
    Codex default) when ``directory`` is omitted, so the same window applies to
    whichever ledger is active; pass the canonical absolute directory for a
    one-off cleanup. The window defaults to the configured
    ``record_retention_days`` (module ``RECORD_RETENTION_DAYS``, host file
    default 14); pass ``retention_days`` explicitly for tests or manual
    overrides. ``0`` explicitly disables cleanup: nothing is eligible and
    nothing is deleted. Returns a compact dict (``removed`` and ``eligible``
    filename lists plus a coarse ``failed_count``); no file contents are ever
    returned. A missing directory returns the empty result without creating
    anything, and ``dry_run=True`` creates and mutates nothing (including
    ``.append.lock``). An actual prune takes the same exclusive ``.append.lock``
    flock as ``append_record`` so a concurrent append is never deleted.
    """
    days = RECORD_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        # Disabled: zero deletes nothing, and a dry run reports nothing eligible.
        return {"removed": [], "eligible": [], "failed_count": 0}
    target = Path(directory) if directory is not None else record_dir()
    reference = today if today is not None else _retention_reference_day()
    if not target.is_dir():
        return {"removed": [], "eligible": [], "failed_count": 0}
    if dry_run:
        return _retention_scan(target, today=reference, protect=set(), delete=False,
                               retention_days=days)
    lock = target / ".append.lock"
    with lock.open("a", encoding="utf-8") as lock_file:
        os.chmod(lock, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return _retention_scan(target, today=reference, protect=set(), delete=True,
                                   retention_days=days)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def project_id(workdir: Path) -> str:
    # A one-way identifier keeps paths out of records while allowing grouping.
    return "sha256:" + hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:20]


def append_record(record: dict[str, Any], *, day: str | None = None) -> Path:
    directory = record_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    lock = directory / ".append.lock"
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with lock.open("a", encoding="utf-8") as lock_file:
        os.chmod(lock, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        path = directory / f"{day or local_day()}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(line)
                out.flush()
                os.fsync(out.fileno())
            # Automatic retention under the same exclusive lock, after the new
            # record is durable, using the configured retention period. The
            # file just appended is protected for this call, so a manual
            # backdated append never deletes what it just returned; the next
            # real write re-evaluates it. Best-effort: a cleanup failure never
            # masks the successful append.
            try:
                _retention_scan(directory, today=_retention_reference_day(),
                                protect={path.name}, delete=True)
            except Exception:
                pass
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return path


def iter_records() -> list[dict[str, Any]]:
    directory = record_dir()
    records: list[dict[str, Any]] = []
    if not directory.exists():
        return records
    for path in sorted(directory.glob("????-??-??.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        records.append(item)
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return records


def _canonical_receipt_path(path: Path) -> Path:
    """Canonical-resolution policy for a receipt destination.

    Explicit ``..`` components are rejected outright (no silent traversal), then
    the absolute path is resolved to its canonical form. Resolving is what makes
    platform path aliases usable: macOS exposes the real system temp as
    ``/private/var/...`` and ``/private/tmp`` while ``/var`` and ``/tmp`` are
    symlinks. The canonical target is the path returned, validated, and written,
    so an alias is accepted only when its target passes the same parent/mode
    checks as a directly supplied path. Docs use the canonical ``/private/tmp``.
    """
    if ".." in path.parts:
        raise RunnerError("--receipt-file must not contain ..",
                          code="invalid_receipt_file", stage="routing")
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise RunnerError("--receipt-file is not resolvable",
                          code="invalid_receipt_file", stage="routing") from exc


def _check_receipt_path(path: Path) -> None:
    """Read-only safety checks for an absolute receipt destination.

    Refuses ``..`` components, a symlink destination, symlinked parent
    components, and any group/other-writable non-sticky existing parent.
    Missing parents are allowed because :func:`write_receipt_file` creates them
    ``0700``. Nothing is created, chmodded, or truncated, so this helper is safe
    on the ``--validate-only`` path and idempotent at write time. Platform path
    aliases such as ``/tmp`` -> ``/private/tmp`` are symlinks and are refused;
    callers must pass the canonical path (documented as ``/private/tmp``).
    """
    if ".." in path.parts or path.name in ("", ".", ".."):
        raise RunnerError("--receipt-file is not a safe file path",
                          code="invalid_receipt_file", stage="routing")
    if path.is_symlink():
        raise RunnerError("--receipt-file must not be a symlink",
                          code="invalid_receipt_file", stage="routing")
    if path.exists():
        if not path.is_file():
            raise RunnerError("--receipt-file must be a regular file",
                              code="invalid_receipt_file", stage="routing")
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise RunnerError("--receipt-file is not readable",
                              code="invalid_receipt_file", stage="routing") from exc
        if mode & 0o022:
            raise RunnerError("--receipt-file must not be group/other writable",
                              code="invalid_receipt_file", stage="routing")
    for parent in path.parents:
        if parent.is_symlink():
            raise RunnerError("--receipt-file must not traverse a symlink parent",
                              code="invalid_receipt_file", stage="routing")
        if not parent.exists():
            continue
        if not parent.is_dir():
            raise RunnerError("--receipt-file parent is not a directory",
                              code="invalid_receipt_file", stage="routing")
        try:
            parent_mode = parent.stat().st_mode
        except OSError as exc:
            raise RunnerError("--receipt-file parent is not readable",
                              code="invalid_receipt_file", stage="routing") from exc
        if (parent_mode & 0o022) and not (parent_mode & 0o1000):
            raise RunnerError("--receipt-file parent must not be group/other writable",
                              code="invalid_receipt_file", stage="routing")


def validate_receipt_file(value: str | None) -> Path | None:
    """Validate an optional ``--receipt-file`` destination without writing it.

    Runs before routing reservation so an invalid destination never consumes a
    sticky delegation. The checks are read-only: nothing is created, chmodded,
    or truncated here, so the same helper is safe on the ``--validate-only``
    path. The destination must be an absolute path to a regular file whose
    existing parent components are not group/other-writable and are not
    symlinks; ``..`` components and symlinked destinations are refused so an
    unexpected path cannot redirect the private receipt. Callers should pass the
    canonical path (for example ``/private/tmp`` rather than the macOS ``/tmp``
    symlink alias).
    """
    if value is None:
        return None
    if not value:
        raise RunnerError("--receipt-file must not be empty",
                          code="invalid_receipt_file", stage="routing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RunnerError("--receipt-file must be an absolute path",
                          code="invalid_receipt_file", stage="routing")
    # A symlinked destination file is always refused: resolving it would write
    # through the link to an unexpected target. Parent aliases are resolved by
    # the canonical policy above and then validated.
    if path.is_symlink():
        raise RunnerError("--receipt-file must not be a symlink",
                          code="invalid_receipt_file", stage="routing")
    canonical = _canonical_receipt_path(path)
    _check_receipt_path(canonical)
    return canonical


def write_receipt_file(path: Path, record: dict[str, Any]) -> None:
    """Atomically write ``record`` as private UTF-8 JSON at ``path``.

    The path is re-validated at write time (narrowing the validation/write
    race), missing parents are created with mode ``0o700`` only when this call
    actually created them (pre-existing parents are never chmodded), and the
    record is written to a sibling temporary file with mode ``0o600``, flushed,
    fsynced, and ``os.replace``-d into place. A failure leaves any previous
    destination untouched and raises; callers must never treat a missing receipt
    as success.
    """
    _check_receipt_path(path)
    directory = path.parent
    missing: list[Path] = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for item in reversed(missing):
        created = False
        try:
            os.mkdir(item, 0o700)
            created = True
        except FileExistsError:
            # Created concurrently: never chmod a directory this call did not
            # create. The re-check below surfaces any unsafe mode instead.
            pass
        if created:
            try:
                os.chmod(item, 0o700)
            except OSError:
                pass
    # Re-check after creating parents and immediately before writing so a
    # symlink or unsafe mode introduced in the window is refused.
    _check_receipt_path(path)
    data = json.dumps(record, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    temporary = directory / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def checkpoint_dir() -> Path:
    return record_dir() / ".checkpoints"


def write_run_checkpoint(run_id: str, delegation_id: str,
                         progress: dict[str, Any]) -> None:
    """Best-effort coarse activity sidecar for killed runners.

    Only counts, the last RPC event name, the session id, the fixed-schema
    progress metrics, and a timestamp are stored; no RPC payload, contract,
    path, or free-form text. Callers throttle
    writes, so a long run appends a bounded number of snapshots rather than one
    per event. Errors are swallowed by callers: a checkpoint is evidence, never
    the authoritative ledger.
    """
    directory = checkpoint_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    payload = {
        "run_id": run_id,
        "delegation_id": delegation_id,
        "session_id": progress.get("session_id"),
        "event_count": progress.get("event_count", 0),
        "last_event": progress.get("last_event"),
        # Explicit fixed-schema metrics only; the sidecar never carries tool
        # arguments, commands, results, or errors.
        "progress_metrics": progress.get("progress_metrics"),
        "updated_at": utc_now(),
    }
    temporary = directory / f".{run_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / f"{run_id}.json")
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def read_run_checkpoint(run_id: str) -> dict[str, Any] | None:
    path = checkpoint_dir() / f"{run_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def remove_run_checkpoint(run_id: str) -> None:
    try:
        (checkpoint_dir() / f"{run_id}.json").unlink()
    except OSError:
        pass


def validate_intercom_cli(transport: str, cli_path: str | None) -> str | None:
    """Validate an explicit ``--intercom-cli`` path before routing reservation.

    The notification CLI is optional; when omitted the runner resolves the
    trusted extension's sibling ``cli.mjs`` at notification time. An explicit
    path must be an absolute ``.mjs``/``.js`` file so arbitrary extensions are
    not silently assumed to ship a sibling CLI. RPC mode rejects the flag like
    the other intercom-only arguments.
    """
    if transport == "rpc":
        if cli_path:
            raise RunnerError("--transport rpc rejects --intercom-cli",
                              code="invalid_transport_args", stage="routing")
        return None
    if not cli_path:
        return None
    path = Path(cli_path).expanduser()
    if not path.is_absolute():
        raise RunnerError("--intercom-cli must be an absolute path",
                          code="invalid_transport_args", stage="routing")
    if path.suffix.lower() not in (".mjs", ".js"):
        raise RunnerError("--intercom-cli must end with .mjs or .js",
                          code="invalid_transport_args", stage="routing")
    if not path.is_file():
        raise RunnerError(f"--intercom-cli file not found: {path}",
                          code="invalid_transport_args", stage="routing")
    return str(path.resolve())


def resolve_intercom_cli(extension: str | None, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    if extension:
        candidate = Path(extension).resolve().parent / "cli.mjs"
        if candidate.is_file():
            return candidate
    return None


def build_run_finished_text(receipt: dict[str, Any], *,
                            followup_required: bool = True,
                            ledger_written: bool = True,
                            receipt_written: bool = True,
                            receipt_required: bool = False) -> str:
    """Compact sanitized RUN_FINISHED payload; no paths, contract, or secrets.

    The terminal-evidence flags let the main tell an authoritative receipt from
    an unconfirmed one: ``ledger_written`` false means the append-only ledger
    does not hold the terminal record, and ``receipt_written`` is only meaningful
    when ``receipt_required`` is true. A main must not assume a durable receipt
    exists when any of these is false.
    """
    failure = receipt.get("failure") or {}
    payload: dict[str, Any] = {
        "type": RUN_FINISHED_EVENT,
        "run_id": receipt.get("run_id"),
        "delegation_id": receipt.get("delegation_id"),
        "status": (receipt.get("outcome") or {}).get("status"),
        "followup_required": bool(followup_required),
        "ledger_written": bool(ledger_written),
        "receipt_required": bool(receipt_required),
        "receipt_written": bool(receipt_written),
    }
    code = failure.get("code") if isinstance(failure, dict) else None
    if isinstance(code, str) and code:
        payload["failure_code"] = code
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _notification_delivered(stdout: str) -> bool:
    """Only a JSON object with ``ok: true`` AND ``delivered: true`` counts.

    A zero exit alone is not enough, and a contradictory result such as
    ``{"ok": false, "delivered": true}`` is rejected. Malformed output, raw
    error text, and non-object JSON never count as success.
    """
    text = (stdout or "").strip()
    if not text:
        return False
    for candidate in [text, *text.splitlines()]:
        try:
            document = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(document, dict):
            continue
        if document.get("ok") is True and document.get("delivered") is True:
            return True
    return False


def send_run_finished_notification(*, transport: str, receipt: dict[str, Any],
                                   supervisor_id: str | None,
                                   extension: str | None,
                                   explicit_cli: str | None,
                                   record_kind: str,
                                   ledger_written: bool = True,
                                   receipt_written: bool = True,
                                   receipt_required: bool = False,
                                   node: str = "node") -> dict[str, Any] | None:
    """Best-effort runner-side RUN_FINISHED notification over the intercom CLI.

    This runs only for the intercom transport, after the terminal ledger and
    receipt file are attempted. It never calls a model, installs dependencies, or
    polls: at most two bounded ``node <cli> send`` attempts are made. Only a zero
    exit plus ``{"ok": true, "delivered": true}`` counts as routing confirmation;
    that is a delivery fact, never acceptance or read acknowledgment. The
    returned notification event is appended to the ledger and carries only coarse
    routing and terminal-evidence metadata, so a failed notification or an
    unconfirmed terminal write is visible without changing the execution status.
    """
    if transport != "intercom":
        return None
    run_id = receipt.get("run_id")
    delegation_id = receipt.get("delegation_id")
    status = (receipt.get("outcome") or {}).get("status")
    attempts = 0
    delivered = False
    cli = resolve_intercom_cli(extension, explicit_cli)
    if cli is not None and supervisor_id:
        text = build_run_finished_text(
            receipt, ledger_written=ledger_written,
            receipt_written=receipt_written, receipt_required=receipt_required)
        runner_name = f"pi-runner-run-finished-{run_id}"
        argv = [node, str(cli), "send", "--to", supervisor_id, "--text", text,
                "--json", "--name", runner_name]
        for _ in range(NOTIFICATION_MAX_ATTEMPTS):
            attempts += 1
            try:
                proc = subprocess.run(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=NOTIFICATION_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0 and _notification_delivered(proc.stdout or ""):
                delivered = True
                break
    event = {
        "schema_version": 3, "record_type": NOTIFICATION_RECORD_TYPE,
        "phase": "finished", "event": "run_finished",
        "run_id": run_id, "delegation_id": delegation_id,
        "record_kind": record_kind, "transport": "intercom", "status": status,
        "attempts": attempts, "delivery": "delivered" if delivered else "failed",
        "delivered": delivered,
        # Terminal-evidence markers: when the terminal ledger or a required
        # receipt file was not written, a receiver must treat the evidence as
        # unconfirmed even if the notification itself was delivered.
        "ledger_written": bool(ledger_written),
        "receipt_written": bool(receipt_written),
        "receipt_required": bool(receipt_required),
        "created_at": utc_now(),
    }
    try:
        append_record(event)
    except Exception:
        pass
    return event


def previous_selection(delegation_id: str) -> str | None:
    directory = record_dir()
    state = directory / ".routing-state.json"
    lock = directory / ".append.lock"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    value = data.get(delegation_id)
    if isinstance(value, dict):
        value = value.get("profile")
    return value if value in PROFILES else None


def read_sticky_state() -> dict[str, Any]:
    """Read the sticky routing state without taking a lock or creating files.

    The regular :func:`previous_selection` opens the ``.append.lock`` file and
    creates the record directory, which would be an observable side effect on
    the validate-only path. This reader mirrors the JSON decode but never
    touches the filesystem beyond reading the state file; missing, unreadable,
    or invalid state returns ``{}``.
    """
    state = record_dir() / ".routing-state.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_previous_selection(delegation_id: str) -> str | None:
    """Return the previously selected profile for ``delegation_id`` without
    acquiring any lock or creating files.

    This is the non-mutating counterpart to :func:`previous_selection`. It
    inspects the on-disk sticky state directly so callers like
    :func:`validate_sticky_reservation` can verify delegation compatibility
    without leaving a ``.append.lock`` artefact behind.

    The recorded name is returned as written, without a registry membership
    filter, so a delegation pinned to a profile that has since been removed can
    be reported instead of being silently redrawn. Unrelated entries and
    malformed records (missing, empty, or non-string ``profile`` values) still
    return ``None`` and are treated as "no sticky delegation".
    """
    value = read_sticky_state().get(delegation_id)
    if isinstance(value, dict):
        value = value.get("profile")
    return value if isinstance(value, str) and value else None


def removed_profile_error(delegation_id: str, profile: str) -> RunnerError:
    """Error for a sticky delegation pinned to an unregistered profile.

    Retired profiles are never migrated or silently redrawn to a different
    worker: doing so would change the pinned model behind an existing
    delegation id. Routing state and run history are left untouched, and the
    caller continues the work under a new delegation id.
    """
    return RunnerError(
        f"delegation {delegation_id!r} is pinned to profile {profile!r}, which is no "
        "longer registered; start a new delegation ID to continue this work",
        code="profile_removed", stage="routing")


def check_routing_class(routing_class: str) -> None:
    """Reject a routing class that is not part of the supported table."""
    if routing_class not in ROUTING_CLASSES:
        raise RunnerError(
            f"unsupported routing class: {routing_class}; expected "
            + " or ".join(ROUTING_CLASSES),
            code="profile_config", stage="routing")


# Provider wording that indicates an account usage limit rather than a transient fault.
# Matched locally only; raw provider text is never persisted or echoed.
USAGE_LIMIT_PATTERNS = (
    r"\b429\b", r"rate[\s_-]*limit", r"too many requests",
    r"usage[\s_-]*limit", r"usage[\s_-]*cap", r"\bquota\b",
    r"insufficient[\s_-]*(?:quota|balance|credit|funds|tokens)",
    r"out of credits", r"payment required", r"\b402\b", r"spending limit",
    r"(?:daily|weekly|monthly)[\s_-]*limit",
    r"用量限制", r"余额不足", r"配额", r"频率限制", r"请求过于频繁", r"超出限制",
)
_USAGE_LIMIT_RE = re.compile("|".join(USAGE_LIMIT_PATTERNS), re.IGNORECASE)


def classify_provider_error(text: str, default: str = "provider_error") -> str:
    """Map raw provider error text to a sanitized failure code."""
    return ("provider_usage_limit" if _USAGE_LIMIT_RE.search(text or "")
            else default)


def profile_state_path() -> Path:
    return record_dir() / "profile-state.json"


def read_profile_state() -> dict[str, Any]:
    path = profile_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_profile_state(mutate: Any) -> dict[str, Any]:
    """Apply a mutation to the auto-disable sidecar under one exclusive lock."""
    directory = record_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = profile_state_path()
    lock = directory / ".append.lock"
    with lock.open("a", encoding="utf-8") as lock_file:
        os.chmod(lock, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
            except (OSError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            result = mutate(data)
            temporary = state.with_name(f"{state.name}.{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, state)
            return result if isinstance(result, dict) else data
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def auto_disable_seconds() -> float:
    """Cooldown before an auto-disabled profile rejoins the pool; 0 means manual reset."""
    try:
        return float(os.environ.get("PI_WORKER_AUTO_DISABLE_SECONDS", "3600"))
    except ValueError:
        return 3600.0


def profile_auto_disabled(name: str, state: dict[str, Any] | None = None) -> bool:
    entry = (state if state is not None else read_profile_state()).get(name)
    if not isinstance(entry, dict):
        return False
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, str):
        return True
    try:
        deadline = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return deadline > dt.datetime.now(dt.timezone.utc)


def auto_disable_profile(name: str, *, run_id: str | None,
                         reason: str = "usage_limit") -> dict[str, Any]:
    seconds = auto_disable_seconds()
    entry: dict[str, Any] = {"reason": reason, "disabled_at": utc_now(), "run_id": run_id}
    entry["expires_at"] = (
        (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds))
        .isoformat().replace("+00:00", "Z") if seconds > 0 else None)

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        data[name] = entry
        return entry

    return update_profile_state(mutate)


def reset_profile_disabled(name: str) -> None:
    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        data.pop(name, None)
        return {}

    update_profile_state(mutate)


def profile_state_snapshot() -> dict[str, Any]:
    state = read_profile_state()
    snapshot: dict[str, Any] = {}
    for name, config in PROFILES.items():
        entry = state.get(name)
        active = isinstance(entry, dict) and profile_auto_disabled(name, state)
        snapshot[name] = {
            "routing_class": config["routing_class"], "provider": config["provider"],
            "model": config["model"],
            "configured": "enabled" if config["enabled"] else "disabled",
            "auto_disabled": active,
            "auto_disable": entry if active else None,
        }
    return snapshot


def reserve_selection(delegation_id: str, requested: str, routing_class: str) -> tuple[str, int | None, str]:
    """Atomically retrieve or create a sticky routing decision."""
    check_routing_class(routing_class)
    directory = record_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = directory / ".routing-state.json"
    lock = directory / ".append.lock"
    with lock.open("a", encoding="utf-8") as lock_file:
        os.chmod(lock, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
            except (OSError, json.JSONDecodeError):
                data = {}
            existing = data.get(delegation_id)
            if isinstance(existing, str):
                existing = {"profile": existing, "draw": None, "policy": "legacy_sticky_selection"}
            if isinstance(existing, dict) and isinstance(existing.get("profile"), str) \
                    and existing["profile"] and existing["profile"] not in PROFILES:
                # Sticky selection names a profile that is no longer registered.
                # Nothing is rewritten: no redraw, no state write, no lock change.
                raise removed_profile_error(delegation_id, existing["profile"])
            if isinstance(existing, dict) and existing.get("profile") in PROFILES:
                profile = existing["profile"]
                if not PROFILES[profile]["enabled"]:
                    raise RunnerError(f"profile {profile} is currently disabled")
                if profile_auto_disabled(profile):
                    if requested not in ("auto", profile):
                        raise RunnerError(
                            f"profile {profile} is auto-disabled after a usage limit; "
                            "rerun later or pick another --profile",
                            code="profile_auto_disabled", stage="routing")
                    try:
                        profile, draw = draw_profile(routing_class)
                    except RunnerError:
                        raise RunnerError(
                            f"no enabled {routing_class} profile remains after auto-disable",
                            code="no_enabled_profile", stage="routing")
                    policy = "auto_disabled_redraw"
                    data[delegation_id] = {"profile": profile, "draw": draw, "policy": policy}
                    temporary = state.with_name(f"{state.name}.{os.getpid()}.tmp")
                    with temporary.open("w", encoding="utf-8") as out:
                        json.dump(data, out, sort_keys=True, separators=(",", ":"))
                        out.flush()
                        os.fsync(out.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, state)
                    return profile, draw, policy
                if requested not in ("auto", profile):
                    raise RunnerError(f"delegation {delegation_id!r} is already fixed to {profile}")
                return profile, existing.get("draw"), existing.get("policy", "reused_existing_delegation")
            profile, draw, policy = select_profile(requested, routing_class, delegation_id, check_existing=False)
            data[delegation_id] = {"profile": profile, "draw": draw, "policy": policy}
            temporary = state.with_name(f"{state.name}.{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as out:
                json.dump(data, out, sort_keys=True, separators=(",", ":"))
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, state)
            return profile, draw, policy
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def validate_sticky_reservation(delegation_id: str, requested: str, routing_class: str) -> None:
    """Non-mutating mirror of ``reserve_selection``'s sticky-compatibility checks.

    The function confirms whether the requested profile would survive the
    sticky-reservation logic against an existing sticky delegation without
    writing routing state, drawing randomly, or appending records. When no
    sticky delegation exists yet, there is nothing to verify; the pool and draw
    logic are exercised at the actual reservation step.

    A sticky selection naming a profile that is no longer registered is
    reported (``code=profile_removed``) exactly as the normal reservation path
    reports it, so --validate-only never approves a launch that the real run
    would refuse.

    Raises a ``RunnerError`` with the same message, code, and stage the main
    path would produce when an incompatibility is detected. Returns nothing on
    success.
    """
    check_routing_class(routing_class)
    existing = read_previous_selection(delegation_id)
    if existing is None:
        # No sticky delegation recorded (missing, empty, or malformed entry).
        return

    if existing not in PROFILES:
        raise removed_profile_error(delegation_id, existing)

    if not PROFILES[existing]["enabled"]:
        raise RunnerError(f"profile {existing} is currently disabled")

    auto_disabled = profile_auto_disabled(existing)

    if auto_disabled:
        # Sticky exists but is auto-disabled after a usage limit. Callers may
        # reuse or redraw only when the request stays flexible (``auto``) or
        # matches the sticky name; conflicting requests reject.
        if requested not in ("auto", existing):
            raise RunnerError(
                f"delegation {delegation_id!r} is already fixed to {existing}")
        # Auto redraw eligibility: at least one alternative enabled profile
        # must remain in the routing class, but no random selection is
        # performed here (a real draw happens at reservation time).
        if requested == "auto" and not enabled_profiles(routing_class):
            raise RunnerError(
                f"no enabled {routing_class} profile remains after auto-disable",
                code="no_enabled_profile", stage="routing")
        return

    # Sticky is enabled and not auto-disabled: ordinary matching reuse path.
    # The request must agree with the sticky.
    if requested not in ("auto", existing):
        raise RunnerError(
            f"delegation {delegation_id!r} is already fixed to {existing}")


def find_agents(workdir: Path) -> Path | None:
    current = workdir.resolve()
    while True:
        candidate = current / "AGENTS.md"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def project_overrides(workdir: Path) -> dict[str, str]:
    path = find_agents(workdir)
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"(?ims)^##\s+Pi Worker\s*$\n(.*?)(?=^##\s+|\Z)", text)
    if not match:
        return {}
    result: dict[str, str] = {}
    for key, value in re.findall(r"(?im)^\s*-\s*(Profile|Thinking)\s*:\s*([^\s#]+)", match.group(1)):
        result[key.lower()] = value
    return result


def enabled_profiles(routing_class: str) -> list[str]:
    """Configured and not auto-disabled profiles for a class, in stable table order."""
    state = read_profile_state()
    return [name for name, config in PROFILES.items()
            if config["routing_class"] == routing_class and config["enabled"]
            and not profile_auto_disabled(name, state)]


def draw_profile(routing_class: str) -> tuple[str, int]:
    """Uniformly assign an enabled profile for the class; return it with its draw index."""
    pool = enabled_profiles(routing_class)
    if not pool:
        raise RunnerError(f"no enabled {routing_class} profile",
                          code="profile_config", stage="routing")
    draw = secrets.randbelow(len(pool))
    return pool[draw], draw


def select_profile(requested: str, routing_class: str, delegation_id: str, *, check_existing: bool = True) -> tuple[str, int | None, str]:
    check_routing_class(routing_class)
    existing = previous_selection(delegation_id) if check_existing else None
    if existing:
        if requested not in ("auto", existing):
            raise RunnerError(f"delegation {delegation_id!r} is already fixed to {existing}")
        if not profile_auto_disabled(existing):
            return existing, None, "reused_existing_delegation"
        profile, draw = draw_profile(routing_class)
        return profile, draw, "auto_disabled_redraw"
    if requested != "auto":
        profile = requested
        if not PROFILES[profile]["enabled"]:
            raise RunnerError(f"profile {profile} is currently disabled")
        if profile_auto_disabled(profile):
            raise RunnerError(
                f"profile {profile} is auto-disabled after a usage limit",
                code="profile_auto_disabled", stage="routing")
        expected = PROFILES[profile]["routing_class"]
        if expected != routing_class:
            raise RunnerError(f"profile {profile} is {expected}, not {routing_class}")
        return profile, None, "fixed_profile"
    profile, draw = draw_profile(routing_class)
    return profile, draw, "random_enabled_profile"


# Pi-native auth: the worker inherits the runner environment unchanged
# (``os.environ.copy()``). Pi's default auth storage, models config, and any
# provider credentials are used as-is; the runner manages no credential
# environment variable, spawns no login shell, and copies or writes no
# key/auth settings. Availability and exact-model checks stay with
# ``check_provider`` (pi auth check --no-refresh, exact catalog, deadline).


def check_provider(pi: str, provider: str, model: str, env: dict[str, str],
                   *, deadline: float | None = None) -> None:
    """Check Pi auth and exact-model availability, bounded by ``deadline``.

    ``deadline`` is an optional ``time.monotonic()`` value shared with the rest of
    the run. When supplied, the remaining budget is enforced before each call and
    each subprocess timeout is ``min(per-step cap, remaining)`` so preflight can
    never overrun the run deadline. A ``subprocess.TimeoutExpired`` that consumes
    the actual total deadline is mapped to ``TimeoutError`` (terminal timeout),
    while a per-step timeout that still leaves budget is a sanitized preflight
    failure. Direct callers that omit ``deadline`` keep the original fixed
    30s/60s caps and never get a deadline-derived ``TimeoutError``.
    """
    def _budget(per_step: float) -> float:
        if deadline is None:
            return per_step
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Pi preflight timed out")
        return min(per_step, remaining)

    auth_budget = _budget(PREFLIGHT_AUTH_TIMEOUT_SECONDS)
    try:
        auth = subprocess.run(
            [pi, "auth", "check", "--model", f"{provider}/{model}", "--json", "--no-refresh"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=auth_budget,
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Pi preflight timed out") from exc
        raise RunnerError(f"Pi authentication check timed out for {provider}/{model}",
                          code="authentication_timeout", stage="preflight") from exc
    if auth.returncode != 0:
        raise RunnerError(f"Pi authentication check failed for {provider}/{model}",
                          code="authentication_failed", stage="preflight")
    catalog_budget = _budget(PREFLIGHT_CATALOG_TIMEOUT_SECONDS)
    try:
        catalog = subprocess.run(
            [pi, "--list-models", model], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=catalog_budget,
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Pi preflight timed out") from exc
        raise RunnerError(f"Pi model catalog check timed out for {provider}/{model}",
                          code="model_catalog_timeout", stage="preflight") from exc
    if catalog.returncode != 0:
        raise RunnerError(f"Pi model catalog check failed for {provider}/{model}",
                          code="model_catalog_failed", stage="preflight")
    exact = f"{provider}/{model}"
    found = False
    for line in catalog.stdout.splitlines():
        columns = line.strip().split()
        if len(columns) >= 2 and columns[0] == provider and columns[1] == model:
            found = True
        if exact in columns:
            found = True
    if not found:
        raise RunnerError(f"exact model unavailable: {exact}",
                          code="exact_model_unavailable", stage="preflight")


def intercom_contract_appendix(supervisor_id: str, delegation_id: str) -> str:
    """Build the bounded communication contract appended to a task in intercom mode.

    The contract is only sent to the worker; it is intentionally not persisted in the
    run receipt. Acceptance is decided by the main agent's receipt and review, not by
    any DONE message. The text is treated as instructions for the worker, not as a
    security sandbox.
    """
    encoded_id = json.dumps(delegation_id, ensure_ascii=False)
    return (
        "\n\nINTERCOM COMMUNICATION CONTRACT (added by pi-worker runner; "
        "do not edit the assignment above):\n"
        f"- The exact full supervisor UUID you must talk to is: {supervisor_id}\n"
        f"- Your delegation identifier is {encoded_id}. Include it in EVERY READY, "
        "ask, and DONE message you send.\n"
        "- Before any edits:\n"
        "  1. Call the `intercom` tool with action `list` to see the shortened roster. "
        "Roster entries are shortened IDs only; they do NOT prove identity.\n"
        "  2. Call `intercom` with action `status` to confirm your own current full "
        "session ID. `list` cannot resolve a short ID to a full UUID, and there is no "
        "tool that does; the full supervisor UUID above is trusted input from your "
        "runner, and no claimed UUID in a message proves authentication.\n"
        "  3. Send READY (with your delegation ID and your own session ID) to that "
        "exact full supervisor UUID via the `intercom` tool with action `send`. If "
        "delivery fails, stop without editing and report the blocker. A successful "
        "exact-target delivery confirms routing only: it is NOT authentication and "
        "NOT proof the supervisor processed the message.\n"
        "- Communicate only with the assigned supervisor. Contract unknowns require "
        "the `intercom` tool with action `ask`, then wait; do not infer permission "
        "from unrelated peers. Report blockers or permission needs with `ask` early.\n"
        "- Default QUIET: send one short READY (max 2 lines) via action `send`, then "
        "work. No routine PROGRESS, heartbeat, courtesy updates, verbose plans, or "
        "per-test status. Send a message via action `send` only for an actionable "
        "ask, blocker, scope deviation, or a status specifically requested by the "
        "main agent.\n"
        "- Work in small increments: narrow outcomes reached in focused 2-5 minute "
        "targets, saving each scoped change to disk and re-running its scoped check "
        "before moving on. The main agent keeps architecture decisions.\n"
        "- On completion: send exactly one DONE via action `send`, then settle "
        "immediately (return your final answer and let the runner reach "
        "`agent_settled`); do not wait for a reply. DONE is at most 1200 characters "
        "in this fixed shape: delegation; changed relative paths (max 8, plus an "
        "overflow count); checks (max 3, command plus pass/fail summary); limits "
        "(max 2); receipt/run IDs known, else unknown. Never invent IDs or success. "
        "Do NOT send a completion acknowledgment or ask at completion. The DONE "
        "summary is not acceptance: the main agent decides from the receipt and "
        "review, even if DONE never arrives (crash or failure included). The "
        "runner's RUN_FINISHED record is authoritative.\n"
        "- This runner is one-run-per-process; it terminates at `agent_settled`. "
        "Ask timeout or cancel is not proof the work stopped. Cancel and scope changes "
        "via intercom are advisory only; the runner enforces the deadline and the main "
        "agent decides scope.\n"
    )


def validate_intercom_args(transport: str, extension: str | None,
                            supervisor: str | None) -> tuple[str | None, str | None]:
    """Validate explicit transport arguments before routing reservation runs.

    Returns the resolved extension absolute path and the supervisor id when valid, or
    raises RunnerError with code ``invalid_transport_args`` and stage ``routing``. The
    runner performs these checks before reserving a sticky selection so that an invalid
    opt-in does not silently consume routing state.
    """
    if transport == "rpc":
        if extension or supervisor:
            raise RunnerError(
                "--transport rpc rejects --intercom-extension and --supervisor",
                code="invalid_transport_args", stage="routing")
        return None, None
    if not extension:
        raise RunnerError("--transport intercom requires --intercom-extension",
                          code="invalid_transport_args", stage="routing")
    if not supervisor:
        raise RunnerError("--transport intercom requires --supervisor",
                          code="invalid_transport_args", stage="routing")
    ext = Path(extension).expanduser()
    if not ext.is_absolute():
        raise RunnerError("--intercom-extension must be an absolute path",
                          code="invalid_transport_args", stage="routing")
    if ext.suffix.lower() not in (".ts", ".js"):
        raise RunnerError("--intercom-extension must end with .ts or .js",
                          code="invalid_transport_args", stage="routing")
    if not ext.is_file():
        raise RunnerError(f"--intercom-extension file not found: {ext}",
                          code="invalid_transport_args", stage="routing")
    if not _UUID_RE.fullmatch(supervisor):
        raise RunnerError("--supervisor must be a full UUID",
                          code="invalid_transport_args", stage="routing")
    return str(ext.resolve()), supervisor


def send(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _bump_progress(progress: dict[str, Any] | None, kind: str,
                   checkpoint: Any) -> None:
    """Accumulate a coarse event counter and snapshot it at most every interval."""
    if progress is None:
        return
    counts = progress.setdefault("event_counts", {})
    counts[kind] = counts.get(kind, 0) + 1
    progress["event_count"] = progress.get("event_count", 0) + 1
    progress["last_event"] = kind
    progress["updated_at"] = utc_now()
    if checkpoint is not None:
        try:
            checkpoint(progress)
        except Exception:
            pass


def _message_session_id(message: Any) -> str | None:
    """Extract a session id Pi exposes on an RPC event or response, if any."""
    if not isinstance(message, dict):
        return None
    for container in (message, message.get("data")):
        if not isinstance(container, dict):
            continue
        value = container.get("sessionId")
        if isinstance(value, str) and value:
            return value
    return None


def _bounded_tool_limit(value: Any) -> float | None:
    """A usable per-tool bound in seconds, or ``None`` when it is disabled.

    ``0`` (and any non-positive, non-finite, or non-numeric value) disables the
    bound entirely: there is no idle/no-activity timeout of any kind, only the
    existing global deadline and this per-running-tool bound.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _validated_max_auto_retries(value: Any) -> int:
    """Normalize and validate the automatic-retry count bound.

    A non-negative whole number of retries; ``0`` explicitly rejects every
    automatic retry. A bool is never a count, and a fractional or negative value
    is rejected before any side effect runs, so a bad value never consumes
    routing state.
    """
    if isinstance(value, bool):
        number, exact = -1, False
    else:
        try:
            number = int(value)
            exact = float(value) == float(number)
        except (TypeError, ValueError):
            number, exact = -1, False
    if number < 0 or not exact:
        raise RunnerError(
            "--max-auto-retries must be a non-negative whole number of retries "
            "(0 rejects every automatic retry)",
            code="invalid_max_auto_retries", stage="routing")
    return number


def _bounded_retry_budget(value: Any) -> float | None:
    """A usable aggregate retry-time budget in seconds, or ``None`` when disabled.

    ``0`` (and any non-positive, non-finite, or non-numeric value) disables the
    time bound only; the retry *count* bound stays a separate, always-enforced
    value. Mirrors ``_bounded_tool_limit`` so the two bounds read the same way.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _validated_retry_budget(value: Any) -> float:
    """Normalize and validate the aggregate retry-time bound for the CLI.

    ``0`` is a valid, explicit "disabled" value. Negative, NaN, infinite, and
    non-numeric values are rejected before any side effect runs.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = -1.0
    if isinstance(value, bool) or not math.isfinite(number) or number < 0.0:
        raise RunnerError(
            "--retry-budget-seconds must be a finite, non-negative number of "
            "seconds (0 disables the retry time budget)",
            code="invalid_retry_budget", stage="routing")
    return number


def run_rpc(
    pi: str, workdir: Path, contract: str, profile: str, thinking: str,
    tools: str, timeout: float, env: dict[str, str], delegation_id: str,
    *,
    intercom_extension: str | None = None,
    supervisor_id: str | None = None,
    progress: dict[str, Any] | None = None,
    checkpoint: Any = None,
    tool_timeout_seconds: Any = DEFAULT_TOOL_TIMEOUT_SECONDS,
    max_auto_retries: Any = DEFAULT_MAX_AUTO_RETRIES,
    retry_budget_seconds: Any = DEFAULT_RETRY_BUDGET_SECONDS,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Run one Pi worker over JSONL RPC, optionally via the intercom extension.

    Keyword-only ``intercom_extension`` and ``supervisor_id`` opt into the optional
    Pi-main intercom transport; the default RPC argv and call shape are preserved when
    both are left as ``None``. The default RPC path still uses ``--no-extensions``;
    intercom only extends the argv with ``-e <extension>`` before it, so the named
    extension is loaded while auto-discovery stays off, and appends ``intercom`` to
    the allowed tool list (it is the only new tool, and only on top of the chosen
    tool mode; read-only stays free of bash/edit/write).

    ``tool_timeout_seconds`` is also keyword-only and defaults to the documented
    ``DEFAULT_TOOL_TIMEOUT_SECONDS``, so every existing positional caller is
    unchanged. It bounds a *single* running tool call from its
    ``tool_execution_start`` to its matching ``tool_execution_end``; a value of ``0``
    (or any non-positive value) disables it. Soft checkpoint steering is keyed to
    ``timeout`` (the RPC task budget) and is independent of that bound.

    ``max_auto_retries`` and ``retry_budget_seconds`` are keyword-only with the
    documented defaults ``DEFAULT_MAX_AUTO_RETRIES`` and
    ``DEFAULT_RETRY_BUDGET_SECONDS``, so every existing positional caller and
    the Pi argv are unchanged. They bound automatic *agent-turn* retries from
    the observed ``auto_retry_start``/``auto_retry_end`` events only (compaction
    retries are excluded by the observer): the ``(count + 1)``-th start raises a
    sanitized ``retry_limit`` failure, and reaching the aggregate observed retry
    time raises ``retry_budget_exceeded``. Both fire even when the retry is
    silent or unrelated streaming keeps arriving, because they are computed from
    real observed spans rather than from any per-retry ``delayMs``, and both end
    the run through the same bounded graceful abort as a per-tool timeout. The
    global deadline and its preflight-inclusive behaviour are unchanged.
    """
    config = PROFILES[profile]
    provider, model = config["provider"], config["model"]
    base_allowed = ("read,grep,find,ls" if tools == "read-only"
                    else "read,bash,edit,write,grep,find,ls")
    intercom_mode = bool(intercom_extension)
    allowed = base_allowed + "," + INTERCOM_TOOL if intercom_mode else base_allowed
    # Default argv order is fixed and Codex-compatible; the intercom opt-in only
    # extends it with a single '-e <path>' pair before --no-extensions so exactly
    # the named extension loads while auto-discovery stays off.
    cmd = [
        pi, "--mode", "rpc", "--provider", provider, "--model", model,
        "--thinking", thinking, "--tools", allowed,
    ]
    if intercom_mode:
        assert intercom_extension is not None
        cmd.extend(["-e", intercom_extension])
    cmd.extend([
        "--no-extensions", "--no-skills", "--no-prompt-templates", "--approve",
        "--name", f"pi-worker-{delegation_id}",
    ])
    # Validated before the child is spawned: a bad retry bound must never leave
    # an orphan Pi process behind, and the values are normalized once here. The
    # API boundary is strict like the CLI: only an explicit ``0`` disables the
    # time bound, a negative/non-finite/non-numeric value is an error rather
    # than a silently disabled bound.
    retry_limit = _validated_max_auto_retries(max_auto_retries)
    retry_budget = _bounded_retry_budget(_validated_retry_budget(retry_budget_seconds))
    proc = subprocess.Popen(
        cmd, cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    events: list[str] = []
    observer: "ProgressObserver | None" = None
    stats: dict[str, Any] | None = None
    terminal_error = False
    error_text = ""
    stderr_tail: deque[str] = deque(maxlen=40)
    output: queue.Queue[tuple[str, str | None]] = queue.Queue()
    assert proc.stdout is not None and proc.stderr is not None

    def drain(name: str, stream: Any) -> None:
        for line in stream:
            if name == "stderr":
                stderr_tail.append(line)
            output.put((name, line))
        output.put((name, None))

    threading.Thread(target=drain, args=("stdout", proc.stdout), daemon=True).start()
    threading.Thread(target=drain, args=("stderr", proc.stderr), daemon=True).start()
    deadline = time.monotonic() + timeout
    write_task = tools != "read-only"
    tool_limit = _bounded_tool_limit(tool_timeout_seconds)

    def receive(until: float, *, allow_wake: bool = False) -> dict[str, Any]:
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0:
                if allow_wake:
                    # A per-tool bound or checkpoint fired before the global
                    # deadline; the caller re-evaluates instead of failing.
                    raise _Wake()
                raise TimeoutError("Pi worker timed out")
            try:
                source, line = output.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RunnerError(f"Pi exited unexpectedly (code {proc.returncode})",
                                      code=classify_provider_error(
                                          "".join(stderr_tail), "rpc_process_exit"),
                                      stage="rpc")
                continue
            if line is None:
                if source == "stdout":
                    raise RunnerError(f"Pi stdout closed unexpectedly (code {proc.poll()})",
                                      code=classify_provider_error(
                                          "".join(stderr_tail), "rpc_stdout_closed"),
                                      stage="rpc")
                continue
            if source == "stderr":
                # Drain it to avoid deadlocks, but never echo potentially sensitive provider output.
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    def abort_with_grace() -> None:
        if proc.poll() is not None:
            return
        try:
            send(proc, {"id": "abort", "type": "abort"})
            grace = time.monotonic() + 3
            while time.monotonic() < grace:
                message = receive(grace)
                if (message.get("type") == "response" and message.get("id") == "abort"
                        and message.get("command") == "abort" and message.get("success")):
                    break
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def tool_wake_after() -> float | None:
        """Seconds until the oldest running tool exceeds the per-tool bound.

        ``None`` when no tool is running or the bound is disabled. The value
        comes from the already-tracked tool start, so a partial tool update or
        unrelated model traffic cannot move it.
        """
        if observer is None or tool_limit is None:
            return None
        try:
            return observer.tool_timeout_wake_after(tool_limit)
        except Exception:
            return None

    def raise_tool_timeout() -> None:
        """Fail the run with a sanitized per-tool timeout.

        The message carries only the configured bound: no tool name, call id,
        arguments, or output is read here or persisted anywhere.
        """
        raise RunnerError(
            f"Pi tool call exceeded the per-tool timeout of {tool_limit:g}s",
            code="tool_timeout", stage="rpc")

    def retry_wake_after() -> float | None:
        """Seconds until the aggregate observed retry time reaches the budget.

        ``None`` when the budget is disabled or no retry has been observed yet.
        Derived from real observed spans (in-flight included), so partial retry
        updates or unrelated model traffic can neither reset nor postpone it.
        """
        if observer is None or retry_budget is None:
            return None
        try:
            return observer.retry_budget_wake_after(retry_budget)
        except Exception:
            return None

    def retry_budget_spent() -> bool:
        """True once the aggregate observed retry time reached the budget."""
        if observer is None or retry_budget is None:
            return False
        try:
            return bool(observer.retry_budget_exhausted(retry_budget))
        except Exception:
            return False

    def raise_retry_limit() -> None:
        """Fail the run with a sanitized automatic-retry count failure.

        The message carries only the configured count: no provider text, retry
        attempt, delay, or payload is read here or persisted anywhere.
        """
        raise RunnerError(
            f"Pi automatic retries exceeded the configured limit of {retry_limit}",
            code="retry_limit", stage="rpc")

    def raise_retry_budget() -> None:
        """Fail the run with a sanitized retry-time budget failure.

        Like :func:`raise_retry_limit`, only the configured budget is reported.
        """
        raise RunnerError(
            f"Pi automatic retries exhausted the retry budget of {retry_budget:g}s",
            code="retry_budget_exceeded", stage="rpc")

    def send_next_checkpoint() -> None:
        """Steer at most one due checkpoint, if any is due.

        Per ``docs/rpc-commands.md`` a ``steer`` is queued and delivered after
        the current assistant turn finishes its tool calls, so this never
        interrupts a running tool; the docs do not promise delivery either.
        It is therefore fire-and-forget: no response is awaited, because a Pi
        build (or the test fake) may legitimately ignore it. A notice is
        registered only when the request actually reached stdin.
        """
        code = checkpoints.next_code()
        if code is None:
            return
        message = checkpoint_message(code, write_task=write_task)
        if not message:  # pragma: no cover - unreachable for known codes
            checkpoints.mark_sent(code)
            return
        try:
            send(proc, {"id": f"checkpoint-{code}", "type": "steer", "message": message})
        except Exception:
            # The pipe is gone; the next read reports the real process failure.
            return
        checkpoints.mark_sent(code)
        if observer is not None:
            try:
                observer.record_checkpoint(code)
            except Exception:
                pass

    try:
        # Best-effort pre-prompt session-id capture for failure diagnostics. It is
        # an optional, bounded RPC lookup: it never calls a model, never runs
        # before the intercom preflight, and never fails the run when unsupported.
        # The value is only used to preserve the actual session on a failed run.
        def record_session(value: str | None) -> bool:
            if not isinstance(value, str) or not value:
                return False
            if progress is not None:
                progress["session_id"] = value
                if checkpoint is not None:
                    try:
                        checkpoint(progress)
                    except Exception:
                        pass
            return True

        def capture_early_session() -> None:
            if progress is None or progress.get("session_id"):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            window = min(EARLY_SESSION_TIMEOUT_SECONDS, remaining)
            request_id = "early-session"
            try:
                send(proc, {"id": request_id, "type": "get_session_stats"})
            except Exception:
                return
            until = time.monotonic() + window
            while True:
                if time.monotonic() >= until:
                    return
                try:
                    message = receive(until)
                except TimeoutError:
                    # Unsupported or slow early RPC: fall back, do not fail.
                    return
                if record_session(_message_session_id(message)):
                    return
                if (isinstance(message, dict)
                        and message.get("type") == "response"
                        and message.get("id") == request_id):
                    # Correlated response without a usable session id.
                    return

        if intercom_mode:
            assert supervisor_id is not None
            # Load preflight: confirm the extension registered an `intercom` command
            # before any task prompt is sent. Only a response correlated by id AND
            # command with success===true counts; anything else is drained. A missing
            # response stays bounded by the run deadline (timeout); a received but
            # failed/malformed response is sanitized to intercom_unavailable.
            send(proc, {"id": INTERCOM_GET_COMMANDS_ID, "type": "get_commands"})
            preflight_response: dict[str, Any] | None = None
            while preflight_response is None:
                message = receive(deadline)
                if not isinstance(message, dict):
                    continue
                if (message.get("type") == "response"
                        and message.get("id") == INTERCOM_GET_COMMANDS_ID
                        and message.get("command") == "get_commands"):
                    preflight_response = message
                # Unrelated RPC events (extension-load notices, prompts, etc.) are
                # drained so the preflight stays correlated.
            data = preflight_response.get("data")
            commands = data.get("commands") if isinstance(data, dict) else None
            found = (
                preflight_response.get("success") is True
                and isinstance(commands, list)
                and any(isinstance(entry, dict)
                        and entry.get("name") == INTERCOM_TOOL
                        and entry.get("source") == "extension"
                        for entry in commands)
            )
            if not found:
                raise RunnerError(
                    "intercom extension command not registered by Pi",
                    code="intercom_unavailable", stage="preflight")
            prompt_text = contract + intercom_contract_appendix(supervisor_id, delegation_id)
        else:
            prompt_text = contract
        # Concise upfront time-budget reminder, appended last so it never
        # displaces the contract, the intercom communication contract, or the
        # permission instructions. It describes the soft checkpoints and the
        # per-tool bound; both are also enforced by this process.
        prompt_text += time_budget_reminder(
            budget_seconds=timeout, tool_timeout_seconds=tool_limit,
            write_task=write_task)
        capture_early_session()
        # Prompt-relative phase telemetry. The observer only reads the RPC
        # messages it already receives; it is created after the prompt is sent,
        # so a run that never reached the prompt carries no metrics at all.
        send(proc, {"id": "work", "type": "prompt", "message": prompt_text})
        # Created unconditionally: the per-tool bound and the soft checkpoints
        # must hold for every run, including a direct ``run_rpc`` call that
        # passes no progress dict. ``progress`` only controls where the
        # fixed-schema snapshot is published, never whether it is tracked.
        observer = ProgressObserver()
        observer.start()
        # Soft checkpoints are keyed to the RPC task budget, proposed one code at a
        # time, and never abort, pause, or score a run on their own.
        checkpoints = CheckpointPolicy(timeout).start()
        settled = False
        while not settled:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("Pi worker timed out")
            # Wake for the earliest of: the global deadline, the per-tool bound on
            # the oldest running tool, and the next soft checkpoint. Only a wake
            # strictly before the global deadline raises the internal sentinel,
            # so an exhausted global budget is still the global run timeout.
            wake_at = deadline
            allow_wake = False
            tool_wake = tool_wake_after()
            if tool_wake is not None and now + tool_wake < wake_at:
                wake_at = now + tool_wake
                allow_wake = True
            checkpoint_wake = checkpoints.next_due_after()
            if checkpoint_wake is not None and now + checkpoint_wake < wake_at:
                wake_at = now + checkpoint_wake
                allow_wake = True
            retry_wake = retry_wake_after()
            if retry_wake is not None and now + retry_wake < wake_at:
                wake_at = now + retry_wake
                allow_wake = True
            try:
                message = receive(wake_at, allow_wake=allow_wake)
            except _Wake:
                # The idle read timed out on an internal timer, not the deadline.
                if tool_wake_after() is not None and tool_wake_after() <= 0.0:
                    raise_tool_timeout()
                if retry_budget_spent():
                    raise_retry_budget()
                send_next_checkpoint()
                continue
            kind = str(message.get("type", ""))
            events.append(kind)
            record_session(_message_session_id(message))
            # Always fold the message into the observer (per-tool bound, phase
            # timings); publish the snapshot only when a progress dict exists.
            if observer is not None:
                snapshot = observer.observe(message)
                if progress is not None:
                    progress["progress_metrics"] = snapshot
                # Retry bounds are enforced on every observed message, not only
                # on an idle wake, so a chatty retry cannot slip past them.
                if observer.retry_count > retry_limit:
                    raise_retry_limit()
                if retry_budget_spent():
                    raise_retry_budget()
            _bump_progress(progress, kind, checkpoint)
            # Streaming model traffic never resets a tool's start, so the bound is
            # re-checked after every message, not only on an idle wake.
            if tool_wake_after() is not None and tool_wake_after() <= 0.0:
                raise_tool_timeout()
            send_next_checkpoint()
            if kind in ("message_end", "turn_end"):
                agent_message = message.get("message") or {}
                if (agent_message.get("role") == "assistant"
                        and agent_message.get("stopReason") == "error"):
                    terminal_error = True
                    detail = agent_message.get("errorMessage")
                    if isinstance(detail, str):
                        error_text += " " + detail
            if kind in RETRY_END_EVENTS and message.get("success") is True:
                # A successful automatic retry *recovered* the provider error:
                # the turn really did produce an answer, so the earlier sticky
                # error must not fail a run that later settles cleanly, and its
                # text must not misclassify a later, unrelated failure. Only an
                # explicit success clears it; a failed retry end leaves the
                # error in place, so a final provider error still fails.
                terminal_error = False
                error_text = ""
            if kind == "agent_settled":
                settled = True
        if terminal_error:
            raise RunnerError("Pi provider returned an assistant error",
                              code=classify_provider_error(error_text), stage="rpc")
        send(proc, {"id": "stats", "type": "get_session_stats"})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pi stats request timed out")
            message = receive(deadline)
            if message.get("type") == "response" and message.get("id") == "stats":
                if not message.get("success"):
                    raise RunnerError("Pi get_session_stats failed",
                                      code="stats_failed", stage="stats")
                stats = message.get("data") if isinstance(message.get("data"), dict) else None
                if progress is not None and isinstance(stats, dict):
                    progress["session_id"] = stats.get("sessionId")
                    if checkpoint is not None:
                        try:
                            checkpoint(progress)
                        except Exception:
                            pass
                break
        return stats, events
    except (TimeoutError, KeyboardInterrupt):
        abort_with_grace()
        raise
    except RunnerError as exc:
        # A per-tool timeout or an exhausted automatic-retry bound ends the
        # worker, but only after the same bounded graceful abort used for the
        # global timeout, so Pi can stop its turn (and an in-flight automatic
        # retry) before the process is terminated. This is Pi's own abort
        # handshake plus SIGTERM/SIGKILL of the runner's own child: it is not a
        # per-shell sandbox guarantee and cannot promise cleanup of descendants
        # Pi spawned itself.
        if exc.code in ("tool_timeout", "retry_limit", "retry_budget_exceeded"):
            abort_with_grace()
        raise
    finally:
        # Final snapshot so a failed or aborted run still keeps the stages it
        # reached; fixed-schema numbers only, never payload content.
        if observer is not None:
            try:
                snapshot = observer.snapshot()
            except Exception:
                snapshot = None
            if progress is not None and isinstance(snapshot, dict):
                progress["progress_metrics"] = snapshot
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def previous_run_id(delegation_id: str) -> str | None:
    for item in reversed(iter_records()):
        if item.get("delegation_id") == delegation_id and item.get("run_id"):
            return str(item["run_id"])
    return None


def usage_from_stats(stats: dict[str, Any] | None) -> dict[str, Any]:
    tokens = stats.get("tokens") if stats else None
    tokens = tokens if isinstance(tokens, dict) else {}
    return {
        "input_tokens": tokens.get("input"), "cached_input_tokens": tokens.get("cacheRead"),
        "output_tokens": tokens.get("output"), "reasoning_tokens": tokens.get("reasoning"),
        "total_tokens": tokens.get("total"), "cost_usd": stats.get("cost") if stats else None,
        "gpt_tokens_saved": None, "source": "pi_rpc_session_stats",
    }


def verification_items(values: list[str]) -> list[dict[str, str]]:
    items = []
    for value in values:
        command, separator, result = value.partition("::")
        items.append({"command": command, "result": result if separator else "observed"})
    return items


def metadata_quality(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    if args.assignment_goal == "Bounded Pi worker assignment":
        warnings.append("generic_goal")
    if not args.scope:
        warnings.append("missing_scope")
    if args.acceptance_mode == "deterministic" and not args.acceptance_check:
        warnings.append("missing_acceptance_checks")
    return {"complete": not warnings, "warnings": warnings}


def legacy_review_event(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    results = {"accepted": "usable", "needs_rework": "partial", "rejected": "unusable"}
    return {
        "schema_version": 3, "record_type": "review", "phase": "reviewed",
        "review_id": secrets.token_hex(16), "target_run_id": run_id,
        "supersedes_review_id": None, "created_at": utc_now(),
        "outcome": {"review_verdict": args.review_status,
                    "result": results[args.review_status],
                    "summary": "Review supplied with runner invocation",
                    "changed_files": [], "main_rework": args.main_rework},
        "verification": verification_items(args.verification),
        "timing": {"main_review_seconds": None, "main_rework_seconds": None,
                   "main_bookkeeping_seconds": None},
        # Retired fallback-routing marker; kept only for receipt shape
        # compatibility and always null.
        "routing_lesson": None,
    }


def base_record(args: argparse.Namespace, profile: str, draw: int | None, policy: str) -> dict[str, Any]:
    config = PROFILES[profile]
    provider, model, routing = config["provider"], config["model"], config["routing_class"]
    # contract_digest identifies the exact assignment text (SHA-256 hex only,
    # never content or a path); the override records a deliberate
    # same-contract retry after a timeout. Both appear on every terminal
    # receipt, successful or failed, including runs that never triggered the
    # gate. task_group_id is a caller-supplied coarse correlation label: it is
    # recorded only when supplied so the legacy assignment shape is unchanged,
    # and it never influences identity, gating, or routing.
    assignment = {"goal": args.assignment_goal, "scope": args.scope,
                  "tool_mode": args.tools, "acceptance_checks": args.acceptance_check,
                  "contract_digest": getattr(args, "contract_digest", None),
                  "unchanged_timeout_retry_override":
                      bool(getattr(args, "allow_unchanged_timeout_retry", False)),
                  "features": {"task_kind": args.task_kind,
                               "estimated_files": args.estimated_files,
                               "risk": args.risk,
                               "acceptance_mode": args.acceptance_mode},
                  "metadata_quality": metadata_quality(args)}
    if getattr(args, "task_group_id", None) is not None:
        assignment["task_group_id"] = args.task_group_id
    return {
        "schema_version": 3, "record_type": "run", "phase": "completed",
        "run_id": secrets.token_hex(16),
        "record_kind": getattr(args, "record_kind", "production"),
        "task_id": args.task_id or args.delegation_id, "delegation_id": args.delegation_id,
        "previous_run_id": previous_run_id(args.delegation_id),
        "created_at": utc_now(), "finished_at": None,
        "worker": {"worker_type": profile, "runtime": "pi", "provider": provider,
                   "model": model, "thinking": args.thinking, "session_id": None,
                   # Default (rpc) receipts keep the original shape with no transport
                   # field; only the explicit intercom opt-in adds its marker.
                   **({"transport": "intercom"}
                      if getattr(args, "transport", "rpc") == "intercom" else {})},
        "routing": {"routing_class": routing, "profile_source": args.profile_source,
                    "random_policy": policy, "random_draw": draw},
        "project": {"id": project_id(Path(args.workdir))},
        "assignment": assignment,
        "outcome": {"status": "failed", "result": "unusable", "summary": None,
                    "changed_files": [], "review_verdict": "pending",
                    "main_rework": "none"},
        "verification": verification_items(args.verification),
        "usage": usage_from_stats(None),
        "timing": {"worker_seconds": None, "main_briefing_seconds": None,
                   "main_monitoring_seconds": None, "main_review_seconds": None,
                   "main_rework_seconds": None, "main_bookkeeping_seconds": None},
        "failure": None,
        # Retired fallback-routing marker; kept only for receipt shape
        # compatibility and always null.
        "routing_lesson": None,
    }


def validate_contract_file_utf8(path: Path) -> None:
    """Confirm the contract file is UTF-8 decodable without retaining content.

    The real launch path reads the contract via ``read_text(encoding='utf-8')``,
    so any decode error there would surface only after a routing reservation.
    This helper mirrors that decode on the validate-only path by streaming the
    file in chunks and discarding each decoded chunk immediately. Nothing is
    retained, printed, or echoed; only read errors and ``UnicodeDecodeError``
    are surfaced as ``RunnerError`` so an invalid encoding is caught before
    any side effect runs.

    A standard incremental UTF-8 decoder is used so a multi-byte code point
    that straddles the 64 KiB read boundary is not rejected by the helper:
    each chunk's leading 1-2 leading bytes and the trailing continuation bytes
    both span the chunk boundary, but the incremental decoder buffers the
    partial state and only surfaces an invalid encoding once enough bytes
    arrive (or at finalize time). The decoder is finalized at EOF so a
    truncated trailing multi-byte sequence still raises ``UnicodeDecodeError``
    before any side effect runs.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(path, "rb") as raw:
            while True:
                chunk = raw.read(64 * 1024)
                if not chunk:
                    break
                # Returned decoded text is intentionally discarded; the
                # decoder only retains the partial trailing bytes needed to
                # complete the next code point. Contract content is never
                # accumulated, persisted, or echoed.
                decoder.decode(chunk, final=False)
            # Finalize flushes any buffered partial bytes: an incomplete
            # trailing multi-byte sequence raises UnicodeDecodeError here.
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise RunnerError(f"contract file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise RunnerError(f"contract file not readable: {path}") from exc


def read_contract_text(path: Path) -> str:
    """Return the exact contract text that will be handed to ``run_rpc``.

    The launch path sends this single decoded string verbatim, so the digest
    taken from it can never describe different bytes than the ones the worker
    sees. Decoding mirrors ``validate_contract_file_utf8``: a standard
    incremental decoder is used so a multi-byte code point straddling a chunk
    boundary is buffered rather than rejected, and the decoder is finalized at
    EOF so a truncated trailing sequence still fails here rather than after a
    routing reservation. Read and decode errors surface as ``RunnerError`` with
    the same wording the validate-only path has always used.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    parts: list[str] = []
    try:
        with open(path, "rb") as raw:
            while True:
                chunk = raw.read(64 * 1024)
                if not chunk:
                    break
                parts.append(decoder.decode(chunk, final=False))
            parts.append(decoder.decode(b"", final=True))
    except UnicodeDecodeError as exc:
        raise RunnerError(f"contract file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise RunnerError(f"contract file not readable: {path}") from exc
    return "".join(parts)


def contract_digest(text: str) -> str:
    """SHA-256 hex digest of the exact contract text sent to ``run_rpc``.

    The digest is taken from the decoded string *before* any intercom appendix
    is appended, so it identifies the assignment itself and not the transport
    scaffolding. It proves byte-text sameness only: an identical digest never
    proves the scope is any better than the run that timed out.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def latest_prior_run_record(delegation_id: str, record_kind: str) -> dict[str, Any] | None:
    """Newest actual RUN terminal record for this delegation, or ``None``.

    Only ``record_type == "run"`` records count. Lifecycle, review, and
    notification records are skipped, and a legacy schema-v2 run written before
    ``record_type`` existed is accepted as a run. Records are filtered to the
    caller's own ``record_kind`` (an absent tag reads as ``production``), so a
    production launch is never gated by a test-fixture run that happens to share
    the delegation ID. This is a read-only scan: it mutates nothing.
    """
    for item in reversed(iter_records()):
        if item.get("delegation_id") != delegation_id:
            continue
        record_type = item.get("record_type")
        if record_type not in ("run", None):
            continue
        if not item.get("run_id"):
            continue
        if (item.get("record_kind") or "production") != record_kind:
            continue
        return item
    return None


def check_unchanged_timeout_retry(delegation_id: str, digest: str, *,
                                  record_kind: str = "production",
                                  override: bool = False) -> None:
    """Reject a blind same-contract retry after a timeout, before any side effect.

    Reads the newest actual RUN record for this delegation. If that run failed
    with a timeout-class code (``timeout`` or ``tool_timeout``) *and* recorded
    the same nonempty ``assignment.contract_digest`` as this launch, the retry is
    refused with ``RunnerError(code="unchanged_timeout_retry")``.

    Explicit ``override`` (the ``--allow-unchanged-timeout-retry`` flag) skips the
    refusal and is recorded in the terminal receipt, so the bypass stays
    visible. Older receipts without a digest are allowed to retry unchanged, a
    non-timeout prior failure never trips the gate, and a delegation with no
    prior run has nothing to compare against. Only the newest actual run counts:
    an old timeout that has already been superseded by a successful run is not
    resurrected.
    """
    if not digest or override:
        return
    prior = latest_prior_run_record(delegation_id, record_kind)
    if prior is None:
        return
    failure = prior.get("failure")
    if not isinstance(failure, dict):
        return
    if failure.get("code") not in ("timeout", "tool_timeout"):
        return
    prior_digest = (prior.get("assignment") or {}).get("contract_digest")
    if not isinstance(prior_digest, str) or not prior_digest or prior_digest != digest:
        return
    raise RunnerError(
        "unchanged retry after a timeout: the latest run for this delegation "
        "timed out with an identical contract digest "
        f"(prior run {prior.get('run_id')}). Inspect the diff and progress "
        "metrics, then narrow the scope, checks, or budget and retry with the "
        "same delegation id, or pass --allow-unchanged-timeout-retry deliberately.",
        code="unchanged_timeout_retry", stage="routing")


def _validated_tool_timeout(value: Any) -> float:
    """Normalize and validate the per-tool bound: finite, non-negative seconds.

    ``0`` is a valid, explicit "disabled" value. Negative, NaN, infinite, and
    non-numeric values are rejected before any side effect runs, so a bad value
    never consumes routing state.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = -1.0
    if isinstance(value, bool) or not math.isfinite(number) or number < 0.0:
        raise RunnerError(
            "--tool-timeout-seconds must be a finite, non-negative number of "
            "seconds (0 disables the per-tool timeout)",
            code="invalid_tool_timeout", stage="routing")
    return number


def validate_launch_inputs(args: argparse.Namespace) -> dict[str, Any]:
    """Validate launch inputs and resolve project overrides without mutating
    routing state, checking the provider, spawning Pi, or writing any record.

    The function performs every check the real launch path runs up to and
    including the sticky-routing decision: required fields, workdir, contract
    UTF-8 decodability, AGENTS.md overrides, profile/thinking resolution,
    transport/intercom arguments, non-mutating routing/profile compatibility,
    and sticky-reservation compatibility against any existing delegation.
    Nothing is written: no records, no routing-state mutation, no random draw,
    and no profile-state side effect.

    Returns a small non-sensitive summary suitable for ``--validate-only`` output
    and populates ``args`` with resolved values so the main path can reuse them.

    The summary intentionally omits the supervisor UUID, the extension path, and
    the contract content so it is safe to print in ``--validate-only`` output.
    On any failure a ``RunnerError`` is raised with the same code/stage that the
    main path would have produced.
    """
    if not args.workdir:
        raise RunnerError("--workdir is required")
    if args.contract is None and not args.contract_file:
        raise RunnerError("--contract or --contract-file is required")
    if not args.tools:
        raise RunnerError("--tools is required")
    if not args.delegation_id:
        raise RunnerError("--delegation-id is required")
    if args.estimated_files is not None and args.estimated_files < 0:
        raise RunnerError("--estimated-files must be non-negative")
    # Optional opaque group label: rejected here, before sticky reservation, the
    # provider preflight, the Pi process, and any ledger/lifecycle write -- in
    # normal mode and under --validate-only alike. Absence (None) is the default
    # and always valid.
    task_group_id = getattr(args, "task_group_id", None)
    if task_group_id is not None and not TASK_GROUP_ID_RE.fullmatch(task_group_id):
        raise RunnerError(
            "--task-group-id must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    # The per-tool bound is a launch input, so it is checked here as well: the
    # dry-run path runs exactly this check and mutates nothing.
    args.tool_timeout_seconds = _validated_tool_timeout(
        getattr(args, "tool_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS))
    # Same for the two automatic-retry bounds: launch inputs, so the dry-run
    # path runs exactly these checks and mutates nothing.
    args.max_auto_retries = _validated_max_auto_retries(
        getattr(args, "max_auto_retries", DEFAULT_MAX_AUTO_RETRIES))
    args.retry_budget_seconds = _validated_retry_budget(
        getattr(args, "retry_budget_seconds", DEFAULT_RETRY_BUDGET_SECONDS))
    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        raise RunnerError(f"not a directory: {workdir}")
    if args.contract_file:
        contract_path = Path(args.contract_file)
        if not contract_path.is_file():
            raise RunnerError(f"contract file not found: {contract_path}")
        # An invalid encoding or unreadable file surfaces here, before any side
        # effect. Nothing from the content is echoed in the validation summary.
        file_text: str | None = read_contract_text(contract_path)
    else:
        file_text = None
    # Launch-text precedence is unchanged from the historical launch path: an
    # inline --contract wins, and the file is only used when there is no inline
    # text. (The CLI still rejects both flags together as mutually exclusive;
    # this keeps programmatic callers on the old order too.)
    args.contract_text = args.contract if args.contract is not None else file_text
    if args.contract_text is None:  # pragma: no cover - guarded above
        raise RunnerError("--contract or --contract-file is required")
    # The contract is read exactly once, here, and this is the very text handed
    # to run_rpc. Digesting it is therefore immune to a validate-then-file-change
    # race: no second read happens between the digest and the launch.
    args.contract_digest = contract_digest(args.contract_text)
    # Deliberate-retry guard: a timeout run followed by a byte-identical retry is
    # refused here, ahead of sticky reservation, provider preflight, the Pi
    # process, and any ledger/lifecycle write. It only reads records, so
    # --validate-only enforces it with no side effects.
    check_unchanged_timeout_retry(
        args.delegation_id, args.contract_digest,
        record_kind=getattr(args, "record_kind", "production"),
        override=bool(getattr(args, "allow_unchanged_timeout_retry", False)))
    overrides = project_overrides(workdir)
    explicit_profile = args.profile
    args.profile = args.profile or overrides.get("profile", "auto")
    args.profile_source = ("auto" if args.profile == "auto" else
                           "explicit" if explicit_profile else "agents" if "profile" in overrides else "auto")
    args.thinking = args.thinking or overrides.get("thinking", "medium")
    if args.profile not in ("auto", *PROFILES):
        # Source-aware, side-effect-free rejection: an explicit CLI profile and
        # an AGENTS.md override get distinct actionable messages, but both are
        # refused here before any reservation or record.
        if args.profile_source == "explicit":
            raise RunnerError(f"invalid --profile: {args.profile}")
        raise RunnerError(f"invalid AGENTS.md Profile: {args.profile}")
    if args.thinking not in THINKING:
        raise RunnerError(f"invalid AGENTS.md Thinking: {args.thinking}")
    if args.routing_class is None:
        args.routing_class = (PROFILES[args.profile]["routing_class"]
                              if args.profile != "auto" else "standard")
    # Unsupported classes are refused here for any caller-supplied or derived
    # value, before any reservation, draw, or record side effect.
    check_routing_class(args.routing_class)
    # Validate the explicit transport arguments before reserving routing state so
    # an invalid opt-in does not silently pin or re-draw a delegation.
    args.intercom_extension, args.supervisor = validate_intercom_args(
        args.transport, args.intercom_extension, args.supervisor)
    # Optional private receipt destination and explicit notification CLI are also
    # validated before any reservation, so a bad path never consumes routing state.
    args.receipt_file_path = validate_receipt_file(getattr(args, "receipt_file", None))
    args.intercom_cli = validate_intercom_cli(args.transport, getattr(args, "intercom_cli", None))
    # Non-mutating routing/profile compatibility: for a fresh delegation with
    # no sticky selection we confirm the resolved profile is enabled and not
    # auto-disabled (for an explicit profile) and matches the requested
    # routing class, or that at least one enabled profile remains in the
    # requested routing class (for --profile auto). When a sticky delegation
    # already exists, validate_sticky_reservation below mirrors
    # reserve_selection exactly -- which means an explicit profile matching a
    # disabled or auto-disabled sticky is still eligible for redraw -- so we
    # skip the first-run explicit check in that case.
    state = read_profile_state()
    existing_sticky = read_previous_selection(args.delegation_id)
    if existing_sticky is None:
        if args.profile != "auto":
            if not PROFILES[args.profile]["enabled"]:
                raise RunnerError(f"profile {args.profile} is currently disabled")
            if profile_auto_disabled(args.profile, state):
                raise RunnerError(
                    f"profile {args.profile} is auto-disabled after a usage limit",
                    code="profile_auto_disabled", stage="routing")
            expected = PROFILES[args.profile]["routing_class"]
            if expected != args.routing_class:
                raise RunnerError(
                    f"profile {args.profile} is {expected}, not {args.routing_class}")
        else:
            if not enabled_profiles(args.routing_class):
                raise RunnerError(
                    f"no enabled {args.routing_class} profile",
                    code="no_enabled_profile", stage="routing")
    # Mirror reserve_selection's sticky-reservation logic against any existing
    # delegation state without writing or randomly drawing. This catches
    # explicit-vs-sticky mismatches, delegations pinned to a removed profile,
    # and the auto-disabled redraw eligibility before any side effect runs.
    validate_sticky_reservation(args.delegation_id, args.profile, args.routing_class)
    return {
        "workdir": str(workdir),
        "routing_class": args.routing_class,
        "profile": args.profile,
        "profile_source": args.profile_source,
        "thinking": args.thinking,
        "transport": args.transport,
        "tools": args.tools,
        "receipt_file": args.receipt_file_path is not None,
        "task_kind": args.task_kind,
        "acceptance_mode": args.acceptance_mode,
        "risk": args.risk,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir")
    contract = parser.add_mutually_exclusive_group()
    contract.add_argument("--contract")
    contract.add_argument("--contract-file")
    parser.add_argument("--show-profile-state", action="store_true")
    parser.add_argument("--reset-profile-state", action="append", default=[])
    # Optional host configuration override. Precedence: this explicit flag >
    # PI_WORKER_PROFILES_FILE > ~/.config/pi-worker/profiles.json. The repo and
    # the project working directory are never consulted automatically.
    parser.add_argument("--profiles-file", default=None,
                        help="Explicit profiles configuration file (JSON). "
                             "Overrides PI_WORKER_PROFILES_FILE and the default "
                             "~/.config/pi-worker/profiles.json. Validated before "
                             "any reservation, provider check, Pi process, or "
                             "ledger write.")
    parser.add_argument("--routing-class", choices=ROUTING_CLASSES)
    # Profile ids are host configuration, so no static argparse choices: an
    # explicit profile is validated against the loaded table in
    # validate_launch_inputs, before any side effect.
    parser.add_argument("--profile", default=None)
    parser.add_argument("--thinking", choices=sorted(THINKING), default=None)
    parser.add_argument("--tools", choices=("read-only", "write"))
    parser.add_argument("--delegation-id")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--tool-timeout-seconds", type=float,
                        default=DEFAULT_TOOL_TIMEOUT_SECONDS,
                        help="Per-running-tool bound in seconds, enforced from a tool's "
                             "start to its matching end (default: %(default)s). Pass 0 to "
                             "disable it. It never resets on a partial tool update, "
                             "tracks concurrent tools independently, and is independent of "
                             "the global --timeout. A tool that exceeds it ends the run "
                             "with a sanitized tool_timeout failure.")
    parser.add_argument("--max-auto-retries", type=int,
                        default=DEFAULT_MAX_AUTO_RETRIES,
                        help="Maximum automatic agent-turn retries for the whole run "
                             "(default: %(default)s). The (count + 1)-th "
                             "auto_retry_start ends the run with a sanitized "
                             "retry_limit failure; pass 0 to reject every "
                             "automatic retry. Compaction retries are not counted, "
                             "and the bound is independent of --retry-budget-seconds "
                             "and --timeout.")
    parser.add_argument("--retry-budget-seconds", type=float,
                        default=DEFAULT_RETRY_BUDGET_SECONDS,
                        help="Aggregate budget in seconds for automatic agent-turn "
                             "retries (default: %(default)s). Counted from the "
                             "observed auto_retry_start/auto_retry_end spans, "
                             "in-flight retries included, so overlapping retries "
                             "can exceed wall-clock time; a silent or still-"
                             "streaming retry cannot reset it. Reaching it ends the "
                             "run with a sanitized retry_budget_exceeded failure. "
                             "Pass 0 to disable the time bound only: the "
                             "--max-auto-retries count still applies.")
    parser.add_argument("--pi", default=os.environ.get("PI_WORKER_PI", "pi"))
    parser.add_argument("--task-id")
    parser.add_argument("--assignment-goal", default="Bounded Pi worker assignment")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--acceptance-check", action="append", default=[])
    parser.add_argument("--task-kind",
                        choices=("investigation", "implementation", "test", "docs",
                                 "config", "refactor", "other"),
                        default="other",
                        help="Coarse task class recorded in routing features (never "
                             "used for routing decisions). Canonical values: "
                             "investigation, implementation, test, docs, config, "
                             "refactor, other. Common intent mappings: review and "
                             "root-cause debugging -> investigation; "
                             "implementation debugging that edits files -> "
                             "implementation. Invalid aliases such as 'review' or "
                             "'debugging' are rejected.")
    parser.add_argument("--estimated-files", type=int, default=None)
    parser.add_argument("--risk", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--acceptance-mode",
                        choices=("deterministic", "mixed", "manual"),
                        default="deterministic",
                        help="How acceptance is decided for this run. Canonical "
                             "values: deterministic, mixed, manual. Common intent "
                             "mappings: deterministic checks combined with human "
                             "judgment -> mixed; mostly human acceptance -> "
                             "manual. Invalid aliases such as 'judgment' or "
                             "'judgmental' are rejected.")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate the launch inputs and resolve project "
                             "overrides without reserving routing state, checking "
                             "the provider, starting Pi, or writing a run/review "
                             "record. On success prints a small JSON validation "
                             "result (no supervisor UUID, extension path, or "
                             "contract content) and exits 0. Use this to dry-run "
                             "inputs before reserving a delegation.")
    parser.add_argument("--verification", action="append", default=[])
    parser.add_argument("--review-status", choices=("accepted", "needs_rework", "rejected", "pending"), default="pending")
    parser.add_argument("--main-rework", choices=("none", "minor", "major", "rewrite"), default="none")

    parser.add_argument("--allow-unchanged-timeout-retry", action="store_true",
                        help="Deliberately allow retrying this delegation with a "
                             "byte-identical contract after the previous run "
                             "timed out. Without it the runner refuses that retry "
                             "(code unchanged_timeout_retry, stage routing) before "
                             "reserving routing, checking the provider, starting "
                             "Pi, or writing any record -- --validate-only included. "
                             "Prefer narrowing the contract scope, checks, or "
                             "budget and retrying with the same delegation id; pass "
                             "this flag only with a stated reason. It changes no "
                             "model or profile and does not change sticky identity.")
    # Opt-in Pi-main intercom transport. Explicit-only: the defaults are unchanged,
    # the harness is never auto-detected, and these flags validate before any routing
    # reservation so a bad opt-in cannot silently mutate routing state.
    parser.add_argument("--transport", choices=("rpc", "intercom"), default="rpc")
    parser.add_argument("--intercom-extension", default=None,
                        help="Absolute path to a trusted .ts/.js Pi extension that "
                             "exposes the `intercom` command; required with "
                             "--transport intercom.")
    parser.add_argument("--supervisor", default=None,
                        help="Full UUID of the assigned supervisor; required with "
                             "--transport intercom and never persisted in receipts.")
    parser.add_argument("--receipt-file", default=None,
                        help="Optional absolute path for the terminal run receipt. "
                             "All terminal runs (success, failure, timeout, "
                             "interrupt) atomically write the same private UTF-8 "
                             "JSON object there with mode 0600; the append-only "
                             "ledger remains authoritative.")
    parser.add_argument("--intercom-cli", default=None,
                        help="Optional absolute .mjs/.js path for the installed "
                             "intercom CLI used for the best-effort RUN_FINISHED "
                             "notification. Defaults to cli.mjs next to "
                             "--intercom-extension when present.")
    parser.add_argument("--record-kind", choices=RECORD_KINDS, default="production",
                        help="Tag for the run record: production (default) or test. "
                             "The review report excludes test records unless "
                             "--include-tests is passed.")
    parser.add_argument("--task-group-id", default=None,
                        help="Optional opaque caller-supplied group label "
                             "([A-Za-z0-9][A-Za-z0-9_-]{0,63}) recorded in the "
                             "assignment block only when supplied. Purely a coarse "
                             "correlation id: it never changes the delegation pin, "
                             "the contract-digest gate, routing, or the model, and "
                             "no id is inferred or auto-generated. A group may span "
                             "different delegation ids and phases.")
    return parser.parse_args(argv)


def _install_sigterm_handler() -> Any:
    """Best-effort SIGTERM handling so a terminated runner can finalize a receipt.

    Returns the previous handler for restoration, or ``None`` when signals are
    unavailable (for example when ``main`` is invoked off the main thread). A
    SIGKILL cannot be caught; reconciliation surfaces those runs as stale.
    """
    def _raise(signum: int, frame: Any) -> None:
        raise RunnerTerminated()

    try:
        return signal.signal(signal.SIGTERM, _raise)
    except (ValueError, OSError, AttributeError):
        return None


def main(argv: list[str] | None = None) -> int:
    old_umask = os.umask(0o077)
    previous_sigterm = _install_sigterm_handler()
    try:
        args = parse_args(argv)
        # Strict configuration load first: precedence explicit > env > default,
        # validated before the show/reset helpers, reservation, provider checks,
        # Pi, or ledger writes. An absent env/default file is simply an empty
        # table; an explicit path must exist and validate.
        configure_profiles(getattr(args, "profiles_file", None))
        if args.show_profile_state or args.reset_profile_state:
            for name in args.reset_profile_state:
                if name not in PROFILES:
                    raise RunnerError(f"unknown profile: {name}")
                reset_profile_disabled(name)
            print(json.dumps(profile_state_snapshot(), ensure_ascii=False, sort_keys=True))
            return 0
        summary = validate_launch_inputs(args)
        if args.validate_only:
            print(json.dumps({"status": "valid", "validated": summary},
                             ensure_ascii=False, sort_keys=True))
            return 0
        workdir = Path(summary["workdir"])
        profile, draw, policy = reserve_selection(
            args.delegation_id, args.profile, args.routing_class)
        receipt = base_record(args, profile, draw, policy)
        run_id = receipt["run_id"]
        # Lifecycle evidence: written once the receipt identity is reserved and
        # before preflight, so a killed runner still leaves a start record with a
        # deadline. The deadline starts now and therefore includes preflight.
        bounded_timeout = max(0.0, float(args.timeout))
        deadline_monotonic = time.monotonic() + bounded_timeout
        deadline_at = (dt.datetime.now(dt.timezone.utc)
                       + dt.timedelta(seconds=bounded_timeout)
                       ).isoformat().replace("+00:00", "Z")
        append_record({
            "schema_version": 3, "record_type": LIFECYCLE_RECORD_TYPE,
            "phase": "started", "event": "run_started",
            "run_id": run_id, "delegation_id": args.delegation_id,
            "record_kind": args.record_kind, "worker_type": profile,
            "transport": args.transport, "created_at": utc_now(),
            "deadline": deadline_at, "deadline_scope": "preflight_included",
        })
        progress: dict[str, Any] = {"event_counts": {}, "session_id": None,
                                    "last_event": None, "event_count": 0}
        checkpoint_window = {"last": 0.0}

        def checkpoint(state: dict[str, Any]) -> None:
            now = time.monotonic()
            if now - checkpoint_window["last"] < CHECKPOINT_INTERVAL_SECONDS:
                return
            checkpoint_window["last"] = now
            write_run_checkpoint(run_id, args.delegation_id, state)

        stats: dict[str, Any] | None = None
        error: BaseException | None = None
        rpc_started: float | None = None
        try:
            config = PROFILES[profile]
            provider, model = config["provider"], config["model"]
            env = os.environ.copy()  # Pi default auth inherited unchanged
            check_provider(args.pi, provider, model, env, deadline=deadline_monotonic)
            text = args.contract_text  # validated and digested in validate_launch_inputs
            rpc_started = time.monotonic()
            remaining = deadline_monotonic - rpc_started
            if remaining <= 0:
                raise TimeoutError("Pi worker timed out")
            stats, _events = run_rpc(
                args.pi, workdir, text, profile, args.thinking, args.tools,
                remaining, env, args.delegation_id,
                intercom_extension=args.intercom_extension,
                supervisor_id=args.supervisor,
                progress=progress, checkpoint=checkpoint,
                tool_timeout_seconds=args.tool_timeout_seconds,
                max_auto_retries=args.max_auto_retries,
                retry_budget_seconds=args.retry_budget_seconds,
            )
        except (Exception, KeyboardInterrupt, RunnerTerminated) as exc:
            error = exc

        receipt["finished_at"] = utc_now()
        if error is None:
            receipt["worker"]["session_id"] = (
                (stats.get("sessionId") if stats else None) or progress.get("session_id"))
            receipt["outcome"].update({"status": "completed", "result": "partial",
                                       "summary": "Pi worker settled; main-agent review remains authoritative"})
            receipt["usage"] = usage_from_stats(stats)
            if progress["event_counts"]:
                receipt["rpc_event_counts"] = dict(sorted(progress["event_counts"].items()))
        else:
            terminated = isinstance(error, (KeyboardInterrupt, RunnerTerminated))
            status = ("interrupted" if terminated else "timed_out"
                      if isinstance(error, TimeoutError) else "failed")
            receipt["outcome"].update({
                "status": "interrupted" if terminated or status == "timed_out" else "failed",
                "result": "unusable", "summary": "Pi worker did not deliver"})
            if status == "timed_out":
                category, code, stage = "timeout", "timeout", "rpc"
            elif status == "interrupted":
                category, code, stage = "interrupted", "interrupted", "runner"
            elif isinstance(error, RunnerError):
                category, code, stage = "preflight_or_runtime", error.code, error.stage
            else:
                category, code, stage = "preflight_or_runtime", "unexpected_exception", "runner"
            if code == "provider_usage_limit" and profile in PROFILES:
                entry = auto_disable_profile(profile, run_id=run_id)
                receipt["routing"]["profile_auto_disabled"] = {
                    "reason": entry["reason"], "disabled_at": entry["disabled_at"],
                    "expires_at": entry["expires_at"]}
            receipt["failure"] = {"category": category, "code": code, "stage": stage,
                                  "evidence": type(error).__name__}
            # Preserve useful accumulated evidence from a failed worker: coarse
            # event counts, session id when known, and elapsed worker time.
            if progress.get("session_id"):
                receipt["worker"]["session_id"] = progress["session_id"]
            if progress["event_counts"]:
                receipt["rpc_event_counts"] = dict(sorted(progress["event_counts"].items()))
        if rpc_started is not None and receipt["timing"].get("worker_seconds") is None:
            receipt["timing"]["worker_seconds"] = time.monotonic() - rpc_started
        # Optional phase telemetry, identical shape for success and failure.
        # Absent when the run never reached the prompt (older receipts omit it).
        observed_metrics = progress.get("progress_metrics")
        if isinstance(observed_metrics, dict):
            receipt["progress_metrics"] = dict(observed_metrics)

        ledger_written = True
        ledger_error: Exception | None = None
        try:
            append_record(receipt)
        except Exception as exc:  # pragma: no cover - ledger failure is exceptional
            ledger_written = False
            ledger_error = exc
            print(f"pi-worker: terminal ledger not written: {type(exc).__name__}",
                  file=sys.stderr)
        review_written = True
        if error is None and args.review_status != "pending":
            try:
                append_record(legacy_review_event(args, run_id))
            except Exception as exc:
                # An explicit review verdict is audit evidence: never swallow its
                # append failure and report success.
                review_written = False
                print(f"pi-worker: review event not written: {type(exc).__name__}",
                      file=sys.stderr)

        receipt_required = args.receipt_file_path is not None
        receipt_written = True
        if receipt_required:
            try:
                write_receipt_file(args.receipt_file_path, receipt)
            except Exception as exc:
                receipt_written = False
                print(f"pi-worker: receipt file not written: {type(exc).__name__}",
                      file=sys.stderr)

        # Best-effort terminal notification, after the ledger/receipt attempts.
        # It carries the terminal-evidence flags so an unconfirmed write stays
        # visible; it never changes execution status. Failures are ignored.
        try:
            send_run_finished_notification(
                transport=args.transport, receipt=receipt,
                supervisor_id=args.supervisor, extension=args.intercom_extension,
                explicit_cli=args.intercom_cli, record_kind=args.record_kind,
                ledger_written=ledger_written, receipt_written=receipt_written,
                receipt_required=receipt_required)
        except Exception:
            pass
        # Keep the checkpoint when the terminal ledger write failed so
        # reconciliation still sees an unconfirmed start; only a durable terminal
        # record removes it.
        if ledger_written:
            remove_run_checkpoint(run_id)

        if error is None:
            if not ledger_written or not review_written or (
                    receipt_required and not receipt_written):
                print("pi-worker: run settled but terminal evidence was not fully written",
                      file=sys.stderr)
                return 1
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return 0
        # Preserve the legacy stderr failure receipt and exit codes.
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        if isinstance(error, RunnerTerminated):
            return 143
        if isinstance(error, KeyboardInterrupt):
            return 130
        if isinstance(error, TimeoutError):
            return 124
        return 1
    except (Exception, KeyboardInterrupt, RunnerTerminated) as exc:
        # Catch Exception (RunnerError, TimeoutError, ordinary exceptions)
        # together with KeyboardInterrupt explicitly. SystemExit and
        # GeneratorExit are intentionally NOT caught: argparse's SystemExit
        # (--help exits 0; invalid choices exit 2) must propagate with its
        # own exit code, and GeneratorExit is never expected in this CLI
        # entry point so it should propagate rather than be turned into a
        # generic failure exit. TimeoutError still maps to 124 below.
        if isinstance(exc, RunnerTerminated):
            print("pi-worker: terminated", file=sys.stderr)
            return 143
        print(f"pi-worker: {exc}", file=sys.stderr)
        if isinstance(exc, KeyboardInterrupt):
            return 130
        if isinstance(exc, TimeoutError):
            return 124
        return 1
    finally:
        os.umask(old_umask)
        if previous_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, previous_sigterm)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
