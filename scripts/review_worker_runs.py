#!/usr/bin/env python3
"""Read-only materialization, filtering, and summary of worker records."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import datetime as dt
from decimal import Decimal
import json
import math
import os
from pathlib import Path
from typing import Any

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
LARGEST_RUN_METRICS = (
    ("cost_usd", ("usage", "cost_usd")),
    ("total_tokens", ("usage", "total_tokens")),
    ("worker_seconds", ("timing", "worker_seconds")),
)
# Optional top-level progress_metrics schema (flat). Older receipts may omit the
# block entirely or leave individual fields null; absent/null is unobserved and
# is never treated as zero. Durations are observed relative to the prompt.
PROGRESS_METRIC_NUMERIC_FIELDS = (
    "first_tool_seconds",
    "first_edit_seconds",
    "first_test_candidate_seconds",
    "last_activity_seconds",
    "last_tool_completion_seconds",
    "tool_seconds",
    "retry_seconds",
    "retry_count",
)
# Stage durations get a median; tool_seconds and retry_seconds are overlapping
# aggregates, so only their observation counts and totals are reported.
PROGRESS_STAGE_FIELDS = (
    "first_tool_seconds",
    "first_edit_seconds",
    "first_test_candidate_seconds",
    "last_activity_seconds",
    "last_tool_completion_seconds",
)
PROGRESS_CHECKPOINT_CODES = (
    "investigation_checkpoint",
    "first_delivery_checkpoint",
    "wrap_up_checkpoint",
)
# Optional top-level `main_metrics` on a review event. The block is emitted only
# when the main supplied at least one value; the six keys are fixed and any
# single value may be null. Every value measures THAT review event only: it is
# never cumulative and never inherited from an earlier review.
MAIN_COST_KEYS = (
    "briefing_count",
    "review_count",
    "recovery_count",
    "diff_bytes",
    "input_tokens",
    "output_tokens",
)
TIMEOUT_FAILURE_CODES = (
    "timeout",
    "tool_timeout",
    "retry_limit",
    "retry_budget_exceeded",
)
TERMINAL_REVIEW = frozenset({"accepted", "needs_rework", "rejected"})
# New append-only event kinds. They are recognized (so they never inflate
# unknown-record diagnostics) and are attached to their run during
# materialization, but they are not themselves run metrics.
LIFECYCLE_RECORD_TYPE = "lifecycle"
NOTIFICATION_RECORD_TYPE = "notification"

def _is_suspected_fixture(item: dict[str, Any]) -> bool:
    """Narrow fixture detection: only the exact legacy fake session id."""
    worker = item.get("worker")
    return isinstance(worker, dict) and worker.get("session_id") == "fake-session"


def _load_record_dir():
    """The runner's resolver, which owns the per-harness default.

    Imported normally when this script runs from its own directory; loaded by path when a
    test imports this file directly, so both paths share one implementation.
    """
    try:
        from run_pi_worker import record_dir  # noqa: PLC0415
    except ModuleNotFoundError:
        import importlib.util  # noqa: PLC0415

        runner_path = Path(__file__).resolve().parent / "run_pi_worker.py"
        spec = importlib.util.spec_from_file_location("run_pi_worker", runner_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        record_dir = module.record_dir
    return record_dir


record_dir = _load_record_dir()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-dir", default=None, help="defaults to the resolver in run_pi_worker")
    parser.add_argument("--days", type=int)
    parser.add_argument("--since", type=dt.date.fromisoformat)
    parser.add_argument("--until", type=dt.date.fromisoformat)
    parser.add_argument("--worker", action="append", default=[])
    parser.add_argument("--status", action="append", default=[])
    parser.add_argument("--project")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reconcile", action="store_true",
                        help="Read-only lifecycle reconciliation: report stale "
                             "started runs without a terminal receipt, completed "
                             "runs pending review, failed/interrupted runs without "
                             "closure review, and missing/failed terminal "
                             "notifications for intercom runs. Never modifies the "
                             "ledger.")
    parser.add_argument("--include-tests", action="store_true",
                        help="Include --record-kind test records (excluded from the "
                             "production report by default).")
    return parser.parse_args()


def _bounds(days: int | None, since: dt.date | None, until: dt.date | None):
    if days is not None:
        if days < 1:
            raise ValueError("--days must be positive")
        since = max(filter(None, (since, dt.date.today() - dt.timedelta(days=days - 1))), default=None)
    return since, until


def _valid_run(item: dict[str, Any]) -> bool:
    return isinstance(item.get("run_id"), str) and isinstance(item.get("worker"), dict) and isinstance(item.get("outcome"), dict)


def _parse_records(directory: Path):
    """Parse every record file into runs, reviews, lifecycle and notifications.

    Runs stay keyed by run_id with their partition date; reviews are order-
    preserved; lifecycle and notification events are grouped by run_id. New
    event kinds are recognized here so they never inflate unknown-records.
    """
    runs: dict[str, tuple[dt.date, dict[str, Any]]] = {}
    reviews: list[dict[str, Any]] = []
    lifecycle: dict[str, list[tuple[dt.date, dict[str, Any]]]] = defaultdict(list)
    notifications: dict[str, list[tuple[dt.date, dict[str, Any]]]] = defaultdict(list)
    diagnostics = Counter()
    for path in sorted(directory.glob("????-??-??.jsonl")) if directory.exists() else []:
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: malformed JSON: {exc.msg}") from exc
                if not isinstance(item, dict):
                    diagnostics["invalid_records"] += 1
                    continue
                version, kind = item.get("schema_version"), item.get("record_type")
                if version == 2 or (version == 3 and kind == "run"):
                    if _valid_run(item) and (version == 2 or item.get("phase") == "completed"):
                        runs[item["run_id"]] = (day, item)
                    else:
                        diagnostics["invalid_records"] += 1
                elif version == 3 and kind == "review":
                    if (item.get("phase") == "reviewed"
                            and isinstance(item.get("review_id"), str)
                            and isinstance(item.get("target_run_id"), str)
                            and isinstance(item.get("outcome"), dict)):
                        reviews.append(item)
                    else:
                        diagnostics["invalid_records"] += 1
                elif version == 3 and kind == LIFECYCLE_RECORD_TYPE:
                    if (item.get("phase") == "started" and item.get("event") == "run_started"
                            and isinstance(item.get("run_id"), str)):
                        lifecycle[item["run_id"]].append((day, item))
                    else:
                        diagnostics["invalid_records"] += 1
                elif version == 3 and kind == NOTIFICATION_RECORD_TYPE:
                    if (item.get("phase") == "finished" and item.get("event") == "run_finished"
                            and isinstance(item.get("run_id"), str)):
                        notifications[item["run_id"]].append((day, item))
                    else:
                        diagnostics["invalid_records"] += 1
                else:
                    diagnostics["unknown_records"] += 1
    return runs, reviews, lifecycle, notifications, diagnostics


def _is_valid_main_cost(value: Any) -> bool:
    """Accept a non-negative whole-number observation, rejecting the rest.

    Booleans, negative, fractional, NaN, infinite, and non-numeric values are
    unobserved, never zero. ``5.0`` is accepted because JSON does not preserve
    an integer/float distinction; ``5.5`` is not an integer count.
    """
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return False
        return float(value).is_integer()
    return False


def _accumulate_main_cost(target: dict[str, Any], review: dict[str, Any]) -> None:
    """Add one review event's measured main cost to its run.

    Genuine measured cost is kept even when the review is later superseded: the
    main really spent that cost, so history is never replaced by the final
    verdict. A repeated ``review_id`` for the same run counts once.
    """
    block = target.get("main_cost")
    if not isinstance(block, dict):
        block = {"review_event_count": 0, "review_ids": set(),
                 "observations": Counter(), "totals": Counter()}
        target["main_cost"] = block
    review_id = review["review_id"]
    if review_id in block["review_ids"]:
        # The same review event recorded twice is one real cost event.
        return
    block["review_ids"].add(review_id)
    block["review_event_count"] += 1
    metrics = review.get("main_metrics")
    if not isinstance(metrics, dict):
        return
    for key in MAIN_COST_KEYS:
        value = metrics.get(key)
        if _is_valid_main_cost(value):
            block["observations"][key] += 1
            block["totals"][key] += int(value)


def _finalize_main_cost(block: dict[str, Any]) -> dict[str, Any]:
    """Drop the internal dedup set and keep fixed keys with null unobserved totals."""
    observations = block["observations"]
    return {
        "review_event_count": block["review_event_count"],
        "observations": {key: observations[key] for key in MAIN_COST_KEYS},
        "totals": {key: (block["totals"][key] if observations[key] else None)
                   for key in MAIN_COST_KEYS},
    }


def _materialize(runs, reviews, lifecycle, notifications, excluded):
    """Apply the latest review and attach lifecycle/notification evidence."""
    materialized = {key: copy.deepcopy(value[1]) for key, value in runs.items()
                    if key not in excluded}
    reviewed: set[str] = set()
    orphan_reviews = 0
    for review in reviews:
        target_id = review["target_run_id"]
        if target_id in excluded:
            continue
        target = materialized.get(target_id)
        if target is None:
            orphan_reviews += 1
            continue
        reviewed.add(target_id)
        _accumulate_main_cost(target, review)
        target["outcome"].update(review["outcome"])
        target["verification"] = copy.deepcopy(review.get("verification") or [])
        for key, value in (review.get("timing") or {}).items():
            if value is not None:
                target.setdefault("timing", {})[key] = value
        if review.get("routing_lesson") is not None:
            target["routing_lesson"] = review["routing_lesson"]
        target["review"] = {"review_id": review["review_id"],
                            "reviewed_at": review.get("created_at")}
    for run_id, run in materialized.items():
        block = run.get("main_cost")
        if isinstance(block, dict):
            run["main_cost"] = _finalize_main_cost(block)
    for run_id, entries in lifecycle.items():
        if run_id not in materialized or not entries:
            continue
        started = entries[0][1]
        materialized[run_id]["lifecycle"] = {
            "event": "run_started",
            "started_at": started.get("created_at"),
            "deadline": started.get("deadline"),
            "deadline_scope": started.get("deadline_scope"),
            "record_kind": started.get("record_kind"),
            "worker_type": started.get("worker_type"),
            "transport": started.get("transport"),
        }
    for run_id, entries in notifications.items():
        if run_id not in materialized or not entries:
            continue
        latest = entries[-1][1]
        materialized[run_id]["notification"] = {
            "event": "run_finished",
            "delivery": latest.get("delivery"),
            "delivered": latest.get("delivered"),
            "attempts": latest.get("attempts"),
            "status": latest.get("status"),
            "created_at": latest.get("created_at"),
        }
    return materialized, reviewed, orphan_reviews


def _exclusions(runs: dict[str, tuple[dt.date, dict[str, Any]]],
                include_tests: bool) -> tuple[set[str], dict[str, int]]:
    """Compute default exclusions and their diagnostics.

    ``record_kind: test`` and the narrowly detected ``fake-session`` fixtures are
    both excluded from the default production report and reconciliation; passing
    ``--include-tests`` restores both. The two counts are always reported so
    filtered evidence stays visible even when it is excluded.
    """
    test_ids = {run_id for run_id, (_, item) in runs.items()
                if item.get("record_kind") == "test"}
    fixture_ids = {run_id for run_id, (_, item) in runs.items()
                   if _is_suspected_fixture(item)}
    counts = {"test_records_excluded": len(test_ids),
              "suspected_fixtures": len(fixture_ids)}
    if include_tests:
        return set(), counts
    return test_ids | fixture_ids, counts


def load_with_diagnostics(directory: Path, days: int | None = None, since: dt.date | None = None,
                          until: dt.date | None = None,
                          include_tests: bool = False) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Select runs by invocation date and apply each run's latest review from any date."""
    since, until = _bounds(days, since, until)
    runs, reviews, lifecycle, notifications, diagnostics = _parse_records(directory)
    excluded, exclusion_counts = _exclusions(runs, include_tests)
    diagnostics.update(exclusion_counts)
    materialized, _reviewed, orphan_reviews = _materialize(
        runs, reviews, lifecycle, notifications, excluded)
    diagnostics["orphan_reviews"] += orphan_reviews
    selected = []
    for run_id, (day, _) in runs.items():
        if run_id not in materialized:
            continue
        if not ((since and day < since) or (until and day > until)):
            selected.append(materialized[run_id])
    return selected, dict(diagnostics)


