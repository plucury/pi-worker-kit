#!/usr/bin/env python3
"""Deterministic, payload-free observer for Pi RPC phase telemetry.

This module only *observes* the JSONL RPC events a worker already receives
(``docs/json.md`` tool execution and automatic-retry events). It sends no
command, changes no protocol behaviour, and never fails a run: a malformed or
unexpected event is ignored.

Privacy model (enforced by construction)
----------------------------------------
``snapshot()`` returns exactly the fixed schema below: numbers, ``null``, and a
list of fixed checkpoint codes. Arguments, commands, results, error strings,
paths, and prompts are read only to classify an event and are dropped
immediately; they are never stored. The internal pending-tool map holds only the
call id, the relative start offset, and a one-word classification.

Semantics (see references/worker-efficiency.md)
-----------------------------------------------
* All values are monotonic, non-negative seconds relative to the prompt, read
  from an injectable clock (``time.monotonic`` in production).
* Unobserved stages stay ``null``; an absent observation is never rewritten to
  ``0``. Aggregate totals may legitimately be ``0`` once tracking started.
* ``first_edit_seconds`` requires a *successful* ``edit``/``write`` completion.
  A bash command is never inferred to be an edit.
* ``first_test_candidate_seconds`` is a small, explicit command heuristic for a
  test-like bash invocation. It is a candidate, not a test result.
* ``tool_seconds``/``retry_seconds`` aggregate durations by matching start/end;
  in-flight spans are included at snapshot time and concurrent spans can
  overlap, so they are busy-time figures, not wall-clock.
* ``checkpoint_notices`` only ever lists a code from ``CHECKPOINT_CODES`` that the
  caller *actually sent* as a steer RPC (see ``record_checkpoint``). Scheduling a
  checkpoint is not sending it, and nothing here ever aborts, pauses, or penalises
  a run: a missing first edit or a quiet thinking stretch is never a trigger.
* ``CheckpointPolicy`` is pure policy: it decides *when* a code becomes due from
  the RPC task budget and an injectable monotonic clock. It sends nothing and
  holds no RPC payload; the runner sends and then registers the code here.
* ``ProgressObserver.oldest_live_tool_start_seconds`` /
  ``tool_timeout_wake_after`` expose the per-running-tool deadline from the
  already-tracked start times. They return durations only: the raw pending map
  stays private, and neither a partial tool update nor unrelated model traffic
  can move a tool's start, so neither resets the timer.
* ``ProgressObserver.retry_count`` / ``retry_seconds_now`` /
  ``retry_budget_wake_after`` / ``retry_budget_exhausted`` expose the
  automatic-retry bounds from the already-folded retry events. They read the
  real observed spans (including an in-flight retry at the current clock), so a
  stream of unrelated traffic cannot reset or postpone an exhausted budget, and
  nothing here reads or stores retry payloads such as ``delayMs``.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

# Exact receipt/checkpoint schema. Order is the documented order; consumers use
# key lookups, so this stays the only place the field list is written.
METRIC_FIELDS = (
    "first_tool_seconds",
    "first_edit_seconds",
    "first_test_candidate_seconds",
    "last_activity_seconds",
    "last_tool_completion_seconds",
    "tool_seconds",
    "retry_seconds",
    "retry_count",
    "checkpoint_notices",
)
# The only tool completions that can set ``first_edit_seconds``.
EDIT_TOOL_NAMES = frozenset({"edit", "write"})
# The only checkpoint codes that can ever appear in ``checkpoint_notices``, in
# canonical order. Order is also the send order: the runner never sends a later
# code before an earlier unsent one.
CHECKPOINT_CODES = (
    "investigation_checkpoint",
    "first_delivery_checkpoint",
    "wrap_up_checkpoint",
)
# (code, absolute floor in seconds, fraction of the RPC task budget). The trigger
# is ``max(floor, fraction * budget)``: a short task cannot be checkpointed every
# few seconds, and a long task is not checkpointed at an absolute 180 s.
CHECKPOINT_TRIGGERS = (
    ("investigation_checkpoint", 180.0, 0.20),
    ("first_delivery_checkpoint", 300.0, 0.35),
    ("wrap_up_checkpoint", 0.0, 0.70),
)


def _finite_non_negative(value: Any) -> float | None:
    """``value`` as a finite non-negative float, or ``None`` when unusable.

    A bool is not a number here: ``True`` must never become a one-second bound.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        return None
    return number


