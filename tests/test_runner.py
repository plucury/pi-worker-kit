import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_pi_worker.py"
FAKE = ROOT / "tests" / "fake_pi.py"
# Committed fake-only fixture (never a production fallback); also injected for
# every module/subprocess by tests/conftest.py.
FIXTURE = ROOT / "tests" / "fixtures" / "profiles.json"
spec = importlib.util.spec_from_file_location("runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()  # canonical: /var is a macOS alias
        self.records = self.base / "records"
        self.work = self.base / "work"
        self.work.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            "PI_WORKER_RECORD_DIR": str(self.records), "PI_WORKER_PI": str(FAKE),
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test", "MINIMAX_CN_API_KEY": "test",
        })

    def command(self, profile, routing, delegation="d1", timeout="5", *, tools="write", extra=None):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--profile", profile,
               "--routing-class", routing, "--thinking", "medium",
               "--tools", tools, "--delegation-id", delegation,
               "--timeout", timeout]
        if extra:
            cmd.extend(extra)
        return cmd

    def run_worker(self, profile="standard-glm", routing="standard", mode="ok",
                    env_extra=None, **kwargs):
        env = self.env | {"FAKE_PI_MODE": mode} | (env_extra or {})
        return subprocess.run(self.command(profile, routing, **kwargs), env=env, text=True, capture_output=True)

    def records_for_today(self, include_events=False):
        paths = list(self.records.glob("*.jsonl"))
        self.assertEqual(len(paths), 1)
        items = [json.loads(x) for x in paths[0].read_text().splitlines()]
        if include_events:
            return items
        # Lifecycle and notification events are coarse append-only evidence; the
        # focused tests here inspect run/review records unless they opt in.
        return [item for item in items
                if item.get("record_type") not in ("lifecycle", "notification")]

    def test_all_profiles_success(self):
        # Production standard pool: all four enabled workers run successfully;
        # the retired simple routing class and the disabled legacy standard
        # worker are covered by the rejection regression tests.
        profiles = (("standard-glm", "standard"), ("standard-deepseek", "standard"),
                    ("standard-minimax", "standard"), ("standard-mimo", "standard"))
        for index, (profile, routing) in enumerate(profiles):
            result = self.run_worker(profile, routing, delegation=f"d{index}")
            self.assertEqual(result.returncode, 0, result.stderr)
        terminal = self.records_for_today()
        self.assertEqual({r["worker"]["worker_type"] for r in terminal},
                         {"standard-glm", "standard-deepseek",
                          "standard-minimax", "standard-mimo"})
        self.assertEqual({(r["worker"]["provider"], r["worker"]["model"]) for r in terminal},
                         {("test-provider-a", "test-model-a"),
                          ("test-provider-a", "test-model-b"),
                          ("test-provider-b", "test-model-c"),
                          ("test-provider-a", "test-model-d")})
        self.assertTrue(all(r["worker"]["session_id"] == "fake-session" for r in terminal))
        self.assertTrue(all(r["schema_version"] == 3 for r in terminal))
        self.assertTrue(all(r["record_type"] == "run" and r["phase"] == "completed" for r in terminal))
        self.assertTrue(all("rpc_event_counts" in r and "rpc_events" not in r for r in terminal))

    def test_usage_limit_classification(self):
        for text in ("429 Too Many Requests", "Rate limit exceeded",
                     "You exceeded your current quota", "insufficient_quota",
                     "余额不足", "用量限制", "请求过于频繁"):
            self.assertEqual(runner.classify_provider_error(text), "provider_usage_limit", text)
        for text in ("internal server error", "secret provider detail", "",
                     "context length exceeded", "overloaded"):
            self.assertEqual(runner.classify_provider_error(text), "provider_error", text)
        self.assertEqual(runner.classify_provider_error("429", "rpc_process_exit"),
                         "provider_usage_limit")
        self.assertEqual(runner.classify_provider_error("boom", "rpc_process_exit"),
                         "rpc_process_exit")

    # The exact provider message the fake reports for a usage-limit failure.
    # Probing for a bare "429" over the whole receipt or stderr is unreliable:
    # random timing and hashes naturally contain that substring (for example
    # worker_seconds 0.030374292), so the privacy guarantee is asserted against
    # the real provider text instead, plus the fixed record schema.
    PROVIDER_USAGE_TEXT = "429 Too Many Requests: rate limit exceeded"

    def assert_no_provider_text(self, haystack):
        """No provider message text anywhere in ``haystack``.

        The provider's raw text (and its distinctive phrases) must never reach
        stderr or the record; the sanitized code is what survives.
        """
        self.assertNotIn(self.PROVIDER_USAGE_TEXT, haystack)
        lowered = haystack.lower()
        for phrase in ("too many requests", "rate limit exceeded",
                       "secret provider detail", "secret-provider-error"):
            self.assertNotIn(phrase, lowered)

    def test_usage_limit_auto_disables_profile(self):
        result = self.run_worker("standard-deepseek", "standard", mode="usage-limit",
                                 delegation="limit")
        self.assertNotEqual(result.returncode, 0)
        terminal = [r for r in self.records_for_today() if r["delegation_id"] == "limit"][0]
        self.assertEqual(terminal["failure"]["code"], "provider_usage_limit")
        self.assertEqual(terminal["routing"]["profile_auto_disabled"]["reason"], "usage_limit")
        # The failure is reported through the fixed schema only, and neither the
        # stderr receipt nor the record carries the provider's message text.
        self.assertEqual(set(terminal["failure"]),
                         {"category", "code", "stage", "evidence"})
        self.assertEqual(terminal["failure"]["stage"], "rpc")
        self.assertEqual(terminal["outcome"]["status"], "failed")
        self.assert_no_provider_text(result.stderr)
        self.assert_no_provider_text(json.dumps(terminal))
        # Structured telemetry keeps the fixed numeric/null schema: no free
        # text field can carry provider output.
        metrics = terminal.get("progress_metrics")
        if isinstance(metrics, dict):
            for key, value in metrics.items():
                if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                    continue
                self.assertIsInstance(value, list, key)
                self.assertTrue(all(isinstance(code, str) for code in value), key)
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            self.assertTrue(runner.profile_auto_disabled("standard-deepseek"))
            self.assertEqual(runner.enabled_profiles("standard"),
                             ["standard-glm", "standard-minimax", "standard-mimo"])
            with self.assertRaisesRegex(runner.RunnerError, "auto-disabled"):
                runner.select_profile("standard-deepseek", "standard", "again",
                                      check_existing=False)
            with mock.patch.object(runner.secrets, "randbelow", return_value=1):
                self.assertEqual(
                    runner.select_profile("auto", "standard", "after", check_existing=False),
                    ("standard-minimax", 1, "random_enabled_profile"))

    def test_retry_bounds_end_the_run_with_a_sanitized_code(self):
        burst = {"FAKE_PI_MODE": "ok", "FAKE_PI_RETRY_SCENARIO": "burst",
                 "FAKE_PI_RETRY_COUNT": "3"}
        # One allowed retry below the count, so the third start is refused.
        result = self.run_worker("standard-glm", "standard", mode="ok",
                                 delegation="retry-limit",
                                 extra=["--max-auto-retries", "1"],
                                 env_extra=burst)
        self.assertNotEqual(result.returncode, 0)
        terminal = [r for r in self.records_for_today()
                    if r["delegation_id"] == "retry-limit"][0]
        self.assertEqual(terminal["failure"]["code"], "retry_limit")
        self.assertEqual(terminal["failure"]["stage"], "rpc")
        self.assertEqual(terminal["outcome"]["status"], "failed")
        # The metrics reached before the bound fired are preserved.
        self.assertEqual(terminal["progress_metrics"]["retry_count"], 2)
        self.assertGreater(terminal["progress_metrics"]["retry_seconds"], 0.0)
        self.assertNotIn("SECRET-PROVIDER-ERROR", json.dumps(terminal))
        self.assertNotIn("SECRET-PROVIDER-ERROR", result.stderr)
        self.assertNotIn("provider_usage_limit", json.dumps(terminal))
        # The same run completes when the count allows every retry.
        result = self.run_worker("standard-glm", "standard", mode="ok",
                                 delegation="retry-ok",
                                 extra=["--max-auto-retries", "5"],
                                 env_extra=burst)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = [r for r in self.records_for_today()
                   if r["delegation_id"] == "retry-ok"][0]
        self.assertEqual(receipt["outcome"]["status"], "completed")
        self.assertEqual(receipt["progress_metrics"]["retry_count"], 3)

    def test_retry_budget_ends_a_silent_retry_and_zero_only_disables_time(self):
        silent = {"FAKE_PI_MODE": "ok", "FAKE_PI_RETRY_SCENARIO": "silent",
                  "FAKE_PI_HANG_SECONDS": "60"}
        result = self.run_worker("standard-glm", "standard", mode="ok",
                                 delegation="retry-budget", timeout="60",
                                 extra=["--retry-budget-seconds", "0.4",
                                        "--max-auto-retries", "10"],
                                 env_extra=silent)
        self.assertNotEqual(result.returncode, 0)
        terminal = [r for r in self.records_for_today()
                    if r["delegation_id"] == "retry-budget"][0]
        self.assertEqual(terminal["failure"]["code"], "retry_budget_exceeded")
        self.assertEqual(terminal["failure"]["stage"], "rpc")
        self.assertNotIn("SECRET-PROVIDER-ERROR", json.dumps(terminal))
        # 0 disables the time bound only: the count still ends the run.
        burst = {"FAKE_PI_MODE": "ok", "FAKE_PI_RETRY_SCENARIO": "burst",
                 "FAKE_PI_RETRY_COUNT": "3"}
        result = self.run_worker("standard-glm", "standard", mode="ok",
                                 delegation="retry-budget-zero",
                                 extra=["--retry-budget-seconds", "0"],
                                 env_extra=burst)
        self.assertNotEqual(result.returncode, 0)
        terminal = [r for r in self.records_for_today()
                    if r["delegation_id"] == "retry-budget-zero"][0]
        self.assertEqual(terminal["failure"]["code"], "retry_limit")

    def test_invalid_retry_bounds_are_rejected_before_any_side_effect(self):
        # Rejected in launch validation: no record, no routing state, no Pi.
        for flag, value in (("--max-auto-retries", "-1"),
                            ("--retry-budget-seconds", "nan"),
                            ("--retry-budget-seconds", "inf"),
                            ("--retry-budget-seconds", "-1")):
            with self.subTest(flag=flag, value=value):
                result = self.run_worker(
                    "standard-glm", "standard", mode="ok", delegation="invalid",
                    extra=[f"{flag}={value}"])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(flag.strip("-"), result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])
        # A non-integer count is an argparse error (exit 2), also with no record.
        result = self.run_worker("standard-glm", "standard", mode="ok",
                                 delegation="invalid",
                                 extra=["--max-auto-retries=1.5"])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_auto_disabled_sticky_delegation_redraws(self):
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.secrets, "randbelow", return_value=1):
                pinned = runner.reserve_selection("pin", "auto", "standard")
            self.assertEqual(pinned, ("standard-deepseek", 1, "random_enabled_profile"))
            runner.auto_disable_profile("standard-deepseek", run_id="r")
            with mock.patch.object(runner.secrets, "randbelow", return_value=0):
                redrawn = runner.reserve_selection("pin", "auto", "standard")
            self.assertEqual(redrawn, ("standard-glm", 0, "auto_disabled_redraw"))
            self.assertEqual(runner.reserve_selection("pin", "auto", "standard"), redrawn)

    def test_auto_disable_expiry_and_reset(self):
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records),
                                          "PI_WORKER_AUTO_DISABLE_SECONDS": "0"}):
            entry = runner.auto_disable_profile("standard-deepseek", run_id="r")
            self.assertIsNone(entry["expires_at"])
            self.assertTrue(runner.profile_auto_disabled("standard-deepseek"))
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            def expire(data):
                data["standard-deepseek"]["expires_at"] = "2020-01-01T00:00:00Z"
            runner.update_profile_state(expire)
            self.assertFalse(runner.profile_auto_disabled("standard-deepseek"))
            self.assertIn("standard-deepseek", runner.enabled_profiles("standard"))
            runner.auto_disable_profile("standard-deepseek", run_id="r2")
            self.assertTrue(runner.profile_auto_disabled("standard-deepseek"))
            self.assertNotIn("standard-deepseek", runner.enabled_profiles("standard"))
            runner.reset_profile_disabled("standard-deepseek")
            self.assertFalse(runner.profile_auto_disabled("standard-deepseek"))
            self.assertEqual(runner.enabled_profiles("standard"),
                             ["standard-glm", "standard-deepseek",
                              "standard-minimax", "standard-mimo"])

    def test_profile_state_cli_shows_and_resets(self):
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            runner.auto_disable_profile("standard-glm-zai", run_id="r")
        shown = subprocess.run([sys.executable, str(RUNNER), "--show-profile-state"],
                               env=self.env, text=True, capture_output=True)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        state = json.loads(shown.stdout)
        self.assertTrue(state["standard-glm-zai"]["auto_disabled"])
        self.assertFalse(state["standard-glm"]["auto_disabled"])
        self.assertEqual(state["standard-glm-zai"]["provider"], "test-provider-retired")
        reset = subprocess.run([sys.executable, str(RUNNER), "--reset-profile-state",
                                "standard-glm-zai"], env=self.env, text=True,
                               capture_output=True)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertFalse(json.loads(reset.stdout)["standard-glm-zai"]["auto_disabled"])
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_failure_modes_and_missing_usage(self):
        for mode in ("auth-fail", "model-missing", "exit", "provider-error"):
            result = self.run_worker(mode=mode, delegation=mode)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("secret provider detail", result.stderr)
        result = self.run_worker(mode="missing-usage", delegation="usage")
        self.assertEqual(result.returncode, 0, result.stderr)
        terminal = [r for r in self.records_for_today() if r["delegation_id"] == "usage"][0]
        self.assertIsNone(terminal["usage"]["total_tokens"])
        auth = [r for r in self.records_for_today() if r["delegation_id"] == "auth-fail"][0]
        self.assertEqual(auth["failure"]["code"], "authentication_failed")
        self.assertEqual(auth["failure"]["stage"], "preflight")

    def test_timeout_sends_abort_and_receipt(self):
        marker = self.base / "abort"
        self.env["FAKE_ABORT_MARKER"] = str(marker)
        result = self.run_worker(mode="timeout", delegation="timeout", timeout="0.2")
        self.assertEqual(result.returncode, 124)
        self.assertTrue(marker.exists())
        self.assertEqual(self.records_for_today()[-1]["failure"]["category"], "timeout")

    def test_retired_routing_class_is_rejected_and_standard_is_sticky(self):
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            # The retired routing class has no profiles at all: the internal
            # selection, reservation, and sticky-validation APIs all refuse it
            # consistently instead of inventing a pool or selecting a worker.
            for call in (lambda: runner.select_profile("auto", "simple", "simple"),
                         lambda: runner.reserve_selection("same", "auto", "simple"),
                         lambda: runner.validate_sticky_reservation(
                             "same", "auto", "simple")):
                with self.assertRaisesRegex(runner.RunnerError,
                                            "unsupported routing class"):
                    call()
            self.assertEqual(runner.enabled_profiles("simple"), [])
            first = runner.reserve_selection("same", "auto", "standard")
            second = runner.reserve_selection("same", "auto", "standard")
            self.assertIn(first[0], ("standard-glm", "standard-deepseek",
                                     "standard-minimax", "standard-mimo"))
            self.assertEqual(first[2], "random_enabled_profile")
            self.assertEqual(first, second)

    def test_standard_auto_draws_among_enabled_profiles(self):
        # Production regression: the auto pool is exactly the four enabled
        # standard profiles, in table order, drawn uniformly. The disabled
        # standard-glm-zai entry keeps its table slot so historical records and
        # their old draw indexes stay interpretable; the live draw index is the
        # 0-based position in the enabled pool.
        self.assertEqual(runner.enabled_profiles("standard"),
                         ["standard-glm", "standard-deepseek", "standard-minimax",
                          "standard-mimo"])
        self.assertEqual(runner.enabled_profiles("simple"), [])
        expected = [("standard-glm", 0), ("standard-deepseek", 1),
                    ("standard-minimax", 2), ("standard-mimo", 3)]
        for draw, (profile, index) in enumerate(expected):
            with mock.patch.object(runner.secrets, "randbelow", return_value=draw):
                self.assertEqual(
                    runner.select_profile("auto", "standard", f"d{draw}", check_existing=False),
                    (profile, index, "random_enabled_profile"))

    def test_mimo_auto_draw_index_three_and_sticky_reuse(self):
        # The new profile occupies the fourth enabled-pool slot (index 3) and,
        # once drawn, stays pinned to the delegation ID so retries reuse it.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.secrets, "randbelow", return_value=3):
                first = runner.reserve_selection("mimo-sticky", "auto", "standard")
            self.assertEqual(first, ("standard-mimo", 3, "random_enabled_profile"))
            self.assertEqual(
                runner.reserve_selection("mimo-sticky", "auto", "standard"), first)

    def test_auto_disabled_mimo_is_skipped_by_fresh_draw(self):
        # Generic quota auto-disable: the new profile is dropped from the pool
        # and a fresh auto draw never selects it.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            runner.auto_disable_profile("standard-mimo", run_id="r")
            self.assertEqual(runner.enabled_profiles("standard"),
                             ["standard-glm", "standard-deepseek", "standard-minimax"])
            with mock.patch.object(runner.secrets, "randbelow", return_value=2):
                self.assertEqual(
                    runner.select_profile("auto", "standard", "skip-mimo",
                                          check_existing=False),
                    ("standard-minimax", 2, "random_enabled_profile"))

    def test_explicit_mimo_profile_accepted_in_normal_and_dry_run(self):
        # Explicit standard-mimo is an enabled profile: --validate-only accepts
        # it without side effects, and a normal launch clears the fake catalog
        # preflight (test-provider-a/test-model-d) and completes.
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        result = subprocess.run(
            self._validate_only_routing_command(
                delegation="mimo-vo", routing_class="standard",
                profile="standard-mimo"),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        validated = json.loads(result.stdout)["validated"]
        self.assertEqual(validated["profile"], "standard-mimo")
        self.assertEqual(validated["profile_source"], "explicit")
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        result = self.run_worker("standard-mimo", "standard", mode="ok",
                                 delegation="mimo-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        terminal = [r for r in self.records_for_today()
                    if r["delegation_id"] == "mimo-run"][0]
        self.assertEqual(terminal["worker"]["worker_type"], "standard-mimo")
        self.assertEqual(terminal["worker"]["provider"], "test-provider-a")
        self.assertEqual(terminal["worker"]["model"], "test-model-d")

    def test_escalation_runtime_is_fully_removed(self):
        # There is no escalation code path left: the CLI flag is gone and no
        # public helper accepts the retired escalation keyword.
        self.assertNotIn("simple_failure_evidence", dir(runner))
        for func in (runner.reserve_selection, runner.validate_sticky_reservation,
                     runner.select_profile, runner.draw_profile):
            self.assertNotIn("escalate_to_standard", inspect.signature(func).parameters)
        env = self.env | {"FAKE_PI_MODE": "ok"}
        result = subprocess.run(
            self.command("standard-glm", "standard", delegation="esc-flag",
                         extra=["--escalate-to-standard"]),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertEqual(self._sidecar_files(), [])

    def test_retired_profile_is_absent_from_the_table(self):
        # Production regression: the retired simple profile is fully removed
        # from PROFILES (no disabled placeholder), and every standard profile
        # keeps its exact fields.
        self.assertNotIn("simple-m3", runner.PROFILES)
        self.assertEqual(list(runner.PROFILES),
                         ["standard-glm", "standard-deepseek", "standard-glm-zai",
                          "standard-minimax", "standard-mimo"])
        self.assertEqual(runner.PROFILES["standard-minimax"]["routing_class"], "standard")
        self.assertEqual(runner.PROFILES["standard-minimax"]["provider"], "test-provider-b")
        self.assertEqual(runner.PROFILES["standard-minimax"]["model"],
                         "test-model-c")
        # SUB-only worker table: disabled entries stay registered, and no
        # credential field is carried (auth is solely Pi's default).
        self.assertEqual(runner.PROFILES["standard-minimax"]["roles"], ["sub"])
        self.assertNotIn("credential", runner.PROFILES["standard-minimax"])
        self.assertTrue(runner.PROFILES["standard-minimax"]["enabled"])
        for profile in ("standard-glm", "standard-deepseek"):
            self.assertTrue(runner.PROFILES[profile]["enabled"], profile)
        # The zai worker is permanently cancelled but keeps its registered
        # provider/model/credential routing so historical records stay readable.
        self.assertFalse(runner.PROFILES["standard-glm-zai"]["enabled"])
        self.assertEqual(runner.PROFILES["standard-glm-zai"]["routing_class"], "standard")
        # Appended after the existing standard profiles so recorded draw indices
        # for previously enabled pools stay stable.
        self.assertEqual(list(runner.PROFILES),
                         ["standard-glm", "standard-deepseek", "standard-glm-zai",
                          "standard-minimax", "standard-mimo"])

    def test_fresh_home_without_any_config_has_no_profiles(self):
        # Open-source release guarantee: a clean machine with no configuration
        # ships NO built-in provider, model, or profile. Auto launch fails with
        # an informative no_enabled_profile error before any reservation,
        # provider preflight, Pi spawn, or ledger write, and there is no
        # fallback provider. The fixture env var is explicitly absent here.
        home = self.base / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(home), "PI_WORKER_RECORD_DIR": str(self.records),
               "PI_WORKER_PI": str(FAKE), "FAKE_PI_MODE": "ok"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--thinking", "medium", "--tools", "write",
               "--delegation-id", "fresh-auto", "--timeout", "5"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("no enabled standard profile", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        # An explicit empty profiles file behaves identically, and an explicit
        # valid file overrides everything (--validate-only succeeds).
        empty = self.base / "empty-profiles.json"
        empty.write_text(json.dumps({"version": 1, "profiles": {}}))
        for extra in (["--profiles-file", str(empty)],):
            result = subprocess.run(
                self.command("auto", "standard", delegation="fresh-empty",
                             extra=extra + ["--validate-only"]),
                env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("no enabled standard profile", result.stderr)
        result = subprocess.run(
            self.command("auto", "standard", delegation="fresh-explicit",
                         extra=["--profiles-file", str(FIXTURE), "--validate-only"]),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["validated"]["profile"], "auto")

    def test_retired_profile_requests_are_rejected_before_side_effects(self):
        # Production regression: the retired routing class and profile name are
        # refused by argument parsing in normal mode and under --validate-only,
        # before any routing, record, or provider side effect.
        env = self.env | {"FAKE_PI_MODE": "ok"}
        cases = (
            ("class", ["--routing-class", "simple"], "invalid choice: 'simple'"),
            ("profile", ["--profile", "simple-m3"], "invalid --profile: simple-m3"),
        )
        for label, extra, message in cases:
            for dry_run in (False, True):
                with self.subTest(label=label, dry_run=dry_run):
                    cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
                           "--contract", "Do it", "--thinking", "medium",
                           "--tools", "write", "--delegation-id", f"retired-{label}"]
                    cmd.extend(extra)
                    if dry_run:
                        cmd.append("--validate-only")
                    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(message, result.stderr)
                    # No ledger record, routing state, or profile-state effect.
                    self.assertEqual(list(self.records.glob("*.jsonl")), [])
                    self.assertFalse((self.records / ".routing-state.json").exists())
                    self.assertFalse((self.records / "profile-state.json").exists())

    def test_retired_m3_fallback_flag_is_gone(self):
        # Production regression: the retired M3 fallback marker has no CLI flag
        # and no branch left. Normal and dry-run modes both refuse it during
        # argument parsing, before launch, routing state, or any record, and a
        # new ordinary receipt carries routing_lesson null.
        for dry_run in (False, True):
            with self.subTest(mode="dry-run" if dry_run else "normal"):
                env = self.env | {"FAKE_PI_MODE": "ok"}
                result = subprocess.run(
                    self.command("standard-glm", "standard",
                                 delegation="m3-flag" if not dry_run else "m3-flag-dry",
                                 extra=["--m3-fallback-used"] + (
                                     ["--validate-only"] if dry_run else [])),
                    env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])
                self.assertEqual(self._sidecar_files(), [])
                self.assertFalse((self.records / ".routing-state.json").exists())
                self.assertFalse((self.records / "profile-state.json").exists())
        # An ordinary run and its terminal receipt keep the field, always null.
        result = self.run_worker("standard-glm", "standard", delegation="m3-null")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout)["routing_lesson"])
        terminal = [r for r in self.records_for_today()
                    if r["delegation_id"] == "m3-null"][0]
        self.assertIn("routing_lesson", terminal)
        self.assertIsNone(terminal["routing_lesson"])
        # A stale Namespace that still carries the old attribute is ignored:
        # neither helper reads it, and no fallback string is produced.
        args = runner.parse_args(["--workdir", str(self.work), "--contract", "Do it",
                                  "--tools", "write", "--delegation-id", "m3-stale",
                                  "--review-status", "accepted"])
        args.thinking = "medium"
        args.profile_source = "auto"
        args.main_rework = "none"
        args.m3_fallback_used = True
        self.assertIsNone(runner.base_record(
            args, "standard-minimax", None, "random_enabled_profile")["routing_lesson"])
        self.assertIsNone(runner.legacy_review_event(args, "run-id")["routing_lesson"])

    def test_default_routing_class_is_standard(self):
        # Omitting --routing-class (and --profile) resolves to the production
        # default: a uniform draw among the four enabled standard profiles.
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--thinking", "medium", "--tools", "write",
               "--delegation-id", "default-class", "--validate-only"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        validated = json.loads(result.stdout)["validated"]
        self.assertEqual(validated["routing_class"], "standard")
        self.assertEqual(validated["profile"], "auto")
        # A real run without routing arguments settles on one standard worker.
        env = self.env | {"FAKE_PI_MODE": "ok"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--thinking", "medium", "--tools", "write",
               "--delegation-id", "default-class-run", "--timeout", "5"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["routing"]["routing_class"], "standard")
        self.assertEqual(receipt["routing"]["random_policy"], "random_enabled_profile")
        self.assertIn(receipt["worker"]["worker_type"],
                      ("standard-glm", "standard-deepseek",
                       "standard-minimax", "standard-mimo"))

    def test_agents_retired_profile_override_is_rejected(self):
        # A legacy AGENTS.md Profile override naming the removed profile parses
        # but is rejected as an invalid profile, before any side effect.
        (self.work / "AGENTS.md").write_text(
            "# X\n## Pi Worker\n- Profile: simple-m3\n- Thinking: medium\n")
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--thinking", "medium", "--tools", "write",
               "--delegation-id", "agents-legacy", "--validate-only"]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid AGENTS.md Profile: simple-m3", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())

    def test_all_standard_profiles_enabled(self):
        self.assertEqual(runner.PROFILES["standard-glm"]["provider"], "test-provider-a")
        self.assertEqual(runner.PROFILES["standard-glm"]["model"], "test-model-a")
        self.assertEqual(runner.PROFILES["standard-deepseek"]["provider"], "test-provider-a")
        self.assertEqual(runner.PROFILES["standard-deepseek"]["model"], "test-model-b")
        self.assertEqual(runner.PROFILES["standard-minimax"]["provider"], "test-provider-b")
        # New standard worker: exact synthetic model id on the synthetic provider
        # as a registered SUB role.
        self.assertEqual(runner.PROFILES["standard-mimo"]["routing_class"], "standard")
        self.assertEqual(runner.PROFILES["standard-mimo"]["provider"], "test-provider-a")
        self.assertEqual(runner.PROFILES["standard-mimo"]["model"], "test-model-d")
        self.assertEqual(runner.PROFILES["standard-mimo"]["roles"], ["sub"])
        self.assertNotIn("credential", runner.PROFILES["standard-mimo"])
        self.assertTrue(runner.PROFILES["standard-mimo"]["enabled"])
        for profile in ("standard-glm", "standard-deepseek", "standard-minimax",
                        "standard-mimo"):
            self.assertTrue(runner.PROFILES[profile]["enabled"], profile)

    def test_zai_standard_worker_is_disabled_but_still_registered(self):
        # Permanent user cancellation of test-provider-retired/test-model-a as a worker.
        # Selection only: the entry keeps its provider, model, roles, and
        # routing class so historical records and receipts stay readable.
        zai = runner.PROFILES["standard-glm-zai"]
        self.assertFalse(zai["enabled"])
        self.assertEqual(zai["routing_class"], "standard")
        self.assertEqual(zai["provider"], "test-provider-retired")
        self.assertEqual(zai["model"], "test-model-a")
        self.assertEqual(zai["roles"], ["sub"])
        self.assertNotIn("credential", zai)
        # Table order is unchanged: the entry keeps its slot so old draw indexes
        # stay historical evidence.
        self.assertEqual(list(runner.PROFILES),
                         ["standard-glm", "standard-deepseek", "standard-glm-zai",
                          "standard-minimax", "standard-mimo"])
        # Auto routing never offers the retired worker.
        self.assertEqual(runner.enabled_profiles("standard"),
                         ["standard-glm", "standard-deepseek", "standard-minimax",
                          "standard-mimo"])
        with self.assertRaisesRegex(runner.RunnerError, "currently disabled"):
            runner.select_profile("standard-glm-zai", "standard", "zai",
                                  check_existing=False)
        # A historical record naming the profile still reads unchanged.
        record = {"schema_version": 3, "record_type": "run", "phase": "completed",
                  "run_id": "old-zai", "delegation_id": "old-zai",
                  "worker": {"worker_type": "standard-glm-zai",
                             "provider": "test-provider-retired", "model": "test-model-a"},
                  "routing": {"routing_class": "standard",
                              "profile_source": "auto", "random_draw": 2},
                  "outcome": {"status": "completed"}}
        self.assertEqual(record["worker"]["worker_type"], "standard-glm-zai")
        self.assertEqual(record["routing"]["random_draw"], 2)
        self.assertEqual(runner.profile_state_snapshot()["standard-glm-zai"]["configured"],
                         "disabled")

    def test_zai_launches_rejected_in_normal_and_dry_run(self):
        # Production regression: the retired zai worker is refused for new work
        # in normal mode and under --validate-only, with no side effects.
        env = self.env | {"FAKE_PI_MODE": "ok"}
        for mode_flag in ([], ["--validate-only"]):
            with self.subTest(dry_run=bool(mode_flag)):
                cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
                       "--contract", "Do it", "--thinking", "medium",
                       "--tools", "write", "--delegation-id", "zai-reject"]
                cmd.extend(["--routing-class", "standard",
                            "--profile", "standard-glm-zai"])
                cmd.extend(mode_flag)
                result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("currently disabled", result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])
                self.assertFalse((self.records / ".routing-state.json").exists())
                self.assertFalse((self.records / "profile-state.json").exists())

    def test_agents_override(self):
        (self.work / "AGENTS.md").write_text("# X\n## Pi Worker\n- Profile: standard-minimax\n- Thinking: high\n")
        self.assertEqual(runner.project_overrides(self.work), {"profile": "standard-minimax", "thinking": "high"})

    def test_privacy_safe_routing_features(self):
        args = runner.parse_args(["--workdir", str(self.work), "--contract", "secret full prompt",
                                  "--tools", "write", "--delegation-id", "features",
                                  "--task-kind", "implementation", "--estimated-files", "4",
                                  "--risk", "low", "--acceptance-mode", "deterministic"])
        args.thinking = "medium"
        args.profile_source = "auto"
        args.review_status = "pending"
        args.main_rework = "none"
        receipt = runner.base_record(args, "standard-minimax", None, "random_enabled_profile")
        self.assertEqual(receipt["assignment"]["features"]["estimated_files"], 4)
        self.assertFalse(receipt["assignment"]["metadata_quality"]["complete"])
        self.assertNotIn("secret full prompt", json.dumps(receipt))

    def test_pi_default_auth_exposes_no_credential_bridge_apis(self):
        # Auth is solely Pi's default (stored `pi auth`): the runner manages no
        # credential environment variable and spawns no login shell. The old
        # bridge API is deleted, not retained for tests.
        for attr in ("credential_env", "_bridge_shell", "CREDENTIAL_SHELL_BRIDGE",
                     "CREDENTIAL_BRIDGE_TIMEOUT_SECONDS"):
            self.assertFalse(hasattr(runner, attr), attr)

    def test_pi_default_auth_needs_no_managed_credential_key(self):
        # No provider credential environment variable is required any more: a
        # worker runs with the retired fake keys removed and Pi inherits the
        # runner environment unchanged.
        env = {k: v for k, v in self.env.items()
               if k not in ("OPENCODE_API_KEY", "ZAI_CODING_CN_API_KEY",
                            "MINIMAX_CN_API_KEY")}
        env["FAKE_PI_MODE"] = "ok"
        result = subprocess.run(
            self.command("standard-minimax", "standard",
                         delegation="pi-default-auth"),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_concurrent_append_json_integrity_and_rotation(self):
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}), \
                mock.patch.object(runner, "local_day", return_value="2026-10-07"):
            # Frozen reference local day (mocked, not weakened retention): both
            # fixture days stay inside the 14-day retention window, so the
            # automatic post-append prune never deletes them.
            import multiprocessing
            context = multiprocessing.get_context("fork")
            procs = [context.Process(target=runner.append_record, args=({"n": i},), kwargs={"day": "2026-10-07"}) for i in range(30)]
            for p in procs:
                p.start()
            for p in procs:
                p.join()
            runner.append_record({"n": 31}, day="2026-10-06")
        first = [json.loads(x) for x in (self.records / "2026-10-07.jsonl").read_text().splitlines()]
        self.assertEqual(len(first), 30)
        self.assertTrue((self.records / "2026-10-06.jsonl").exists())

    def test_two_column_catalog_and_single_receipt_per_run(self):
        self.env["FAKE_MODEL_COLUMNS"] = "1"
        result = self.run_worker(delegation="columns")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.records_for_today()), 1)

    def test_legacy_review_flags_append_review_event(self):
        command = self.command("standard-minimax", "standard", delegation="legacy-review")
        command.extend(["--review-status", "accepted", "--verification", "pytest::passed"])
        result = subprocess.run(command, env=self.env | {"FAKE_PI_MODE": "ok"},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.records_for_today()
        self.assertEqual([item["record_type"] for item in records], ["run", "review"])
        self.assertEqual(records[0]["outcome"]["review_verdict"], "pending")
        self.assertEqual(records[1]["outcome"]["review_verdict"], "accepted")

    def test_read_only_tool_allowlist_and_context_files_enabled(self):
        captured = {}
        class Dummy:
            stdin = stdout = stderr = None
        with mock.patch.object(runner.subprocess, "Popen", side_effect=lambda cmd, **kwargs: captured.setdefault("cmd", cmd) or Dummy()):
            with self.assertRaises(Exception):
                runner.run_rpc("pi", self.work, "x", "standard-minimax", "medium", "read-only", 1, {}, "d")
        self.assertEqual(captured["cmd"][captured["cmd"].index("--tools") + 1], "read,grep,find,ls")
        self.assertNotIn("--no-context-files", captured["cmd"])

    # ----- Pi-main intercom transport (opt-in, explicit only) -----

    def _intercom_args(self, *, extension_path=None, supervisor="11111111-2222-3333-4444-555555555555"):
        ext = extension_path or self._write_extension()
        return ["--transport", "intercom",
                "--intercom-extension", str(ext),
                "--supervisor", supervisor]

    def _write_extension(self):
        ext = self.base / "trusted-intercom.ts"
        ext.write_text("// stub trusted intercom extension\n")
        return ext

    def test_validate_intercom_args_rejects_invalid_combinations_before_routing(self):
        ext = self._write_extension()
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            # RPC mode rejects intercom-specific args.
            with self.assertRaisesRegex(runner.RunnerError, "rejects --intercom-extension"):
                runner.validate_intercom_args("rpc", str(ext),
                                               "11111111-2222-3333-4444-555555555555")
            with self.assertRaisesRegex(runner.RunnerError, "rejects --intercom-extension"):
                runner.validate_intercom_args("rpc", None, "11111111-2222-3333-4444-555555555555")
            # Intercom mode requires both extension and supervisor.
            with self.assertRaisesRegex(runner.RunnerError, "requires --intercom-extension"):
                runner.validate_intercom_args("intercom", None, "11111111-2222-3333-4444-555555555555")
            with self.assertRaisesRegex(runner.RunnerError, "requires --supervisor"):
                runner.validate_intercom_args("intercom", str(ext), None)
            # Extension must be absolute and a real .ts/.js file.
            with self.assertRaisesRegex(runner.RunnerError, "must be an absolute path"):
                runner.validate_intercom_args("intercom", "relative/path.ts",
                                               "11111111-2222-3333-4444-555555555555")
            with self.assertRaisesRegex(runner.RunnerError, "must end with .ts or .js"):
                runner.validate_intercom_args("intercom", str(self.base / "wrong.txt"),
                                               "11111111-2222-3333-4444-555555555555")
            missing = self.base / "absent.ts"
            with self.assertRaisesRegex(runner.RunnerError, "file not found"):
                runner.validate_intercom_args("intercom", str(missing),
                                               "11111111-2222-3333-4444-555555555555")
            # Supervisor must be a strict full UUID; short roster ids are not accepted.
            for bad in ("short-id", "11111111-2222-3333-4444", "not-a-uuid",
                        "11111111222233334444555555555555"):
                with self.assertRaisesRegex(runner.RunnerError, "must be a full UUID"):
                    runner.validate_intercom_args("intercom", str(ext), bad)
            # Valid combo returns the resolved absolute extension and the supervisor id.
            resolved, supervisor = runner.validate_intercom_args(
                "intercom", str(ext), "11111111-2222-3333-4444-555555555555")
            self.assertTrue(Path(resolved).is_absolute())
            self.assertEqual(resolved, str(ext.resolve()))
            self.assertEqual(supervisor, "11111111-2222-3333-4444-555555555555")
            # RPC default still validates cleanly with no intercom args.
            self.assertEqual(runner.validate_intercom_args("rpc", None, None), (None, None))

    def test_default_rpc_unchanged_in_pi_env(self):
        argv_file = self.base / "argv.json"
        base_env = {"FAKE_PI_MODE": "ok", "FAKE_PI_ARGV_FILE": str(argv_file)}
        # The default receipt shape is unchanged whether the harness looks like Pi
        # (PI_CODING_AGENT=true), is absent, or is explicitly false.
        for label, extra in (("pi-env-true", {"PI_CODING_AGENT": "true"}),
                             ("pi-env-absent", {}),
                             ("pi-env-false", {"PI_CODING_AGENT": "false"})):
            with self.subTest(env=label):
                env = self.env | base_env | extra
                result = subprocess.run(self.command("standard-minimax", "standard",
                                                     delegation=f"pi-default-{label}"),
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(argv_file.read_text())
                # The rpc-mode invocation is the last writer; its argv reflects the real cmd.
                self.assertIn("--mode", argv)
                self.assertIn("rpc", argv)
                self.assertNotIn("-e", argv)
                self.assertNotIn("intercom", argv)
                # Exact DEFAULT RPC argv ordering and the exact DEFAULT receipt shape:
                # no transport field and no other intercom-specific field exists.
                self.assertEqual(
                    argv,
                    [str(FAKE), "--mode", "rpc", "--provider", "test-provider-b",
                     "--model", "test-model-c", "--thinking", "medium",
                     "--tools", "read,bash,edit,write,grep,find,ls",
                     "--no-extensions", "--no-skills", "--no-prompt-templates",
                     "--approve", "--name", f"pi-worker-pi-default-{label}"])
                # Recorded receipt keeps the original default shape: no transport key.
                receipt = self.records_for_today()[-1]
                self.assertNotIn("transport", receipt["worker"])
                self.assertEqual(set(receipt["worker"]),
                                 {"worker_type", "runtime", "provider", "model",
                                  "thinking", "session_id"})
                # Routing policy, profile pinning, and review flags are untouched.
                self.assertEqual(receipt["worker"]["worker_type"], "standard-minimax")
                self.assertEqual(receipt["routing"]["routing_class"], "standard")
                self.assertEqual(receipt["outcome"]["review_verdict"], "pending")

    def test_intercom_optin_adds_extension_and_tool_read_only_keeps_no_edit_write(self):
        argv_file = self.base / "argv.json"
        env = self.env | {"FAKE_PI_MODE": "ok", "FAKE_PI_ARGV_FILE": str(argv_file)}
        cmd = self.command("standard-minimax", "standard", delegation="intercom-ro",
                           tools="read-only", extra=self._intercom_args())
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(argv_file.read_text())
        # Exact DEFAULT RPC argv ordering, restored verbatim; optin only extends it.
        self.assertEqual(
            argv[:8],
            [str(FAKE), "--mode", "rpc", "--provider", "test-provider-b",
             "--model", "test-model-c", "--thinking"])
        self.assertEqual(argv[8], "medium")
        self.assertEqual(argv[9:11], ["--tools", "read,grep,find,ls,intercom"])
        self.assertEqual(argv[11:13], ["-e", str((self.base / "trusted-intercom.ts").resolve())])
        self.assertEqual(argv[13:],
                         ["--no-extensions", "--no-skills", "--no-prompt-templates",
                          "--approve", "--name", "pi-worker-intercom-ro"])
        # Only the trusted extension is loaded; no other skills/extensions are enabled.
        self.assertEqual(argv.count("-e"), 1)
        self.assertEqual(argv.count("--no-extensions"), 1)
        self.assertIn("--no-skills", argv)
        self.assertIn("--no-prompt-templates", argv)
        tools_value = argv[argv.index("--tools") + 1]
        # intercom is appended, but read-only still excludes bash/edit/write.
        self.assertEqual(tools_value, "read,grep,find,ls,intercom")
        for forbidden in ("bash", "edit", "write"):
            self.assertNotIn(forbidden, tools_value.split(","))
        # Receipt records the intercom transport but no supervisor id or extension path.
        receipt = self.records_for_today()[0]
        self.assertEqual(receipt["worker"]["transport"], "intercom")
        serialized = json.dumps(receipt)
        self.assertNotIn("11111111-2222-3333-4444-555555555555", serialized)
        self.assertNotIn("trusted-intercom.ts", serialized)

    def test_intercom_optin_keeps_full_tool_set_for_write_mode(self):
        argv_file = self.base / "argv.json"
        env = self.env | {"FAKE_PI_MODE": "ok", "FAKE_PI_ARGV_FILE": str(argv_file)}
        cmd = self.command("standard-minimax", "standard", delegation="intercom-write",
                           tools="write", extra=self._intercom_args())
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(argv_file.read_text())
        tools_value = argv[argv.index("--tools") + 1]
        self.assertEqual(tools_value,
                         "read,bash,edit,write,grep,find,ls,intercom")

    def test_intercom_rpc_rejects_intercom_args_before_routing(self):
        env = self.env | {"FAKE_PI_MODE": "ok"}
        cmd = self.command("standard-minimax", "standard", delegation="reject")
        cmd.extend(["--transport", "rpc", "--intercom-extension", "/abs/extension.ts"])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        # No routing side effect: nothing was written to the record ledger.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        # The error message must not echo provider details or extension paths.
        self.assertNotIn("/abs/extension.ts", result.stderr)

    def test_intercom_rpc_rejects_supervisor_without_extension(self):
        env = self.env | {"FAKE_PI_MODE": "ok"}
        cmd = self.command("standard-minimax", "standard", delegation="reject2")
        cmd.extend(["--transport", "rpc", "--supervisor",
                    "11111111-2222-3333-4444-555555555555"])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("11111111-2222-3333-4444-555555555555", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_intercom_requires_both_extension_and_supervisor(self):
        env = self.env | {"FAKE_PI_MODE": "ok"}
        ext = self._write_extension()
        # Missing supervisor.
        cmd = self.command("standard-minimax", "standard", delegation="missing-sup")
        cmd.extend(["--transport", "intercom", "--intercom-extension", str(ext)])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--supervisor", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        # Missing extension.
        cmd = self.command("standard-minimax", "standard", delegation="missing-ext")
        cmd.extend(["--transport", "intercom",
                    "--supervisor", "11111111-2222-3333-4444-555555555555"])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--intercom-extension", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_intercom_extension_path_must_be_absolute_ts_or_js(self):
        env = self.env | {"FAKE_PI_MODE": "ok"}
        # Relative path: rejected.
        cwd_cmd = self.command("standard-minimax", "standard", delegation="rel")
        cwd_cmd.extend(["--transport", "intercom", "--intercom-extension", "relative.ts",
                        "--supervisor", "11111111-2222-3333-4444-555555555555"])
        result = subprocess.run(cwd_cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absolute path", result.stderr)
        # Wrong suffix: rejected.
        bad_suffix = self.base / "extension.txt"
        bad_suffix.write_text("x")
        cmd = self.command("standard-minimax", "standard", delegation="suffix")
        cmd.extend(["--transport", "intercom", "--intercom-extension", str(bad_suffix),
                    "--supervisor", "11111111-2222-3333-4444-555555555555"])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".ts or .js", result.stderr)
        # Missing file: rejected.
        missing = self.base / "absent.ts"
        cmd = self.command("standard-minimax", "standard", delegation="missing")
        cmd.extend(["--transport", "intercom", "--intercom-extension", str(missing),
                    "--supervisor", "11111111-2222-3333-4444-555555555555"])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file not found", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_intercom_supervisor_must_be_full_uuid(self):
        env = self.env | {"FAKE_PI_MODE": "ok"}
        ext = self._write_extension()
        for bad in ("short", "11111111-2222-3333-4444",
                    "GGGGGGGG-2222-3333-4444-555555555555"):
            cmd = self.command("standard-minimax", "standard", delegation=f"bad-{bad[:6]}")
            cmd.extend(["--transport", "intercom", "--intercom-extension", str(ext),
                        "--supervisor", bad])
            result = subprocess.run(cmd, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("full UUID", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_intercom_missing_extension_command_fails_before_work_prompt(self):
        argv_file = self.base / "argv.json"
        prompt_file = self.base / "prompts.jsonl"
        env = self.env | {"FAKE_PI_MODE": "ok",
                          "FAKE_PI_ARGV_FILE": str(argv_file),
                          "FAKE_PI_PROMPT_FILE": str(prompt_file),
                          "FAKE_PI_NO_INTERCOM": "1"}
        cmd = self.command("standard-minimax", "standard", delegation="no-cmd",
                           extra=self._intercom_args())
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        # Receipt is appended on failure but records the bounded failure code/stage.
        receipt = [r for r in self.records_for_today()
                   if r["delegation_id"] == "no-cmd"][0]
        self.assertEqual(receipt["failure"]["code"], "intercom_unavailable")
        self.assertEqual(receipt["failure"]["stage"], "preflight")
        self.assertEqual(receipt["worker"]["transport"], "intercom")
        # No task prompt was ever sent to the worker (get_commands is not a prompt).
        if prompt_file.exists():
            prompts = [json.loads(line) for line
                       in prompt_file.read_text().splitlines() if line]
            self.assertEqual(prompts, [])
        # Failure receipt must not echo supervisor id or extension path.
        serialized = json.dumps(receipt)
        self.assertNotIn("11111111-2222-3333-4444-555555555555", serialized)
        self.assertNotIn("trusted-intercom.ts", serialized)

    def test_intercom_preflight_survives_unrelated_response_before_right_one(self):
        # The ok-mode fake always emits an unrelated correlated-shape response
        # (different id and command) before the real get_commands response, so this
        # exercises the runner's id AND command correlation.
        prompt_file = self.base / "prompts.jsonl"
        env = self.env | {"FAKE_PI_MODE": "ok",
                          "FAKE_PI_PROMPT_FILE": str(prompt_file)}
        cmd = self.command("standard-minimax", "standard", delegation="preflight-order",
                           extra=self._intercom_args())
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(prompt_file.exists())

    def test_intercom_preflight_rejects_failed_and_malformed_get_commands(self):
        for mode in ("get-cmds-fail", "get-cmds-malformed"):
            with self.subTest(mode=mode):
                prompt_file = self.base / f"prompts-{mode}.jsonl"
                env = self.env | {"FAKE_PI_MODE": mode,
                                  "FAKE_PI_PROMPT_FILE": str(prompt_file)}
                cmd = self.command("standard-minimax", "standard", delegation=f"pf-{mode}",
                                   extra=self._intercom_args())
                result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                receipt = [r for r in self.records_for_today()
                           if r["delegation_id"] == f"pf-{mode}"][0]
                self.assertEqual(receipt["failure"]["code"], "intercom_unavailable")
                self.assertEqual(receipt["failure"]["stage"], "preflight")
                # No work prompt is sent on a failed preflight.
                self.assertFalse(prompt_file.exists())
        prompt_file = self.base / "prompts.jsonl"
        env = self.env | {"FAKE_PI_MODE": "ok",
                          "FAKE_PI_PROMPT_FILE": str(prompt_file)}
        delegation = "intercom-prompt"
        supervisor = "abcdef01-2345-6789-abcd-ef0123456789"
        cmd = self.command("standard-minimax", "standard", delegation=delegation,
                           extra=self._intercom_args(supervisor=supervisor))
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        prompts = [json.loads(line) for line
                   in prompt_file.read_text().splitlines() if line]
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]["message"]
        # The exact full supervisor UUID is included, not a short roster id.
        self.assertIn(supervisor, prompt)
        self.assertIn(json.dumps(delegation), prompt)
        # Protocol keywords the contract binds the worker to, in real tool syntax.
        for keyword in ("READY", "DONE", "action `send`", "action `ask`",
                        "action `list`", "agent_settled", "main agent"):
            self.assertIn(keyword, prompt)
        # The augmented contract never enters the run receipt.
        receipt = [r for r in self.records_for_today()
                   if r["delegation_id"] == delegation][0]
        serialized = json.dumps(receipt)
        self.assertNotIn(supervisor, serialized)
        self.assertNotIn("trusted-intercom.ts", serialized)
        self.assertNotIn("INTERCOM COMMUNICATION CONTRACT", serialized)

    def test_intercom_receipt_omits_supervisor_path_and_full_contract(self):
        prompt_file = self.base / "prompts.jsonl"
        env = self.env | {"FAKE_PI_MODE": "ok",
                          "FAKE_PI_PROMPT_FILE": str(prompt_file)}
        delegation = "intercom-no-leak"
        unique_contract = "secret-assignment-marker-zz9plural-z-alpha"
        # Embed the unique marker via the contract file to make leak detection tight.
        contract_path = self.base / "contract.txt"
        contract_path.write_text(f"Implement {unique_contract} in module x")
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract-file", str(contract_path),
               "--profile", "standard-minimax", "--routing-class", "standard",
               "--thinking", "medium", "--tools", "write",
               "--delegation-id", delegation, "--timeout", "5"]
        cmd.extend(self._intercom_args())
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["worker"]["transport"], "intercom")
        serialized = json.dumps(receipt)
        # Supervisor UUID and extension path are not recorded anywhere.
        self.assertNotIn("11111111-2222-3333-4444-555555555555", serialized)
        self.assertNotIn("trusted-intercom.ts", serialized)
        # The full assignment text is never persisted in receipts either.
        self.assertNotIn(unique_contract, serialized)

    def test_intercom_contract_appendix_quiet_done_shape(self):
        # Direct unit regression for the appended contract text (package A req 1).
        appendix = runner.intercom_contract_appendix(
            "full supervisor UUID", "testid")
        # Exact supervisor delivery and delegation identifier stay bound.
        self.assertIn("full supervisor UUID", appendix)
        self.assertIn(json.dumps("testid"), appendix)
        # Quiet default: no routine progress mandate; only actionable messages.
        self.assertNotIn("Meaningful progress", appendix)
        for quiet in ("QUIET", "No routine PROGRESS", "courtesy updates",
                      "scope deviation"):
            self.assertIn(quiet, appendix)
        # Bounded DONE shape: limit, paths, checks, limits, unknown IDs.
        for term in ("1200", "relative paths", "max 3", "limits",
                     "known, else unknown", "Never invent"):
            self.assertIn(term, appendix)
        # Security caveats and lifecycle terms are preserved.
        for term in ("NOT authentication", "NOT proof", "action `send`",
                     "action `ask`", "action `list`", "RUN_FINISHED",
                     "not acceptance", "agent_settled", "main agent",
                     "settle immediately"):
            self.assertIn(term, appendix)
        # Small-increment work instruction is present.
        self.assertIn("2-5 minute", appendix)

    # ----- --validate-only (dry-run, no side effects) -----

    def _validate_only_command(self, *, delegation="validate-only", extra=None,
                               contract_file=None, contract=None):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--routing-class", "standard", "--profile", "standard-glm",
               "--thinking", "medium", "--tools", "write",
               "--delegation-id", delegation, "--validate-only"]
        if contract_file is not None:
            cmd.extend(["--contract-file", contract_file])
        elif contract is not None:
            cmd.extend(["--contract", contract])
        else:
            cmd.extend(["--contract", "Do it"])
        if extra:
            cmd.extend(extra)
        return cmd

    def _sidecar_files(self):
        if not self.records.exists():
            return []
        return sorted(p.name for p in self.records.iterdir())

    def test_validate_only_has_no_routing_state_record_provider_or_worker_side_effects(self):
        # Use a fake_pi that would fail noisily if invoked; success of validate-only
        # therefore proves the runner never reached the provider check or worker.
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        result = subprocess.run(self._validate_only_command(delegation="vo-ok"),
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # No JSONL records were appended.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        # No routing sidecar or profile-state sidecar was created or written.
        self.assertEqual(self._sidecar_files(), [])
        # The success result is a small JSON validation summary.
        result_doc = json.loads(result.stdout)
        self.assertEqual(result_doc["status"], "valid")
        validated = result_doc["validated"]
        # The summary is small: only resolved routing/launch metadata is exposed.
        self.assertEqual(set(validated.keys()),
                         {"workdir", "routing_class", "profile", "profile_source",
                          "thinking", "transport", "tools", "receipt_file",
                          "task_kind", "acceptance_mode", "risk"})
        self.assertEqual(validated["routing_class"], "standard")
        self.assertEqual(validated["profile"], "standard-glm")
        self.assertEqual(validated["profile_source"], "explicit")
        self.assertEqual(validated["transport"], "rpc")
        self.assertEqual(validated["tools"], "write")
        self.assertEqual(validated["task_kind"], "other")
        # The summary never includes the supervisor UUID, the extension path, or
        # the contract content. Run it again with sensitive intercom inputs and
        # a unique contract marker to verify.
        ext = self._write_extension()
        supervisor = "22222222-3333-4444-5555-666666666666"
        unique_contract = "secret-contract-marker-vo-alpha-9"
        contract_path = self.base / "secret-contract.txt"
        contract_path.write_text(f"Implement {unique_contract}")
        result = subprocess.run(self._validate_only_command(
            delegation="vo-leak-check", extra=self._intercom_args(
                extension_path=ext, supervisor=supervisor),
            contract_file=str(contract_path)), env=env, text=True,
            capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertEqual(self._sidecar_files(), [])
        # Both stdout and stderr (the only places validate-only can leak) must
        # contain none of the sensitive inputs.
        for stream in (result.stdout, result.stderr):
            self.assertNotIn(supervisor, stream)
            self.assertNotIn("trusted-intercom.ts", stream)
            self.assertNotIn(unique_contract, stream)
        # The resolved transport is recorded as intercom but no extension path.
        validated = json.loads(result.stdout)["validated"]
        self.assertEqual(validated["transport"], "intercom")
        self.assertNotIn("intercom_extension", validated)
        self.assertNotIn("supervisor", validated)
        self.assertNotIn("contract", validated)

    def test_validate_only_does_not_write_routing_state_when_already_pinned(self):
        # Pre-seed routing state for a delegation. --validate-only must not
        # observe or mutate it: running twice with the same delegation must
        # still report the same resolved profile and never reserve anything.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            first = runner.reserve_selection("pinned", "auto", "standard")
        self.assertTrue((self.records / ".routing-state.json").exists())
        before = (self.records / ".routing-state.json").read_text()
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        result = subprocess.run(self._validate_only_command(
            delegation="pinned", extra=["--routing-class", "standard",
                                        "--profile", "auto"]),
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Routing-state file is unchanged byte-for-byte.
        self.assertEqual((self.records / ".routing-state.json").read_text(), before)
        # No record was appended and no profile-state sidecar exists.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / "profile-state.json").exists())
        validated = json.loads(result.stdout)["validated"]
        self.assertEqual(validated["profile"], "auto")
        self.assertEqual(validated["routing_class"], "standard")
        # A second --validate-only call still passes: it does not consume the
        # routing state.
        result2 = subprocess.run(self._validate_only_command(
            delegation="pinned", extra=["--routing-class", "standard",
                                         "--profile", "auto"]),
                                 env=env, text=True, capture_output=True)
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertEqual((self.records / ".routing-state.json").read_text(), before)

    def test_validate_only_validates_contract_readability(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        # Valid contract file -> success.
        good = self.base / "good-contract.txt"
        good.write_text("Implement bounded change")
        result = subprocess.run(self._validate_only_command(
            delegation="vo-good", contract_file=str(good)), env=env,
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # No side effects and the contract marker is not echoed.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertNotIn("Implement bounded change", result.stdout)
        self.assertNotIn("Implement bounded change", result.stderr)
        # Missing contract file -> rejection, no side effects.
        missing = self.base / "missing-contract.txt"
        result = subprocess.run(self._validate_only_command(
            delegation="vo-missing", contract_file=str(missing)), env=env,
            text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contract file not found", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        # Unreadable contract file -> rejection, no side effects. Use a directory
        # as a contract-file so open() raises; chmod 0 would be skipped by root.
        directory_as_file = self.base / "contract-dir"
        directory_as_file.mkdir()
        result = subprocess.run(self._validate_only_command(
            delegation="vo-unreadable", contract_file=str(directory_as_file)),
                                env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        # 'is_file()' is False for a directory, so the missing-file message fires;
        # what matters is that the contract content was never loaded and no state
        # was written.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())

    def test_validate_only_validates_intercom_inputs_without_reserving_routing(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        # Valid intercom opt-in: success, no side effects.
        ext = self._write_extension()
        result = subprocess.run(self._validate_only_command(
            delegation="vo-intercom-ok",
            extra=self._intercom_args(extension_path=ext)), env=env,
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        self.assertEqual(json.loads(result.stdout)["validated"]["transport"],
                         "intercom")
        # Missing supervisor: rejection, no routing state was written.
        result = subprocess.run(self._validate_only_command(
            delegation="vo-no-supervisor",
            extra=["--transport", "intercom",
                   "--intercom-extension", str(ext)]),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--supervisor", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        # Wrong extension suffix: rejection, no routing state was written.
        wrong_suffix = self.base / "extension.txt"
        wrong_suffix.write_text("// not a ts extension\n")
        result = subprocess.run(self._validate_only_command(
            delegation="vo-wrong-suffix",
            extra=["--transport", "intercom",
                   "--intercom-extension", str(wrong_suffix),
                   "--supervisor", "11111111-2222-3333-4444-555555555555"]),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".ts or .js", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        # Short supervisor id: rejection, no routing state was written.
        result = subprocess.run(self._validate_only_command(
            delegation="vo-short-supervisor",
            extra=["--transport", "intercom",
                   "--intercom-extension", str(ext),
                   "--supervisor", "short-roster-id"]),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full UUID", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        # RPC mode with an intercom extension path must be rejected before any
        # routing state is written.
        result = subprocess.run(self._validate_only_command(
            delegation="vo-rpc-rejects",
            extra=["--transport", "rpc",
                   "--intercom-extension", str(ext),
                   "--supervisor", "11111111-2222-3333-4444-555555555555"]),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejects --intercom-extension", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())

    def test_validate_only_keeps_invalid_enum_rejection(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        for bad in ("review", "debugging"):
            cmd = self._validate_only_command(delegation=f"vo-tk-{bad}")
            cmd.extend(["--task-kind", bad])
            result = subprocess.run(cmd, env=env, text=True,
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--task-kind", result.stderr)
            self.assertIn("invalid choice", result.stderr)
            self.assertEqual(list(self.records.glob("*.jsonl")), [])
            self.assertFalse((self.records / ".routing-state.json").exists())
        for bad in ("judgment", "judgmental"):
            cmd = self._validate_only_command(delegation=f"vo-am-{bad}")
            cmd.extend(["--acceptance-mode", bad])
            result = subprocess.run(cmd, env=env, text=True,
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--acceptance-mode", result.stderr)
            self.assertIn("invalid choice", result.stderr)
            self.assertEqual(list(self.records.glob("*.jsonl")), [])
            self.assertFalse((self.records / ".routing-state.json").exists())

    # ----- optional --task-group-id (coarse correlation label) ----------------

    def test_task_group_id_validate_only_accepts_valid_and_rejects_invalid(self):
        # Absence and a well-formed id both pass dry-run validation with the
        # historical summary shape; malformed ids are rejected before any
        # routing state, record, or provider/worker side effect.
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        ok = subprocess.run(self._validate_only_command(
            delegation="tg-absent"), env=env, text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        ok2 = subprocess.run(self._validate_only_command(
            delegation="tg-valid", extra=["--task-group-id", "Grp_01-a_B"]),
            env=env, text=True, capture_output=True)
        self.assertEqual(ok2.returncode, 0, ok2.stderr)
        # Default shape unchanged: the summary keys never grow a group field.
        self.assertEqual(set(json.loads(ok2.stdout)["validated"].keys()),
                         {"workdir", "routing_class", "profile", "profile_source",
                          "thinking", "transport", "tools", "receipt_file",
                          "task_kind", "acceptance_mode", "risk"})
        for bad in ("", "a" * 65, "spa ce", "grp.1"):
            result = subprocess.run(self._validate_only_command(
                delegation=f"tg-bad-{abs(hash(bad)) % 1000}",
                extra=["--task-group-id", bad]), env=env, text=True,
                capture_output=True)
            self.assertNotEqual(result.returncode, 0, repr(bad))
        # No dry-run invocation produced any side effect.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertEqual(self._sidecar_files(), [])

    def test_task_group_id_recorded_only_when_supplied(self):
        # The assignment block records task_group_id only for runs launched
        # with the flag; absence keeps the legacy record shape key-for-key.
        # Either way the resolved model, profile, and routing are unchanged.
        plain = self.run_worker(delegation="tg-rec-absent")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        grouped = self.run_worker(delegation="tg-rec-present",
                                  extra=["--task-group-id", "Grp_01"])
        self.assertEqual(grouped.returncode, 0, grouped.stderr)
        runs = {r["delegation_id"]: r for r in self.records_for_today()}
        self.assertEqual(set(runs), {"tg-rec-absent", "tg-rec-present"})
        absent, present = runs["tg-rec-absent"], runs["tg-rec-present"]
        self.assertNotIn("task_group_id", absent["assignment"])
        self.assertEqual(present["assignment"]["task_group_id"], "Grp_01")
        for run in (absent, present):
            self.assertEqual(run["schema_version"], 3)
            self.assertEqual(run["worker"]["model"], "test-model-a")
            self.assertEqual(run["worker"]["worker_type"], "standard-glm")
            self.assertEqual(run["routing"]["routing_class"], "standard")
        # Distinct runs; the flag changed no identity or gate field, and no
        # group is inferred from a delegation name.
        self.assertNotEqual(absent["run_id"], present["run_id"])
        self.assertEqual(absent["assignment"]["contract_digest"],
                         present["assignment"]["contract_digest"])

    def test_task_group_id_invalid_is_rejected_before_reservation(self):
        # A malformed group id fails identically in normal (non-dry-run) mode:
        # no routing reservation, no record, no provider call, no worker spawn.
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        result = subprocess.run(
            self.command("standard-glm", "standard", delegation="tg-bad-run",
                         extra=["--task-group-id", "bad group!"]),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task-group-id", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertEqual(self._sidecar_files(), [])

    def test_validate_only_rejects_routing_incompatibilities_without_writing(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        # An unsupported routing class is refused by argument parsing, in dry run and
        # normal mode alike (covered by
        # test_retired_profile_requests_are_rejected_before_side_effects).
        # Profile auto-disabled after a usage limit: explicit request is rejected.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            runner.auto_disable_profile("standard-deepseek", run_id="r")
        cmd = self._validate_only_command(delegation="vo-disabled",
                                          extra=["--routing-class", "standard",
                                                 "--profile", "standard-deepseek"])
        result = subprocess.run(cmd, env=env, text=True,
                                capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("auto-disabled", result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    # ----- --validate-only: sticky reservation + UTF-8 regression tests -----

    def _validate_only_routing_command(self, *, delegation, routing_class,
                                       profile, extra=None):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--contract", "Do it", "--routing-class", routing_class,
               "--profile", profile, "--thinking", "medium",
               "--tools", "write", "--delegation-id", delegation,
               "--validate-only"]
        if extra:
            cmd.extend(extra)
        return cmd

    def test_validate_only_rejects_explicit_profile_sticky_mismatch_without_writing(self):
        # Pre-seed a sticky delegation so we know exactly which profile is pinned.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.secrets, "randbelow", return_value=0):
                self.assertEqual(
                    runner.reserve_selection("mismatch", "auto", "standard"),
                    ("standard-glm", 0, "random_enabled_profile"))
        before = (self.records / ".routing-state.json").read_text()
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        # An explicit request that conflicts with the pinned stick must be
        # rejected without a draw and without writing any state.
        result = subprocess.run(
            self._validate_only_routing_command(
                delegation="mismatch", routing_class="standard",
                profile="standard-deepseek"),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("already fixed", result.stderr)
        self.assertIn("standard-glm", result.stderr)
        # No routing state mutation, no records, no profile-state sidecar.
        self.assertEqual((self.records / ".routing-state.json").read_text(), before)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / "profile-state.json").exists())

    def test_validate_only_passes_matching_sticky_reuse_without_writing(self):
        # Pin a sticky and reuse it via two matching --validate-only calls.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.secrets, "randbelow", return_value=2):
                self.assertEqual(
                    runner.reserve_selection("reuse", "auto", "standard"),
                    ("standard-minimax", 2, "random_enabled_profile"))
        before = (self.records / ".routing-state.json").read_text()
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        # Auto request reuses the pinned profile.
        result = subprocess.run(
            self._validate_only_routing_command(
                delegation="reuse", routing_class="standard", profile="auto"),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.records / ".routing-state.json").read_text(), before)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        # Explicit matching request also passes the sticky check.
        result = subprocess.run(
            self._validate_only_routing_command(
                delegation="reuse", routing_class="standard",
                profile="standard-minimax"),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.records / ".routing-state.json").read_text(), before)

    def _seed_historical_sticky(self, delegation, profile):
        """Write a pre-existing routing state file naming ``profile``.

        Simulates state left by an earlier release; the runner must reject a
        delegation pinned to a profile that is no longer registered instead of
        rewriting the file or redrawing a different worker.
        """
        self.records.mkdir(parents=True, exist_ok=True)
        state = self.records / ".routing-state.json"
        existing = json.loads(state.read_text()) if state.exists() else {}
        existing[delegation] = {"profile": profile, "draw": 0,
                                "policy": "random_enabled_profile"}
        state.write_text(json.dumps(existing, sort_keys=True))
        return state.read_text()

    def test_historical_removed_sticky_is_rejected_without_redraw_or_mutation(self):
        # Sticky safety for a retired profile: a delegation pinned to a removed
        # profile fails with the generic removed-profile error in both the
        # normal and --validate-only paths. Routing state and run history are
        # left byte-for-byte identical, no random draw happens, no record or
        # provider call is made, and nothing is auto-migrated.
        env = self.env | {"FAKE_PI_MODE": "ok"}
        seed = self._seed_historical_sticky("legacy-sticky", "simple-m3")
        history = self.records / "2026-01-01.jsonl"
        history.write_text(json.dumps({
            "schema_version": 3, "record_type": "run", "phase": "completed",
            "run_id": "old-simple", "delegation_id": "legacy-sticky",
            "worker": {"worker_type": "simple-m3", "provider": "test-provider-b",
                       "model": "test-model-legacy"},
            "routing": {"routing_class": "simple", "random_draw": 0},
            "outcome": {"status": "completed"}}) + "\n")
        history_before = history.read_text()
        # Normal launch and dry run both refuse the retired sticky delegation.
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                cmd = self.command("auto", "standard", delegation="legacy-sticky",
                                   extra=["--validate-only"] if dry_run else None)
                result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("no longer registered", result.stderr)
                # No redraw, no rewrite, no provider call, no new record.
                self.assertEqual(
                    (self.records / ".routing-state.json").read_text(), seed)
                self.assertEqual(history.read_text(), history_before)
                self.assertEqual([json.loads(line) for line in
                                  history.read_text().splitlines()],
                                 [json.loads(line) for line in
                                  history_before.splitlines()])
                self.assertFalse((self.records / "profile-state.json").exists())
        # The internal API reports the same generic error, with the routing
        # code/stage, and performs no draw.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            self.assertEqual(runner.read_previous_selection("legacy-sticky"),
                             "simple-m3")
            with mock.patch.object(
                    runner.secrets, "randbelow",
                    side_effect=AssertionError("no draw for a removed profile")):
                with self.assertRaises(runner.RunnerError) as raised:
                    runner.reserve_selection("legacy-sticky", "auto", "standard")
                self.assertEqual(raised.exception.code, "profile_removed")
                self.assertEqual(raised.exception.stage, "routing")
                with self.assertRaises(runner.RunnerError) as raised:
                    runner.validate_sticky_reservation(
                        "legacy-sticky", "standard-glm", "standard")
                self.assertEqual(raised.exception.code, "profile_removed")
        self.assertEqual((self.records / ".routing-state.json").read_text(), seed)
        # A fresh delegation id continues normally with the same record dir.
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.secrets, "randbelow", return_value=0):
                self.assertEqual(
                    runner.reserve_selection("new-delegation", "auto", "standard"),
                    ("standard-glm", 0, "random_enabled_profile"))

    def test_registered_disabled_sticky_still_rejected_without_migration(self):
        # A still-registered but permanently disabled profile keeps its own
        # distinct rejection: no auto-migration to an enabled worker either.
        seed = self._seed_historical_sticky("zai-sticky", "standard-glm-zai")
        env = self.env | {"FAKE_PI_MODE": "ok"}
        result = subprocess.run(
            self.command("auto", "standard", delegation="zai-sticky"),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("currently disabled", result.stderr)
        self.assertEqual((self.records / ".routing-state.json").read_text(), seed)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_validate_only_checks_auto_disabled_sticky_redraw_eligibility(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):

            def seed_sticky_then_disable():
                # Clear any previous routing state so a fresh sticky is created
                # for the "auto-redraw" delegation each time.
                for name in (".routing-state.json", ".append.lock"):
                    target = self.records / name
                    if target.exists():
                        target.unlink()
                # Also clear any auto-disabled profile state left over from a
                # previous run so the redraw pool is the full standard set.
                if (self.records / "profile-state.json").exists():
                    (self.records / "profile-state.json").unlink()
                with mock.patch.object(runner.secrets, "randbelow", return_value=1):
                    self.assertEqual(
                        runner.reserve_selection("auto-redraw", "auto", "standard"),
                        ("standard-deepseek", 1, "random_enabled_profile"))
                runner.auto_disable_profile("standard-deepseek", run_id="r")
                for path in list(self.records.glob("*.jsonl")):
                    path.unlink()

            # 1) Sticky is auto-disabled; auto request is eligible for redraw
            # even though the explicit profile check would reject. The sticky
            # helper accepts without performing a draw.
            seed_sticky_then_disable()
            with mock.patch.object(runner.secrets, "randbelow",
                                   side_effect=AssertionError("no random draws during validation")):
                result = subprocess.run(
                    self._validate_only_routing_command(
                        delegation="auto-redraw", routing_class="standard",
                        profile="auto"),
                    env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])

            # 2) Sticky is auto-disabled; explicit request matching the sticky
            # is also eligible (the runner would redraw because the cooldown
            # hasn't expired).
            seed_sticky_then_disable()
            with mock.patch.object(runner.secrets, "randbelow",
                                   side_effect=AssertionError("no random draws during validation")):
                result = subprocess.run(
                    self._validate_only_routing_command(
                        delegation="auto-redraw", routing_class="standard",
                        profile="standard-deepseek"),
                    env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])

            # 3) Sticky is auto-disabled; explicit request conflicting with
            # the sticky is rejected by the sticky helper before any state
            # mutation occurs.
            seed_sticky_then_disable()
            result = subprocess.run(
                self._validate_only_routing_command(
                    delegation="auto-redraw", routing_class="standard",
                    profile="standard-glm"),
                env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("already fixed", result.stderr)
            self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_validate_only_rejects_invalid_utf8_contract(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        # Write a file that is intentionally not valid UTF-8: a lone 0xFF byte.
        bad = self.base / "bad-utf8.txt"
        bad.write_bytes(b"\xff\xfe\xfd hello \xc3\x28 world \xff")
        result = subprocess.run(self._validate_only_command(
            delegation="vo-bad-utf8", contract_file=str(bad)),
            env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("UTF-8", result.stderr)
        # No routing state, no records, no profile-state sidecar.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        self.assertFalse((self.records / "profile-state.json").exists())
        self.assertFalse((self.records / ".append.lock").exists())
        # A valid UTF-8 contract still passes; the UTF-8 check must not reject
        # multi-byte characters or BOM-prefixed files.
        good = self.base / "good-utf8.txt"
        good.write_text("Implementação \u4e2d\u6587 — Bounded \U0001f4dd assignment",
                        encoding="utf-8")
        result = subprocess.run(self._validate_only_command(
            delegation="vo-good-utf8", contract_file=str(good)),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        # The contract content is never echoed in stdout or stderr.
        self.assertNotIn("Implementação", result.stdout)
        self.assertNotIn("Implementação", result.stderr)

    def test_validate_only_validate_only_does_not_mutate_state(self):
        # Run --validate-only in a clean record dir and in a pre-seeded record
        # dir. Neither call should write a routing-state file, a profile-state
        # file, a JSONL record, or the append-lock sidecar.
        clean_dir = self.base / "clean-records"
        clean_dir.mkdir()
        env = self.env | {"FAKE_PI_MODE": "auth-fail",
                          "PI_WORKER_RECORD_DIR": str(clean_dir)}
        result = subprocess.run(self._validate_only_command(delegation="clean-1"),
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # record_dir() may have been created by other helpers, but no sidecar
        # files should exist after validate-only finishes.
        for name in (".routing-state.json", "profile-state.json", ".append.lock"):
            self.assertFalse((clean_dir / name).exists(), name)
        self.assertEqual(list(clean_dir.glob("*.jsonl")), [])

        seeded_dir = self.base / "seeded-records"
        seeded_dir.mkdir()
        env = self.env | {"FAKE_PI_MODE": "auth-fail",
                          "PI_WORKER_RECORD_DIR": str(seeded_dir)}
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(seeded_dir)}):
            runner.reserve_selection("seeded", "auto", "standard")
        # Snapshot routing state after the pre-seed only.
        seeded_state = (seeded_dir / ".routing-state.json").read_text()
        result = subprocess.run(self._validate_only_command(delegation="seeded",
                                                            extra=["--routing-class",
                                                                   "standard",
                                                                   "--profile", "auto"]),
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Routing state is identical to the pre-seed snapshot.
        self.assertEqual((seeded_dir / ".routing-state.json").read_text(), seeded_state)
        # Required pre-seeded sidecars still exist after validate-only.
        for name in (".routing-state.json",):
            self.assertTrue((seeded_dir / name).exists(), name)
        # No new sidecars or records were written by validate-only.
        self.assertFalse((seeded_dir / "profile-state.json").exists())
        self.assertEqual(list(seeded_dir.glob("*.jsonl")), [])

    def test_validate_sticky_reservation_unit_checks(self):
        # Direct unit-level exercise of validate_sticky_reservation. Confirms
        # every branch without going through the subprocess CLI: no sticky,
        # ordinary reuse, mismatch, a sticky pinned to a removed profile, and
        # auto-disabled redraw eligibility (no random draw performed).
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.secrets, "randbelow", return_value=0):
                self.assertEqual(
                    runner.reserve_selection("pin", "auto", "standard"),
                    ("standard-glm", 0, "random_enabled_profile"))
            with mock.patch.object(runner.secrets, "randbelow", return_value=1):
                self.assertEqual(
                    runner.reserve_selection("dis", "auto", "standard"),
                    ("standard-deepseek", 1, "random_enabled_profile"))
            runner.auto_disable_profile("standard-deepseek", run_id="r")
            # A delegation pinned to a profile that is no longer registered.
            self._seed_historical_sticky("gone", "simple-m3")
            self._seed_historical_sticky("zai", "standard-glm-zai")
            # Malformed / empty / no-profile sticky records are skipped safely
            # (treated as "no sticky"); a legacy bare-string record is read as
            # the profile it names.
            state_path = self.records / ".routing-state.json"
            raw = json.loads(state_path.read_text())
            raw["malformed"] = {"profile": ""}
            raw["no-profile"] = {"draw": 2}
            raw["legacy-string"] = "standard-glm"
            state_path.write_text(json.dumps(raw, sort_keys=True))
            pre_routing = state_path.read_text()
            # No sticky: passes through silently.
            self.assertIsNone(runner.validate_sticky_reservation(
                "missing", "auto", "standard"))
            # Malformed and no-profile records behave like "no sticky"; the
            # explicit-profile checks belong to the argument layer, not here.
            for delegation in ("malformed", "no-profile"):
                self.assertIsNone(runner.validate_sticky_reservation(
                    delegation, "auto", "standard"))
                self.assertIsNone(runner.validate_sticky_reservation(
                    delegation, "standard-glm", "standard"))
            self.assertIsNone(runner.validate_sticky_reservation(
                "legacy-string", "auto", "standard"))
            with self.assertRaisesRegex(runner.RunnerError, "already fixed"):
                runner.validate_sticky_reservation(
                    "legacy-string", "standard-deepseek", "standard")
            # Ordinary reuse: auto and the matching explicit profile pass.
            self.assertIsNone(runner.validate_sticky_reservation(
                "pin", "auto", "standard"))
            self.assertIsNone(runner.validate_sticky_reservation(
                "pin", "standard-glm", "standard"))
            # Mismatching explicit profile against the sticky rejects.
            with self.assertRaisesRegex(runner.RunnerError, "already fixed"):
                runner.validate_sticky_reservation(
                    "pin", "standard-deepseek", "standard")
            # Auto-disabled sticky + auto request is eligible (no draw runs).
            with mock.patch.object(runner.secrets, "randbelow",
                                   side_effect=AssertionError("draw")):
                self.assertIsNone(runner.validate_sticky_reservation(
                    "dis", "auto", "standard"))
                self.assertIsNone(runner.validate_sticky_reservation(
                    "dis", "standard-deepseek", "standard"))
            with self.assertRaisesRegex(runner.RunnerError, "already fixed"):
                runner.validate_sticky_reservation(
                    "dis", "standard-glm", "standard")
            # Unsupported routing class is refused before any sticky logic.
            for delegation in ("missing", "pin", "dis"):
                with self.assertRaisesRegex(runner.RunnerError,
                                            "unsupported routing class"):
                    runner.validate_sticky_reservation(
                        delegation, "auto", "simple")

            # Removed sticky: generic profile_removed, no migration. The
            # registered-but-disabled zai sticky keeps its own distinct error.
            with mock.patch.object(
                    runner.secrets, "randbelow",
                    side_effect=AssertionError("draw")):
                with self.assertRaises(runner.RunnerError) as raised:
                    runner.validate_sticky_reservation("gone", "auto", "standard")
                self.assertEqual(raised.exception.code, "profile_removed")
                self.assertEqual(raised.exception.stage, "routing")
                with self.assertRaisesRegex(runner.RunnerError, "currently disabled"):
                    runner.validate_sticky_reservation("zai", "auto", "standard")
            # Routing state must be byte-for-byte unchanged by validation.
            self.assertEqual(
                (self.records / ".routing-state.json").read_text(), pre_routing)

    # ----- UTF-8 streaming boundary regression tests -----
    #
    # validate_contract_file_utf8 reads the contract file in 64 KiB chunks
    # before the launch path runs any routing, record, or worker side effect.
    # The naive chunk.decode('utf-8', errors='strict') per chunk would reject
    # a multi-byte code point whose bytes straddle the 64 KiB read boundary
    # because each chunk would see a partial sequence in isolation. The
    # standard incremental UTF-8 decoder carries state across chunk reads and
    # only raises at EOF finalize when the trailing bytes are truly invalid.

    BOUNDARY = 64 * 1024
    # 3-byte code point U+4E2D (\u4e2d): E4 B8 AD.
    CP3 = "中"
    CP3_BYTES = b"\xe4\xb8\xad"
    # 2-byte code point U+00E9: C3 A9.
    CP2 = "é"
    CP2_BYTES = b"\xc3\xa9"
    # 4-byte code point U+1F4DD: F0 9F 93 9D.
    CP4 = "\U0001f4dd"
    CP4_BYTES = b"\xf0\x9f\x93\x9d"

    def _build_boundary_split_file(self, name, ascii_before, codepoint):
        """Write a file whose final code point straddles the 64 KiB boundary.

        The chosen codepoint starts at byte offset ``ascii_before`` (within the
        first chunk) and continues into the second chunk. The first read
        returns 65536 bytes ending with 1-3 leading bytes of the code point;
        the second read starts with the remaining continuation bytes. The
        per-chunk decode bug rejects this file; the incremental decoder
        accepts it.
        """
        path = self.base / name
        # Write the ASCII prefix, then patch the boundary bytes with the
        # chosen code point's raw bytes, then a small ASCII suffix.
        with open(path, "wb") as raw:
            raw.write(b"a" * ascii_before)
            codepoint_bytes = codepoint.encode("utf-8")
            raw.write(codepoint_bytes)
            raw.write(b"b" * 100)
        return path

    def _assert_no_side_effects(self, result, prefix=""):
        # No JSONL records, no routing-state, no profile-state, no append-lock.
        self.assertEqual(list(self.records.glob("*.jsonl")), [],
                         msg=f"{prefix}records were written")
        self.assertFalse((self.records / ".routing-state.json").exists(),
                         msg=f"{prefix}routing state was written")
        self.assertFalse((self.records / "profile-state.json").exists(),
                         msg=f"{prefix}profile state was written")
        self.assertFalse((self.records / ".append.lock").exists(),
                         msg=f"{prefix}append lock was created")
        # No provider/record JSON reached stderr on validation-only.
        self.assertNotIn("delegation", result.stderr)
        self.assertNotIn("provider", result.stderr)

    def test_validate_contract_file_utf8_accepts_boundary_split_valid_utf8(self):
        # Multi-byte code points straddling the 64 KiB boundary must decode
        # cleanly via the incremental decoder. The file is small enough that
        # the per-chunk decode bug rejects it (each 64 KiB chunk would see
        # a partial code point), but the fix accepts the whole file.
        for label, ascii_before, codepoint in (
                ("cp3-at-boundary-1", self.BOUNDARY - 1, self.CP3),
                ("cp3-at-boundary-2", self.BOUNDARY - 2, self.CP3),
                ("cp2-at-boundary-1", self.BOUNDARY - 1, self.CP2),
                ("cp4-at-boundary-1", self.BOUNDARY - 1, self.CP4),
                ("cp4-at-boundary-3", self.BOUNDARY - 3, self.CP4)):
            with self.subTest(label=label):
                path = self._build_boundary_split_file(
                    f"split-{label}.txt", ascii_before, codepoint)
                # Direct unit call: must succeed without raising.
                runner.validate_contract_file_utf8(path)
                # Subprocess: --validate-only must exit 0 with no side effects.
                env = self.env | {"FAKE_PI_MODE": "auth-fail"}
                result = subprocess.run(
                    self._validate_only_command(
                        delegation=f"vo-split-{label}",
                        contract_file=str(path)),
                    env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0,
                                 msg=f"stderr={result.stderr!r}")
                self._assert_no_side_effects(result, prefix=f"{label}: ")

    def test_validate_contract_file_utf8_rejects_truncated_eof(self):
        # Files ending with an incomplete multi-byte sequence must be
        # rejected at EOF finalize, before any side effect runs. Cover both
        # the 'trailing chunk is empty' case (file ends mid-sequence) and
        # the 'truncated continuation bytes' case (1-3 leading bytes only).
        for label, trailing in (
                ("trail-empty-1byte", b"\xe4"),
                ("trail-empty-2byte", b"\xe4\xb8"),
                ("trail-empty-3byte", b"\xf0\x9f\x93"),
                ("trail-next-1byte", b"\xe4"),
                ("trail-next-2byte", b"\xc3"),
                ("trail-lone-continuation", b"\xad")):
            with self.subTest(label=label):
                path = self.base / f"trunc-{label}.txt"
                # Make the file end exactly at the boundary (one 64 KiB
                # read returns everything) plus a tiny tail that holds the
                # truncated sequence. The finalizer must surface it.
                path.write_bytes(b"a" * self.BOUNDARY + trailing)
                # Direct unit call: must raise RunnerError with a
                # privacy-safe message that does not echo the bytes.
                with self.assertRaisesRegex(runner.RunnerError, "UTF-8") as cm:
                    runner.validate_contract_file_utf8(path)
                self.assertNotIn(b"\xe4".decode("latin-1"), str(cm.exception))
                # Subprocess: --validate-only must fail with nonzero exit.
                env = self.env | {"FAKE_PI_MODE": "auth-fail"}
                result = subprocess.run(
                    self._validate_only_command(
                        delegation=f"vo-trunc-{label}",
                        contract_file=str(path)),
                    env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, msg=result.stderr)
                self.assertIn("UTF-8", result.stderr)
                self._assert_no_side_effects(result, prefix=f"{label}: ")

    def test_validate_contract_file_utf8_does_not_retain_content(self):
        # The contract content is never persisted, echoed, or accumulated by
        # the validator. Build a file large enough to require several reads
        # and a multi-byte code point that spans a chunk boundary; confirm
        # the validator returns without raising, no sidecar files appear,
        # and the unique contract marker never appears on stdout/stderr.
        unique = "secret-boundary-marker-vo-9z-plural-z-alpha"
        path = self.base / "boundary-leak-check.txt"
        with open(path, "wb") as raw:
            raw.write((unique + " ").encode("utf-8") * 50)
            raw.write(b"a" * (self.BOUNDARY - 10))
            raw.write(self.CP3_BYTES)  # straddles the boundary
            raw.write((unique + " ").encode("utf-8") * 10)
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        result = subprocess.run(
            self._validate_only_command(
                delegation="vo-boundary-leak", contract_file=str(path)),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for stream in (result.stdout, result.stderr):
            self.assertNotIn(unique, stream)
        # No records, no routing state, no profile-state, no append-lock.
        self.assertEqual(list(self.records.glob("*.jsonl")), [])
        self.assertFalse((self.records / ".routing-state.json").exists())
        self.assertFalse((self.records / "profile-state.json").exists())
        self.assertFalse((self.records / ".append.lock").exists())

    # ----- argparse SystemExit propagation: --help exits 0; bad enums exit 2 -----

    def test_help_exits_zero_with_no_routing_or_record_side_effects(self):
        # `python3 scripts/run_pi_worker.py --help` must print argparse's help
        # text and exit 0 without the top-level error handler converting
        # SystemExit(0) into a failure. It must also leave the record
        # directory and routing state untouched.
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--help"],
            env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0,
                         msg=f"stderr={result.stderr!r}")
        # argparse's help text appears on stdout with the documented flags.
        self.assertIn("--help", result.stdout)
        self.assertIn("--workdir", result.stdout)
        self.assertIn("--validate-only", result.stdout)
        self.assertIn("--transport", result.stdout)
        # No side effects: no record directory was created and no sidecar
        # files exist. argparse's help path runs before parse_args even
        # touches record_dir().
        if self.records.exists():
            for name in (".routing-state.json", "profile-state.json",
                         ".append.lock"):
                self.assertFalse((self.records / name).exists(), name)
            self.assertEqual(list(self.records.glob("*.jsonl")), [])

    def test_invalid_argparse_enum_exits_nonzero_with_no_side_effects(self):
        # Every argparse 'choices=...' constraint must reject bad values
        # before our routing logic runs. The runner must exit with
        # argparse's nonzero exit code (typically 2) and leave the record
        # directory untouched: no records, no routing state, no profile
        # state, no append lock.
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        bad_invokes = (
            ("task-kind", ["--task-kind", "review"]),
            ("routing-class", ["--routing-class", "fast"]),
            ("thinking", ["--thinking", "extreme"]),
            ("tools", ["--tools", "all"]),
            ("risk", ["--risk", "critical"]),
            ("acceptance-mode", ["--acceptance-mode", "judgment"]),
            ("profile", ["--profile", "unknown-profile"]),  # not an argparse choice: validated later, before any side effect
            ("transport", ["--transport", "grpc"]),
            ("review-status", ["--review-status", "approved"]),
            ("main-rework", ["--main-rework", "patch"]))
        for label, argv in bad_invokes:
            with self.subTest(label=label):
                cmd = [sys.executable, str(RUNNER),
                       "--workdir", str(self.work),
                       "--contract", "Do it",
                       "--routing-class", "standard",
                       "--profile", "standard-glm",
                       "--thinking", "medium",
                       "--tools", "write",
                       "--delegation-id", f"vo-bad-enum-{label}"]
                cmd.extend(argv)
                result = subprocess.run(cmd, env=env, text=True,
                                        capture_output=True)
                # argparse exits with 2 for invalid choices; profile ids are
                # host-configured (no static choices) so an unknown explicit
                # profile is refused by the pre-side-effect validation instead.
                self.assertNotEqual(result.returncode, 0,
                                    msg=f"{label}: stderr={result.stderr!r}")
                if label == "profile":
                    self.assertIn("invalid --profile", result.stderr,
                                  msg=f"{label}")
                else:
                    self.assertIn("invalid choice", result.stderr,
                                  msg=f"{label}")
                # No routing/record/profile-state sidecar files were created.
                self.assertEqual(list(self.records.glob("*.jsonl")), [],
                                 msg=f"{label}: records were written")
                self.assertFalse((self.records / ".routing-state.json").exists(),
                                 msg=f"{label}: routing state was written")
                self.assertFalse((self.records / "profile-state.json").exists(),
                                 msg=f"{label}: profile state was written")
                self.assertFalse((self.records / ".append.lock").exists(),
                                 msg=f"{label}: append lock was created")

    # ----- KeyboardInterrupt handling: receipt-after and receipt-before paths -----

    def test_keyboard_interrupt_after_receipt_writes_interrupted_receipt(self):
        # Once a receipt has been created, KeyboardInterrupt (e.g. raised
        # inside run_rpc via SIGINT) must append an interrupted receipt
        # with category=code="interrupted", stage="runner", and exit 130.
        # We mock the slow/rpc paths so the unit test is deterministic and
        # does not need a fake_pi subprocess; the SIGINT subprocess test
        # below covers the real run_rpc KeyboardInterrupt path end-to-end.
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)},
                             clear=False), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc",
                               side_effect=KeyboardInterrupt()):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = runner.main(["--workdir", str(self.work),
                                  "--contract", "Do it",
                                  "--profile", "standard-minimax",
                                  "--routing-class", "standard",
                                  "--thinking", "medium",
                                  "--tools", "write",
                                  "--delegation-id", "ki-after"])

        self.assertEqual(rc, 130)
        records = [r for r in self.records_for_today()
                   if r.get("delegation_id") == "ki-after"]
        self.assertEqual(len(records), 1, msg=stderr.getvalue())
        receipt = records[0]
        self.assertEqual(receipt["record_type"], "run")
        self.assertIsNotNone(receipt["finished_at"])
        self.assertEqual(receipt["outcome"]["status"], "interrupted")
        self.assertEqual(receipt["outcome"]["result"], "unusable")
        self.assertEqual(receipt["outcome"]["summary"], "Pi worker did not deliver")
        self.assertEqual(receipt["failure"]["category"], "interrupted")
        self.assertEqual(receipt["failure"]["code"], "interrupted")
        self.assertEqual(receipt["failure"]["stage"], "runner")
        self.assertEqual(receipt["failure"]["evidence"], "KeyboardInterrupt")
        # The receipt is also echoed to stderr so callers can read it.
        self.assertIn("ki-after", stderr.getvalue())
        self.assertIn("interrupted", stderr.getvalue())

    def test_keyboard_interrupt_before_receipt_returns_130_with_stderr_message(self):
        # A KeyboardInterrupt raised before the receipt dict is created
        # (here, by mocking reserve_selection) must still return 130 with
        # the existing stderr "pi-worker:" prefix and must not write any
        # receipt, routing-state sidecar, or append-lock.
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)},
                             clear=False), \
             mock.patch.object(runner, "reserve_selection",
                               side_effect=KeyboardInterrupt()):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = runner.main(["--workdir", str(self.work),
                                  "--contract", "Do it",
                                  "--profile", "standard-minimax",
                                  "--routing-class", "standard",
                                  "--thinking", "medium",
                                  "--tools", "write",
                                  "--delegation-id", "ki-before"])

        self.assertEqual(rc, 130)
        # No record was appended and no record-sidecar files were created.
        self.assertEqual(list(self.records.glob("*.jsonl")), [],
                         msg=f"unexpected records: {list(self.records.glob('*'))}")
        self.assertFalse((self.records / ".routing-state.json").exists())
        self.assertFalse((self.records / ".append.lock").exists())
        # Existing stderr behavior: a "pi-worker:" prefix line is emitted.
        self.assertIn("pi-worker:", stderr.getvalue())

    def test_subprocess_sigint_after_receipt_writes_interrupted_receipt(self):
        # End-to-end: start the runner as a subprocess, wait for fake_pi to
        # prove the runner reached run_rpc (where the receipt has already
        # been created), then deliver SIGINT. The runner must exit 130 and
        # an interrupted receipt must be appended to the record ledger.
        argv_file = self.base / "argv.json"
        # Use FAKE_PI_MODE="timeout" so fake_pi stays parked in its stdin
        # loop after responding to the prompt: the runner is then blocked
        # inside receive(deadline), which is exactly the spot where a real
        # SIGINT reliably surfaces as a Python KeyboardInterrupt.
        proc = subprocess.Popen(
            self.command("standard-minimax", "standard", delegation="ki-sigint",
                         timeout="30"),
            env=self.env | {"FAKE_PI_MODE": "timeout",
                            "FAKE_PI_ARGV_FILE": str(argv_file)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Wait until fake_pi has captured its argv (it does so at process
        # start), proving the runner already called Popen and therefore has
        # already created the receipt dict in main().
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if argv_file.exists():
                break
            time.sleep(0.02)
        self.assertTrue(argv_file.exists(),
                        msg="fake_pi never started; runner did not reach run_rpc")

        # Brief grace so the runner is parked inside its JSONL receive loop
        # (the only place the subprocess KeyboardInterrupt is reliably
        # observed on the RPC path), then send SIGINT.
        time.sleep(0.2)
        proc.send_signal(signal.SIGINT)

        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(f"runner did not exit after SIGINT; "
                      f"stdout={stdout!r}, stderr={stderr!r}")

        self.assertEqual(proc.returncode, 130,
                         msg=f"stderr={stderr!r}")
        records = [r for r in self.records_for_today()
                   if r.get("delegation_id") == "ki-sigint"]
        self.assertEqual(len(records), 1,
                         msg=f"stderr={stderr!r}, stdout={stdout!r}")
        receipt = records[0]
        self.assertEqual(receipt["record_type"], "run")
        self.assertEqual(receipt["outcome"]["status"], "interrupted")
        self.assertEqual(receipt["failure"]["code"], "interrupted")
        self.assertEqual(receipt["failure"]["category"], "interrupted")
        self.assertEqual(receipt["failure"]["stage"], "runner")
        self.assertEqual(receipt["failure"]["evidence"], "KeyboardInterrupt")

    # ----- terminal receipt file (optional, atomic, private) -----

    def _receipt_path(self, name="receipt.json"):
        return self.base / "receipts" / name

    def test_receipt_file_success_is_private_json_matching_stdout(self):
        path = self._receipt_path()
        result = self.run_worker(delegation="receipt-ok",
                                 extra=["--receipt-file", str(path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["run_id"], json.loads(result.stdout)["run_id"])
        self.assertEqual(document["outcome"]["status"], "completed")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Atomic replace leaves no temporary sibling behind.
        self.assertEqual(list(path.parent.glob(".*.tmp")), [])
        # Started lifecycle evidence precedes the terminal run record.
        events = self.records_for_today(include_events=True)
        self.assertEqual([item["record_type"] for item in events],
                         ["lifecycle", "run"])
        self.assertEqual(events[0]["deadline_scope"], "preflight_included")

    def test_receipt_file_written_on_failure_timeout_and_interrupt(self):
        fail_path = self._receipt_path("fail.json")
        result = self.run_worker(mode="provider-error", delegation="receipt-fail",
                                 extra=["--receipt-file", str(fail_path)])
        self.assertNotEqual(result.returncode, 0)
        document = json.loads(fail_path.read_text())
        self.assertEqual(document["outcome"]["status"], "failed")
        self.assertEqual(document["failure"]["category"], "preflight_or_runtime")
        timeout_path = self._receipt_path("timeout.json")
        result = self.run_worker(mode="timeout", delegation="receipt-timeout",
                                 timeout="0.2",
                                 extra=["--receipt-file", str(timeout_path)])
        self.assertEqual(result.returncode, 124, result.stderr)
        timeout_receipt = json.loads(timeout_path.read_text())
        self.assertEqual(timeout_receipt["failure"]["category"], "timeout")
        self.assertEqual(timeout_receipt["outcome"]["status"], "interrupted")
        interrupt_path = self._receipt_path("interrupt.json")
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc", side_effect=KeyboardInterrupt()):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = runner.main(["--workdir", str(self.work), "--contract", "Do it",
                                  "--profile", "standard-minimax", "--routing-class", "standard",
                                  "--thinking", "medium", "--tools", "write",
                                  "--delegation-id", "receipt-int",
                                  "--receipt-file", str(interrupt_path)])
        self.assertEqual(rc, 130)
        self.assertEqual(json.loads(interrupt_path.read_text())["outcome"]["status"],
                         "interrupted")

    def test_receipt_file_validation_rejects_unsafe_destinations_without_side_effects(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        directory = self.base / "as-dir"
        directory.mkdir()
        symlink = self.base / "link.json"
        symlink.symlink_to(self.base / "target.json")
        writable = self.base / "writable.json"
        writable.write_text("{}")
        os.chmod(writable, 0o666)
        world = self.base / "world"
        world.mkdir()
        os.chmod(world, 0o777)
        unsafe_target = self.base / "unsafe-link-target"
        unsafe_target.mkdir()
        os.chmod(unsafe_target, 0o777)
        unsafe_link = self.base / "unsafe-link"
        unsafe_link.symlink_to(unsafe_target)
        candidates = (("relative", "rel.json"), ("directory", str(directory)),
                      ("symlink", str(symlink)), ("group-writable", str(writable)),
                      ("world-writable-parent", str(world / "r.json")),
                      ("parent-symlink-to-unsafe", str(unsafe_link / "r.json")),
                      ("dotdot", str(self.base / "nested" / ".." / "r.json")))
        for label, value in candidates:
            with self.subTest(label=label):
                result = subprocess.run(
                    self.command("standard-minimax", "standard", delegation=f"rf-{label}",
                                 extra=["--receipt-file", value]),
                    env=env, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("receipt-file", result.stderr)
                self.assertEqual(list(self.records.glob("*.jsonl")), [])
                self.assertFalse((self.records / ".routing-state.json").exists())
        # Nothing was written through the unsafe parent alias or via traversal.
        self.assertFalse((unsafe_target / "r.json").exists())
        self.assertFalse((self.base / "nested").exists())

    def test_receipt_file_validate_only_is_nonmutating(self):
        path = self.base / "newdir" / "nested" / "receipt.json"
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        result = subprocess.run(self._validate_only_command(
            delegation="vo-receipt", extra=["--receipt-file", str(path)]),
            env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.parent.exists())
        self.assertEqual(self._sidecar_files(), [])
        self.assertTrue(json.loads(result.stdout)["validated"]["receipt_file"])

    def test_receipt_write_failure_does_not_claim_success_or_mask_failure(self):
        path = self._receipt_path("forced.json")
        argv = ["--workdir", str(self.work), "--contract", "Do it",
                "--profile", "standard-minimax", "--routing-class", "standard",
                "--thinking", "medium", "--tools", "write",
                "--delegation-id", "rw-fail", "--receipt-file", str(path)]
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc",
                               return_value=({"sessionId": "s"}, ["agent_settled"])), \
             mock.patch.object(runner, "write_receipt_file", side_effect=OSError("no")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = runner.main(argv)
        # A settled run whose receipt file was not written must not claim success.
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(len([r for r in self.records_for_today()
                              if r.get("delegation_id") == "rw-fail"]), 1)
        # A receipt-file failure must not mask the original execution failure.
        argv[-1] = str(self._receipt_path("forced2.json"))
        argv[argv.index("--delegation-id") + 1] = "rw-fail2"
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc", side_effect=TimeoutError()), \
             mock.patch.object(runner, "write_receipt_file", side_effect=OSError("no")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = runner.main(argv)
        self.assertEqual(rc, 124)
        self.assertIn("timeout", stderr.getvalue())

    # ----- best-effort RUN_FINISHED notification -----

    def _notification_receipt(self, **overrides):
        receipt = {"run_id": "a" * 32, "delegation_id": "deleg-1",
                   "outcome": {"status": "completed"}}
        receipt.update(overrides)
        return receipt

    def test_notification_requires_delivered_true_and_bounded_retries(self):
        supervisor = "11111111-2222-3333-4444-555555555555"
        success = mock.Mock(returncode=0, stdout='{"ok": true, "delivered": true}', stderr="")
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.subprocess, "run", return_value=success) as called:
                event = runner.send_run_finished_notification(
                    transport="intercom", receipt=self._notification_receipt(),
                    supervisor_id=supervisor, extension=None,
                    explicit_cli="/tmp/fake.mjs", record_kind="production")
        self.assertTrue(event["delivered"])
        self.assertEqual(event["attempts"], 1)
        self.assertEqual(called.call_count, 1)
        # Only ok:true AND delivered:true together count. A zero exit with a
        # contradictory, partial, malformed, or raw-text result is not success.
        for label, proc in (
                ("nonzero", mock.Mock(returncode=1, stdout='{"ok": true, "delivered": true}', stderr="x")),
                ("ok-missing", mock.Mock(returncode=0, stdout='{"delivered": true}', stderr="")),
                ("delivered-missing", mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")),
                ("contradictory", mock.Mock(returncode=0, stdout='{"ok": false, "delivered": true}', stderr="")),
                ("undelivered", mock.Mock(returncode=0, stdout='{"ok": true, "delivered": false}', stderr="")),
                ("malformed", mock.Mock(returncode=0, stdout="not json", stderr="")),
                ("raw-error-text", mock.Mock(returncode=0, stdout="delivered: true ok", stderr=""))):
            with self.subTest(label=label):
                with mock.patch.dict(os.environ,
                                     {"PI_WORKER_RECORD_DIR": str(self.records)}):
                    with mock.patch.object(runner.subprocess, "run",
                                           return_value=proc) as retried:
                        event = runner.send_run_finished_notification(
                            transport="intercom", receipt=self._notification_receipt(),
                            supervisor_id=supervisor, extension=None,
                            explicit_cli="/tmp/fake.mjs", record_kind="production")
                self.assertFalse(event["delivered"])
                self.assertEqual(event["delivery"], "failed")
                self.assertEqual(event["attempts"], runner.NOTIFICATION_MAX_ATTEMPTS)
                self.assertEqual(retried.call_count, runner.NOTIFICATION_MAX_ATTEMPTS)

    def test_notification_timeout_is_bounded_and_non_intercom_is_unchanged(self):
        supervisor = "11111111-2222-3333-4444-555555555555"
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("node", 5)) as called:
                event = runner.send_run_finished_notification(
                    transport="intercom", receipt=self._notification_receipt(),
                    supervisor_id=supervisor, extension=None,
                    explicit_cli="/tmp/fake.mjs", record_kind="production")
        self.assertFalse(event["delivered"])
        self.assertEqual(event["attempts"], runner.NOTIFICATION_MAX_ATTEMPTS)
        self.assertEqual(called.call_count, runner.NOTIFICATION_MAX_ATTEMPTS)
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": fresh.name}):
            with mock.patch.object(runner.subprocess, "run") as unused:
                event = runner.send_run_finished_notification(
                    transport="rpc", receipt=self._notification_receipt(),
                    supervisor_id=None, extension=None, explicit_cli=None,
                    record_kind="production")
        self.assertIsNone(event)
        unused.assert_not_called()
        self.assertEqual(list(Path(fresh.name).glob("*.jsonl")), [])

    def test_notification_text_and_record_are_private_and_sanitized(self):
        supervisor = "abcdef01-2345-6789-abcd-ef0123456789"
        receipt = self._notification_receipt(
            delegation_id="deleg-secret", outcome={"status": "failed"},
            failure={"code": "timeout"}, assignment={"goal": "secret-goal"},
            worker={"session_id": "sess"}, contract="secret-contract")
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return mock.Mock(returncode=0, stdout='{"delivered": true}', stderr="")

        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
                event = runner.send_run_finished_notification(
                    transport="intercom", receipt=receipt, supervisor_id=supervisor,
                    extension=None, explicit_cli="/opt/pi/intercom/cli.mjs",
                    record_kind="production")
        argv = captured["argv"]
        self.assertEqual(argv[:3], ["node", "/opt/pi/intercom/cli.mjs", "send"])
        self.assertEqual(argv[argv.index("--to") + 1], supervisor)
        self.assertIn("--json", argv)
        payload = json.loads(argv[argv.index("--text") + 1])
        self.assertEqual(payload["type"], "RUN_FINISHED")
        self.assertEqual(payload["run_id"], "a" * 32)
        self.assertEqual(payload["delegation_id"], "deleg-secret")
        self.assertEqual(payload["failure_code"], "timeout")
        self.assertTrue(payload["followup_required"])
        self.assertNotIn(supervisor, argv[argv.index("--text") + 1])
        serialized = json.dumps(event)
        for secret in (supervisor, "secret-contract", "secret-goal",
                       "/opt/pi/intercom/cli.mjs"):
            self.assertNotIn(secret, serialized)

    def test_intercom_run_uses_fake_cli_and_records_notification(self):
        bindir = self.base / "bin"
        bindir.mkdir()
        log = self.base / "node-args.log"
        node = bindir / "node"
        node.write_text("#!/bin/sh\necho \"$@\" >> \"$NODE_ARGS_LOG\"\n"
                        "printf '%s\\n' '{\"ok\": true, \"delivered\": true}'\n")
        node.chmod(0o755)
        cli = self.base / "cli.mjs"
        cli.write_text("// fake cli\n")
        supervisor = "11111111-2222-3333-4444-555555555555"
        env = self.env | {"FAKE_PI_MODE": "ok", "NODE_ARGS_LOG": str(log),
                          "PATH": f"{bindir}:{os.environ['PATH']}"}
        cmd = self.command("standard-minimax", "standard", delegation="notify-ok", extra=[
            "--transport", "intercom", "--intercom-extension",
            str(self._write_extension()), "--supervisor", supervisor,
            "--intercom-cli", str(cli)])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = log.read_text().strip()
        self.assertIn(f"send --to {supervisor}", line)
        self.assertIn("--json", line)
        notifications = [item for item in self.records_for_today(include_events=True)
                         if item["record_type"] == "notification"]
        self.assertEqual(len(notifications), 1)
        self.assertTrue(notifications[0]["delivered"])
        self.assertEqual(notifications[0]["transport"], "intercom")

    def test_notification_failure_does_not_break_completed_run(self):
        argv_file = self.base / "argv.json"
        env = self.env | {"FAKE_PI_MODE": "ok", "FAKE_PI_ARGV_FILE": str(argv_file)}
        cmd = self.command("standard-minimax", "standard", delegation="notify-fail", extra=[
            "--transport", "intercom", "--intercom-extension",
            str(self._write_extension()),
            "--supervisor", "11111111-2222-3333-4444-555555555555"])
        # The stub extension has no sibling cli.mjs, so the best-effort
        # notification fails naturally without touching a live supervisor.
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = [r for r in self.records_for_today()
                   if r.get("delegation_id") == "notify-fail"][0]
        self.assertEqual(receipt["outcome"]["status"], "completed")
        notifications = [item for item in self.records_for_today(include_events=True)
                         if item["record_type"] == "notification"]
        self.assertEqual(len(notifications), 1)
        self.assertFalse(notifications[0]["delivered"])

    def test_notification_attempted_after_preflight_failure(self):
        env = self.env | {"FAKE_PI_MODE": "auth-fail"}
        cmd = self.command("standard-minimax", "standard", delegation="notify-preflight", extra=[
            "--transport", "intercom", "--intercom-extension",
            str(self._write_extension()),
            "--supervisor", "11111111-2222-3333-4444-555555555555"])
        result = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        receipt = [r for r in self.records_for_today()
                   if r.get("delegation_id") == "notify-preflight"][0]
        self.assertEqual(receipt["failure"]["stage"], "preflight")
        notifications = [item for item in self.records_for_today(include_events=True)
                         if item["record_type"] == "notification"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["status"], "failed")

    def test_failure_receipt_preserves_partial_diagnostics(self):
        result = self.run_worker(mode="provider-error", delegation="partial")
        self.assertNotEqual(result.returncode, 0)
        receipt = [r for r in self.records_for_today()
                   if r["delegation_id"] == "partial"][0]
        self.assertEqual(receipt["outcome"]["status"], "failed")
        self.assertGreaterEqual(receipt["rpc_event_counts"].get("agent_start", 0), 1)
        self.assertIsNotNone(receipt["timing"]["worker_seconds"])

    def test_subprocess_sigterm_writes_interrupted_receipt(self):
        argv_file = self.base / "argv-term.json"
        receipt_path = self._receipt_path("sigterm.json")
        proc = subprocess.Popen(
            self.command("standard-minimax", "standard", delegation="term", timeout="30",
                         extra=["--receipt-file", str(receipt_path)]),
            env=self.env | {"FAKE_PI_MODE": "timeout",
                            "FAKE_PI_ARGV_FILE": str(argv_file)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if argv_file.exists():
                break
            time.sleep(0.02)
        self.assertTrue(argv_file.exists())
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            self.fail(f"runner did not exit after SIGTERM; stdout={stdout!r} stderr={stderr!r}")
        self.assertEqual(proc.returncode, 143, msg=stderr)
        records = [r for r in self.records_for_today()
                   if r.get("delegation_id") == "term"]
        self.assertEqual(len(records), 1, msg=f"stderr={stderr!r}")
        self.assertEqual(records[0]["outcome"]["status"], "interrupted")
        self.assertEqual(records[0]["failure"]["code"], "interrupted")
        self.assertEqual(json.loads(receipt_path.read_text())["outcome"]["status"],
                         "interrupted")

    # ----- shared deadline covers preflight (finding: bounded budget) -----

    def _stall_pi(self, seconds=5):
        script = self.base / "stall_pi"
        script.write_text(f"#!/bin/sh\nsleep {seconds}\n")
        script.chmod(0o755)
        return str(script)

    def test_check_provider_deadline_is_bounded_and_maps_to_timeout(self):
        stall = self._stall_pi(5)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            runner.check_provider(stall, "test-provider-a", "model", dict(self.env),
                                  deadline=time.monotonic() + 0.4)
        elapsed = time.monotonic() - start
        # Bounded by the shared deadline, not the fixed 30s/60s per-step caps.
        self.assertLess(elapsed, 3.0)

    def test_check_provider_expired_deadline_raises_without_spawning(self):
        with mock.patch.object(runner.subprocess, "run") as spawned:
            with self.assertRaises(TimeoutError):
                runner.check_provider("pi", "provider", "model", {},
                                      deadline=time.monotonic() - 1.0)
            spawned.assert_not_called()

    def test_check_provider_clamps_each_step_to_remaining_budget(self):
        captured = []

        def fake_run(argv, **kwargs):
            captured.append(kwargs.get("timeout"))
            if "--list-models" in argv:
                return mock.Mock(returncode=0, stdout="provider/model\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            runner.check_provider("pi", "provider", "model", {},
                                  deadline=time.monotonic() + 120)
        # Plenty of budget: the fixed per-step caps still apply.
        self.assertAlmostEqual(captured[0], runner.PREFLIGHT_AUTH_TIMEOUT_SECONDS)
        self.assertAlmostEqual(captured[1], runner.PREFLIGHT_CATALOG_TIMEOUT_SECONDS)
        captured.clear()
        with mock.patch.object(runner.subprocess, "run", side_effect=fake_run):
            runner.check_provider("pi", "provider", "model", {},
                                  deadline=time.monotonic() + 0.5)
        self.assertLessEqual(captured[0], 0.5)

    def test_runner_preflight_timeout_is_terminal_timeout_not_30s_overrun(self):
        stall = self._stall_pi(5)
        start = time.monotonic()
        result = subprocess.run(
            self.command("standard-minimax", "standard", delegation="preflight-stop",
                         timeout="0.5"),
            env=self.env | {"PI_WORKER_PI": stall}, text=True, capture_output=True)
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(elapsed, 5.0)
        receipt = [r for r in self.records_for_today()
                   if r.get("delegation_id") == "preflight-stop"][0]
        self.assertEqual(receipt["failure"]["category"], "timeout")
        self.assertEqual(receipt["outcome"]["status"], "interrupted")

    # ----- persistence failure keeps the checkpoint and stays visible -----

    def test_terminal_ledger_failure_keeps_checkpoint_and_returns_nonzero(self):
        real_append = runner.append_record

        def flaky_append(record, **kwargs):
            if record.get("record_type") == "run":
                raise OSError("simulated terminal ledger failure")
            return real_append(record, **kwargs)

        def fake_rpc(*args, **kwargs):
            progress = kwargs["progress"]
            progress["event_counts"] = {"agent_settled": 1}
            progress["session_id"] = "sess-ledger"
            kwargs["checkpoint"](progress)
            return {"sessionId": "sess-ledger"}, ["agent_settled"]

        argv = ["--workdir", str(self.work), "--contract", "Do it",
                "--profile", "standard-minimax", "--routing-class", "standard",
                "--thinking", "medium", "--tools", "write",
                "--delegation-id", "ledger-fail"]
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc", side_effect=fake_rpc), \
             mock.patch.object(runner, "append_record", side_effect=flaky_append):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = runner.main(argv)
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("terminal evidence was not fully written", stderr.getvalue())
        # Only the started lifecycle event is durable; no terminal run record was
        # fabricated, so the start must not be silently cleaned up.
        events = self.records_for_today(include_events=True)
        self.assertEqual([item["record_type"] for item in events], ["lifecycle"])
        checkpoint_path = (self.records / ".checkpoints"
                           / f"{events[0]['run_id']}.json")
        self.assertTrue(checkpoint_path.exists())
        checkpoint = json.loads(checkpoint_path.read_text())
        self.assertEqual(checkpoint["session_id"], "sess-ledger")

    def test_durable_ledger_removes_checkpoint_even_when_receipt_write_fails(self):
        def fake_rpc(*args, **kwargs):
            kwargs["progress"]["session_id"] = "sess-rm"
            kwargs["checkpoint"](kwargs["progress"])
            return {"sessionId": "sess-rm"}, ["agent_settled"]

        argv = ["--workdir", str(self.work), "--contract", "Do it",
                "--profile", "standard-minimax", "--routing-class", "standard",
                "--thinking", "medium", "--tools", "write",
                "--delegation-id", "ledger-ok-receipt-fail",
                "--receipt-file", str(self._receipt_path("cp.json"))]
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc", side_effect=fake_rpc), \
             mock.patch.object(runner, "write_receipt_file",
                               side_effect=OSError("no")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = runner.main(argv)
        self.assertEqual(rc, 1)
        self.assertEqual(list((self.records / ".checkpoints").glob("*.json")), [])

    def test_run_finished_notification_reports_unconfirmed_terminal_evidence(self):
        supervisor = "11111111-2222-3333-4444-555555555555"
        receipt = self._notification_receipt(outcome={"status": "failed"})
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            with mock.patch.object(runner.subprocess, "run",
                                   side_effect=subprocess.TimeoutExpired("node", 5)):
                event = runner.send_run_finished_notification(
                    transport="intercom", receipt=receipt, supervisor_id=supervisor,
                    extension=None, explicit_cli="/tmp/fake.mjs", record_kind="production",
                    ledger_written=False, receipt_written=False, receipt_required=True)
        self.assertFalse(event["ledger_written"])
        self.assertFalse(event["receipt_written"])
        self.assertTrue(event["receipt_required"])
        payload = json.loads(runner.build_run_finished_text(
            receipt, followup_required=True, ledger_written=False,
            receipt_written=False, receipt_required=True))
        self.assertFalse(payload["ledger_written"])
        self.assertFalse(payload["receipt_written"])
        self.assertTrue(payload["receipt_required"])
        self.assertTrue(payload["followup_required"])

    # ----- early session diagnostics survive a failed run -----

    def test_failure_receipt_records_early_session_id(self):
        for mode in ("provider-error", "timeout"):
            with self.subTest(mode=mode):
                result = self.run_worker(mode=mode, delegation=f"early-{mode}",
                                         timeout="1")
                self.assertNotEqual(result.returncode, 0)
                receipt = [r for r in self.records_for_today()
                           if r.get("delegation_id") == f"early-{mode}"][0]
                self.assertEqual(receipt["worker"]["session_id"], "fake-session")

    # ----- receipt parent safety and write-time re-validation -----

    def test_receipt_file_parent_symlink_alias_policy(self):
        safe = self.base / "safe-parent"
        safe.mkdir()
        os.chmod(safe, 0o700)
        alias = self.base / "alias-parent"
        alias.symlink_to(safe)
        # Canonical resolution documents platform aliases (macOS /tmp ->
        # /private/tmp): a parent symlink whose target is safe is accepted and
        # the receipt lands at the canonical target.
        result = self.run_worker(delegation="alias-ok",
                                 extra=["--receipt-file", str(alias / "receipt.json")])
        self.assertEqual(result.returncode, 0, result.stderr)
        canonical = safe / "receipt.json"
        self.assertTrue(canonical.exists())
        self.assertEqual(canonical.stat().st_mode & 0o777, 0o600)

    def test_receipt_file_platform_tmp_alias_is_canonicalized(self):
        # The documented policy resolves platform parent aliases (macOS
        # /tmp -> /private/tmp) instead of refusing them, and validate-only is
        # non-mutating even for the real system temp directory.
        candidate = Path("/tmp") / f"pi-runner-alias-{os.getpid()}.json"
        try:
            canonical = runner.validate_receipt_file(str(candidate))
        except runner.RunnerError as exc:  # pragma: no cover - platform-specific
            self.skipTest(f"/tmp is not a usable receipt alias here: {exc}")
        self.assertTrue(canonical.is_absolute())
        self.assertNotIn("..", canonical.parts)
        self.assertFalse(candidate.exists())
        self.assertFalse(canonical.exists())

    def test_receipt_file_write_rechecks_and_refuses_unsafe_parent(self):
        world = self.base / "write-world"
        world.mkdir()
        os.chmod(world, 0o777)
        with self.assertRaises(runner.RunnerError):
            runner.write_receipt_file(world / "r.json", {"run_id": "x"})
        self.assertFalse((world / "r.json").exists())
        # Safety is re-checked at write time, after parent creation, narrowing the
        # validate/write race.
        calls = []
        real_check = runner._check_receipt_path

        def counting(path):
            calls.append(path)
            return real_check(path)

        target = self.base / "fresh" / "nested" / "r.json"
        with mock.patch.object(runner, "_check_receipt_path", side_effect=counting):
            runner.write_receipt_file(target, {"run_id": "x"})
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    # ----- explicit review verdict append failure stays visible -----

    def test_review_status_append_failure_is_visible_and_nonzero(self):
        real_append = runner.append_record

        def flaky_append(record, **kwargs):
            if record.get("record_type") == "review":
                raise OSError("simulated review audit failure")
            return real_append(record, **kwargs)

        argv = ["--workdir", str(self.work), "--contract", "Do it",
                "--profile", "standard-minimax", "--routing-class", "standard",
                "--thinking", "medium", "--tools", "write",
                "--delegation-id", "review-append-fail",
                "--review-status", "accepted"]
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runner, "check_provider"), \
             mock.patch.object(runner, "run_rpc",
                               return_value=({"sessionId": "s"}, ["agent_settled"])), \
             mock.patch.object(runner, "append_record", side_effect=flaky_append):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = runner.main(argv)
        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("review event not written", stderr.getvalue())
        events = self.records_for_today(include_events=True)
        self.assertEqual([item["record_type"] for item in events], ["lifecycle", "run"])


class RecordDirTests(unittest.TestCase):
    """The two harnesses keep separate ledgers; an explicit override still wins."""

    def _clear(self):
        for name in ("PI_CODING_AGENT", "AI_AGENT", "PI_WORKER_RECORD_DIR"):
            os.environ.pop(name, None)

    def test_codex_and_other_harnesses_keep_the_original_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            self._clear()
            self.assertEqual(runner.record_dir(), Path.home() / ".codex" / "worker-run-records")

    def test_pi_marker_routes_records_under_the_pi_agent_directory(self):
        with mock.patch.dict(os.environ, {"PI_CODING_AGENT": "true"}, clear=False):
            self._clear()
            os.environ["PI_CODING_AGENT"] = "true"
            self.assertEqual(runner.record_dir(), Path.home() / ".pi" / "agent" / "worker-run-records")

    def test_ai_agent_marker_also_routes_to_pi(self):
        with mock.patch.dict(os.environ, {"AI_AGENT": "pi"}, clear=False):
            self._clear()
            os.environ["AI_AGENT"] = "pi"
            self.assertEqual(runner.record_dir(), Path.home() / ".pi" / "agent" / "worker-run-records")

    def test_explicit_override_wins_in_both_harnesses(self):
        for marker in ({}, {"PI_CODING_AGENT": "true"}):
            with mock.patch.dict(os.environ, marker, clear=False):
                self._clear()
                os.environ.update(marker)
                os.environ["PI_WORKER_RECORD_DIR"] = "/tmp/records"
                self.assertEqual(runner.record_dir(), Path("/tmp/records"))

class UnchangedTimeoutRetryGateTests(unittest.TestCase):
    """Same-contract retry after a timeout is refused before any side effect."""

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
            "ZAI_CODING_CN_API_KEY": "test", "OPENCODE_API_KEY": "test", "MINIMAX_CN_API_KEY": "test",
        })
        self.contract = "Do it"
        self.baseline = set()

    # ----- helpers -----

    def command(self, delegation="dgate", *, contract=None, contract_file=None,
                extra=None, validate_only=False, record_kind=None):
        cmd = [sys.executable, str(RUNNER), "--workdir", str(self.work),
               "--routing-class", "standard", "--profile", "standard-glm",
               "--thinking", "medium", "--tools", "write",
               "--delegation-id", delegation, "--timeout", "5"]
        if contract_file is not None:
            cmd.extend(["--contract-file", contract_file])
        else:
            cmd.extend(["--contract", contract if contract is not None else self.contract])
        if validate_only:
            cmd.append("--validate-only")
        if record_kind:
            cmd.extend(["--record-kind", record_kind])
        if extra:
            cmd.extend(extra)
        return cmd

    def run_gate(self, **kwargs):
        env = self.env | {"FAKE_PI_MODE": kwargs.pop("mode", "ok")}
        return subprocess.run(self.command(**kwargs), env=env, text=True, capture_output=True)

    def digest(self, text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def append(self, record):
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}):
            path = runner.append_record(record, day="2026-10-07")
        # Fixture records are the baseline: later runs must add nothing.
        self.baseline = self.snapshot()
        return path

    def snapshot(self):
        if not self.records.exists():
            return set()
        return {p.name for p in self.records.iterdir()}

    def prior_run(self, *, delegation="dgate", digest=None, code="timeout",
                  record_kind="production", record_type="run", run_id="prior-run-1",
                  status="failed", schema_version=3):
        assignment = {"goal": "g", "scope": ["s"], "tool_mode": "write",
                      "acceptance_checks": []}
        if digest is not None:
            assignment["contract_digest"] = digest
        record = {
            "schema_version": schema_version, "phase": "completed",
            "run_id": run_id, "delegation_id": delegation,
            "record_kind": record_kind, "worker": {"worker_type": "standard-glm"},
            "assignment": assignment,
            "outcome": {"status": status, "result": "unusable", "summary": None},
            "failure": None if code is None else {
                "category": "timeout", "code": code, "stage": "rpc",
                "evidence": "TimeoutError"},
        }
        if record_type is not None:
            record["record_type"] = record_type
        return record

    def ledger_records(self):
        return [item for path in sorted(self.records.glob("*.jsonl"))
                for item in (json.loads(x) for x in path.read_text().splitlines())]

    def assert_no_side_effects(self):
        new = self.snapshot() - self.baseline
        self.assertEqual(new, set(),
                         "the gate must not write records, routing state, or "
                         "profile-state sidecars")

    # ----- rejection -----

    def test_same_digest_after_timeout_is_rejected_before_reserve_provider_or_launch(self):
        self.append(self.prior_run(digest=self.digest(self.contract)))
        # In-process: the guard must fire before reserve_selection, the provider
        # preflight, Popen, and every record/receipt write.
        argv = ["--workdir", str(self.work), "--contract", self.contract,
                "--routing-class", "standard", "--profile", "standard-glm",
                "--thinking", "medium", "--tools", "write", "--delegation-id", "dgate"]
        with mock.patch.dict(os.environ, {"PI_WORKER_RECORD_DIR": str(self.records)}), \
             mock.patch.object(runner, "reserve_selection") as reserve, \
             mock.patch.object(runner, "check_provider") as provider, \
             mock.patch.object(runner.subprocess, "Popen") as popen, \
             mock.patch.object(runner, "append_record") as append_record, \
             mock.patch.object(runner, "write_receipt_file") as write_receipt, \
             mock.patch.object(runner, "previous_run_id") as previous_run, \
             contextlib.redirect_stderr(io.StringIO()) as err, \
             contextlib.redirect_stdout(io.StringIO()):
                status = runner.main(argv)
        self.assertEqual(status, 1)
        self.assertIn("unchanged retry after a timeout", err.getvalue())
        for mock_obj in (reserve, provider, popen, append_record, write_receipt, previous_run):
            mock_obj.assert_not_called()
        # CLI surface: same refusal, exit 1, nothing written, no contract echoed.
        result = self.run_gate(mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged retry after a timeout", result.stderr)
        self.assertNotIn(self.contract, result.stderr)
        self.assert_no_side_effects()

    def test_tool_timeout_with_same_digest_is_also_rejected(self):
        self.append(self.prior_run(digest=self.digest(self.contract), code="tool_timeout"))
        result = self.run_gate(mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged retry after a timeout", result.stderr)
        self.assert_no_side_effects()

    def test_real_timeout_run_then_identical_retry_is_gated(self):
        first = self.run_gate(delegation="dreal", mode="timeout", extra=["--timeout", "0.2"])
        self.assertEqual(first.returncode, 124)
        receipt = json.loads(first.stderr)
        self.assertEqual(receipt["failure"]["code"], "timeout")
        self.assertEqual(receipt["assignment"]["contract_digest"], self.digest(self.contract))
        self.assertIs(receipt["assignment"]["unchanged_timeout_retry_override"], False)
        second = self.run_gate(delegation="dreal", mode="auth-fail")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("unchanged retry after a timeout", second.stderr)
        # Only the first run's records exist: no reservation, no new lifecycle.
        kinds = [item.get("record_type") for item in self.ledger_records()]
        self.assertEqual(kinds.count("lifecycle"), 1)
        self.assertEqual(kinds.count("run"), 1)

    # ----- permitted retries -----

    def test_revised_contract_with_same_delegation_id_runs_and_stays_pinned(self):
        first = self.run_gate(delegation="dscope", mode="timeout", extra=["--timeout", "0.2"])
        self.assertEqual(first.returncode, 124)
        self.assertEqual(json.loads(first.stderr)["routing"]["routing_class"], "standard")
        second = self.run_gate(delegation="dscope", contract=self.contract + " (narrowed)",
                               extra=["--profile", "standard-glm"])
        self.assertEqual(second.returncode, 0, second.stderr)
        receipt = json.loads(second.stdout)
        self.assertEqual(receipt["outcome"]["status"], "completed")
        self.assertEqual(receipt["routing"]["routing_class"], "standard")
        self.assertEqual(receipt["routing"]["profile_source"], "explicit")
        self.assertNotEqual(receipt["assignment"]["contract_digest"],
                            self.digest(self.contract))
        self.assertEqual(receipt["previous_run_id"], json.loads(first.stderr)["run_id"])

    def test_prior_record_without_digest_is_allowed_to_retry_unchanged(self):
        self.append(self.prior_run(digest=None))
        result = self.run_gate(delegation="dlegacy")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_timeout_prior_failure_permits_unchanged_retry(self):
        for code in ("provider_usage_limit", "unexpected_exception", "runner_error", None):
            with self.subTest(code=code):
                self.setUp()
                self.append(self.prior_run(digest=self.digest(self.contract), code=code))
                result = self.run_gate(delegation="dnon")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_delegation_id_is_never_gated(self):
        self.append(self.prior_run(delegation="other", digest=self.digest(self.contract)))
        result = self.run_gate(delegation="dnew")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_newest_actual_run_wins_over_newer_review_and_lifecycle_records(self):
        self.append(self.prior_run(digest=self.digest(self.contract), code="timeout",
                                   run_id="older-timeout"))
        self.append({"schema_version": 3, "record_type": runner.LIFECYCLE_RECORD_TYPE,
                     "phase": "started", "event": "run_started", "run_id": "newer",
                     "delegation_id": "dgate", "record_kind": "production"})
        self.append({"schema_version": 3, "record_type": "review", "phase": "reviewed",
                     "review_id": "rv1", "target_run_id": "newer", "supersedes_review_id": None,
                     "outcome": {"review_verdict": "pending", "result": "partial"}})
        result = self.run_gate(mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older-timeout", result.stderr)

    def test_superseded_timeout_does_not_gate_a_later_successful_run(self):
        self.append(self.prior_run(digest=self.digest(self.contract), code="timeout",
                                   run_id="older-timeout"))
        self.append(self.prior_run(digest=self.digest(self.contract), code=None,
                                   status="completed", run_id="newer-success"))
        result = self.run_gate(delegation="dgate")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_schema_two_run_without_record_type_is_inspected(self):
        self.append(self.prior_run(digest=self.digest(self.contract), record_type=None,
                                   schema_version=2))
        result = self.run_gate(mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged retry after a timeout", result.stderr)

    def test_test_record_kind_is_gated_separately_from_production(self):
        self.append(self.prior_run(delegation="dmix", digest=self.digest(self.contract),
                                   record_kind="test"))
        # A production launch ignores the test record...
        result = self.run_gate(delegation="dmix", record_kind="production")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.setUp()
        # ...and a test launch in its own isolated ledger is gated by it.
        self.append(self.prior_run(delegation="dmix", digest=self.digest(self.contract),
                                   record_kind="test"))
        result = self.run_gate(delegation="dmix", record_kind="test", mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged retry after a timeout", result.stderr)

    # ----- inline vs file text -----

    def test_inline_and_file_contract_texts_share_one_digest_and_gate_each_other(self):
        first = self.run_gate(delegation="dtext", mode="timeout", extra=["--timeout", "0.2"])
        self.assertEqual(first.returncode, 124)
        inline_digest = json.loads(first.stderr)["assignment"]["contract_digest"]
        self.assertEqual(inline_digest, self.digest(self.contract))
        path = self.base / "contract.txt"
        path.write_text(self.contract, encoding="utf-8")
        result = self.run_gate(delegation="dtext", contract_file=str(path), mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged retry after a timeout", result.stderr)
        # The validated file bytes are what get launched, so the gate cannot be
        # bypassed by swapping the file between validation and launch.
        self.assertEqual(runner.read_contract_text(path), self.contract)
        self.assertEqual(runner.contract_digest(runner.read_contract_text(path)),
                         inline_digest)

    # ----- explicit override -----

    def test_override_allows_the_retry_and_is_recorded_in_the_receipt(self):
        self.append(self.prior_run(digest=self.digest(self.contract)))
        result = self.run_gate(extra=["--allow-unchanged-timeout-retry"])
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        assignment = receipt["assignment"]
        self.assertIs(assignment["unchanged_timeout_retry_override"], True)
        self.assertEqual(assignment["contract_digest"], self.digest(self.contract))
        # The override changes no identity: same profile, class, and sticky state.
        self.assertEqual(receipt["worker"]["model"], runner.PROFILES["standard-glm"]["model"])
        self.assertEqual(receipt["routing"]["routing_class"], "standard")
        self.assertEqual(receipt["routing"]["profile_source"], "explicit")
        # Receipt never carries the raw contract or a contract path.
        self.assertNotIn(self.contract, json.dumps(receipt))

    def test_override_flag_is_a_harmless_boolean_defaulting_to_false(self):
        args = runner.parse_args(["--workdir", str(self.work), "--contract", "x",
                                  "--tools", "write", "--delegation-id", "d"])
        self.assertIs(args.allow_unchanged_timeout_retry, False)
        args = runner.parse_args(["--workdir", str(self.work), "--contract", "x",
                                  "--tools", "write", "--delegation-id", "d",
                                  "--allow-unchanged-timeout-retry"])
        self.assertIs(args.allow_unchanged_timeout_retry, True)
        with self.assertRaises(SystemExit):
            runner.parse_args(["--workdir", str(self.work), "--contract", "x",
                               "--tools", "write", "--delegation-id", "d",
                               "--allow-unchanged-timeout-retry=maybe"])

    # ----- dry run -----

    def test_validate_only_enforces_the_gate_without_side_effects(self):
        self.append(self.prior_run(digest=self.digest(self.contract)))
        result = self.run_gate(validate_only=True, mode="auth-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unchanged retry after a timeout", result.stderr)
        self.assertNotIn(self.contract, result.stderr)
        self.assert_no_side_effects()
        # A revised contract still dry-runs clean.
        self.assertEqual(self.run_gate(contract=self.contract + " (narrowed)",
                                       validate_only=True).returncode, 0)
        # ...and so does the explicit override.
        self.assertEqual(self.run_gate(validate_only=True,
                                       extra=["--allow-unchanged-timeout-retry"]).returncode, 0)

    def test_inline_contract_keeps_precedence_over_contract_file(self):
        # The CLI rejects both flags together (mutually exclusive), but the
        # programmatic path must keep the historical order: inline text wins.
        self.assertEqual(runner.parse_args.__module__, "runner")
        path = self.base / "other-contract.txt"
        path.write_text("file text", encoding="utf-8")
        args = runner.parse_args(["--workdir", str(self.work), "--contract", self.contract,
                                  "--tools", "write", "--delegation-id", "d"])
        args.contract_file = str(path)
        runner.validate_launch_inputs(args)
        self.assertEqual(args.contract_text, self.contract)
        self.assertEqual(args.contract_digest, self.digest(self.contract))
        # File-only launches still use the file text and its digest.
        file_only = runner.parse_args(["--workdir", str(self.work),
                                       "--contract-file", str(path),
                                       "--tools", "write", "--delegation-id", "d"])
        runner.validate_launch_inputs(file_only)
        self.assertEqual(file_only.contract_text, "file text")
        self.assertEqual(file_only.contract_digest, self.digest("file text"))
        # ...and the two spellings are not silently interchangeable.
        self.assertNotEqual(file_only.contract_digest, args.contract_digest)
        # The CLI itself refuses both flags at once, as before.
        with self.assertRaises(SystemExit) as caught:
            runner.parse_args(["--workdir", str(self.work), "--contract", "x",
                               "--contract-file", str(path), "--tools", "write",
                               "--delegation-id", "d"])
        self.assertEqual(caught.exception.code, 2)

    def test_validate_only_never_echoes_contract_text_and_keeps_the_old_summary(self):
        secret = "unique-contract-marker-9f2b"
        path = self.base / "secret-contract.txt"
        path.write_text(secret, encoding="utf-8")
        result = self.run_gate(delegation="dvalid", contract_file=str(path), validate_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn(str(path), result.stdout)
        validated = json.loads(result.stdout)["validated"]
        self.assertEqual(set(validated.keys()),
                         {"workdir", "routing_class", "profile", "profile_source",
                          "thinking", "transport", "tools", "receipt_file",
                          "task_kind", "acceptance_mode", "risk"})
        self.assert_no_side_effects()


if __name__ == "__main__":
    unittest.main()
