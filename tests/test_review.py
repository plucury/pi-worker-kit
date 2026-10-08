import importlib.util
from pathlib import Path
import tempfile
import unittest
import argparse
import contextlib
import datetime as dt
import io
import json
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_worker_runs.py"
spec = importlib.util.spec_from_file_location("review", SCRIPT)
review = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def _run(self, run_id, status, verdict, delegation_id="d", worker_type="simple-m3", **extra):
        record = {
            "schema_version": 3, "record_type": "run", "phase": "completed",
            "run_id": run_id, "delegation_id": delegation_id, "record_kind": "production",
            "worker": {"worker_type": worker_type},
            "outcome": {"status": status, "review_verdict": verdict},
            "usage": {}, "timing": {},
        }
        record.update(extra)
        return record

    def _started(self, run_id, delegation_id, *, deadline="2026-01-01T00:00:00Z",
                 record_kind="production"):
        return {"schema_version": 3, "record_type": "lifecycle", "phase": "started",
                "event": "run_started", "run_id": run_id,
                "delegation_id": delegation_id, "record_kind": record_kind,
                "worker_type": "simple-m3", "transport": "intercom",
                "created_at": "2026-01-01T00:00:00Z", "deadline": deadline,
                "deadline_scope": "preflight_included"}

    def _review(self, review_id, target_run_id, verdict):
        return {"schema_version": 3, "record_type": "review", "phase": "reviewed",
                "review_id": review_id, "target_run_id": target_run_id,
                "outcome": {"review_verdict": verdict, "result": "usable"}}

    def _cost_review(self, review_id, target_run_id, verdict, main_metrics=None):
        review = self._review(review_id, target_run_id, verdict)
        if main_metrics is not None:
            review["main_metrics"] = main_metrics
        return review

    def _notification(self, run_id, delegation_id, *, delivered, status="completed",
                      attempts=2):
        return {"schema_version": 3, "record_type": "notification", "phase": "finished",
                "event": "run_finished", "run_id": run_id,
                "delegation_id": delegation_id, "record_kind": "production",
                "transport": "intercom", "status": status, "attempts": attempts,
                "delivery": "delivered" if delivered else "failed", "delivered": delivered,
                "created_at": "2026-01-01T00:01:00Z"}

    def _write(self, path, items):
        path.write_text("\n".join(json.dumps(item) for item in items) + "\n")

    def test_summary_and_terminal_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2026-09-19.jsonl"
            path.write_text(
                '{"schema_version":1,"worker":{"worker_type":"standard-glm"}}\n'
                '{"schema_version":2,"run_id":"r1","worker":{"worker_type":"standard-glm"},"routing":{"routing_class":"standard","profile_source":"auto"},"outcome":{"status":"completed","result":"usable","review_verdict":"accepted","main_rework":"minor"},"usage":{"input_tokens":10,"cost_usd":0.1}}\n'
                '{"schema_version":2,"run_id":"r2","worker":{"worker_type":"standard-glm-zai"},"routing":{"routing_class":"standard","profile_source":"auto"},"outcome":{"status":"failed","result":"unusable","main_rework":"none"},"usage":{"output_tokens":4,"cost_usd":0.2}}\n'
                '{"schema_version":2,"run_id":"r3","worker":{"worker_type":"standard-glm"},"routing":{"routing_class":"standard","profile_source":"explicit"},"outcome":{"status":"failed","result":"unusable","main_rework":"none"},"usage":{}}\n'
            )
            records = review.load(Path(temp), None)
            self.assertEqual(len(records), 3)
            summary = review.summarize(records)
            self.assertEqual(summary["runs"], 3)
            self.assertEqual(summary["workers"]["standard-glm"]["completion_rate"], 0.5)
            self.assertEqual(summary["workers"]["standard-glm"]["acceptance_rate"], 1)
            self.assertEqual(summary["workers"]["standard-glm-zai"]["completion_rate"], 0)
            self.assertEqual(summary["standard_selection"]["standard-glm"]["ratio"], 0.5)
            self.assertEqual(summary["workers"]["standard-glm"]["cost"], "0.1")
            self.assertEqual(summary["workers"]["standard-glm"]["missing_usage"], 1)

    def test_standard_selection_covers_every_enabled_profile(self):
        records = [
            {"worker": {"worker_type": "standard-deepseek"},
             "routing": {"routing_class": "standard", "profile_source": "auto"},
             "outcome": {"status": "completed"}},
            {"worker": {"worker_type": "standard-glm"},
             "routing": {"routing_class": "standard", "profile_source": "auto"},
             "outcome": {"status": "completed"}},
            {"worker": {"worker_type": "standard-deepseek"},
             "routing": {"routing_class": "standard", "profile_source": "explicit"},
             "outcome": {"status": "completed"}},
        ]
        summary = review.summarize(records)
        self.assertEqual(summary["standard_selection"]["standard-deepseek"],
                         {"count": 1, "ratio": 0.5})
        self.assertEqual(summary["standard_selection"]["standard-glm"],
                         {"count": 1, "ratio": 0.5})

    def test_filters_and_malformed_line_reporting(self):
        records = [
            {"worker": {"worker_type": "simple-m3"}, "project": {"id": "p1"},
             "outcome": {"status": "completed"}},
            {"worker": {"worker_type": "standard-glm"}, "project": {"id": "p2"},
             "outcome": {"status": "failed"}},
        ]
        args = argparse.Namespace(worker=["simple-m3"], status=["completed"], project="p1")
        self.assertEqual(review.filtered(records, args), records[:1])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "2026-09-19.jsonl"
            path.write_text('{"schema_version":2,"run_id":"r","worker":{},"outcome":{}}\nnot-json\n')
            with self.assertRaisesRegex(ValueError, r"2026-09-19\.jsonl:2"):
                review.load(Path(temp))

    def test_latest_review_materializes_and_zero_usage_is_flagged(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "2026-09-18.jsonl").write_text(
                '{"schema_version":3,"record_type":"run","phase":"completed","run_id":"r1","worker":{"worker_type":"simple-m3"},"routing":{},"assignment":{"goal":"g","scope":["x"],"acceptance_checks":["ok"],"features":{"acceptance_mode":"deterministic"}},"outcome":{"status":"completed","result":"partial","review_verdict":"pending","main_rework":"none"},"usage":{"total_tokens":0}}\n')
            (base / "2026-09-19.jsonl").write_text(
                '{"schema_version":3,"record_type":"review","phase":"reviewed","review_id":"v1","target_run_id":"r1","outcome":{"review_verdict":"accepted","result":"usable","main_rework":"none","summary":"ok","changed_files":["x"]},"verification":[{"command":"test","result":"passed"}]}\n')
            records, diagnostics = review.load_with_diagnostics(base)
            self.assertEqual(records[0]["outcome"]["review_verdict"], "accepted")
            summary = review.summarize(records, diagnostics)
            self.assertEqual(summary["accepted"], 1)
            self.assertEqual(summary["workers"]["simple-m3"]["zero_usage_completed"], 1)

    def _delegation_run(self, run_id, status, verdict, delegation_id="d", worker_type="simple-m3"):
        return {
            "run_id": run_id,
            "delegation_id": delegation_id,
            "worker": {"worker_type": worker_type},
            "outcome": {"status": status, "review_verdict": verdict},
            "usage": {},
            "timing": {},
        }

    def test_delegations_rework_chain_counts_only_final_acceptance(self):
        records = [
            self._delegation_run("a", "failed", "pending"),
            self._delegation_run("b", "completed", "needs_rework"),
            self._delegation_run("c", "completed", "accepted"),
        ]
        delegations = review._summarize_delegations(records)
        self.assertEqual(delegations["total"], 1)
        self.assertEqual(delegations["accepted"], 1)
        self.assertEqual(delegations["needs_rework"], 0)
        self.assertEqual(delegations["first_pass_accepted"], 0)
        self.assertEqual(delegations["final_acceptance_rate"], 1)
        self.assertEqual(delegations["first_pass_acceptance_rate"], 0)

    def test_delegations_first_pass_accepted_for_single_accepted_run(self):
        records = [self._delegation_run("a", "completed", "accepted")]
        delegations = review._summarize_delegations(records)
        self.assertEqual(delegations["total"], 1)
        self.assertEqual(delegations["accepted"], 1)
        self.assertEqual(delegations["first_pass_accepted"], 1)
        self.assertEqual(delegations["final_acceptance_rate"], 1)
        self.assertEqual(delegations["first_pass_acceptance_rate"], 1)

    def test_delegations_legacy_run_without_delegation_id_is_alone(self):
        records = [
            self._delegation_run("legacy1", "completed", "accepted", delegation_id=None),
            self._delegation_run("legacy2", "completed", "accepted", delegation_id=""),
            self._delegation_run("legacy3", "completed", "accepted", delegation_id=42),
            self._delegation_run("grouped", "completed", "accepted", delegation_id="real"),
        ]
        delegations = review._summarize_delegations(records)
        self.assertEqual(delegations["total"], 4)
        self.assertEqual(delegations["accepted"], 4)
        self.assertEqual(delegations["first_pass_accepted"], 4)

    def test_delegations_failed_interrupted_and_pending_final_states(self):
        records = [
            self._delegation_run("f1", "failed", "pending", delegation_id="d-fail"),
            self._delegation_run("f2", "interrupted", "pending", delegation_id="d-interrupt"),
            self._delegation_run("f3", "completed", "pending", delegation_id="d-pending"),
        ]
        delegations = review._summarize_delegations(records)
        self.assertEqual(delegations["total"], 3)
        self.assertEqual(delegations["failed"], 1)
        self.assertEqual(delegations["interrupted"], 1)
        self.assertEqual(delegations["pending_review"], 1)
        self.assertEqual(delegations["accepted"], 0)
        self.assertEqual(delegations["final_acceptance_rate"], 0)
        self.assertEqual(delegations["first_pass_accepted"], 0)
        self.assertEqual(delegations["first_pass_acceptance_rate"], 0)

    def test_delegations_empty_input_yields_null_rates(self):
        delegations = review._summarize_delegations([])
        self.assertEqual(delegations["total"], 0)
        self.assertEqual(delegations["accepted"], 0)
        self.assertIsNone(delegations["final_acceptance_rate"])
        self.assertIsNone(delegations["first_pass_acceptance_rate"])

    def test_largest_runs_pick_one_per_metric_and_handle_missing(self):
        records = [
            {"run_id": "small", "delegation_id": "d1", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": 0.05, "total_tokens": 100},
             "timing": {"worker_seconds": 10.0}},
            {"run_id": "big", "delegation_id": "d1", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": 1.25, "total_tokens": 5000},
             "timing": {"worker_seconds": 120.0}},
            {"run_id": "none", "delegation_id": "d2", "worker": {"worker_type": "standard-deepseek"},
             "outcome": {"status": "failed", "review_verdict": "pending"},
             "usage": {}, "timing": {}},
        ]
        largest = review._summarize_largest_runs(records)
        self.assertEqual(largest["cost_usd"]["run_id"], "big")
        self.assertEqual(largest["cost_usd"]["cost_usd"], 1.25)
        self.assertEqual(largest["cost_usd"]["worker_type"], "simple-m3")
        self.assertEqual(largest["cost_usd"]["delegation_id"], "d1")
        self.assertEqual(largest["total_tokens"]["run_id"], "big")
        self.assertEqual(largest["worker_seconds"]["run_id"], "big")
        self.assertIsNone(largest["cost_usd"].get("total_tokens"))
        self.assertNotIn("assignment", largest["cost_usd"])
        self.assertNotIn("changed_files", largest["cost_usd"])
        self.assertNotIn("failure", largest["cost_usd"])
        self.assertNotIn("goal", largest["cost_usd"])
        largest_only_missing = review._summarize_largest_runs([records[2]])
        self.assertIsNone(largest_only_missing["cost_usd"])
        self.assertIsNone(largest_only_missing["total_tokens"])
        self.assertIsNone(largest_only_missing["worker_seconds"])

    def test_largest_runs_ignore_invalid_observations(self):
        nan_value = float("nan")
        inf_value = float("inf")
        records = [
            {"run_id": "bad", "delegation_id": "d", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": nan_value, "total_tokens": True},
             "timing": {"worker_seconds": -3.0}},
            {"run_id": "bool", "delegation_id": "d", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": False},
             "timing": {"worker_seconds": inf_value}},
            {"run_id": "ok", "delegation_id": "d", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": 0.5, "total_tokens": 10},
             "timing": {"worker_seconds": 1.0}},
        ]
        largest = review._summarize_largest_runs(records)
        self.assertEqual(largest["cost_usd"]["run_id"], "ok")
        self.assertEqual(largest["cost_usd"]["cost_usd"], 0.5)
        self.assertEqual(largest["total_tokens"]["run_id"], "ok")
        self.assertEqual(largest["worker_seconds"]["run_id"], "ok")

    def test_largest_runs_keep_first_run_on_ties(self):
        records = [
            {"run_id": "first", "delegation_id": "d", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": 1.0, "total_tokens": 500},
             "timing": {"worker_seconds": 30.0}},
            {"run_id": "second", "delegation_id": "d", "worker": {"worker_type": "simple-m3"},
             "outcome": {"status": "completed", "review_verdict": "accepted"},
             "usage": {"cost_usd": 1.0, "total_tokens": 500},
             "timing": {"worker_seconds": 30.0}},
        ]
        largest = review._summarize_largest_runs(records)
        self.assertEqual(largest["cost_usd"]["run_id"], "first")
        self.assertEqual(largest["total_tokens"]["run_id"], "first")
        self.assertEqual(largest["worker_seconds"]["run_id"], "first")

    def test_summarize_includes_delegations_and_largest_runs(self):
        records = [
            self._delegation_run("only", "completed", "accepted"),
        ]
        summary = review.summarize(records)
        self.assertIn("delegations", summary)
        self.assertIn("largest_runs", summary)
        self.assertEqual(summary["delegations"]["total"], 1)
        self.assertEqual(summary["delegations"]["accepted"], 1)
        self.assertIsNone(summary["largest_runs"]["cost_usd"])

    # ----- optional progress_metrics aggregation -----

    def _progress_run(self, run_id, metrics=None, *, worker_type="simple-m3",
                      failure=None):
        run = self._delegation_run(run_id, "completed", "accepted",
                                   delegation_id="d" + run_id, worker_type=worker_type)
        if metrics is not None:
            run["progress_metrics"] = metrics
        if failure is not None:
            run["failure"] = failure
        return run

    def test_progress_metrics_absent_is_not_synthesized(self):
        # Legacy receipts without the optional block keep the previous worker
        # shape: no progress_metrics key, no derived zeros.
        records = [self._delegation_run("legacy", "completed", "accepted")]
        summary = review.summarize(records)
        self.assertNotIn("progress_metrics", summary["workers"]["simple-m3"])
        self.assertEqual(review._summarize_progress_metrics(records), {})

    def test_progress_metrics_valid_aggregation(self):
        records = [
            self._progress_run("a", {
                "first_tool_seconds": 10, "first_edit_seconds": 20,
                "first_test_candidate_seconds": 25, "last_activity_seconds": 100,
                "last_tool_completion_seconds": 98, "tool_seconds": 40,
                "retry_seconds": 5, "retry_count": 1,
                "checkpoint_notices": ["investigation_checkpoint"],
            }),
            self._progress_run("b", {
                "first_tool_seconds": 30, "first_edit_seconds": 40,
                "first_test_candidate_seconds": 45, "last_activity_seconds": 200,
                "last_tool_completion_seconds": 198, "tool_seconds": 80,
                "retry_seconds": 15, "retry_count": 3,
                "checkpoint_notices": ["investigation_checkpoint",
                                       "first_delivery_checkpoint"],
            }),
        ]
        summary = review.summarize(records)
        progress = summary["workers"]["simple-m3"]["progress_metrics"]
        self.assertEqual(progress["runs_with_metrics"], 2)
        self.assertEqual(progress["observations"]["first_tool_seconds"], 2)
        self.assertEqual(progress["observations"]["retry_count"], 2)
        self.assertEqual(progress["medians"]["first_tool_seconds"], 20.0)
        self.assertEqual(progress["medians"]["first_edit_seconds"], 30.0)
        self.assertEqual(progress["medians"]["first_test_candidate_seconds"], 35.0)
        self.assertEqual(progress["medians"]["last_activity_seconds"], 150.0)
        self.assertEqual(progress["medians"]["last_tool_completion_seconds"], 148.0)
        self.assertEqual(progress["retry_total_count"], 4)
        self.assertEqual(progress["retry_total_seconds"], 20.0)
        self.assertEqual(progress["checkpoint_counts"],
                         {"first_delivery_checkpoint": 1, "investigation_checkpoint": 2})
        self.assertEqual(progress["timeout_causes"], {})

    def test_progress_metrics_null_and_invalid_are_unobserved(self):
        nan_value = float("nan")
        inf_value = float("inf")
        records = [
            self._progress_run("nulls", {
                "first_tool_seconds": None, "first_edit_seconds": None,
                "first_test_candidate_seconds": None, "last_activity_seconds": None,
                "last_tool_completion_seconds": None, "tool_seconds": None,
                "retry_seconds": None, "retry_count": None,
                "checkpoint_notices": None,
            }),
            self._progress_run("bad", {
                "first_tool_seconds": -1, "first_edit_seconds": True,
                "first_test_candidate_seconds": nan_value,
                "last_activity_seconds": inf_value, "last_tool_completion_seconds": "12",
                "tool_seconds": [], "retry_seconds": {}, "retry_count": False,
                "checkpoint_notices": ["unknown_code"],
            }),
            self._progress_run("ok", {"first_tool_seconds": 12, "retry_count": 0}),
        ]
        progress = review._summarize_progress_metrics(records)["simple-m3"]
        self.assertEqual(progress["runs_with_metrics"], 3)
        self.assertEqual(progress["observations"]["first_tool_seconds"], 1)
        self.assertEqual(progress["observations"]["retry_count"], 1)
        self.assertEqual(progress["observations"]["first_edit_seconds"], 0)
        self.assertIsNone(progress["medians"]["first_edit_seconds"])
        self.assertEqual(progress["medians"]["first_tool_seconds"], 12.0)
        self.assertEqual(progress["retry_total_count"], 0)
        self.assertIsNone(progress["retry_total_seconds"])
        self.assertEqual(progress["checkpoint_counts"], {})

    def test_progress_metrics_timeout_and_checkpoint_code_vocabulary(self):
        records = [
            self._progress_run("timeout", {"checkpoint_notices": ["wrap_up_checkpoint"]},
                               failure={"code": "timeout"}),
            self._progress_run("tool", {}, failure={"code": "tool_timeout"}),
            self._progress_run("budget", {}, failure={"code": "retry_budget_exceeded"}),
            self._progress_run("limit", {}, failure={"code": "retry_limit"}),
            self._progress_run("provider", {}, failure={"code": "provider_error"}),
            self._progress_run("other-worker", worker_type="standard-glm",
                               failure={"code": "tool_timeout"}),
        ]
        progress = review._summarize_progress_metrics(records)
        self.assertEqual(progress["simple-m3"]["checkpoint_counts"],
                         {"wrap_up_checkpoint": 1})
        self.assertEqual(progress["simple-m3"]["timeout_causes"],
                         {"retry_budget_exceeded": 1, "retry_limit": 1,
                          "timeout": 1, "tool_timeout": 1})
        self.assertEqual(progress["standard-glm"]["timeout_causes"],
                         {"tool_timeout": 1})
        # A timeout-only worker with no metric block still reports a bounded
        # block; numeric fields stay unobserved rather than becoming zero.
        self.assertEqual(progress["standard-glm"]["runs_with_metrics"], 0)
        self.assertEqual(progress["standard-glm"]["retry_total_count"], None)

    def test_progress_metrics_text_output_is_compact_and_legacy_safe(self):
        with_metrics = [self._progress_run("a", {"first_tool_seconds": 5})]
        legacy = [self._delegation_run("legacy", "completed", "accepted")]
        with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
            review._print_progress("simple-m3",
                                   review._summarize_progress_metrics(with_metrics)["simple-m3"])
            text = buffer.getvalue()
        self.assertIn("progress simple-m3:", text)
        self.assertIn("first_tool=5.0s", text)
        self.assertNotIn("legacy", text)
        self.assertEqual(review._summarize_progress_metrics(legacy), {})

    # ----- lifecycle reconciliation -----

    def test_reconcile_stale_started_without_terminal(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-01.jsonl", [
                self._started("stale1", "d1"),
                self._started("live1", "d2", deadline="2999-01-01T00:00:00Z"),
                self._run("done1", "completed", "pending", "d3"),
            ])
            report = review.reconcile(
                base, now=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc))
            self.assertEqual([item["run_id"] for item in report["stale_unconfirmed"]],
                             ["stale1"])
            self.assertEqual(report["stale_unconfirmed"][0]["deadline"],
                             "2026-01-01T00:00:00Z")
            self.assertIsNotNone(report["stale_unconfirmed"][0]["age_seconds"])
            self.assertEqual(report["runs_considered"], 1)

    def test_reconcile_pending_review_and_failed_closure(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("p1", "completed", "pending", "d-p"),
                self._run("a1", "completed", "accepted", "d-a"),
                self._run("f1", "failed", "pending", "d-f"),
                self._run("i1", "interrupted", "pending", "d-i"),
                self._run("fr", "failed", "rejected", "d-fr"),
            ])
            self._write(base / "2026-01-03.jsonl", [
                self._review("v-fr", "fr", "rejected"),
            ])
            report = review.reconcile(base)
            self.assertEqual({item["run_id"] for item in report["completed_pending_review"]},
                             {"p1"})
            self.assertEqual({item["run_id"] for item in report["failed_without_closure"]},
                             {"f1", "i1"})
            # Closure review removes a failed run from the closure list without
            # relabeling it: no run status is fabricated anywhere.
            flagged = {item["run_id"] for item in report["failed_without_closure"]}
            self.assertNotIn("fr", flagged)
            self.assertNotIn("a1", {item["run_id"]
                                    for item in report["completed_pending_review"]})

    def test_reconcile_uses_latest_review(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("r", "completed", "pending", "d"),
            ])
            self._write(base / "2026-01-03.jsonl", [
                self._review("v1", "r", "needs_rework"),
                self._review("v2", "r", "accepted"),
            ])
            report = review.reconcile(base)
            self.assertEqual(report["completed_pending_review"], [])

    def test_reconcile_legacy_runs_are_not_notification_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            legacy = {
                "schema_version": 2, "run_id": "legacy-intercom",
                "worker": {"worker_type": "simple-m3", "transport": "intercom"},
                "outcome": {"status": "completed", "review_verdict": "pending"},
            }
            self._write(base / "2026-01-02.jsonl", [
                legacy,
                self._started("new-missing", "d-missing"),
                self._run("new-missing", "completed", "pending", "d-missing",
                          worker={"worker_type": "simple-m3", "transport": "intercom"}),
                self._started("new-failed", "d-failed", deadline="2999-01-01T00:00:00Z"),
                self._run("new-failed", "completed", "pending", "d-failed",
                          worker={"worker_type": "simple-m3", "transport": "intercom"}),
                self._notification("new-failed", "d-failed", delivered=False),
            ])
            report = review.reconcile(base)
            gaps = {item["run_id"]: item for item in report["notification_gaps"]}
            self.assertEqual(set(gaps), {"new-missing", "new-failed"})
            self.assertEqual(gaps["new-missing"]["delivery"], "missing")
            self.assertEqual(gaps["new-failed"]["delivery"], "failed")
            # A legacy run predating lifecycle markers is never called a known
            # delivery failure.
            self.assertNotIn("legacy-intercom", gaps)

    def test_record_kind_test_is_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            production = self._run("prod", "completed", "pending", "d-prod")
            test = self._run("test", "completed", "pending", "d-test", record_kind="test")
            self._write(base / "2026-01-02.jsonl", [production, test])
            selected, diagnostics = review.load_with_diagnostics(base)
            self.assertEqual([item["run_id"] for item in selected], ["prod"])
            self.assertEqual(diagnostics["test_records_excluded"], 1)
            all_records, _ = review.load_with_diagnostics(base, include_tests=True)
            self.assertEqual({item["run_id"] for item in all_records}, {"prod", "test"})
            report = review.reconcile(base)
            self.assertEqual(report["runs_considered"], 1)
            self.assertEqual(report["diagnostics"]["test_records_excluded"], 1)
            full_report = review.reconcile(base, include_tests=True)
            self.assertEqual(full_report["runs_considered"], 2)

    def test_reconcile_fixture_session_is_counted_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            fixture = self._run("fixture", "completed", "pending", "d-fix",
                                worker={"worker_type": "simple-m3",
                                        "session_id": "fake-session"})
            real = self._run("real", "completed", "pending", "d-real")
            self._write(base / "2026-01-02.jsonl", [fixture, real])
            report = review.reconcile(base)
            self.assertEqual(report["suspected_fixtures"], 1)
            self.assertEqual({item["run_id"]
                              for item in report["completed_pending_review"]}, {"real"})

    def test_new_event_types_do_not_inflate_unknown_records(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            run = self._run("r", "completed", "accepted", "d")
            self._write(base / "2026-01-02.jsonl", [
                self._started("r", "d", deadline="2999-01-01T00:00:00Z"),
                run,
                self._notification("r", "d", delivered=True),
                self._review("v1", "r", "accepted"),
            ])
            records, diagnostics = review.load_with_diagnostics(base)
            self.assertEqual(diagnostics.get("unknown_records", 0), 0)
            self.assertEqual(diagnostics["orphan_reviews"], 0)
            self.assertEqual(records[0]["lifecycle"]["started_at"],
                             "2026-01-01T00:00:00Z")
            self.assertTrue(records[0]["notification"]["delivered"])
            self.assertEqual(records[0]["outcome"]["review_verdict"], "accepted")

    def test_reconcile_uses_latest_notification(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._started("r", "d", deadline="2999-01-01T00:00:00Z"),
                self._run("r", "completed", "accepted", "d",
                          worker={"worker_type": "simple-m3", "transport": "intercom"}),
                self._notification("r", "d", delivered=False),
            ])
            self._write(base / "2026-01-03.jsonl", [
                self._notification("r", "d", delivered=True),
            ])
            report = review.reconcile(base)
            self.assertEqual(report["notification_gaps"], [])
            records = review.load(base)
            self.assertTrue(records[0]["notification"]["delivered"])

    def test_reconcile_cli_wiring(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("p1", "completed", "pending", "d"),
            ])
            buffer = io.StringIO()
            previous = sys.argv
            sys.argv = ["review_worker_runs.py", "--record-dir", str(base),
                        "--reconcile", "--json"]
            try:
                with contextlib.redirect_stdout(buffer):
                    rc = review.main()
            finally:
                sys.argv = previous
        self.assertEqual(rc, 0)
        document = json.loads(buffer.getvalue())
        self.assertEqual(len(document["completed_pending_review"]), 1)
        self.assertIn("stale_unconfirmed", document)

    def test_reconcile_refuses_unsupported_run_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl",
                        [self._run("p1", "completed", "pending", "d")])
            for flag, value in (("--worker", "simple-m3"),
                                ("--status", "completed"),
                                ("--project", "sha256:abc")):
                with self.subTest(flag=flag):
                    buffer, errors = io.StringIO(), io.StringIO()
                    previous = sys.argv
                    sys.argv = ["review_worker_runs.py", "--record-dir", str(base),
                                "--reconcile", "--json", flag, value]
                    try:
                        with contextlib.redirect_stdout(buffer), \
                             contextlib.redirect_stderr(errors):
                            rc = review.main()
                    finally:
                        sys.argv = previous
                    # Lifecycle reconciliation has no run-level filters; refusing is
                    # clearer than silently ignoring the requested filter.
                    self.assertEqual(rc, 2)
                    self.assertEqual(buffer.getvalue(), "")
                    self.assertIn("does not support", errors.getvalue())
                    self.assertIn(flag, errors.getvalue())

    def test_record_kind_and_fixture_excluded_from_default_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            production = self._run("prod", "completed", "pending", "d-prod")
            tagged = self._run("tagged", "completed", "pending", "d-tagged",
                               record_kind="test")
            fixture = self._run("fixture", "completed", "pending", "d-fix",
                                worker={"worker_type": "simple-m3",
                                        "session_id": "fake-session"})
            # Detection is narrow: only the exact fixture id is excluded.
            real = self._run("real", "completed", "pending", "d-real",
                             worker={"worker_type": "simple-m3",
                                     "session_id": "fake-session-not-really"})
            self._write(base / "2026-01-02.jsonl",
                        [production, tagged, fixture, real])
            selected, diagnostics = review.load_with_diagnostics(base)
            self.assertEqual({item["run_id"] for item in selected}, {"prod", "real"})
            self.assertEqual(diagnostics["test_records_excluded"], 1)
            self.assertEqual(diagnostics["suspected_fixtures"], 1)
            included, _ = review.load_with_diagnostics(base, include_tests=True)
            self.assertEqual({item["run_id"] for item in included},
                             {"prod", "tagged", "fixture", "real"})
            report = review.reconcile(base)
            self.assertEqual(report["runs_considered"], 2)
            self.assertEqual(report["diagnostics"]["test_records_excluded"], 1)
            self.assertEqual(report["diagnostics"]["suspected_fixtures"], 1)
            self.assertEqual(report["suspected_fixtures"], 1)
            full = review.reconcile(base, include_tests=True)
            self.assertEqual(full["runs_considered"], 4)