def checkpoint_thresholds(budget_seconds: Any) -> dict[str, float]:
    """Prompt-relative trigger seconds per code for one RPC task budget.

    Deterministic and side-effect free, so thresholds can be asserted directly
    without a clock or an RPC run. A non-finite or negative budget is treated as
    ``0`` rather than raising: the caller already validated the CLI value, and a
    degenerate budget must not fail an in-flight run.
    """
    budget = _finite_non_negative(budget_seconds)
    budget = 0.0 if budget is None else budget
    return {code: max(floor, fraction * budget)
            for code, floor, fraction in CHECKPOINT_TRIGGERS}


class CheckpointPolicy:
    """Decide when each soft checkpoint becomes due, and remember what was sent.

    The policy is deliberately send-agnostic: ``next_code()`` *proposes* one code
    at a time and ``mark_sent()`` records only what the caller confirmed it put on
    the wire. Each code can fire at most once per run.
    """

    def __init__(self, budget_seconds: Any, *, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._origin: float | None = None
        self._budget = _finite_non_negative(budget_seconds) or 0.0
        self._thresholds = checkpoint_thresholds(self._budget)
        self._sent: set[str] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "CheckpointPolicy":
        """Anchor the policy clock; later ``elapsed`` values are relative to it."""
        self._origin = float(self._clock())
        return self

    @property
    def budget_seconds(self) -> float:
        return self._budget

    @property
    def thresholds(self) -> dict[str, float]:
        """Copy of the prompt-relative trigger seconds, in canonical order."""
        return {code: self._thresholds[code] for code in CHECKPOINT_CODES}

    @property
    def sent(self) -> tuple[str, ...]:
        """Codes confirmed as sent, in canonical order."""
        return tuple(code for code in CHECKPOINT_CODES if code in self._sent)

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return 0.0

    def elapsed(self) -> float:
        """Non-negative seconds since :meth:`start` (0 before it is called)."""
        if self._origin is None:
            return 0.0
        return max(0.0, self._now() - self._origin)

    # -- policy ------------------------------------------------------------
    def next_code(self) -> str | None:
        """The earliest unsent code whose trigger has passed, else ``None``.

        At most one code is proposed per call so the three notices stay ordered
        and a late start cannot emit three notices at once.
        """
        now = self.elapsed()
        for code in CHECKPOINT_CODES:
            if code in self._sent:
                continue
            if self._thresholds[code] <= now:
                return code
        return None

    def next_due_after(self) -> float | None:
        """Seconds until the earliest unsent trigger, or ``None`` when spent."""
        if self._origin is None:
            return None
        elapsed = self.elapsed()
        for code in CHECKPOINT_CODES:
            if code in self._sent:
                continue
            return max(0.0, self._thresholds[code] - elapsed)
        return None

    def mark_sent(self, code: Any) -> bool:
        """Record a confirmed send; ``False`` for unknown or duplicate codes."""
        if not isinstance(code, str) or code not in self._thresholds:
            return False
        if code in self._sent:
            return False
        self._sent.add(code)
        return True


def checkpoint_message(code: Any, *, write_task: bool = True) -> str:
    """The steer text for one checkpoint code.

    Read-only tasks are never asked to write or edit a file they cannot produce;
    they are asked to summarize instead. Returns an empty string for an unknown
    code so a caller cannot accidentally steer arbitrary text.
    """
    if code == "investigation_checkpoint":
        return (
            "Delivery checkpoint (a reminder, not a stop): reply in a few sentences with "
            "what you found so far and whether anything blocks you, then name one bounded "
            "next step and continue. If you are still investigating, say so instead of "
            "editing to look busy."
        )
    if code == "first_delivery_checkpoint":
        if write_task:
            return (
                "Delivery checkpoint (a reminder, not a stop): save what you already have. "
                "Write the scoped changes to disk, run the focused test for them, then say in "
                "a few sentences what you saved, what is still open, and any blocker. Do not "
                "widen the scope to finish more before saving."
            )
        return (
            "Delivery checkpoint (a reminder, not a stop): this task is read-only, so do not "
            "edit or create files. Summarize your findings and any blocker in a few sentences. "
            "Do not widen the scope to finish more before reporting."
        )
    if code == "wrap_up_checkpoint":
        if write_task:
            return (
                "Wrap-up checkpoint (a reminder, not a stop): stop expanding scope. Save any "
                "artifact you have not written yet, run the check you already planned, then "
                "report what is done and state plainly what is still incomplete or unverified. "
                "A bounded, honestly labelled partial beats a late, unbounded one."
            )
        return (
            "Wrap-up checkpoint (a reminder, not a stop): stop expanding scope. This task is "
            "read-only, so do not edit or create files. Report your findings and state plainly "
            "what is still unverified. A bounded, honestly labelled partial beats a late, "
            "unbounded one."
        )
    return ""


def _fmt_seconds(value: float) -> str:
    """Whole-second label for a reminder, so the prompt stays scannable."""
    return f"{round(value):g}"


def time_budget_reminder(*, budget_seconds: Any, tool_timeout_seconds: Any = None,
                         write_task: bool = True) -> str:
    """Concise upfront time-budget reminder appended to the delegated prompt.

    It states the three soft checkpoints, that they are queued rather than
    interrupting a running tool, and that a timeout keeps only saved work. The
    optional per-tool bound is stated plainly, including that it ends the run.
    """
    budget = _finite_non_negative(budget_seconds) or 0.0
    tool_limit = _finite_non_negative(tool_timeout_seconds)
    thresholds = checkpoint_thresholds(budget)
    lines = [
        "",
        "DELIVERY CHECKPOINTS (soft reminders, never a stop): this run has a "
        f"{_fmt_seconds(budget)}s budget. At about {_fmt_seconds(thresholds['investigation_checkpoint'])}s "
        "you are asked for a short progress note, at about "
        f"{_fmt_seconds(thresholds['first_delivery_checkpoint'])}s for a first saved increment, "
        f"and at {_fmt_seconds(thresholds['wrap_up_checkpoint'])}s to stop exploring and wrap up. "
        "Each one is queued and reaches you after the tool calls you are already running "
        "finish; it never interrupts a tool and never ends the run by itself.",
    ]
    if write_task:
        lines.append(
            "Save scoped work to disk as you go: a timeout keeps only what you actually wrote. "
            "If something blocks you, say so early with the blocker and one bounded next step."
        )
    else:
        lines.append(
            "This task is read-only: do not edit or create files. Report findings as you go, and "
            "if something blocks you, say so early with the blocker and one bounded next step."
        )
    if tool_limit:
        lines.append(
            f"PER-TOOL LIMIT: one tool call running longer than {_fmt_seconds(tool_limit)}s "
            "ends the run with a tool timeout, so prefer smaller scoped commands over one long "
            "full check."
        )
    return "\n".join(lines)
# Documented Pi RPC lifecycle events (docs/json.md).
TOOL_START_EVENT = "tool_execution_start"
TOOL_END_EVENT = "tool_execution_end"
# Automatic *agent-turn* retry only (`auto_retry_*`). Compaction/branch-summary
# retries are deliberately excluded: they are not model retries, so counting them
# here would double-count time a future --max-auto-retries budget must not spend.
RETRY_START_EVENTS = ("auto_retry_start",)
RETRY_END_EVENTS = ("auto_retry_end",)
# Small explicit test-invocation heuristic. Deliberately narrow: a bare "test"
# substring is never enough, so unrelated bash work is not counted.
TEST_COMMAND_MARKERS = (
    "pytest", "unittest", "nose2", "tox", "nox", "nextest",
    "jest", "vitest", "mocha", "karma",
    "npm test", "npm run test", "yarn test", "pnpm test", "bun test",
    "cargo test", "go test", "dotnet test", "gradle test", "mvn test",
    "make test", "rake test", "phpunit", "rspec", "ctest",
)


def _bash_command(args: Any) -> Any:
    """Return the bash command string for tool arguments, if present.

    Read once at classification time and dropped; nothing here is stored.
    """
    if isinstance(args, dict):
        command = args.get("command")
        return command if isinstance(command, str) else None
    return args if isinstance(args, str) else None


def looks_like_test_command(command: Any) -> bool:
    """True when a bash command string looks like a test invocation.

    Substring matching over an explicit marker list: cheap, deterministic, and
    intentionally incomplete. It classifies a *candidate* only.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    lowered = " ".join(command.lower().split())
    return any(marker in lowered for marker in TEST_COMMAND_MARKERS)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _relative(origin: float | None, now: float) -> float | None:
    """Non-negative seconds since ``origin``, or ``None`` when unusable."""
    if origin is None:
        return None
    delta = now - origin
    if not _is_number(delta):
        return None
    return round(max(0.0, float(delta)), 6)


class ProgressObserver:
    """Collect prompt-relative phase metrics from full RPC messages.

    The observer keeps no RPC payload: ``observe`` classifies and discards.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._origin: float | None = None
        self._started = False
        self._first_tool: float | None = None
        self._first_edit: float | None = None
        self._first_test: float | None = None
        self._last_activity: float | None = None
        self._last_tool_completion: float | None = None
        self._tool_seconds = 0.0
        self._retry_seconds = 0.0
        self._retry_count = 0
        # Fixed checkpoint codes confirmed as sent. Never payload, never free text.
        self._notices: list[str] = []
        # call id -> (relative start, classification). Never arguments or results.
        self._pending: dict[str, tuple[float, str]] = {}
        # Relative starts of retries that have not been closed yet.
        self._open_retries: list[float] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Mark the prompt timestamp; later observations are relative to it."""
        self._origin = float(self._clock())
        self._started = True

    @property
    def started(self) -> bool:
        return self._started

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return 0.0

    # -- observation -------------------------------------------------------
    def observe(self, message: Any) -> dict[str, Any] | None:
        """Fold one full RPC message in; return a fresh snapshot.

        Any post-prompt message counts as activity. Malformed input is ignored
        (never raises) and still returns the current snapshot.
        """
        if not self._started:
            self.start()
        now = self._now()
        self._last_activity = _relative(self._origin, now)
        try:
            self._fold(message, now)
        except Exception:
            pass
        return self.snapshot()

    def _fold(self, message: Any, now: float) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if not isinstance(kind, str):
            return
        relative_now = _relative(self._origin, now)
        if kind == TOOL_START_EVENT:
            self._on_tool_start(message, relative_now)
        elif kind == TOOL_END_EVENT:
            self._on_tool_end(message, relative_now)
        elif kind in RETRY_START_EVENTS:
            self._retry_count += 1
            if relative_now is not None:
                self._open_retries.append(relative_now)
        elif kind in RETRY_END_EVENTS:
            if self._open_retries and relative_now is not None:
                self._retry_seconds += max(0.0, relative_now - self._open_retries.pop(0))
        # `tool_execution_update` and every other event are activity only.

    def _on_tool_start(self, message: dict[str, Any], start: float | None) -> None:
        call_id = message.get("toolCallId")
        tool_name = message.get("toolName")
        if not isinstance(call_id, str) or not call_id or not isinstance(tool_name, str):
            return
        if start is None:
            return
        if call_id in self._pending:
            # Duplicate start for a live call: keep the first span, ignore the rest.
            return
        # Arguments are inspected once for the test heuristic, then dropped.
        classification = "test" if (tool_name == "bash" and looks_like_test_command(
            _bash_command(message.get("args")))) else "tool"
        self._pending[call_id] = (start, classification)
        if self._first_tool is None:
            self._first_tool = start
        if classification == "test" and self._first_test is None:
            self._first_test = start

    def _on_tool_end(self, message: dict[str, Any], end: float | None) -> None:
        if end is None:
            return
        call_id = message.get("toolCallId")
        tool_name = message.get("toolName")
        self._last_tool_completion = end
        started_here = isinstance(call_id, str) and call_id in self._pending
        if started_here:
            start, _classification = self._pending.pop(call_id)
            self._tool_seconds += max(0.0, end - start)
        # Only an edit/write completion that matches a seen start and reports an
        # explicit successful status counts as an edit: an absent or malformed
        # `isError` is not evidence of success. The result payload is never read.
        if (started_here and isinstance(tool_name, str)
                and tool_name in EDIT_TOOL_NAMES and self._first_edit is None
                and message.get("isError") is False):
            self._first_edit = end

    # -- per-running-tool deadline (payload free) ---------------------------
    def oldest_live_tool_start_seconds(self) -> float | None:
        """Prompt-relative start of the oldest still-running tool call.

        ``None`` when nothing is running. This is the stored *start offset*, the
        same prompt-relative convention as every other field, so concurrent tools
        are tracked independently and a partial update or unrelated model traffic
        cannot move a start. :meth:`tool_timeout_wake_after` turns it into a
        remaining duration without exposing the pending map.
        """
        if not self._pending:
            return None
        return min(start for start, _classification in self._pending.values())

    def tool_timeout_wake_after(self, limit_seconds: Any) -> float | None:
        """Seconds from now until the oldest running tool call exceeds ``limit``.

        ``None`` when no tool is running or the bound is disabled (a
        non-positive, non-finite, or unusable value). ``0.0`` means the bound is
        already exceeded. Callers use it to wake an idle read at the right time;
        comparing against the returned value never exposes the pending map.
        """
        oldest = self.oldest_live_tool_start_seconds()
        if oldest is None or self._origin is None:
            return None
        limit = _finite_non_negative(limit_seconds)
        if limit is None or limit <= 0.0:
            return None
        now = _relative(self._origin, self._now())
        if now is None:
            return None
        return max(0.0, oldest + limit - now)

    # -- automatic-retry bounds (payload free) -----------------------------
    @property
    def retry_count(self) -> int:
        """``auto_retry_start`` events folded in so far (agent turns only).

        Compaction/branch-summary retries are excluded upstream, so a caller can
        compare this directly against a retry-count bound. Counting happens
        globally for the run: the observer is created once per run and never
        reset, so the count is a run total rather than a per-message value.
        """
        return self._retry_count

    def retry_seconds_now(self) -> float:
        """Aggregate retry seconds at the current clock, in-flight spans included.

        Same figure ``snapshot()`` publishes as ``retry_seconds``. Concurrent
        retries overlap, so this is a busy-time total, not wall-clock time.
        """
        now = _relative(self._origin, self._now())
        if now is None:
            return round(self._retry_seconds, 6)
        total = self._retry_seconds + sum(
            max(0.0, now - start) for start in self._open_retries)
        return round(total, 6)

    def retry_budget_wake_after(self, budget_seconds: Any) -> float | None:
        """Seconds from now until aggregate retry time reaches ``budget_seconds``.

        ``None`` when the budget is disabled (a non-positive, non-finite, or
        unusable value) or when no retry has been observed yet, so a run that
        never retries is never woken for a budget it cannot spend. ``0.0`` means
        the budget is already spent. Derived from real observed spans only: a
        partial retry update or unrelated streaming traffic cannot move it.
        """
        budget = _finite_non_negative(budget_seconds)
        if budget is None or budget <= 0.0:
            return None
        if self._origin is None or (not self._open_retries and self._retry_count == 0):
            return None
        return max(0.0, budget - self.retry_seconds_now())

    def retry_budget_exhausted(self, budget_seconds: Any) -> bool:
        """True once the aggregate retry time has reached ``budget_seconds``.

        ``False`` for a disabled budget, so ``0`` explicitly disables the bound
        and never trips it on the very first retry.
        """
        budget = _finite_non_negative(budget_seconds)
        if budget is None or budget <= 0.0:
            return False
        if self._origin is None or (not self._open_retries and self._retry_count == 0):
            return False
        return self.retry_seconds_now() >= budget

    # -- reporting ---------------------------------------------------------
    def record_checkpoint(self, code: Any) -> bool:
        """Register a checkpoint code that was actually sent as a steer RPC.

        Returns ``False`` (and changes nothing) for an unknown code, a non-string,
        or a duplicate, so scheduling a notice twice cannot inflate the report.
        """
        if not isinstance(code, str) or code not in CHECKPOINT_CODES:
            return False
        if code in self._notices:
            return False
        self._notices.append(code)
        return True

    def snapshot(self) -> dict[str, Any]:
        """Exact fixed schema; in-flight spans included at the current clock."""
        now = _relative(self._origin, self._now())
        if now is None:
            now = 0.0
        # In-flight spans are included at snapshot time; concurrent spans overlap.
        tool_total = self._tool_seconds + sum(
            max(0.0, now - start) for start, _ in self._pending.values())
        retry_total = self._retry_seconds + sum(
            max(0.0, now - start) for start in self._open_retries)
        return {
            "first_tool_seconds": self._first_tool,
            "first_edit_seconds": self._first_edit,
            "first_test_candidate_seconds": self._first_test,
            "last_activity_seconds": self._last_activity,
            "last_tool_completion_seconds": self._last_tool_completion,
            "tool_seconds": round(tool_total, 6),
            "retry_seconds": round(retry_total, 6),
            "retry_count": self._retry_count,
            # Only codes the runner actually sent on the wire appear here.
            "checkpoint_notices": list(self._notices),
        }