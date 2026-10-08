import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "record_worker_review.py"
SCRIPTS = ROOT / "scripts"
RUN = {"schema_version": 3, "record_type": "run", "phase": "completed",
       "run_id": "run-1", "worker": {}, "outcome": {"status": "completed"}}
METRIC_KEYS = {"briefing_count", "review_count", "recovery_count",
               "diff_bytes", "input_tokens", "output_tokens"}
# Seed run records into today's local-day file, the same local date appends use
# (``run_pi_worker.local_day``), so the seed is inside the retention window and
# is not pruned by the next append.
TODAY_LOG = datetime.datetime.now().astimezone().date().isoformat() + ".jsonl"


def load_module():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("record_worker_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def namespace(**overrides):
    values = {"run_id": "run-1", "verdict": "accepted", "summary": "s",
              "changed_file": [], "verification": [], "main_rework": "none",
              "main_review_seconds": None, "main_rework_seconds": None,
              "main_bookkeeping_seconds": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def reviews_in(directory):
    found = []
    for path in Path(directory).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("record_type") == "review":
                found.append(item)
    return found


class RecordReviewTests(unittest.TestCase):
    def test_append_and_supersede_review(self):
        with tempfile.TemporaryDirectory() as temp:
            records = Path(temp)
            run = {"schema_version": 3, "record_type": "run", "phase": "completed",
                   "run_id": "run-1", "worker": {}, "outcome": {"status": "completed"}}
            (records / TODAY_LOG).write_text(json.dumps(run) + "\n")
            env = os.environ | {"PI_WORKER_RECORD_DIR": str(records)}
            command = [sys.executable, str(SCRIPT), "--run-id", "run-1",
                       "--verdict", "accepted", "--summary", "verified",
                       "--changed-file", "x.py", "--verification", "pytest::passed"]
            first = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_review = json.loads(first.stdout)
            self.assertEqual(first_review["outcome"]["result"], "usable")
            second = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_review = json.loads(second.stdout)
            self.assertEqual(second_review["supersedes_review_id"], first_review["review_id"])

    def test_unknown_run_and_negative_timing_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ | {"PI_WORKER_RECORD_DIR": temp}
            command = [sys.executable, str(SCRIPT), "--run-id", "missing",
                       "--verdict", "rejected", "--summary", "no"]
            result = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            run = {"schema_version": 2, "run_id": "known", "worker": {}, "outcome": {}}
            (Path(temp) / TODAY_LOG).write_text(json.dumps(run) + "\n")
            negative = subprocess.run(
                [sys.executable, str(SCRIPT), "--run-id", "known", "--verdict", "accepted",
                 "--summary", "no", "--main-review-seconds", "-1"],
                env=env, text=True, capture_output=True)
            self.assertEqual(negative.returncode, 2)

    def test_direct_metric_validation_rejects_bad_types(self):
        module = load_module()
        self.assertEqual(module._validate_count("main_diff_bytes", 0), 0)
        for bad in (True, False, -1, 1.5, 2.0, "3", [1]):
            with self.assertRaises(ValueError):
                module._validate_count("main_diff_bytes", bad)
        for bad in (-1, float("nan"), float("inf"), float("-inf"), "1"):
            with self.assertRaises(ValueError):
                module.make_review(namespace(main_review_seconds=bad), [RUN])

    def test_old_namespace_without_metric_fields_has_no_main_metrics(self):
        module = load_module()
        review = module.make_review(namespace(), [RUN])
        self.assertNotIn("main_metrics", review)
        self.assertEqual(review["timing"], {"main_review_seconds": None,
                                             "main_rework_seconds": None,
                                             "main_bookkeeping_seconds": None})
        review = module.make_review(namespace(main_diff_bytes=0), [RUN])
        self.assertEqual(review["main_metrics"],
                         {key: (0 if key == "diff_bytes" else None) for key in METRIC_KEYS})

    def test_cli_metrics_positive_zero_and_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / TODAY_LOG).write_text(json.dumps(RUN) + "\n")
            env = os.environ | {"PI_WORKER_RECORD_DIR": temp}
            base = [sys.executable, str(SCRIPT), "--run-id", "run-1",
                    "--verdict", "accepted", "--summary", "ok"]
            positive = subprocess.run(
                base + ["--main-briefing-count", "1", "--main-review-count", "2",
                        "--main-recovery-count", "3", "--main-diff-bytes", "4096",
                        "--main-input-tokens", "100", "--main-output-tokens", "50"],
                env=env, text=True, capture_output=True)
            self.assertEqual(positive.returncode, 0, positive.stderr)
            metrics = json.loads(positive.stdout)["main_metrics"]
            self.assertEqual(set(metrics), METRIC_KEYS)
            self.assertEqual(metrics, {"briefing_count": 1, "review_count": 2,
                                       "recovery_count": 3, "diff_bytes": 4096,
                                       "input_tokens": 100, "output_tokens": 50})
            zero = subprocess.run(base + ["--main-diff-bytes", "0"],
                                  env=env, text=True, capture_output=True)
            self.assertEqual(zero.returncode, 0, zero.stderr)
            self.assertEqual(json.loads(zero.stdout)["main_metrics"],
                             {key: (0 if key == "diff_bytes" else None) for key in METRIC_KEYS})
            missing = subprocess.run(base, env=env, text=True, capture_output=True)
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertNotIn("main_metrics", json.loads(missing.stdout))

    def test_cli_invalid_metrics_and_timing_write_no_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / TODAY_LOG).write_text(json.dumps(RUN) + "\n")
            env = os.environ | {"PI_WORKER_RECORD_DIR": temp}
            base = [sys.executable, str(SCRIPT), "--run-id", "run-1",
                    "--verdict", "accepted", "--summary", "ok"]
            cases = [["--main-diff-bytes", "1.5"], ["--main-briefing-count", "abc"],
                     ["--main-diff-bytes", "-2"], ["--main-review-seconds", "nan"],
                     ["--main-rework-seconds", "inf"],
                     ["--main-bookkeeping-seconds", "-inf"]]
            for extra in cases:
                result = subprocess.run(base + extra, env=env, text=True,
                                        capture_output=True)
                self.assertEqual(result.returncode, 2, extra)
            self.assertEqual(reviews_in(temp), [])

    def test_cli_m3_fallback_flag_is_unrecognized_writes_no_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / TODAY_LOG).write_text(json.dumps(RUN) + "\n")
            env = os.environ | {"PI_WORKER_RECORD_DIR": temp}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--run-id", "run-1",
                 "--verdict", "accepted", "--summary", "ok", "--m3-fallback-used"],
                env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(reviews_in(temp), [])

    def test_superseding_review_does_not_inherit_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / TODAY_LOG).write_text(json.dumps(RUN) + "\n")
            env = os.environ | {"PI_WORKER_RECORD_DIR": temp}
            base = [sys.executable, str(SCRIPT), "--run-id", "run-1",
                    "--verdict", "accepted", "--summary", "ok"]
            first = subprocess.run(base + ["--main-diff-bytes", "10"],
                                   env=env, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_review = json.loads(first.stdout)
            self.assertEqual(first_review["main_metrics"]["diff_bytes"], 10)
            second = subprocess.run(base, env=env, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_review = json.loads(second.stdout)
            self.assertEqual(second_review["supersedes_review_id"],
                             first_review["review_id"])
            self.assertNotIn("main_metrics", second_review)
            stored = reviews_in(temp)
            self.assertEqual(len(stored), 2)
            self.assertEqual(set(stored[0]["main_metrics"]), METRIC_KEYS)
            dated = sorted(p.name for p in Path(temp).glob("????-??-??.jsonl"))
            self.assertEqual(dated, [TODAY_LOG])


if __name__ == "__main__":
    unittest.main()