def load(directory: Path, days: int | None = None, since: dt.date | None = None,
         until: dt.date | None = None, include_tests: bool = False) -> list[dict[str, Any]]:
    return load_with_diagnostics(directory, days, since, until, include_tests)[0]


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _age_seconds(value: Any, now: dt.datetime) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _read_checkpoint(directory: Path, run_id: str) -> dict[str, Any] | None:
    path = directory / ".checkpoints" / f"{run_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reconcile(directory: Path, days: int | None = None, since: dt.date | None = None,
              until: dt.date | None = None, include_tests: bool = False,
              now: dt.datetime | None = None) -> dict[str, Any]:
    """Read-only lifecycle reconciliation over the append-only ledger.

    Reports started runs past their recorded deadline without a terminal receipt
    as stale/unconfirmed, completed runs still pending review, failed or
    interrupted runs without closure review, and missing/failed terminal
    notifications for intercom runs that carry new lifecycle markers. It never
    rewrites a record, never relabels a failed run as completed or rejected, and
    never treats a legacy run that predates lifecycle markers as a known
    notification delivery failure.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since, until = _bounds(days, since, until)
    runs, reviews, lifecycle, notifications, diagnostics = _parse_records(directory)
    excluded, exclusion_counts = _exclusions(runs, include_tests)
    diagnostics.update(exclusion_counts)
    materialized, reviewed_ids, orphan_reviews = _materialize(
        runs, reviews, lifecycle, notifications, excluded)
    diagnostics["orphan_reviews"] += orphan_reviews

    stale: list[dict[str, Any]] = []
    pending_review: list[dict[str, Any]] = []
    failed_without_closure: list[dict[str, Any]] = []
    notification_gaps: list[dict[str, Any]] = []
    considered = 0
    for run_id, (day, _) in runs.items():
        run = materialized.get(run_id)
        if run is None or (since and day < since) or (until and day > until):
            continue
        considered += 1
        worker = run.get("worker") or {}
        outcome = run.get("outcome") or {}
        status = outcome.get("status")
        lifecycle_info = run.get("lifecycle") or {}
        if status == "completed" and _effective_review_verdict(run) not in TERMINAL_REVIEW:
            pending_review.append({
                "run_id": run_id, "delegation_id": run.get("delegation_id"),
                "worker_type": worker.get("worker_type"),
                "finished_at": run.get("finished_at"),
                "age_seconds": _age_seconds(run.get("finished_at"), now),
            })
        if status in ("failed", "interrupted") and run_id not in reviewed_ids:
            failed_without_closure.append({
                "run_id": run_id, "delegation_id": run.get("delegation_id"),
                "worker_type": worker.get("worker_type"), "status": status,
                "finished_at": run.get("finished_at"),
                "age_seconds": _age_seconds(run.get("finished_at"), now),
            })
        if worker.get("transport") == "intercom" and lifecycle_info:
            notification = run.get("notification")
            if notification is None:
                notification_gaps.append({
                    "run_id": run_id, "delegation_id": run.get("delegation_id"),
                    "status": status, "delivery": "missing", "attempts": None,
                })
            elif notification.get("delivered") is not True:
                notification_gaps.append({
                    "run_id": run_id, "delegation_id": run.get("delegation_id"),
                    "status": status, "delivery": notification.get("delivery"),
                    "attempts": notification.get("attempts"),
                })

    for run_id, entries in lifecycle.items():
        if run_id in runs or not entries:
            continue
        started = entries[0][1]
        if not include_tests and started.get("record_kind") == "test":
            continue
        day = entries[0][0]
        if (since and day < since) or (until and day > until):
            continue
        started = entries[0][1]
        deadline = _parse_timestamp(started.get("deadline"))
        if deadline is None or now <= deadline:
            continue
        checkpoint = _read_checkpoint(directory, run_id) or {}
        stale.append({
            "run_id": run_id, "delegation_id": started.get("delegation_id"),
            "worker_type": started.get("worker_type"),
            "started_at": started.get("created_at"),
            "deadline": started.get("deadline"),
            "age_seconds": _age_seconds(started.get("created_at"), now),
            "last_activity_at": checkpoint.get("updated_at"),
        })

    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "runs_considered": considered,
        "diagnostics": dict(diagnostics),
        "stale_unconfirmed": stale,
        "completed_pending_review": pending_review,
        "failed_without_closure": failed_without_closure,
        "notification_gaps": notification_gaps,
        "suspected_fixtures": exclusion_counts["suspected_fixtures"],
    }


def filtered(records: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    output = []
    for item in records:
        worker, outcome, project = item.get("worker") or {}, item.get("outcome") or {}, item.get("project") or {}
        if args.worker and worker.get("worker_type") not in set(args.worker):
            continue
        if args.status and outcome.get("status") not in set(args.status):
            continue
        if args.project and project.get("id") != args.project:
            continue
        output.append(item)
    return output


def _metadata_incomplete(item: dict[str, Any]) -> bool:
    assignment = item.get("assignment") or {}
    quality = assignment.get("metadata_quality")
    if isinstance(quality, dict):
        return quality.get("complete") is not True
    features = assignment.get("features") or {}
    return (assignment.get("goal") in (None, "", "Bounded Pi worker assignment") or not assignment.get("scope")
            or (features.get("acceptance_mode") == "deterministic" and not assignment.get("acceptance_checks")))


def _delegation_key(run: dict[str, Any]) -> str:
    """A run without a usable delegation_id is its own delegation, keyed by run_id."""
    delegation_id = run.get("delegation_id")
    if isinstance(delegation_id, str) and delegation_id:
        return delegation_id
    return f"_run:{run.get('run_id') or ''}"


def _effective_review_verdict(run: dict[str, Any]) -> str:
    outcome = run.get("outcome") or {}
    verdict = outcome.get("review_verdict")
    return verdict if isinstance(verdict, str) and verdict else "pending"


def _delegation_final_state(last_run: dict[str, Any]) -> str:
    """The last run's status drives the final delegation state."""
    outcome = last_run.get("outcome") or {}
    status = outcome.get("status")
    if status == "completed":
        verdict = _effective_review_verdict(last_run)
        return verdict if verdict in TERMINAL_REVIEW else "pending_review"
    if status == "interrupted":
        return "interrupted"
    return "failed"


