#!/usr/bin/env python3
"""Append a main-agent review event for an existing Pi worker run."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from run_pi_worker import append_record, iter_records, utc_now, verification_items


RESULTS = {"accepted": "usable", "needs_rework": "partial", "rejected": "unusable"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--verdict", choices=tuple(RESULTS), required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--verification", action="append", default=[])
    parser.add_argument("--main-rework", choices=("none", "minor", "major", "rewrite"),
                        default="none")
    parser.add_argument("--main-review-seconds", type=float)
    parser.add_argument("--main-rework-seconds", type=float)
    parser.add_argument("--main-bookkeeping-seconds", type=float)
    parser.add_argument("--main-briefing-count", type=int,
                        help="this review event's briefing count supplied by main; not cumulative")
    parser.add_argument("--main-review-count", type=int,
                        help="this review event's review count supplied by main; not cumulative")
    parser.add_argument("--main-recovery-count", type=int,
                        help="this review event's recovery count supplied by main; not cumulative")
    parser.add_argument("--main-diff-bytes", type=int,
                        help="bytes of diff main actually read for this review; no paths/content")
    parser.add_argument("--main-input-tokens", type=int,
                        help="host-measured main input tokens for this review; may stay unset")
    parser.add_argument("--main-output-tokens", type=int,
                        help="host-measured main output tokens for this review; may stay unset")
    return parser.parse_args(argv)


# (CLI/attribute name, main_metrics key). Order defines the fixed metric schema.
MAIN_METRIC_FIELDS = (
    ("main_briefing_count", "briefing_count"),
    ("main_review_count", "review_count"),
    ("main_recovery_count", "recovery_count"),
    ("main_diff_bytes", "diff_bytes"),
    ("main_input_tokens", "input_tokens"),
    ("main_output_tokens", "output_tokens"),
)


def _validate_seconds(name: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"--{name.replace('_', '-')} must be finite and non-negative")
    return value


def _validate_count(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"--{name.replace('_', '-')} must be a non-negative integer")
    return value


def is_run(item: dict[str, Any]) -> bool:
    version = item.get("schema_version")
    return version == 2 or (version == 3 and item.get("record_type") == "run")


def make_review(args: argparse.Namespace, records: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [item for item in records if is_run(item) and item.get("run_id") == args.run_id]
    if len(matches) != 1:
        raise ValueError("run-id must identify exactly one recorded run")
    if matches[0].get("outcome", {}).get("status") != "completed" and args.verdict != "rejected":
        raise ValueError("only a completed run can be accepted or marked needs_rework")
    if any(Path(value).is_absolute() for value in args.changed_file):
        raise ValueError("--changed-file values must be relative paths")
    previous = next((item.get("review_id") for item in reversed(records)
                     if item.get("schema_version") == 3
                     and item.get("record_type") == "review"
                     and item.get("target_run_id") == args.run_id), None)
    for name in ("main_review_seconds", "main_rework_seconds", "main_bookkeeping_seconds"):
        _validate_seconds(name, getattr(args, name, None))
    main_metrics: dict[str, int | None] = {}
    for attr, key in MAIN_METRIC_FIELDS:
        value = _validate_count(attr, getattr(args, attr, None))
        if value is not None:
            main_metrics[key] = value
    timing = {name: getattr(args, name, None)
              for name in ("main_review_seconds", "main_rework_seconds",
                           "main_bookkeeping_seconds")}
    review = {
        "schema_version": 3,
        "record_type": "review",
        "phase": "reviewed",
        "review_id": secrets.token_hex(16),
        "target_run_id": args.run_id,
        "supersedes_review_id": previous,
        "created_at": utc_now(),
        "outcome": {
            "review_verdict": args.verdict,
            "result": RESULTS[args.verdict],
            "summary": args.summary,
            "changed_files": args.changed_file,
            "main_rework": args.main_rework,
        },
        "verification": verification_items(args.verification),
        "timing": timing,
        "routing_lesson": None,
    }
    if main_metrics:
        for _attr, key in MAIN_METRIC_FIELDS:
            main_metrics.setdefault(key, None)
        review["main_metrics"] = main_metrics
    return review


def main(argv: list[str] | None = None) -> int:
    old_umask = os.umask(0o077)
    try:
        args = parse_args(argv)
        review = make_review(args, iter_records())
        append_record(review)
        print(json.dumps(review, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"pi-worker review: {exc}", file=sys.stderr)
        return 2
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
