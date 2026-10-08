"""Focused tests for prompt-relative phase telemetry (no real providers)."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_pi_worker.py"
FAKE = ROOT / "tests" / "fake_pi.py"
spec = importlib.util.spec_from_file_location("worker_progress", ROOT / "scripts" / "worker_progress.py")
progress = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(progress)

ProgressObserver = progress.ProgressObserver
METRIC_FIELDS = set(progress.METRIC_FIELDS)
CheckpointPolicy = progress.CheckpointPolicy
CHECKPOINT_CODES = progress.CHECKPOINT_CODES
CHECKPOINT_TRIGGERS = progress.CHECKPOINT_TRIGGERS
checkpoint_thresholds = progress.checkpoint_thresholds
checkpoint_message = progress.checkpoint_message
time_budget_reminder = progress.time_budget_reminder

runner_spec = importlib.util.spec_from_file_location("run_pi_worker_module", RUNNER)
runner = importlib.util.module_from_spec(runner_spec)
assert runner_spec.loader
runner_spec.loader.exec_module(runner)
run_rpc = runner.run_rpc
RunnerError = runner.RunnerError


class FakeClock:
    """Deterministic injectable clock."""

    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def tool_start(call_id, name="bash", args=None):
    message = {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name}
    if args is not None:
        message["args"] = args
    return message


def tool_end(call_id, name="bash", is_error=False, include_status=True):
    message = {"type": "tool_execution_end", "toolCallId": call_id, "toolName": name,
               "result": "SECRET-RESULT"}
    if include_status:
        message["isError"] = is_error
    return message


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.observer = ProgressObserver(clock=self.clock)
        self.observer.start()

    def test_initial_observations_are_null(self):
        snapshot = self.observer.snapshot()
        self.assertEqual(set(snapshot), METRIC_FIELDS)
        for field in ("first_tool_seconds", "first_edit_seconds",
                      "first_test_candidate_seconds", "last_activity_seconds",
                      "last_tool_completion_seconds"):
            self.assertIsNone(snapshot[field], field)
        # Observed totals of zero are allowed once tracking started.
        self.assertEqual(snapshot["tool_seconds"], 0.0)
        self.assertEqual(snapshot["retry_seconds"], 0.0)
        self.assertEqual(snapshot["retry_count"], 0)
        self.assertEqual(snapshot["checkpoint_notices"], [])

    def test_start_end_pairs_duration_and_last_completion(self):
        self.observer.observe(tool_start("c1", "read"))
        self.clock.advance(3.0)
        self.observer.observe(tool_end("c1", "read"))
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["first_tool_seconds"], 0.0)
        self.assertEqual(snapshot["last_tool_completion_seconds"], 3.0)
        self.assertEqual(snapshot["tool_seconds"], 3.0)
        self.assertIsNone(snapshot["first_edit_seconds"])

    def test_running_tool_duration_included_and_monotonic(self):
        self.observer.observe(tool_start("c1"))
        self.clock.advance(2.0)
        self.assertEqual(self.observer.snapshot()["tool_seconds"], 2.0)
        self.clock.advance(1.0)
        self.assertEqual(self.observer.snapshot()["tool_seconds"], 3.0)
        self.observer.observe(tool_end("c1"))
        self.assertEqual(self.observer.snapshot()["tool_seconds"], 3.0)

    def test_concurrent_tool_durations_aggregate_and_overlap(self):
        self.observer.observe(tool_start("c1"))
        self.observer.observe(tool_start("c2"))
        self.clock.advance(5.0)
        snapshot = self.observer.snapshot()
        # Two overlapping 5s spans: busy-time aggregate, not wall clock.
        self.assertEqual(snapshot["tool_seconds"], 10.0)
        self.assertEqual(snapshot["last_activity_seconds"], 0.0)

    def test_successful_edit_sets_first_edit(self):
        self.observer.observe(tool_start("w1", "write", {"path": "/x/y.txt"}))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("w1", "write"))
        self.assertEqual(self.observer.snapshot()["first_edit_seconds"], 1.0)

    def test_failed_edit_does_not_set_first_edit_but_later_success_does(self):
        self.observer.observe(tool_start("e1", "edit"))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("e1", "edit", is_error=True))
        snapshot = self.observer.snapshot()
        self.assertIsNone(snapshot["first_edit_seconds"])
        self.assertEqual(snapshot["last_tool_completion_seconds"], 1.0)
        self.clock.advance(1.0)
        self.observer.observe(tool_start("e2", "edit"))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("e2", "edit"))
        self.assertEqual(self.observer.snapshot()["first_edit_seconds"], 3.0)

    def test_bash_write_is_never_inferred_as_an_edit(self):
        self.observer.observe(tool_start("b1", "bash", {"command": "echo hi > /x/y.txt"}))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("b1", "bash"))
        self.assertIsNone(self.observer.snapshot()["first_edit_seconds"])

    def test_test_candidate_heuristic(self):
        for index, command in enumerate((
                "python3.11 -m pytest -q tests/test_a.py",
                "npm run test -- --watch=false",
                "cargo test --lib",
                "go test ./...")):
            self.clock.advance(1.0)
            self.observer.observe(tool_start(f"t{index}", "bash", {"command": command}))
            # Only the first candidate is recorded; later ones never move it.
            self.assertEqual(self.observer.snapshot()["first_test_candidate_seconds"],
                             1.0, command)
        # A candidate is a start observation, not a passing test result.
        self.assertIsNone(self.observer.snapshot()["first_edit_seconds"])

    def test_non_test_commands_are_not_candidates(self):
        self.assertFalse(progress.looks_like_test_command("ls -la"))
        self.assertFalse(progress.looks_like_test_command("echo 'latest' > notes.md"))
        self.assertFalse(progress.looks_like_test_command(None))
        self.assertFalse(progress.looks_like_test_command({"command": 5}))
        self.observer.observe(tool_start("n1", "bash", {"command": "npm run build"}))
        self.assertIsNone(self.observer.snapshot()["first_test_candidate_seconds"])

    def test_retry_count_and_cumulative_seconds(self):
        self.observer.observe({"type": "auto_retry_start", "attempt": 1})
        self.clock.advance(4.0)
        self.observer.observe({"type": "auto_retry_end", "success": True, "attempt": 2})
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 1)
        self.assertEqual(snapshot["retry_seconds"], 4.0)
        self.observer.observe({"type": "auto_retry_start", "attempt": 1})
        self.clock.advance(6.0)
        # The second retry is still in flight, so the snapshot grows with it.
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 2)
        self.assertEqual(snapshot["retry_seconds"], 10.0)
        self.observer.observe({"type": "auto_retry_end", "success": True, "attempt": 2})
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 2)
        self.assertEqual(snapshot["retry_seconds"], 10.0)

    def test_unmatched_retry_end_is_ignored(self):
        self.observer.observe({"type": "auto_retry_end", "success": False})
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 0)
        self.assertEqual(snapshot["retry_seconds"], 0.0)

    def test_inflight_retry_included_at_snapshot(self):
        self.observer.observe({"type": "auto_retry_start", "attempt": 1})
        self.clock.advance(7.0)
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 1)
        self.assertEqual(snapshot["retry_seconds"], 7.0)

    def test_edit_without_explicit_success_status_is_not_an_edit(self):
        # Regression: an absent (or malformed) isError is not success evidence.
        self.observer.observe(tool_start("e1", "edit"))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("e1", "edit", include_status=False))
        snapshot = self.observer.snapshot()
        self.assertIsNone(snapshot["first_edit_seconds"])
        self.assertEqual(snapshot["last_tool_completion_seconds"], 1.0)
        self.observer.observe(tool_start("e2", "write"))
        self.clock.advance(1.0)
        self.observer.observe({"type": "tool_execution_end", "toolCallId": "e2",
                               "toolName": "write", "isError": "false",
                               "result": "SECRET-RESULT"})
        self.assertIsNone(self.observer.snapshot()["first_edit_seconds"])
        self.clock.advance(1.0)
        self.observer.observe(tool_start("e3", "write"))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("e3", "write", is_error=False))
        self.assertEqual(self.observer.snapshot()["first_edit_seconds"], 4.0)

    def test_only_agent_turn_retries_are_counted(self):
        # Compaction retries are excluded so a future retry budget is not
        # charged for time a model retry budget must never spend.
        self.observer.observe({"type": "summarization_retry_scheduled", "attempt": 1})
        self.observer.observe({"type": "summarization_retry_attempt_start", "source": "compaction"})
        self.clock.advance(3.0)
        self.observer.observe({"type": "summarization_retry_finished"})
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 0)
        self.assertEqual(snapshot["retry_seconds"], 0.0)
        self.observer.observe({"type": "auto_retry_start", "attempt": 1})
        self.clock.advance(2.0)
        self.observer.observe({"type": "auto_retry_end", "success": True, "attempt": 2})
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["retry_count"], 1)
        self.assertEqual(snapshot["retry_seconds"], 2.0)

    def test_malformed_records_are_ignored_safely(self):
        for record in (None, [], "text", 5, {}, {"type": None},
                       {"type": "tool_execution_start"},
                       {"type": "tool_execution_start", "toolCallId": None, "toolName": "bash"},
                       {"type": "tool_execution_start", "toolCallId": "c", "toolName": 7},
                       {"type": "tool_execution_end"},
                       {"type": "tool_execution_end", "toolCallId": "unknown", "toolName": "edit"}):
            snapshot = self.observer.observe(record)
            self.assertEqual(set(snapshot), METRIC_FIELDS)
        snapshot = self.observer.snapshot()
        self.assertIsNone(snapshot["first_tool_seconds"])
        self.assertIsNone(snapshot["first_edit_seconds"])
        self.assertEqual(snapshot["tool_seconds"], 0.0)

    def test_unmatched_end_then_later_valid_span(self):
        self.observer.observe(tool_end("ghost", "edit"))
        self.assertIsNone(self.observer.snapshot()["first_edit_seconds"])
        self.observer.observe(tool_start("real", "edit"))
        self.clock.advance(2.0)
        self.observer.observe(tool_end("real", "edit"))
        snapshot = self.observer.snapshot()
        self.assertEqual(snapshot["first_tool_seconds"], 0.0)
        self.assertEqual(snapshot["first_edit_seconds"], 2.0)

    def test_values_are_non_negative_and_snapshots_are_detached(self):
        observer = ProgressObserver(clock=lambda: 500.0)
        observer.start()
        observer.observe(tool_start("c1"))
        snapshot = observer.snapshot()
        self.assertEqual(snapshot["tool_seconds"], 0.0)
        for field in METRIC_FIELDS:
            value = snapshot[field]
            if isinstance(value, float):
                self.assertGreaterEqual(value, 0.0, field)
        snapshot["checkpoint_notices"].append("tampered")
        self.assertEqual(observer.snapshot()["checkpoint_notices"], [])

    def test_checkpoint_notices_stay_empty(self):
        self.observer.observe({"type": "checkpoint", "code": "investigation_checkpoint"})
        self.assertEqual(self.observer.snapshot()["checkpoint_notices"], [])

    def test_no_payload_leaks_into_snapshot_or_state(self):
        self.observer.observe(tool_start("c1", "bash", {"command": "curl SECRET-URL"}))
        self.clock.advance(1.0)
        self.observer.observe(tool_end("c1", "bash", is_error=True))
        self.observer.observe({"type": "auto_retry_start", "errorMessage": "SECRET-PROVIDER"})
        snapshot = self.observer.snapshot()
        blob = json.dumps(snapshot, sort_keys=True)
        for secret in ("SECRET", "curl", "python", "provider"):
            self.assertNotIn(secret.lower(), blob.lower())
        # Internal pending state holds ids, offsets, and a classification only.
        for call_id, (start, classification) in self.observer._pending.items():
            self.assertIsInstance(call_id, str)
            self.assertIsInstance(start, float)
            self.assertIn(classification, ("tool", "test"))


class RetryBoundTests(unittest.TestCase):
    """The automatic-retry bounds read real observed spans, deterministically."""

    def setUp(self):
        self.clock = FakeClock()
        self.observer = ProgressObserver(clock=self.clock)
        self.observer.start()

    def start_retry(self, attempt=1):
        self.observer.observe({"type": "auto_retry_start", "attempt": attempt,
                               "maxAttempts": 3, "delayMs": 2000,
                               "errorMessage": "SECRET-PROVIDER-ERROR"})
        # The payload is classified away: only the start offset is kept.
        self.assertNotIn("SECRET-PROVIDER-ERROR", json.dumps(self.observer.snapshot()))

    def test_retry_count_is_a_run_total(self):
        self.assertEqual(self.observer.retry_count, 0)
        self.start_retry(1)
        self.start_retry(2)
        self.assertEqual(self.observer.retry_count, 2)

    def test_wake_is_none_until_a_retry_is_observed(self):
        self.assertIsNone(self.observer.retry_budget_wake_after(30))
        self.assertFalse(self.observer.retry_budget_exhausted(30))
        self.clock.advance(100.0)
        # A run that never retries is never woken for a budget it cannot spend.
        self.assertIsNone(self.observer.retry_budget_wake_after(30))
        self.assertFalse(self.observer.retry_budget_exhausted(30))

    def test_zero_or_unusable_budget_disables_the_time_bound(self):
        self.start_retry()
        for disabled in (0, -1, float("nan"), float("inf"), "abc", None, True):
            self.assertIsNone(self.observer.retry_budget_wake_after(disabled), disabled)
            self.assertFalse(self.observer.retry_budget_exhausted(disabled), disabled)
        self.clock.advance(10_000.0)
        # 0 disables the budget only: the count is still observable and exact.
        self.assertEqual(self.observer.retry_count, 1)
        self.assertGreater(self.observer.retry_seconds_now(), 0.0)

    def test_wake_counts_down_the_open_span_and_does_not_reset(self):
        self.start_retry()
        self.assertEqual(self.observer.retry_budget_wake_after(30), 30.0)
        self.clock.advance(10.0)
        self.assertEqual(self.observer.retry_budget_wake_after(30), 20.0)
        self.assertEqual(self.observer.retry_seconds_now(), 10.0)
        # Unrelated streaming traffic cannot move the open retry's start.
        for _ in range(3):
            self.observer.observe({"type": "message_update",
                                   "message": {"role": "assistant",
                                               "content": "SECRET-STREAM"}})
            self.clock.advance(1.0)
        self.assertEqual(self.observer.retry_budget_wake_after(30), 17.0)
        self.assertEqual(self.observer.retry_seconds_now(), 13.0)
        self.assertFalse(self.observer.retry_budget_exhausted(30))

    def test_budget_trips_on_the_observed_total_not_a_single_retry(self):
        self.start_retry(1)
        self.clock.advance(6.0)
        self.observer.observe({"type": "auto_retry_end", "success": True, "attempt": 2})
        self.assertFalse(self.observer.retry_budget_exhausted(10))
        # A second, overlapping retry keeps accumulating the same aggregate total.
        self.start_retry(2)
        self.clock.advance(2.0)
        self.assertEqual(self.observer.retry_seconds_now(), 8.0)
        self.assertEqual(self.observer.retry_budget_wake_after(10), 2.0)
        self.clock.advance(2.0)
        self.assertTrue(self.observer.retry_budget_exhausted(10))
        self.assertEqual(self.observer.retry_budget_wake_after(10), 0.0)

    def test_completed_retry_seconds_survive_a_new_span(self):
        self.start_retry()
        self.clock.advance(4.0)
        self.observer.observe({"type": "auto_retry_end", "success": False, "attempt": 1})
        self.assertEqual(self.observer.retry_seconds_now(), 4.0)
        self.assertEqual(self.observer.retry_budget_wake_after(30), 26.0)
        self.assertFalse(self.observer.retry_budget_exhausted(4.5))
        # The bound is inclusive: a total exactly at the budget is spent.
        self.assertTrue(self.observer.retry_budget_exhausted(4))
        # A closed span is not double counted after the fact.
        self.clock.advance(5.0)
        self.assertEqual(self.observer.retry_seconds_now(), 4.0)


class CheckpointPolicyTests(unittest.TestCase):
    """Deterministic triggers from an injected clock; no run and no waiting."""

    def setUp(self):
        self.clock = FakeClock()

    def test_thresholds_are_the_documented_floor_or_fraction(self):
        # A short task cannot be checkpointed every few seconds; a long one is
        # not checkpointed at a flat absolute 180 s.
        self.assertEqual(checkpoint_thresholds(900),
                         {"investigation_checkpoint": 180.0,
                          "first_delivery_checkpoint": 315.0,
                          "wrap_up_checkpoint": 630.0})
        self.assertEqual(checkpoint_thresholds(1800)["investigation_checkpoint"], 360.0)
        self.assertEqual(checkpoint_thresholds(1800)["first_delivery_checkpoint"], 630.0)
        self.assertEqual(checkpoint_thresholds(60)["investigation_checkpoint"], 180.0)
        self.assertEqual(checkpoint_thresholds(60)["first_delivery_checkpoint"], 300.0)
        self.assertEqual(checkpoint_thresholds(60)["wrap_up_checkpoint"], 42.0)

    def test_degenerate_budgets_never_raise(self):
        for budget in (None, float("nan"), float("inf"), -5, "abc", True, [1]):
            self.assertEqual(checkpoint_thresholds(budget),
                             {"investigation_checkpoint": 180.0,
                              "first_delivery_checkpoint": 300.0,
                              "wrap_up_checkpoint": 0.0}, budget)

    def test_each_checkpoint_fires_exactly_once_in_canonical_order(self):
        policy = CheckpointPolicy(900, clock=self.clock).start()
        self.assertIsNone(policy.next_code())
        self.assertEqual(policy.sent, ())
        for expected in CHECKPOINT_CODES:
            threshold = policy.thresholds[expected]
            self.clock.advance(threshold - policy.elapsed())
            self.assertEqual(policy.next_code(), expected)
            self.assertTrue(policy.mark_sent(expected))
            # A duplicate send is refused, so one slow run cannot repeat a notice.
            self.assertFalse(policy.mark_sent(expected))
            self.assertIsNone(policy.next_code())
            self.assertEqual(policy.sent, tuple(CHECKPOINT_CODES[:CHECKPOINT_CODES.index(expected) + 1]))
        self.assertIsNone(policy.next_due_after())
        self.assertEqual(policy.sent, CHECKPOINT_CODES)

    def test_a_late_start_emits_three_ordered_notices_not_one_batch(self):
        policy = CheckpointPolicy(900, clock=self.clock).start()
        self.clock.advance(1000.0)
        # All three are due at once, but one is proposed per call, in order.
        proposed = []
        while True:
            code = policy.next_code()
            if code is None:
                break
            proposed.append(code)
            policy.mark_sent(code)
        self.assertEqual(proposed, list(CHECKPOINT_CODES))

    def test_next_due_after_counts_down_to_the_next_unsent_code(self):
        policy = CheckpointPolicy(900, clock=self.clock).start()
        self.assertEqual(policy.next_due_after(), 180.0)
        self.clock.advance(180.0)
        # A due-but-unsent code reports zero rather than skipping ahead.
        self.assertEqual(policy.next_due_after(), 0.0)
        policy.mark_sent("investigation_checkpoint")
        self.assertEqual(policy.next_due_after(), 135.0)
        policy.mark_sent("first_delivery_checkpoint")
        self.assertEqual(policy.next_due_after(), 630.0 - 180.0)
        # An unstarted policy never claims a due time.
        self.assertIsNone(CheckpointPolicy(900, clock=self.clock).next_due_after())

    def test_mark_sent_rejects_unknown_and_non_string_codes(self):
        policy = CheckpointPolicy(900, clock=self.clock).start()
        for code in ("not_a_checkpoint", "", None, 7, ["wrap_up_checkpoint"]):
            self.assertFalse(policy.mark_sent(code), code)
        self.assertEqual(policy.sent, ())

    def test_thresholds_property_is_a_copy(self):
        policy = CheckpointPolicy(900, clock=self.clock).start()
        thresholds = policy.thresholds
        thresholds["wrap_up_checkpoint"] = -1
        self.assertEqual(policy.thresholds["wrap_up_checkpoint"], 630.0)

    def test_short_budget_only_reaches_its_own_earliest_checkpoint(self):
        # A 3s task cannot reach the 180 s / 300 s floors, so only wrap-up is due.
        policy = CheckpointPolicy(3, clock=self.clock).start()
        self.clock.advance(2.1)
        self.assertEqual(policy.next_code(), "wrap_up_checkpoint")
        policy.mark_sent("wrap_up_checkpoint")
        self.assertIsNone(policy.next_code())
        # Nothing else is due; the next reported time is the unreachable floor.
        self.assertAlmostEqual(policy.next_due_after(), 177.9)


class CheckpointMessageTests(unittest.TestCase):
    """Each code carries the ask the assignment documents, mode-aware."""

    def test_investigation_asks_for_findings_blocker_and_bounded_next_step(self):
        for write_task in (True, False):
            message = checkpoint_message("investigation_checkpoint", write_task=write_task)
            self.assertIn("what you found so far", message)
            self.assertIn("blocks you", message)
            self.assertIn("one bounded next step", message)
            self.assertIn("not a stop", message)

    def test_first_delivery_asks_write_tasks_to_save_and_test(self):
        message = checkpoint_message("first_delivery_checkpoint", write_task=True)
        self.assertIn("save", message)
        self.assertIn("Write the scoped changes to disk", message)
        self.assertIn("run the focused test", message)

    def test_first_delivery_asks_read_only_tasks_to_summarize_and_not_edit(self):
        message = checkpoint_message("first_delivery_checkpoint", write_task=False)
        self.assertIn("read-only", message)
        self.assertIn("Summarize your findings", message)
        self.assertIn("do not edit or create files", message)
        # A read-only task must never be told to save a diff it cannot produce.
        self.assertNotIn("Write the scoped changes", message)
        self.assertNotIn("run the focused test", message)

    def test_wrap_up_asks_to_stop_scope_expansion_and_save_verify_report(self):
        write_task = checkpoint_message("wrap_up_checkpoint", write_task=True)
        for needle in ("stop expanding scope", "Save any artifact", "run the check",
                       "still incomplete or unverified"):
            self.assertIn(needle, write_task)
        read_only = checkpoint_message("wrap_up_checkpoint", write_task=False)
        self.assertIn("stop expanding scope", read_only)
        self.assertIn("do not edit or create files", read_only)
        self.assertIn("unverified", read_only)
        self.assertNotIn("Save any artifact", read_only)

    def test_unknown_code_yields_no_text(self):
        for code in (None, "", "arbitrary instruction", 7):
            self.assertEqual(checkpoint_message(code), "")

    def test_no_message_promises_an_interrupt_or_a_kill(self):
        # docs/rpc-commands.md: a steer is queued after the current turn's tool
        # calls finish. The text must not claim it interrupts a running tool.
        for code in CHECKPOINT_CODES:
            for write_task in (True, False):
                message = checkpoint_message(code, write_task=write_task)
                lowered = message.lower()
                self.assertNotIn("interrupt", lowered)
                self.assertNotIn("immediately", lowered)
                self.assertIn("not a stop", lowered)


class TimeBudgetReminderTests(unittest.TestCase):
    def test_reminder_states_budget_checkpoints_and_queued_delivery(self):
        text = time_budget_reminder(budget_seconds=900, tool_timeout_seconds=180)
        self.assertIn("900s budget", text)
        for threshold in ("180s", "315s", "630s"):
            self.assertIn(threshold, text)
        self.assertIn("queued", text)
        self.assertIn("after the tool calls you are already running", text)
        self.assertIn("never interrupts a tool", text)
        self.assertIn("Save scoped work to disk", text)
        self.assertIn("PER-TOOL LIMIT", text)
        self.assertIn("180s", text)

    def test_disabled_tool_limit_is_not_advertised(self):
        for value in (0, 0.0, None, -1, float("nan"), "abc"):
            text = time_budget_reminder(budget_seconds=900, tool_timeout_seconds=value)
            self.assertNotIn("PER-TOOL LIMIT", text, value)

    def test_read_only_reminder_forbids_editing(self):
        text = time_budget_reminder(budget_seconds=900, tool_timeout_seconds=180,
                                    write_task=False)
        self.assertIn("read-only: do not edit or create files", text)
        self.assertNotIn("Save scoped work to disk", text)

    def test_degenerate_budget_still_renders(self):
        for budget in (None, float("nan"), -3, "abc"):
            text = time_budget_reminder(budget_seconds=budget, tool_timeout_seconds=None)
            self.assertIn("DELIVERY CHECKPOINTS", text)
            self.assertIn("0s budget", text)


class ToolDeadlineTests(unittest.TestCase):
    """The per-running-tool bound is derived from tracked starts, not activity."""

    def setUp(self):
        self.clock = FakeClock()
        self.observer = ProgressObserver(clock=self.clock)
        self.observer.start()

    def test_no_running_tool_has_no_deadline(self):
        self.assertIsNone(self.observer.oldest_live_tool_start_seconds())
        self.assertIsNone(self.observer.tool_timeout_wake_after(1))
        self.observer.observe(tool_start("c1"))
        self.observer.observe(tool_end("c1"))
        self.assertIsNone(self.observer.oldest_live_tool_start_seconds())
        self.assertIsNone(self.observer.tool_timeout_wake_after(1))

    def test_partial_updates_and_model_traffic_never_reset_the_timer(self):
        self.observer.observe(tool_start("c1"))
        self.clock.advance(1.0)
        self.assertEqual(self.observer.tool_timeout_wake_after(3), 2.0)
        for _ in range(5):
            self.observer.observe({"type": "tool_execution_update", "toolCallId": "c1",
                                   "toolName": "bash", "args": {"command": "SECRET"},
                                   "partialResult": "SECRET-PARTIAL"})
            self.clock.advance(0.5)
            self.observer.observe({"type": "message_update", "usage": {"input": 1}})
            # The stored start never moves, whatever the stream carries.
            self.assertEqual(self.observer.oldest_live_tool_start_seconds(), 0.0)
        # The bound is due once the oldest start reaches the limit, and never resets.
        self.assertEqual(self.observer.tool_timeout_wake_after(3), 0.0)
        self.clock.advance(1.0)
        self.assertEqual(self.observer.tool_timeout_wake_after(3), 0.0)

    def test_concurrent_tools_are_tracked_independently(self):
        self.observer.observe(tool_start("c1"))
        self.clock.advance(2.0)
        self.observer.observe(tool_start("c2"))
        self.clock.advance(1.0)
        # Each live call keeps its own start: c2 started 2s after c1.
        self.assertEqual(self.observer.oldest_live_tool_start_seconds(), 0.0)
        # The oldest live start decides the bound.
        self.assertEqual(self.observer.tool_timeout_wake_after(4), 1.0)
        # Ending only the younger call leaves the older one on the clock.
        self.observer.observe(tool_end("c2"))
        self.assertEqual(self.observer.oldest_live_tool_start_seconds(), 0.0)
        self.observer.observe(tool_end("c1"))
        self.assertIsNone(self.observer.oldest_live_tool_start_seconds())

    def test_a_missing_end_keeps_the_tool_live(self):
        self.observer.observe(tool_start("c1"))
        self.clock.advance(10.0)
        # No tool_execution_end ever arrived: the bound must still fire.
        self.assertEqual(self.observer.tool_timeout_wake_after(5), 0.0)
        self.assertEqual(self.observer.oldest_live_tool_start_seconds(), 0.0)
        # An end event for an unknown call never clears a live one.
        self.observer.observe(tool_end("ghost"))
        self.assertEqual(self.observer.oldest_live_tool_start_seconds(), 0.0)

    def test_unknown_or_malformed_starts_are_ignored_safely(self):
        for message in ({"type": "tool_execution_start"},
                        {"type": "tool_execution_start", "toolCallId": "", "toolName": "bash"},
                        {"type": "tool_execution_start", "toolCallId": 7, "toolName": "bash"},
                        {"type": "tool_execution_start", "toolCallId": "x", "toolName": 7}):
            self.clock.advance(1.0)
            self.observer.observe(message)
            self.assertIsNone(self.observer.oldest_live_tool_start_seconds())
            self.assertIsNone(self.observer.tool_timeout_wake_after(1))

    def test_disabled_or_unusable_limits_never_produce_a_deadline(self):
        self.observer.observe(tool_start("c1"))
        for value in (0, 0.0, -1, float("nan"), float("inf"), None, "abc", True):
            self.assertIsNone(self.observer.tool_timeout_wake_after(value), value)

    def test_deadline_helper_is_payload_free(self):
        self.observer.observe(tool_start("c1", "bash", {"command": "curl SECRET-URL"}))
        self.clock.advance(0.5)
        blob = json.dumps({"oldest": self.observer.oldest_live_tool_start_seconds(),
                           "wake": self.observer.tool_timeout_wake_after(10)})
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("curl", blob)


class CheckpointNoticeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.observer = ProgressObserver(clock=self.clock)
        self.observer.start()

    def test_notices_are_registered_only_for_known_codes_once_each(self):
        self.assertEqual(self.observer.snapshot()["checkpoint_notices"], [])
        self.assertTrue(self.observer.record_checkpoint("investigation_checkpoint"))
        self.assertFalse(self.observer.record_checkpoint("investigation_checkpoint"))
        self.assertFalse(self.observer.record_checkpoint("not_a_code"))
        self.assertFalse(self.observer.record_checkpoint(None))
        self.assertEqual(self.observer.snapshot()["checkpoint_notices"],
                         ["investigation_checkpoint"])

    def test_snapshot_returns_a_detached_notice_list(self):
        self.observer.record_checkpoint("wrap_up_checkpoint")
        snapshot = self.observer.snapshot()
        snapshot["checkpoint_notices"].append("tampered")
        self.assertEqual(self.observer.snapshot()["checkpoint_notices"],
                         ["wrap_up_checkpoint"])

    def test_a_raw_checkpoint_event_is_not_a_notice(self):
        # Only a real steer send registers a code; an inbound event never does.
        self.observer.observe({"type": "checkpoint", "code": "investigation_checkpoint"})
        self.assertEqual(self.observer.snapshot()["checkpoint_notices"], [])

    def test_no_first_edit_or_quiet_thinking_is_ever_a_trigger(self):
        # Reading, thinking, and a long silent stretch change nothing: the
        # observer has no code path that turns them into a notice or a failure.
        self.observer.observe({"type": "message_update", "assistantMessageEvent":
                               {"type": "thinking_delta", "delta": "SECRET-THOUGHT"}})
        for index in range(5):
            self.clock.advance(60.0)
            self.observer.observe(tool_start(f"r{index}", "read"))
            self.observer.observe(tool_end(f"r{index}", "read"))
        snapshot = self.observer.snapshot()
        self.assertIsNone(snapshot["first_edit_seconds"])
        self.assertEqual(snapshot["checkpoint_notices"], [])
        self.assertNotIn("SECRET", json.dumps(snapshot))


class RunRpcIntegrationTests(unittest.TestCase):
    """Targeted runner integration: metrics reach the receipt on both outcomes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.records = self.base / "records"
        self.work = self.base / "work"
        self.work.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            "PI_WORKER_RECORD_DIR": str(self.records), "PI_WORKER_PI": str(FAKE),
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test",
            "MINIMAX_CN_API_KEY": "test", "FAKE_PI_TOOL_FLOW": "1",
        })

    def run_worker(self, mode="ok", extra=None):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", "write", "--delegation-id", "metrics-1",
               "--timeout", "20"]
        if extra:
            cmd.extend(extra)
        return subprocess.run(cmd, env=self.env | {"FAKE_PI_MODE": mode},
                              text=True, capture_output=True)

    def receipt_for(self, delegation="metrics-1"):
        items = []
        for path in self.records.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                item = json.loads(line)
                if item.get("record_type") not in ("lifecycle", "notification"):
                    items.append(item)
        self.assertEqual(len(items), 1)
        return items[0]

    def test_success_receipt_carries_phase_metrics(self):
        result = self.run_worker("ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        metrics = receipt["progress_metrics"]
        self.assertEqual(set(metrics), METRIC_FIELDS)
        # Observed tool flow: bash test candidate, write edit, one retry.
        self.assertIsNotNone(metrics["first_tool_seconds"])
        self.assertIsNotNone(metrics["first_edit_seconds"])
        self.assertIsNotNone(metrics["first_test_candidate_seconds"])
        self.assertIsNotNone(metrics["last_tool_completion_seconds"])
        self.assertIsNotNone(metrics["last_activity_seconds"])
        self.assertGreaterEqual(metrics["tool_seconds"], 0.0)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertGreaterEqual(metrics["retry_seconds"], 0.0)
        self.assertEqual(metrics["checkpoint_notices"], [])
        # Stage order is preserved even though durations are coarse.
        self.assertLessEqual(metrics["first_tool_seconds"], metrics["first_edit_seconds"])
        # The ledger copy matches, and existing event counts are unchanged.
        self.assertEqual(self.receipt_for()["progress_metrics"], metrics)
        self.assertEqual(receipt["rpc_event_counts"]["tool_execution_start"], 2)

    def test_failure_receipt_carries_phase_metrics(self):
        result = self.run_worker("provider-error")
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(receipt["outcome"]["status"], "failed")
        metrics = receipt["progress_metrics"]
        self.assertEqual(set(metrics), METRIC_FIELDS)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertIsNotNone(metrics["first_test_candidate_seconds"])
        self.assertIsNotNone(metrics["first_edit_seconds"])

    def test_no_secrets_in_receipt_or_checkpoint(self):
        result = self.run_worker("ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        blobs = [result.stdout, result.stderr]
        for path in self.records.glob("*.jsonl"):
            blobs.append(path.read_text())
        for blob in blobs:
            for secret in ("SECRET-URL", "SECRET-RESULT", "SECRET-FILE-CONTENT",
                           "SECRET-PROVIDER", "SECRET-BASH-OUTPUT",
                           "/private/secret/dir", "pytest -q"):
                self.assertNotIn(secret, blob)

    def test_failure_before_prompt_carries_no_metrics(self):
        # Argument validation rejects this launch before any prompt, so no
        # terminal record (and no metrics block) is produced at all.
        result = self.run_worker("ok", extra=["--transport", "rpc", "--intercom-cli", "/nope"])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("progress_metrics", result.stderr)
        for path in self.records.glob("*.jsonl"):
            for line in path.read_text().splitlines():
                self.assertNotIn("progress_metrics", line)


class ToolTimeoutIntegrationTests(unittest.TestCase):
    """Per-running-tool bound end to end, with a very short CLI override."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.records = self.base / "records"
        self.work = self.base / "work"
        self.work.mkdir()
        self.abort_marker = self.base / "aborted"
        self.steer_log = self.base / "steer.jsonl"
        self.prompt_file = self.base / "prompt.jsonl"
        self.env = os.environ.copy()
        self.env.update({
            "PI_WORKER_RECORD_DIR": str(self.records), "PI_WORKER_PI": str(FAKE),
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test",
            "MINIMAX_CN_API_KEY": "test", "FAKE_PI_MODE": "ok",
            "FAKE_PI_HANG_TOOL": "stream", "FAKE_PI_HANG_SECONDS": "30",
            "FAKE_ABORT_MARKER": str(self.abort_marker),
            "FAKE_PI_STEER_LOG": str(self.steer_log),
            "FAKE_PI_PROMPT_FILE": str(self.prompt_file),
        })

    def run_worker(self, *, extra=None, tools="write", delegation="tool-1"):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", tools, "--delegation-id", delegation, "--timeout", "15"]
        if extra:
            cmd.extend(extra)
        return subprocess.run(cmd, env=self.env, text=True, capture_output=True)

    def test_streaming_tool_that_never_ends_times_out_and_fails_the_run(self):
        result = self.run_worker(extra=["--tool-timeout-seconds", "1"])
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(receipt["failure"]["code"], "tool_timeout")
        self.assertEqual(receipt["failure"]["stage"], "rpc")
        # Terminal FAILED, never accepted, and the main agent keeps the authority.
        self.assertEqual(receipt["outcome"]["status"], "failed")
        self.assertEqual(receipt["outcome"]["result"], "unusable")
        self.assertEqual(receipt["outcome"]["review_verdict"], "pending")
        # The graceful abort handshake ran before the process was terminated.
        self.assertTrue(self.abort_marker.exists())
        # Final metrics survive the failure, with no payload content.
        metrics = receipt["progress_metrics"]
        self.assertEqual(set(metrics), METRIC_FIELDS)
        self.assertIsNotNone(metrics["first_tool_seconds"])
        self.assertGreaterEqual(metrics["tool_seconds"], 1.0)
        self.assertIsNone(metrics["last_tool_completion_seconds"])
        self.assertEqual(metrics["checkpoint_notices"], [])

    def test_tool_timeout_is_sanitized_everywhere_it_is_written(self):
        result = self.run_worker(extra=["--tool-timeout-seconds", "1"],
                                 delegation="tool-secrets")
        self.assertNotEqual(result.returncode, 0)
        blobs = [result.stdout, result.stderr]
        for path in self.records.glob("*.jsonl"):
            blobs.append(path.read_text())
        for blob in blobs:
            for secret in ("SECRET-HANG", "SECRET-HANG-PARTIAL", "call_hang_1",
                           "sleep SECRET"):
                self.assertNotIn(secret, blob)
        # The failure evidence carries only the class name and the fixed code.
        receipt = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(receipt["failure"]["evidence"], "RunnerError")
        self.assertNotIn("timeout of", receipt["failure"]["code"])

    def test_a_quiet_tool_is_also_bounded(self):
        env = self.env | {"FAKE_PI_HANG_TOOL": "quiet"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", "write", "--delegation-id", "tool-quiet",
               "--timeout", "15", "--tool-timeout-seconds", "1"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(receipt["failure"]["code"], "tool_timeout")

    def test_zero_explicitly_disables_the_bound(self):
        env = self.env | {"FAKE_PI_HANG_TOOL": "short", "FAKE_PI_HANG_SECONDS": "2"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", "write", "--delegation-id", "tool-off",
               "--timeout", "20", "--tool-timeout-seconds", "0"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["outcome"]["status"], "completed")
        self.assertIsNone(receipt["failure"])
        # The 2s tool completed normally: no per-tool failure and no abort.
        self.assertGreaterEqual(receipt["progress_metrics"]["tool_seconds"], 1.5)
        self.assertIsNotNone(receipt["progress_metrics"]["last_tool_completion_seconds"])
        self.assertFalse(self.abort_marker.exists())

    def test_the_global_timeout_is_unchanged_and_not_a_tool_timeout(self):
        env = self.env | {"FAKE_PI_HANG_TOOL": "quiet"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", "write", "--delegation-id", "tool-global",
               "--timeout", "4", "--tool-timeout-seconds", "30"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 124)
        receipt = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(receipt["failure"]["code"], "timeout")
        self.assertEqual(receipt["failure"]["stage"], "rpc")

    def test_cli_rejects_unusable_values_without_side_effects(self):
        for value in ("-1", "nan", "inf", "-0.5"):
            with self.subTest(value=value):
                result = self.run_worker(extra=["--tool-timeout-seconds", value],
                                         delegation=f"bad-{value}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--tool-timeout-seconds", result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])
                self.assertFalse(self.records.exists())

    def test_validate_only_applies_the_same_checks(self):
        base = [sys.executable, str(RUNNER), "--workdir", str(self.work),
                "--contract", "Do it", "--profile", "standard-glm",
                "--routing-class", "standard", "--thinking", "medium",
                "--tools", "write", "--validate-only"]
        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value):
                result = subprocess.run(
                    base + ["--tool-timeout-seconds", value, "--delegation-id", f"v-{value}"],
                    env=self.env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--tool-timeout-seconds", result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])
        for value in ("0", "0.0", "180", "2.5"):
            with self.subTest(value=value):
                result = subprocess.run(
                    base + ["--tool-timeout-seconds", value, "--delegation-id", f"v-{value}"],
                    env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "valid")
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_the_prompt_states_the_bound_and_the_checkpoint_contract(self):
        result = self.run_worker(extra=["--tool-timeout-seconds", "1"],
                                 delegation="prompt-bound")
        self.assertNotEqual(result.returncode, 0)
        prompt = json.loads(self.prompt_file.read_text().splitlines()[0])["message"]
        self.assertIn("PER-TOOL LIMIT", prompt)
        self.assertIn("queued", prompt)
        self.assertIn("never interrupts a tool", prompt)
        # The contract itself is preserved ahead of the appended reminder.
        self.assertTrue(prompt.startswith("Do it"))


class CheckpointSteeringIntegrationTests(unittest.TestCase):
    """Soft checkpoints fire on idle wakes and never fail a run by themselves."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.records = self.base / "records"
        self.work = self.base / "work"
        self.work.mkdir()
        self.steer_log = self.base / "steer.jsonl"
        self.prompt_file = self.base / "prompt.jsonl"
        self.env = os.environ.copy()
        self.env.update({
            "PI_WORKER_RECORD_DIR": str(self.records), "PI_WORKER_PI": str(FAKE),
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test",
            "MINIMAX_CN_API_KEY": "test", "FAKE_PI_MODE": "ok",
            # A tool that streams nothing: the runner must wake on its own timer.
            "FAKE_PI_HANG_TOOL": "quiet", "FAKE_PI_HANG_SECONDS": "3.2",
            "FAKE_PI_STEER_SILENT": "1", "FAKE_PI_STEER_LOG": str(self.steer_log),
            "FAKE_PI_PROMPT_FILE": str(self.prompt_file),
        })

    def steers(self):
        if not self.steer_log.exists():
            return []
        return [json.loads(line) for line in self.steer_log.read_text().splitlines() if line]

    def run_worker(self, *, timeout="4", tools="write", delegation="cp-1", extra=None):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", tools, "--delegation-id", delegation,
               "--timeout", timeout, "--tool-timeout-seconds", "30"]
        if extra:
            cmd.extend(extra)
        return subprocess.run(cmd, env=self.env, text=True, capture_output=True)

    def test_a_silent_stream_still_gets_a_checkpoint_wakeup(self):
        result = self.run_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        # The 70%-of-budget wrap-up fired while Pi was silent.
        self.assertEqual(receipt["progress_metrics"]["checkpoint_notices"],
                         ["wrap_up_checkpoint"])
        self.assertEqual(receipt["outcome"]["status"], "completed")
        self.assertIsNone(receipt["failure"])
        steers = self.steers()
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["type"], "steer")
        self.assertEqual(steers[0]["id"], "checkpoint-wrap_up_checkpoint")
        self.assertIn("stop expanding scope", steers[0]["message"])

    def test_one_notice_is_sent_once_even_when_time_keeps_passing(self):
        result = self.run_worker(delegation="cp-once")
        self.assertEqual(result.returncode, 0, result.stderr)
        codes = [entry["id"].replace("checkpoint-", "") for entry in self.steers()]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertLessEqual(len(codes), len(CHECKPOINT_CODES))

    def test_a_rejected_steer_response_does_not_fail_a_short_run(self):
        for env_extra in ({"FAKE_PI_STEER_SILENT": "1"}, {"FAKE_PI_STEER_REJECT": "1"}):
            with self.subTest(env=sorted(env_extra)):
                for name in ("FAKE_PI_STEER_SILENT", "FAKE_PI_STEER_REJECT"):
                    self.env.pop(name, None)
                env = self.env | env_extra
                cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
                       "--contract", "Do it", "--profile", "standard-glm",
                       "--routing-class", "standard", "--thinking", "medium",
                       "--tools", "write", "--delegation-id", f"cp-{sorted(env_extra)[0]}",
                       "--timeout", "4", "--tool-timeout-seconds", "30"]
                result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["outcome"]["status"], "completed")
                self.assertEqual(receipt["progress_metrics"]["checkpoint_notices"],
                                 ["wrap_up_checkpoint"])

    def test_a_short_run_with_no_due_checkpoint_sends_nothing(self):
        env = self.env | {"FAKE_PI_HANG_TOOL": "short", "FAKE_PI_HANG_SECONDS": "1"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", "write", "--delegation-id", "cp-none",
               "--timeout", "30", "--tool-timeout-seconds", "30"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        # A 30s budget cannot reach the 180s/300s floors, and 70% is never reached.
        self.assertEqual(receipt["progress_metrics"]["checkpoint_notices"], [])
        self.assertEqual(self.steers(), [])

    def test_read_only_run_is_never_asked_to_write(self):
        result = self.run_worker(tools="read-only", delegation="cp-readonly")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = json.loads(self.prompt_file.read_text().splitlines()[0])["message"]
        self.assertIn("read-only: do not edit or create files", prompt)
        steers = self.steers()
        self.assertEqual(len(steers), 1)
        message = steers[0]["message"]
        self.assertIn("read-only", message)
        self.assertIn("do not edit or create files", message)
        self.assertNotIn("Save any artifact", message)

    def test_write_run_checkpoint_asks_to_save_and_test(self):
        result = self.run_worker(delegation="cp-write")
        self.assertEqual(result.returncode, 0, result.stderr)
        steers = self.steers()
        self.assertEqual(len(steers), 1)
        self.assertIn("stop expanding scope", steers[0]["message"])
        self.assertIn("Save any artifact", steers[0]["message"])

    def test_no_hard_kill_for_a_slow_first_edit_or_quiet_thinking(self):
        # The fake never edits and streams nothing; the run still settles normally.
        env = self.env | {"FAKE_PI_HANG_TOOL": "short", "FAKE_PI_HANG_SECONDS": "1"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", "standard-glm",
               "--routing-class", "standard", "--thinking", "medium",
               "--tools", "write", "--delegation-id", "cp-no-edit",
               "--timeout", "30", "--tool-timeout-seconds", "30"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertIsNone(receipt["progress_metrics"]["first_edit_seconds"])
        self.assertEqual(receipt["outcome"]["status"], "completed")


class DirectRunRpcWithoutProgressTests(unittest.TestCase):
    """``run_rpc`` keeps its guarantees when no progress dict is passed.

    The observer used to be created only when ``progress`` was not None, so a
    direct API call (``progress=None``) silently lost the per-tool bound and
    the soft checkpoints. These cover the API path; the CLI always has a
    progress dict and is covered above.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.work = self.base / "work"
        self.work.mkdir()
        self.abort_marker = self.base / "aborted"
        self.steer_log = self.base / "steer.jsonl"
        self.env = os.environ.copy()
        self.env.update({
            "PI_WORKER_RECORD_DIR": str(self.base / "records"),
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test",
            "MINIMAX_CN_API_KEY": "test", "FAKE_PI_MODE": "ok",
            "FAKE_PI_HANG_TOOL": "stream", "FAKE_PI_HANG_SECONDS": "30",
            "FAKE_ABORT_MARKER": str(self.abort_marker),
            "FAKE_PI_STEER_LOG": str(self.steer_log),
        })

    def call_rpc(self, *, timeout, tool_timeout_seconds, **extra):
        return run_rpc(str(FAKE), self.work, "Do it", "standard-glm", "medium",
                       "write", timeout, self.env, "direct-1",
                       tool_timeout_seconds=tool_timeout_seconds, **extra)

    def test_hanging_tool_times_out_even_without_a_progress_dict(self):
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(timeout=30, tool_timeout_seconds=1)
        error = caught.exception
        self.assertEqual(error.code, "tool_timeout")
        self.assertEqual(error.stage, "rpc")
        # Sanitized: only the configured bound, never tool name, id, or output.
        self.assertIn("1s", str(error))
        for secret in ("SECRET-HANG", "SECRET-HANG-PARTIAL", "call_hang_1",
                       "sleep SECRET"):
            self.assertNotIn(secret, str(error))
        # The graceful abort handshake ran before the process was terminated.
        self.assertTrue(self.abort_marker.exists())

    def test_a_quiet_hanging_tool_is_also_bounded_without_progress(self):
        env_backup = self.env["FAKE_PI_HANG_TOOL"]
        self.env["FAKE_PI_HANG_TOOL"] = "quiet"
        try:
            with self.assertRaises(RunnerError) as caught:
                self.call_rpc(timeout=30, tool_timeout_seconds=1)
        finally:
            self.env["FAKE_PI_HANG_TOOL"] = env_backup
        self.assertEqual(caught.exception.code, "tool_timeout")

    def test_a_short_successful_run_without_progress_is_unchanged(self):
        self.env["FAKE_PI_HANG_TOOL"] = "short"
        self.env["FAKE_PI_HANG_SECONDS"] = "1"
        stats, events = self.call_rpc(timeout=20, tool_timeout_seconds=30)
        self.assertIsInstance(events, list)
        self.assertIn("agent_settled", events)
        # The statistics request still completed, and nothing failed.
        self.assertTrue(stats is None or isinstance(stats, dict))
        self.assertFalse(self.abort_marker.exists())

    def test_zero_still_disables_the_bound_without_progress(self):
        self.env["FAKE_PI_HANG_TOOL"] = "short"
        self.env["FAKE_PI_HANG_SECONDS"] = "1"
        _, events = self.call_rpc(timeout=20, tool_timeout_seconds=0)
        self.assertIn("agent_settled", events)
        self.assertFalse(self.abort_marker.exists())


if __name__ == "__main__":
    unittest.main()

class RetryBoundsIntegrationTests(unittest.TestCase):
    """``run_rpc`` bounds automatic agent-turn retries, with or without progress.

    The fixtures are opt-in (``FAKE_PI_RETRY_SCENARIO``), no real provider is
    contacted, and the fake worker is a child of this process, so every case is
    bounded by the run timeout.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.work = self.base / "work"
        self.work.mkdir()
        self.abort_marker = self.base / "aborted"
        self.env = os.environ.copy()
        self.env.update({
            "PI_WORKER_RECORD_DIR": str(self.base / "records"),
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test",
            "MINIMAX_CN_API_KEY": "test", "FAKE_PI_MODE": "ok",
            "FAKE_PI_HANG_TOOL": "", "FAKE_PI_HANG_SECONDS": "60",
            "FAKE_ABORT_MARKER": str(self.abort_marker),
            "FAKE_PI_RETRY_SCENARIO": "", "FAKE_PI_RETRY_COUNT": "",
        })

    def scenario(self, name, *, retries=None):
        self.env["FAKE_PI_RETRY_SCENARIO"] = name
        if retries is not None:
            self.env["FAKE_PI_RETRY_COUNT"] = str(retries)
        return self.env

    def call_rpc(self, *, timeout=30, progress=None, **extra):
        return run_rpc(str(FAKE), self.work, "Do it", "standard-glm", "medium",
                       "write", timeout, self.env, "retry-1",
                       progress=progress, **extra)

    def assert_sanitized(self, error, *forbidden):
        text = str(error)
        for secret in ("SECRET-PROVIDER-ERROR", "SECRET-TRANSIENT-ERROR",
                       "secret provider detail", "delayMs") + tuple(forbidden):
            self.assertNotIn(secret, text)

    def test_max_zero_rejects_the_first_retry(self):
        self.scenario("burst", retries=1)
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(max_auto_retries=0, tool_timeout_seconds=30)
        self.assertEqual(caught.exception.code, "retry_limit")
        self.assertEqual(caught.exception.stage, "rpc")
        self.assert_sanitized(caught.exception)
        # Bounded cleanup on the full worker: the graceful abort ran.
        self.assertTrue(self.abort_marker.exists())

    def test_two_retries_are_allowed_and_the_third_is_not(self):
        self.scenario("burst", retries=2)
        stats, events = self.call_rpc(max_auto_retries=2)
        self.assertIn("agent_settled", events)
        self.assertTrue(stats is None or isinstance(stats, dict))
        self.scenario("burst", retries=3)
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(max_auto_retries=2)
        self.assertEqual(caught.exception.code, "retry_limit")

    def test_default_allows_two_retries(self):
        # No retry arguments at all: the documented defaults still apply, so the
        # old positional/keyword call shape is unchanged.
        self.scenario("burst", retries=2)
        _, events = self.call_rpc()
        self.assertIn("agent_settled", events)
        self.scenario("burst", retries=3)
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc()
        self.assertEqual(caught.exception.code, "retry_limit")
        self.assertIn("2", str(caught.exception))

    def test_silent_retry_trips_the_budget(self):
        self.scenario("silent")
        started = time.monotonic()
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(timeout=60, retry_budget_seconds=0.5,
                          max_auto_retries=10)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "retry_budget_exceeded")
        self.assertEqual(caught.exception.stage, "rpc")
        self.assert_sanitized(caught.exception)
        # It fired on the retry budget, not on the global deadline or the count.
        self.assertLess(elapsed, 20)
        self.assertTrue(self.abort_marker.exists())

    def test_streaming_traffic_cannot_postpone_the_budget(self):
        self.scenario("stream")
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(timeout=60, retry_budget_seconds=0.5,
                          max_auto_retries=10)
        self.assertEqual(caught.exception.code, "retry_budget_exceeded")
        self.assert_sanitized(caught.exception)

    def test_zero_budget_disables_only_the_time_bound(self):
        # Count still enforced with a disabled budget...
        self.scenario("burst", retries=3)
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(retry_budget_seconds=0, max_auto_retries=2)
        self.assertEqual(caught.exception.code, "retry_limit")
        # ...and short retries then run to completion on the count alone.
        self.scenario("burst", retries=3)
        self.abort_marker.unlink(missing_ok=True)
        _, events = self.call_rpc(retry_budget_seconds=0, max_auto_retries=5)
        self.assertIn("agent_settled", events)
        self.assertFalse(self.abort_marker.exists())

    def test_budget_also_applies_without_a_progress_dict(self):
        self.scenario("silent")
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(timeout=60, retry_budget_seconds=0.5,
                          max_auto_retries=10, progress=None)
        self.assertEqual(caught.exception.code, "retry_budget_exceeded")

    def test_successful_retry_clears_the_recovered_provider_error(self):
        self.scenario("recovered")
        _, events = self.call_rpc(max_auto_retries=5)
        self.assertIn("agent_settled", events)
        self.assertFalse(self.abort_marker.exists())

    def test_a_later_error_after_a_successful_retry_still_fails(self):
        self.scenario("recovered-then-error")
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(max_auto_retries=5)
        self.assertEqual(caught.exception.code, "provider_error")
        self.assert_sanitized(caught.exception)

    def test_a_failed_retry_end_keeps_the_provider_error(self):
        self.scenario("failed-end")
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(max_auto_retries=5)
        self.assertEqual(caught.exception.code, "provider_error")
        self.assert_sanitized(caught.exception)

    def test_usage_limit_survives_a_recovered_retry(self):
        # The recovery clears the *first* error only; the final usage-limit
        # error still classifies as such (and auto-disables at the CLI level).
        self.scenario("usage-limit-retry")
        with self.assertRaises(RunnerError) as caught:
            self.call_rpc(max_auto_retries=5)
        self.assertEqual(caught.exception.code, "provider_usage_limit")
        self.assert_sanitized(caught.exception)

    def test_invalid_retry_bounds_are_rejected_before_the_worker_starts(self):
        # Both bounds are strict at the API boundary too (only an explicit 0
        # disables the time bound): a bool, a fractional, negative, or
        # non-numeric value is an error, never a silently different bound.
        for bad in (True, 1.5, -1, "two", None):
            with self.assertRaises(RunnerError) as caught:
                self.call_rpc(max_auto_retries=bad)
            self.assertEqual(caught.exception.code, "invalid_max_auto_retries")
            self.assertEqual(caught.exception.stage, "routing")
        for bad in (True, -1, float("nan"), float("inf"), "soon", None):
            with self.assertRaises(RunnerError) as caught:
                self.call_rpc(retry_budget_seconds=bad)
            self.assertEqual(caught.exception.code, "invalid_retry_budget")
            self.assertEqual(caught.exception.stage, "routing")
        # A rejected value starts no worker at all.
        self.assertFalse(self.abort_marker.exists())
        # An explicit 0 stays valid and disables only the time bound.
        self.scenario("burst", retries=1)
        _, events = self.call_rpc(retry_budget_seconds=0, max_auto_retries=5)
        self.assertIn("agent_settled", events)

    def test_metrics_survive_a_retry_bound_failure(self):
        progress = {"event_counts": {}, "session_id": None, "last_event": None,
                    "event_count": 0}
        self.scenario("burst", retries=3)
        with self.assertRaises(RunnerError):
            self.call_rpc(max_auto_retries=1, progress=progress)
        metrics = progress["progress_metrics"]
        self.assertEqual(set(metrics), METRIC_FIELDS)
        self.assertEqual(metrics["retry_count"], 2)
        self.assertGreater(metrics["retry_seconds"], 0.0)
        self.assertGreater(progress["event_count"], 0)