def _summarize_delegations(records: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in records:
        grouped.setdefault(_delegation_key(run), []).append(run)
    counts: Counter[str] = Counter()
    first_pass_accepted = 0
    for runs in grouped.values():
        counts[_delegation_final_state(runs[-1])] += 1
        first = runs[0]
        if ((first.get("outcome") or {}).get("status") == "completed"
                and _effective_review_verdict(first) == "accepted"):
            first_pass_accepted += 1
    total = sum(counts.values())
    accepted = counts.get("accepted", 0)
    return {
        "total": total,
        "accepted": accepted,
        "needs_rework": counts.get("needs_rework", 0),
        "rejected": counts.get("rejected", 0),
        "pending_review": counts.get("pending_review", 0),
        "failed": counts.get("failed", 0),
        "interrupted": counts.get("interrupted", 0),
        "first_pass_accepted": first_pass_accepted,
        "final_acceptance_rate": accepted / total if total else None,
        "first_pass_acceptance_rate": first_pass_accepted / total if total else None,
    }


def _is_valid_observation(value: Any) -> bool:
    """Accept non-negative numeric observations, rejecting bool/NaN/infinity."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return False
        return value >= 0
    return False


def _largest_run_descriptor(records: list[dict[str, Any]], *path: str) -> dict[str, Any] | None:
    """Pick the first run (input order) with the largest valid observation."""
    best_run: dict[str, Any] | None = None
    best_value: float | int | None = None
    for run in records:
        value: Any = run
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if not _is_valid_observation(value):
            continue
        if best_run is None or value > best_value:
            best_run = run
            best_value = value
    if best_run is None:
        return None
    return {
        "run_id": best_run.get("run_id"),
        "delegation_id": best_run.get("delegation_id"),
        "worker_type": (best_run.get("worker") or {}).get("worker_type"),
        path[-1]: best_value,
    }


def _summarize_largest_runs(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {name: _largest_run_descriptor(records, *path) for name, path in LARGEST_RUN_METRICS}


def _median(values: list[float]) -> float | None:
    """Median of non-negative observations; None when nothing was observed."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _summarize_progress_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate optional top-level ``progress_metrics`` per worker.

    Read-only and strictly present-data: a missing block, a missing field, or a
    null/invalid observation contributes nothing and is never rewritten to 0.
    Only fixed checkpoint and timeout-code vocabularies are counted, so reported
    keys stay bounded and no raw contract, command, path, or error text leaks in.
    """
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "runs_with_metrics": 0,
        "observed": defaultdict(list),
        "checkpoints": Counter(),
        "timeouts": Counter(),
    })
    for run in records:
        worker = run.get("worker") or {}
        name = str(worker.get("worker_type", "unknown"))
        bucket = grouped[name]
        metrics = run.get("progress_metrics")
        if isinstance(metrics, dict):
            bucket["runs_with_metrics"] += 1
            for field in PROGRESS_METRIC_NUMERIC_FIELDS:
                value = metrics.get(field)
                if _is_valid_observation(value):
                    bucket["observed"][field].append(value)
            notices = metrics.get("checkpoint_notices")
            if isinstance(notices, list):
                for code in notices:
                    if code in PROGRESS_CHECKPOINT_CODES:
                        bucket["checkpoints"][code] += 1
        failure = run.get("failure") or {}
        failure_code = failure.get("code")
        if isinstance(failure_code, str) and failure_code in TIMEOUT_FAILURE_CODES:
            bucket["timeouts"][failure_code] += 1

    output: dict[str, Any] = {}
    for name, bucket in grouped.items():
        observed = bucket["observed"]
        if not bucket["runs_with_metrics"] and not bucket["timeouts"]:
            continue
        retry_counts = observed["retry_count"]
        retry_seconds = observed["retry_seconds"]
        output[name] = {
            "runs_with_metrics": bucket["runs_with_metrics"],
            "observations": {field: len(observed[field]) for field in PROGRESS_METRIC_NUMERIC_FIELDS},
            "medians": {field: _median(observed[field]) for field in PROGRESS_STAGE_FIELDS},
            "retry_total_count": sum(retry_counts) if retry_counts else None,
            "retry_total_seconds": float(sum(retry_seconds)) if retry_seconds else None,
            "checkpoint_counts": dict(sorted(bucket["checkpoints"].items())),
            "timeout_causes": dict(sorted(bucket["timeouts"].items())),
        }
    return output


def _is_timeout_run(run: dict[str, Any]) -> bool:
    code = (run.get("failure") or {}).get("code")
    return isinstance(code, str) and code in TIMEOUT_FAILURE_CODES


def _new_main_cost_bucket() -> dict[str, Any]:
    return {"runs_with_reviews": 0, "runs_with_observations": 0, "review_events": 0,
            "observations": Counter(), "totals": Counter(),
            "main_rework_runs": 0, "unresolved_reviews": 0, "timeout_runs": 0}


def _accumulate_bucket_cost(bucket: dict[str, Any], block: dict[str, Any]) -> None:
    events = block.get("review_event_count")
    if isinstance(events, int) and not isinstance(events, bool) and events > 0:
        bucket["review_events"] += events
    observations = block.get("observations") or {}
    totals = block.get("totals") or {}
    observed_run = False
    for key in MAIN_COST_KEYS:
        count = observations.get(key)
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            continue
        bucket["observations"][key] += count
        value = totals.get(key)
        if _is_valid_main_cost(value):
            bucket["totals"][key] += int(value)
            observed_run = True
    if observed_run:
        bucket["runs_with_observations"] += 1


def _main_cost_totals(bucket: dict[str, Any]) -> dict[str, int | None]:
    return {key: (bucket["totals"][key] if bucket["observations"][key] else None)
            for key in MAIN_COST_KEYS}


def _main_cost_output(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "runs_with_reviews": bucket["runs_with_reviews"],
        "runs_with_observations": bucket["runs_with_observations"],
        "review_events": bucket["review_events"],
        "observations": {key: bucket["observations"][key] for key in MAIN_COST_KEYS},
        "totals": _main_cost_totals(bucket),
        "main_rework_runs": bucket["main_rework_runs"],
        "unresolved_reviews": bucket["unresolved_reviews"],
        "timeout_runs": bucket["timeout_runs"],
    }


def _summarize_main_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate measured main cost per worker, present-data only.

    Only runs that actually carry review events get a block, so legacy
    uninstrumented reports stay unmeasured rather than cheap: absent or invalid
    observations contribute nothing and never become 0. Counts, sums, and the
    existing rework/unresolved/timeout proxies stay numeric and bounded, and no
    raw contract, path, diff, command, or error text is copied.
    """
    grouped: dict[str, dict[str, Any]] = defaultdict(_new_main_cost_bucket)
    observed_workers: set[str] = set()
    for run in records:
        block = run.get("main_cost")
        if not isinstance(block, dict):
            continue
        name = str((run.get("worker") or {}).get("worker_type", "unknown"))
        observed_workers.add(name)
        bucket = grouped[name]
        bucket["runs_with_reviews"] += 1
        _accumulate_bucket_cost(bucket, block)
    # Proxies reuse the existing run-level definitions for every run of a worker
    # that carries review events, so an unreviewed timeout or main rework is not
    # hidden by the measurement block.
    for run in records:
        name = str((run.get("worker") or {}).get("worker_type", "unknown"))
        if name not in observed_workers:
            continue
        bucket = grouped[name]
        if (run.get("outcome") or {}).get("main_rework") not in (None, "none"):
            bucket["main_rework_runs"] += 1
        if _effective_review_verdict(run) not in TERMINAL_REVIEW:
            bucket["unresolved_reviews"] += 1
        if _is_timeout_run(run):
            bucket["timeout_runs"] += 1
    return {name: _main_cost_output(grouped[name]) for name in grouped}


def _task_group_id(run: dict[str, Any]) -> str | None:
    """Only an explicit ``assignment.task_group_id`` groups a run; never inferred."""
    assignment = run.get("assignment") or {}
    if not isinstance(assignment, dict):
        return None
    value = assignment.get("task_group_id")
    return value if isinstance(value, str) and value else None


def _new_group_bucket() -> dict[str, Any]:
    return {"runs": 0, "delegations": set(), "worker_types": set(),
            "statuses": Counter(), "verdicts": Counter(), "reworked": 0,
            "timeouts": 0, "cost": _new_main_cost_bucket()}


def _summarize_task_groups(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize explicit task groups across delegation ids, models, and phases.

    A group aggregates whatever it observed — it never claims a group is
    logically complete or first-pass clean, because phases share one opaque id
    and declare no dependency graph. Runs without an explicit group id stay
    ungrouped and are only counted. Output carries counts and measured cost
    sums only, never paths, contracts, errors, or diff content.
    """
    groups: dict[str, dict[str, Any]] = {}
    ungrouped = 0
    for run in records:
        group_id = _task_group_id(run)
        if group_id is None:
            ungrouped += 1
            continue
        bucket = groups.setdefault(group_id, _new_group_bucket())
        bucket["runs"] += 1
        bucket["delegations"].add(_delegation_key(run))
        bucket["worker_types"].add(str((run.get("worker") or {}).get("worker_type", "unknown")))
        status = (run.get("outcome") or {}).get("status")
        bucket["statuses"][status if isinstance(status, str) and status else "unknown"] += 1
        verdict = _effective_review_verdict(run)
        bucket["verdicts"][verdict] += 1
        if (run.get("outcome") or {}).get("main_rework") not in (None, "none"):
            bucket["reworked"] += 1
        if _is_timeout_run(run):
            bucket["timeouts"] += 1
        block = run.get("main_cost")
        if isinstance(block, dict):
            bucket["cost"]["runs_with_reviews"] += 1
            _accumulate_bucket_cost(bucket["cost"], block)
    output: dict[str, Any] = {"group_count": len(groups), "ungrouped_runs": ungrouped, "groups": {}}
    for group_id, bucket in sorted(groups.items()):
        cost = bucket["cost"]
        output["groups"][group_id] = {
            "runs": bucket["runs"],
            "delegations": len(bucket["delegations"]),
            "models": len(bucket["worker_types"]),
            "worker_types": sorted(bucket["worker_types"]),
            "review_events": cost["review_events"],
            "accepted": bucket["verdicts"].get("accepted", 0),
            "needs_rework": bucket["verdicts"].get("needs_rework", 0),
            "rejected": bucket["verdicts"].get("rejected", 0),
            "unresolved": bucket["verdicts"].get("pending", 0),
            "reworked": bucket["reworked"],
            "timeouts": bucket["timeouts"],
            "launches_by_status": dict(sorted(bucket["statuses"].items())),
            "runs_with_observations": cost["runs_with_observations"],
            "observations": {key: cost["observations"][key] for key in MAIN_COST_KEYS},
            "totals": _main_cost_totals(cost),
        }
    return output


def summarize(records: list[dict[str, Any]], diagnostics: dict[str, int] | None = None) -> dict[str, Any]:
    workers = defaultdict(lambda: {"runs": 0, "completed": 0, "failed": 0, "interrupted": 0,
        "accepted": 0, "pending_review": 0, "needs_rework": 0, "rejected": 0, "reworked": 0,
        "missing_usage": 0, "zero_usage_completed": 0, "incomplete_metadata": 0,
        "tokens": Counter(), "cost": Decimal("0"), "cost_observed": 0})
    standard = Counter()
    for item in records:
        worker, routing, outcome, usage = item.get("worker") or {}, item.get("routing") or {}, item.get("outcome") or {}, item.get("usage") or {}
        name, status = str(worker.get("worker_type", "unknown")), outcome.get("status")
        bucket = workers[name]
        bucket["runs"] += 1
        bucket["completed" if status == "completed" else "interrupted" if status == "interrupted" else "failed"] += 1
        verdict = outcome.get("review_verdict", "pending")
        if verdict in ("accepted", "needs_rework") and status == "completed":
            bucket[verdict] += 1
        elif verdict == "rejected":
            bucket["rejected"] += 1
        elif status == "completed":
            bucket["pending_review"] += 1
        if outcome.get("main_rework") not in (None, "none"):
            bucket["reworked"] += 1
        observed = False
        for key in TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket["tokens"][key] += value
                observed = True
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float, str)) and not isinstance(cost, bool):
            try:
                bucket["cost"] += Decimal(str(cost))
                bucket["cost_observed"] += 1
                observed = True
            except Exception:
                pass
        if not observed:
            bucket["missing_usage"] += 1
        if status == "completed" and usage.get("total_tokens") == 0:
            bucket["zero_usage_completed"] += 1
        if _metadata_incomplete(item):
            bucket["incomplete_metadata"] += 1
        if routing.get("routing_class") == "standard" and routing.get("profile_source") == "auto":
            standard[name] += 1
    output: dict[str, Any] = {"runs": len(records), "diagnostics": diagnostics or {}, "standard_selection": {}, "workers": {},
                              "delegations": _summarize_delegations(records),
                              "task_groups": _summarize_task_groups(records),
                              "largest_runs": _summarize_largest_runs(records)}
    progress_metrics = _summarize_progress_metrics(records)
    main_cost = _summarize_main_cost(records)
    total_standard = sum(standard.values())
    for name in sorted(standard):
        output["standard_selection"][name] = {"count": standard[name], "ratio": standard[name] / total_standard if total_standard else None}
    total_keys = ("completed", "failed", "interrupted", "accepted", "pending_review", "needs_rework", "rejected")
    totals = Counter()
    for name, bucket in sorted(workers.items()):
        runs, completed = bucket["runs"], bucket["completed"]
        data = {key: bucket[key] for key in ("runs", *total_keys, "reworked", "missing_usage", "zero_usage_completed", "incomplete_metadata")}
        data.update({"tokens": dict(bucket["tokens"]), "cost": str(bucket["cost"]) if bucket["cost_observed"] else None,
                     "completion_rate": completed / runs if runs else None,
                     "acceptance_rate": bucket["accepted"] / completed if completed else None,
                     "rework_rate": bucket["reworked"] / runs if runs else None})
        if name in progress_metrics:
            data["progress_metrics"] = progress_metrics[name]
        if name in main_cost:
            data["main_cost"] = main_cost[name]
        output["workers"][name] = data
        for key in total_keys:
            totals[key] += bucket[key]
    for key in total_keys:
        output[key] = totals[key]
    return output


def _rate(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.1%}"


def _short(value: Any, length: int = 8) -> str:
    if isinstance(value, str) and value:
        return value[:length]
    return "unknown"


def _seconds(value: Any) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _print_progress(name: str, data: dict[str, Any]) -> None:
    medians = data["medians"]
    stage = " ".join(f"{field.removesuffix('_seconds')}={_seconds(medians[field])}"
                     for field in PROGRESS_STAGE_FIELDS)
    retries = "n/a" if data["retry_total_count"] is None else str(data["retry_total_count"])
    retry_seconds = _seconds(data["retry_total_seconds"])
    print(f"progress {name}: metrics_runs={data['runs_with_metrics']} {stage} "
          f"retries={retries} retry_time={retry_seconds} "
          f"checkpoints={data['checkpoint_counts']} timeouts={data['timeout_causes']}")


def _print_main_cost(name: str, data: dict[str, Any]) -> None:
    """One compact line per worker, only when something was actually measured."""
    if not any(data["observations"][key] for key in MAIN_COST_KEYS):
        return
    parts = [f"events={data['review_events']}"]
    for key in MAIN_COST_KEYS:
        total = data["totals"][key]
        parts.append(f"{key}=" + ("unmeasured" if total is None
                                  else f"{total}/{data['observations'][key]}"))
    print(f"main_cost {name}: " + " ".join(parts))


def _print_task_groups(task_groups: dict[str, Any]) -> None:
    groups = task_groups.get("groups") or {}
    for group_id, data in groups.items():
        print(f"task_group {group_id}: runs={data['runs']} delegations={data['delegations']} "
              f"models={data['models']} accepted={data['accepted']} "
              f"review_events={data['review_events']} timeouts={data['timeouts']} "
              f"rework={data['reworked']} statuses={data['launches_by_status']} "
              f"observed={data['runs_with_observations']}/{data['runs']}")
    if task_groups.get("ungrouped_runs"):
        print(f"task_groups: ungrouped_runs={task_groups['ungrouped_runs']}")


def _print_reconcile(report: dict[str, Any]) -> None:
    print(f"reconcile: runs={report['runs_considered']} "
          f"stale={len(report['stale_unconfirmed'])} "
          f"pending_review={len(report['completed_pending_review'])} "
          f"failed_without_closure={len(report['failed_without_closure'])} "
          f"notification_gaps={len(report['notification_gaps'])} "
          f"fixtures={report['suspected_fixtures']}")
    for item in report["stale_unconfirmed"]:
        print(f"stale: run={_short(item.get('run_id'))} "
              f"delegation={_short(item.get('delegation_id'), 16)} "
              f"started={item.get('started_at')} deadline={item.get('deadline')} "
              f"age={item.get('age_seconds')}s")
    for item in report["completed_pending_review"]:
        print(f"pending_review: run={_short(item.get('run_id'))} "
              f"delegation={_short(item.get('delegation_id'), 16)} "
              f"finished={item.get('finished_at')} age={item.get('age_seconds')}s")
    for item in report["failed_without_closure"]:
        print(f"failed_without_closure: run={_short(item.get('run_id'))} "
              f"delegation={_short(item.get('delegation_id'), 16)} "
              f"status={item.get('status')} finished={item.get('finished_at')} "
              f"age={item.get('age_seconds')}s")
    for item in report["notification_gaps"]:
        print(f"notification_gap: run={_short(item.get('run_id'))} "
              f"delegation={_short(item.get('delegation_id'), 16)} "
              f"status={item.get('status')} delivery={item.get('delivery')} "
              f"attempts={item.get('attempts')}")
    if any(report["diagnostics"].values()):
        print(f"diagnostics: {report['diagnostics']}")


def main() -> int:
    args = parse_args()
    try:
        target = Path(args.record_dir) if args.record_dir else record_dir()
        if args.reconcile:
            # Lifecycle reconciliation has no run-level filters; refuse them
            # explicitly rather than silently ignoring user intent.
            ignored = [name for name, value in (("--worker", args.worker),
                                                ("--status", args.status),
                                                ("--project", args.project))
                       if value]
            if ignored:
                print("worker-run review: --reconcile does not support "
                      + "/".join(ignored) + " filters", file=os.sys.stderr)
                return 2
            report = reconcile(target, args.days, args.since, args.until,
                               include_tests=args.include_tests)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            else:
                _print_reconcile(report)
            return 0
        records, diagnostics = load_with_diagnostics(
            target, args.days, args.since, args.until,
            include_tests=args.include_tests)
        summary = summarize(filtered(records, args), diagnostics)
    except (OSError, ValueError) as exc:
        print(f"worker-run review: {exc}", file=os.sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(f"runs: {summary['runs']} completed={summary.get('completed', 0)} failed={summary.get('failed', 0)} accepted={summary.get('accepted', 0)} pending_review={summary.get('pending_review', 0)}")
        delegations = summary["delegations"]
        print(f"delegations: total={delegations['total']} accepted={delegations['accepted']} pending_review={delegations['pending_review']} first_pass={delegations['first_pass_accepted']} final_acceptance={_rate(delegations['final_acceptance_rate'])} first_pass_acceptance={_rate(delegations['first_pass_acceptance_rate'])}")
        def _short_run(descriptor: dict[str, Any] | None) -> str:
            if not descriptor:
                return "none"
            run_id = descriptor.get("run_id") or "unknown"
            return run_id[:8] if isinstance(run_id, str) else "unknown"
        largest = summary["largest_runs"]
        print(f"largest_runs: cost={_short_run(largest['cost_usd'])} tokens={_short_run(largest['total_tokens'])} time={_short_run(largest['worker_seconds'])}")
        for name, data in summary["workers"].items():
            cost = data["cost"] if data["cost"] is not None else "unknown"
            print(f"{name}: runs={data['runs']} completion={_rate(data['completion_rate'])} acceptance={_rate(data['acceptance_rate'])} pending={data['pending_review']} rework={_rate(data['rework_rate'])} missing_usage={data['missing_usage']} zero_usage={data['zero_usage_completed']} incomplete_metadata={data['incomplete_metadata']} cost={cost} tokens={data['tokens']}")
            if "progress_metrics" in data:
                _print_progress(name, data["progress_metrics"])
            if "main_cost" in data:
                _print_main_cost(name, data["main_cost"])
        _print_task_groups(summary["task_groups"])
        for name, data in summary["standard_selection"].items():
            print(f"selection {name}: {data['count']} ({_rate(data['ratio'])})")
        if any(summary["diagnostics"].values()):
            print(f"diagnostics: {summary['diagnostics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