# ----- measured main cost from review events -----

    def _metrics(self, **overrides):
        """A fully supplied main_metrics block; ``None`` means unobserved."""
        metrics = {"briefing_count": None, "review_count": None, "recovery_count": None,
                   "diff_bytes": None, "input_tokens": None, "output_tokens": None}
        for key, value in overrides.items():
            self.assertIn(key, metrics)
            metrics[key] = value
        return metrics

    def _grouped_run(self, run_id, task_group_id, *, delegation_id="d", worker_type="simple-m3",
                     status="completed", verdict="accepted", main_rework="none"):
        run = self._delegation_run(run_id, status, verdict, delegation_id=delegation_id,
                                   worker_type=worker_type)
        run["assignment"] = {"task_group_id": task_group_id}
        if main_rework != "none":
            run["outcome"]["main_rework"] = main_rework
        return run

    def test_main_cost_aggregates_every_review_event_of_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("r", "completed", "pending", "d"),
            ])
            # Superseded work was really paid for, so it is retained; the final
            # verdict still comes from the latest review.
            self._write(base / "2026-01-03.jsonl", [
                self._cost_review("v1", "r", "needs_rework",
                                  self._metrics(briefing_count=1, review_count=2,
                                                input_tokens=1000, output_tokens=200)),
                self._cost_review("v2", "r", "accepted",
                                  self._metrics(briefing_count=0, review_count=3,
                                                input_tokens=500, diff_bytes=4096)),
            ])
            records, _ = review.load_with_diagnostics(base)
            self.assertEqual(records[0]["outcome"]["review_verdict"], "accepted")
            cost = records[0]["main_cost"]
            self.assertEqual(cost["review_event_count"], 2)
            self.assertEqual(cost["observations"]["input_tokens"], 2)
            self.assertEqual(cost["totals"]["input_tokens"], 1500)
            self.assertEqual(cost["totals"]["review_count"], 5)
            self.assertEqual(cost["totals"]["diff_bytes"], 4096)
            # output_tokens was measured on the superseded review only, and that
            # real cost is kept even though the review no longer decides outcome.
            self.assertEqual(cost["totals"]["output_tokens"], 200)
            self.assertEqual(cost["observations"]["output_tokens"], 1)
            self.assertEqual(cost["observations"]["recovery_count"], 0)
            self.assertEqual(set(cost), {"review_event_count", "observations", "totals"})

    def test_main_cost_counts_review_events_independently_of_the_verdict(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("a", "completed", "pending", "d-a"),
                self._run("b", "completed", "pending", "d-b"),
            ])
            # A run reviewed twice without any measurement still reports two
            # review events and no measurements.
            self._write(base / "2026-01-03.jsonl", [
                self._cost_review("v1", "a", "needs_rework"),
                self._cost_review("v2", "a", "accepted"),
                self._cost_review("v3", "b", "rejected", self._metrics(review_count=1)),
            ])
            records, _ = review.load_with_diagnostics(base)
            self.assertEqual(records[0]["main_cost"]["review_event_count"], 2)
            self.assertEqual(records[1]["main_cost"]["review_event_count"], 1)
            summary = review.summarize(records)
            cost = summary["workers"]["simple-m3"]["main_cost"]
            self.assertEqual(cost["review_events"], 3)
            self.assertEqual(cost["runs_with_reviews"], 2)
            self.assertEqual(cost["observations"]["review_count"], 1)
            self.assertEqual(cost["totals"]["review_count"], 1)
            self.assertEqual(cost["runs_with_observations"], 1)

    def test_main_cost_rejects_duplicate_review_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [self._run("r", "completed", "pending", "d")])
            duplicate = self._cost_review("v1", "r", "accepted",
                                          self._metrics(review_count=4, input_tokens=99))
            self._write(base / "2026-01-03.jsonl", [duplicate, dict(duplicate)])
            records, _ = review.load_with_diagnostics(base)
            cost = records[0]["main_cost"]
            self.assertEqual(cost["review_event_count"], 1)
            self.assertEqual(cost["totals"]["review_count"], 4)
            self.assertEqual(cost["totals"]["input_tokens"], 99)

    def test_main_cost_invalid_and_null_values_are_unobserved(self):
        nan_value, inf_value = float("nan"), float("inf")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [self._run("r", "completed", "pending", "d")])
            self._write(base / "2026-01-03.jsonl", [
                self._cost_review("bad", "r", "accepted", {
                    "briefing_count": True, "review_count": -2, "recovery_count": 1.5,
                    "diff_bytes": nan_value, "input_tokens": inf_value,
                    "output_tokens": "1200", "extra_key": 9,
                }),
            ])
            records, _ = review.load_with_diagnostics(base)
            cost = records[0]["main_cost"]
            self.assertEqual(set(cost["observations"]),
                             {"briefing_count", "review_count", "recovery_count",
                              "diff_bytes", "input_tokens", "output_tokens"})
            self.assertEqual(set(cost["observations"].values()), {0})
            self.assertEqual(set(cost["totals"].values()), {None})
            # An integral float is still a whole-number observation.
            self._write(base / "2026-01-04.jsonl", [
                self._cost_review("ok", "r", "accepted",
                                  self._metrics(input_tokens=1500.0, output_tokens=0)),
            ])
            records, _ = review.load_with_diagnostics(base)
            cost = records[0]["main_cost"]
            self.assertEqual(cost["observations"]["input_tokens"], 1)
            self.assertEqual(cost["totals"]["input_tokens"], 1500)
            self.assertEqual(cost["totals"]["output_tokens"], 0)
            self.assertEqual(cost["review_event_count"], 2)

    def test_legacy_uninstrumented_reviews_stay_unmeasured_not_cheap(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [self._run("r", "completed", "pending", "d")])
            self._write(base / "2026-01-03.jsonl", [self._review("v1", "r", "accepted")])
            records, _ = review.load_with_diagnostics(base)
            cost = records[0]["main_cost"]
            self.assertEqual(cost["review_event_count"], 1)
            self.assertEqual(set(cost["totals"].values()), {None})
            summary = review.summarize(records)
            self.assertNotIn("progress_metrics", summary["workers"]["simple-m3"])
            self.assertEqual(summary["workers"]["simple-m3"]["main_cost"]["totals"],
                             {key: None for key in review.MAIN_COST_KEYS})
            with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
                review._print_main_cost("simple-m3", summary["workers"]["simple-m3"]["main_cost"])
                text = buffer.getvalue()
            self.assertEqual(text, "")

    def test_main_cost_worker_proxies_and_text_line(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("a", "completed", "pending", "d-a",
                          outcome={"status": "completed", "review_verdict": "pending",
                                   "main_rework": "minor"}),
                self._run("b", "failed", "pending", "d-b",
                          failure={"code": "tool_timeout", "message": "secret path /tmp/x"}),
            ])
            self._write(base / "2026-01-03.jsonl", [
                self._cost_review("v1", "a", "accepted", self._metrics(input_tokens=250)),
            ])
            records, _ = review.load_with_diagnostics(base)
            summary = review.summarize(records)
            cost = summary["workers"]["simple-m3"]["main_cost"]
            self.assertEqual(cost["main_rework_runs"], 1)
            self.assertEqual(cost["unresolved_reviews"], 1)
            self.assertEqual(cost["timeout_runs"], 1)
            with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
                review._print_main_cost("simple-m3", cost)
                text = buffer.getvalue()
            self.assertIn("main_cost simple-m3:", text)
            self.assertIn("input_tokens=250/1", text)
            self.assertIn("briefing_count=unmeasured", text)
            self.assertNotIn("secret", text)
            self.assertNotIn("/tmp/x", text)

    def test_excluded_records_never_contribute_main_cost(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("prod", "completed", "pending", "d-prod"),
                self._run("test", "completed", "pending", "d-test", record_kind="test"),
                self._run("fixture", "completed", "pending", "d-fix",
                          worker={"worker_type": "simple-m3", "session_id": "fake-session"}),
            ])
            self._write(base / "2026-01-03.jsonl", [
                self._cost_review("v-prod", "prod", "accepted", self._metrics(review_count=1)),
                self._cost_review("v-test", "test", "accepted", self._metrics(review_count=50)),
                self._cost_review("v-fix", "fixture", "accepted", self._metrics(review_count=70)),
            ])
            records, diagnostics = review.load_with_diagnostics(base)
            self.assertEqual([item["run_id"] for item in records], ["prod"])
            self.assertEqual(diagnostics["test_records_excluded"], 1)
            self.assertEqual(diagnostics["suspected_fixtures"], 1)
            cost = review.summarize(records)["workers"]["simple-m3"]["main_cost"]
            self.assertEqual(cost["review_events"], 1)
            self.assertEqual(cost["totals"]["review_count"], 1)

    def test_after_dated_reviews_attach_to_an_existing_grouped_run(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            # Runs land in January, the review events in a later file: date
            # selection must materialize every review first, then filter runs.
            self._write(base / "2026-01-02.jsonl", [
                self._run("r", "completed", "pending", "d",
                          assignment={"task_group_id": "grp-opaque-64-ascii"}),
            ])
            self._write(base / "2026-01-20.jsonl", [
                self._cost_review("v1", "r", "accepted", self._metrics(review_count=1)),
            ])
            records, _ = review.load_with_diagnostics(base, since=dt.date(2026, 1, 1),
                                                      until=dt.date(2026, 1, 3))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["main_cost"]["review_event_count"], 1)
            groups = review.summarize(records)["task_groups"]["groups"]
            self.assertEqual(set(groups), {"grp-opaque-64-ascii"})
            self.assertEqual(groups["grp-opaque-64-ascii"]["review_events"], 1)
            self.assertEqual(groups["grp-opaque-64-ascii"]["totals"]["review_count"], 1)

    # ----- task groups -----

    def test_task_groups_aggregate_across_delegation_ids_and_models(self):
        records = [
            self._grouped_run("p1", "g1", delegation_id="d1", worker_type="standard-glm"),
            self._grouped_run("p2", "g1", delegation_id="d2", worker_type="standard-glm",
                              status="failed", verdict="pending", main_rework="minor"),
            self._grouped_run("p3", "g1", delegation_id="d2", worker_type="standard-deepseek",
                              status="completed", verdict="accepted"),
            self._grouped_run("other", "g2", delegation_id="d3"),
            self._delegation_run("ungrouped", "completed", "accepted", delegation_id="d4"),
        ]
        summary = review.summarize(records)
        groups = summary["task_groups"]
        self.assertEqual(groups["group_count"], 2)
        self.assertEqual(groups["ungrouped_runs"], 1)
        group = groups["groups"]["g1"]
        self.assertEqual(group["runs"], 3)
        self.assertEqual(group["delegations"], 2)
        self.assertEqual(group["models"], 2)
        self.assertEqual(group["worker_types"], ["standard-deepseek", "standard-glm"])
        self.assertEqual(group["accepted"], 2)
        self.assertEqual(group["unresolved"], 1)
        self.assertEqual(group["reworked"], 1)
        self.assertEqual(group["launches_by_status"], {"completed": 2, "failed": 1})
        # No logical completion or first-pass claim is derived from the last run.
        for claimed in ("complete", "completed", "first_pass", "first_pass_accepted"):
            self.assertNotIn(claimed, group)
        self.assertEqual(groups["groups"]["g2"]["runs"], 1)

    def test_task_groups_ignore_inferred_group_names(self):
        for group_value in (None, "", 42, True, {"id": "g"}):
            with self.subTest(group_value=group_value):
                run = self._delegation_run("r", "completed", "accepted", delegation_id="d")
                run["assignment"] = {"task_group_id": group_value, "goal": "g1"}
                groups = review.summarize([run])["task_groups"]
                self.assertEqual(groups["groups"], {})
                self.assertEqual(groups["ungrouped_runs"], 1)
        run = self._delegation_run("plain", "completed", "accepted", delegation_id="d")
        groups = review.summarize([run])["task_groups"]
        self.assertEqual((groups["group_count"], groups["ungrouped_runs"]), (0, 1))

    def test_task_groups_accumulate_measured_cost_and_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._write(base / "2026-01-02.jsonl", [
                self._run("p1", "completed", "pending", "d1",
                          assignment={"task_group_id": "g1"}),
                self._run("p2", "completed", "pending", "d2",
                          assignment={"task_group_id": "g1"}),
            ])
            self._write(base / "2026-01-03.jsonl", [
                self._cost_review("v1", "p1", "accepted", self._metrics(diff_bytes=2048)),
                self._cost_review("v2", "p1", "needs_rework", self._metrics(diff_bytes=512)),
                self._cost_review("v3", "p2", "accepted"),
            ])
            records, _ = review.load_with_diagnostics(base)
            group = review.summarize(records)["task_groups"]["groups"]["g1"]
            self.assertEqual(group["review_events"], 3)
            self.assertEqual(group["totals"]["diff_bytes"], 2560)
            self.assertEqual(group["observations"]["diff_bytes"], 2)
            self.assertEqual(group["runs_with_observations"], 1)
            self.assertEqual(group["runs"], 2)

    def test_task_group_text_is_bounded_and_privacy_safe(self):
        records = [self._grouped_run("p1", "grp-opaque-64-ascii",
                                     main_rework="minor")]
        records[0]["failure"] = {"code": "timeout", "message": "raw /private/tmp/secret"}
        groups = review.summarize(records)["task_groups"]
        with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
            review._print_task_groups(groups)
            text = buffer.getvalue()
        self.assertIn("task_group grp-opaque-64-ascii:", text)
        self.assertIn("runs=1", text)
        self.assertIn("timeouts=1", text)
        self.assertNotIn("/private/tmp/secret", text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_task_groups_report_ungrouped_runs_only(self):
        with io.StringIO() as buffer, contextlib.redirect_stdout(buffer):
            review._print_task_groups(review.summarize(
                [self._delegation_run("r", "completed", "accepted")])["task_groups"])
            text = buffer.getvalue()
        self.assertIn("ungrouped_runs=1", text)


if __name__ == "__main__":
    unittest.main()
