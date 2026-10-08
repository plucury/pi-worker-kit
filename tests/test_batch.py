"""Tests for the bounded concurrent batch driver.

Every test mocks the child runner: a subprocess entrypoint imports the batch
module, patches its module-level ``RUNNER_PATH`` to ``tests/fake_batch_runner.py``
and calls ``batch.main``. No test invokes the real runner, a provider, or writes a
production record. Temp workdirs, contracts, output dirs, and
``PI_WORKER_RECORD_DIR`` keep every artifact private. Subprocess tests use short
bounded timeouts and the interrupt test cleans up owned children.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "scripts" / "run_pi_worker_batch.py"
FAKE = ROOT / "tests" / "fake_batch_runner.py"
# Committed fake-only profiles fixture (also injected session-wide by conftest).
FIXTURE = ROOT / "tests" / "fixtures" / "profiles.json"

ENTRYPOINT = (
    "import importlib.util, os, sys\n"
    "from pathlib import Path\n"
    "spec = importlib.util.spec_from_file_location('batch', os.environ['PI_BATCH_TEST_MODULE'])\n"
    "batch = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(batch)\n"
    "batch.RUNNER_PATH = Path(os.environ['PI_BATCH_TEST_FAKE'])\n"
    "raise SystemExit(batch.main(sys.argv[1:]))\n"
)

# Synthetic UUID for fake-only transport tests; never a live supervisor session.
SUPERVISOR = "11111111-2222-4333-8444-555555555555"


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.work = self.base / "work"
        self.work.mkdir()
        (self.work / "a.txt").write_text("a")
        (self.work / "b.txt").write_text("b")
        (self.work / "shared.txt").write_text("shared")
        self.contract = self.base / "contract.txt"
        self.contract.write_text("Do the subtask.")
        self.out = self.base / "out"
        self.manifest = self.base / "manifest.json"
        self.env = dict(os.environ)
        self.env["PI_WORKER_RECORD_DIR"] = str(self.base / "records")
        self.env["PI_BATCH_TEST_MODULE"] = str(BATCH)
        self.env["PI_BATCH_TEST_FAKE"] = str(FAKE)
        self.env.pop("PI_CODING_AGENT", None)
        self.env.pop("AI_AGENT", None)

    # --- helpers --------------------------------------------------------------

    def task(self, tid, delegation=None, *, tools="write", scope=("a.txt",),
             workdir=None, extra=None):
        entry = {"id": tid, "delegation_id": delegation or ("d-" + tid),
                 "workdir": str(workdir or self.work),
                 "contract_file": str(self.contract),
                 "tools": tools, "scope": list(scope)}
        if extra:
            entry.update(extra)
        return entry

    def write_manifest(self, tasks, **extra_fields):
        manifest = {"version": 1, "tasks": tasks}
        manifest.update(extra_fields)
        self.manifest.write_text(json.dumps(manifest))
        return self.manifest

    def batch_args(self, *extra, manifest=None, out=None):
        return ["--manifest", str(manifest or self.manifest),
                "--output-dir", str(out or self.out), *extra]

    def run_batch(self, *extra, mode_env=None, timeout=60, manifest=None, out=None,
                  clean_env=False):
        if clean_env:
            # A truly clean environment: only what the fake entrypoint needs,
            # with PI_WORKER_PROFILES_FILE absent and HOME redirected to an
            # isolated empty dir, so the child sees no profiles anywhere
            # (neither env nor default). self.env is never merged in.
            env = {"PI_BATCH_TEST_MODULE": str(BATCH),
                   "PI_BATCH_TEST_FAKE": str(FAKE),
                   "PI_WORKER_RECORD_DIR": str(self.base / "records"),
                   "HOME": str(self.base / "no-home")}
        else:
            env = dict(self.env)
        if mode_env:
            env.update(mode_env)
        return subprocess.run(
            [sys.executable, "-c", ENTRYPOINT, *self.batch_args(
                *extra, manifest=manifest, out=out)],
            env=env, text=True, capture_output=True, timeout=timeout)

    def summary(self):
        return json.loads((self.out / "batch-summary.json").read_text())

    def statuses(self):
        return {e["id"]: e["status"] for e in self.summary()["tasks"]}

    def markers(self, tid):
        return self.out / tid / "markers"

    def argv_for(self, tid, out=None):
        # Reads the requested output dir (default the first launch's) so a
        # second launch in a different dir is asserted from its own markers.
        return json.loads(((out or self.out) / tid / "markers" /
                           f"argv-{tid}.json").read_text())

    def read_receipt(self, tid):
        return json.loads((self.out / tid / "terminal.json").read_text())

    # --- concurrency ----------------------------------------------------------

    def test_three_tasks_run_concurrently(self):
        gate = self.base / "gate"
        env = {"BATCH_FAKE_BARRIER": str(gate), "BATCH_FAKE_BARRIER_THRESHOLD": "3"}
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", scope=("b.txt",)),
                             self.task("t3", scope=("shared.txt",))])
        result = self.run_batch(mode_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for tid in ("t1", "t2", "t3"):
            self.assertTrue((self.markers(tid) / f"concurrent-{tid}").exists(),
                            f"{tid} never joined the barrier")
        self.assertEqual(self.statuses(), {"t1": "completed", "t2": "completed",
                                           "t3": "completed"})
        self.assertEqual(self.summary()["max_concurrency"], 3)
        for entry in self.summary()["tasks"]:
            self.assertEqual(entry["review_status"], "pending_main_review")

    def test_max_concurrency_one_runs_sequentially(self):
        timings = self.base / "timings.jsonl"
        self.write_manifest([self.task("s1", scope=("a.txt",)),
                             self.task("s2", scope=("b.txt",))])
        result = self.run_batch("--max-concurrency", "1",
                                mode_env={"BATCH_FAKE_TIMINGS": str(timings)})
        self.assertEqual(result.returncode, 0, result.stderr)
        spans = sorted((json.loads(line) for line in timings.read_text().splitlines()),
                       key=lambda item: item["start"])
        self.assertEqual(len(spans), 2)
        self.assertGreaterEqual(spans[1]["start"], spans[0]["start"] + 0.10)

    def test_more_tasks_than_slots_finish(self):
        tasks = [self.task(f"k{i}", scope=(f"f{i}.txt",)) for i in range(5)]
        self.write_manifest(tasks)
        result = self.run_batch("--max-concurrency", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.statuses().values()), {"completed"})
        self.assertEqual(self.summary()["max_concurrency"], 2)

    def test_default_limit_caps_at_three(self):
        tasks = [self.task(f"k{i}", scope=(f"f{i}.txt",)) for i in range(5)]
        self.write_manifest(tasks)
        result = self.run_batch("--validate-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["max_concurrency"], 3)

    # --- CLI validation (no launch) -------------------------------------------

    def test_bad_max_concurrency_values_rejected(self):
        self.write_manifest([self.task("t1")])
        for raw in ("0", "-1", "true", "1.5"):
            out = self.base / ("out-" + raw.replace(".", "-"))
            result = self.run_batch("--max-concurrency", raw, out=out)
            self.assertNotEqual(result.returncode, 0, raw)
            self.assertFalse(out.exists(), f"output created for {raw}")

    def test_validate_only_never_mutates(self):
        self.write_manifest([self.task("t1")])
        result = self.run_batch("--validate-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "status": "validated", "tasks": ["t1"], "max_concurrency": 1})
        self.assertFalse(self.out.exists())
        self.assertFalse((self.base / "records").exists())

    # --- manifest rejection before any output creation --------------------------

    def test_hostile_id_rejected(self):
        self.write_manifest([self.task("../escape")])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_duplicate_delegation_rejected(self):
        self.write_manifest([self.task("t1"),
                             self.task("t2", delegation="d-t1", scope=("b.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_unknown_task_field_rejected(self):
        self.write_manifest([self.task("t1", extra={"depends_on": ["t9"]})])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_unknown_manifest_field_rejected(self):
        self.write_manifest([self.task("t1")], batch_id="zzz")
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_simple_routing_retired(self):
        self.write_manifest([self.task("t1", extra={"routing_class": "simple"})])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("routing_class", result.stderr)
        self.assertFalse(self.out.exists())

    # --- conflict detection (canonical, cross-workdir) ------------------------

    def test_write_write_scope_conflict_rejected(self):
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", scope=("a.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_write_read_scope_conflict_rejected(self):
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", tools="read-only",
                                       scope=("a.txt", "b.txt"))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_read_read_overlap_allowed(self):
        self.write_manifest([self.task("t1", tools="read-only", scope=("a.txt",)),
                             self.task("t2", tools="read-only",
                                       scope=("a.txt", "b.txt"))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.statuses().values()), {"completed"})

    def test_ancestor_descendant_conflict_rejected(self):
        (self.work / "dir").mkdir()
        (self.work / "dir" / "file.txt").write_text("x")
        self.write_manifest([self.task("t1", scope=("dir",)),
                             self.task("t2", scope=("dir/file.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_same_relative_name_in_unrelated_dirs_allowed(self):
        other = self.base / "other"
        other.mkdir()
        (other / "a.txt").write_text("other")
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", workdir=other, scope=("a.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nested_workdir_conflict_rejected(self):
        sub = self.work / "sub"
        sub.mkdir()
        (sub / "x.txt").write_text("x")
        self.write_manifest([self.task("t1", scope=("sub/x.txt",)),
                             self.task("t2", workdir=sub, scope=("x.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_workdir_alias_conflict_rejected(self):
        alias = self.base / "work-alias"
        alias.symlink_to(self.work, target_is_directory=True)
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", workdir=alias, scope=("a.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_symlink_alias_scope_conflict_rejected(self):
        (self.work / "link.txt").symlink_to(self.work / "a.txt")
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", scope=("link.txt",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_scope_symlink_escape_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.work / "link").symlink_to(outside, target_is_directory=True)
        self.write_manifest([self.task("t1", scope=("link",))])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("scope", result.stderr)
        self.assertFalse(self.out.exists())

    def test_resource_conflict_rejected(self):
        self.write_manifest([self.task("t1", extra={"resources": ["db"]}),
                             self.task("t2", scope=("b.txt",),
                                       extra={"resources": ["db"]})])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_existing_nonempty_output_rejected(self):
        self.out.mkdir()
        (self.out / "stale.txt").write_text("old data")
        self.write_manifest([self.task("t1")])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["stale.txt"])

    # --- forwarding and Pi-main policy ----------------------------------------

    def test_transport_and_pi_flags_forwarded_to_child(self):
        ext = self.base / "intercom.ts"
        ext.write_text("// extension")
        cli = self.base / "cli.mjs"
        cli.write_text("// cli")
        self.write_manifest([self.task("t1")])
        result = self.run_batch(
            "--transport", "intercom",
            "--intercom-extension", str(ext),
            "--supervisor", SUPERVISOR,
            "--intercom-cli", str(cli),
            "--pi", "/custom/pi")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.argv_for("t1")
        self.assertIn("--transport", argv)
        self.assertEqual(argv[argv.index("--transport") + 1], "intercom")
        self.assertEqual(argv[argv.index("--intercom-extension") + 1], str(ext))
        self.assertEqual(argv[argv.index("--supervisor") + 1], SUPERVISOR)
        self.assertEqual(argv[argv.index("--intercom-cli") + 1], str(cli))
        self.assertEqual(argv[argv.index("--pi") + 1], "/custom/pi")

    def test_rpc_rejects_intercom_flags(self):
        ext = self.base / "intercom.ts"
        ext.write_text("// extension")
        self.write_manifest([self.task("t1")])
        result = self.run_batch("--intercom-extension", str(ext),
                                "--supervisor", SUPERVISOR)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_pi_main_rejects_rpc(self):
        self.write_manifest([self.task("t1")])
        result = self.run_batch(mode_env={"PI_CODING_AGENT": "true"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("intercom", result.stderr)
        self.assertFalse(self.out.exists())

    def test_validate_only_accepts_pi_main_intercom(self):
        ext = self.base / "intercom.ts"
        ext.write_text("// extension")
        self.write_manifest([self.task("t1")])
        result = self.run_batch(
            "--validate-only", "--transport", "intercom",
            "--intercom-extension", str(ext), "--supervisor", SUPERVISOR,
            mode_env={"PI_CODING_AGENT": "true"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.out.exists())

    # --- classification -------------------------------------------------------

    def test_one_failure_leaves_others_completed(self):
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", scope=("b.txt",))])
        result = self.run_batch(mode_env={"BATCH_FAKE_MODE_t2": "fail"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.statuses(), {"t1": "completed", "t2": "failed"})

    def test_nonzero_exit_is_failed_even_with_completed_receipt(self):
        self.write_manifest([self.task("t1")])
        result = self.run_batch(mode_env={"BATCH_FAKE_MODE_t1": "nonzero_ok"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.statuses(), {"t1": "failed"})
        self.assertEqual(self.summary()["tasks"][0]["error_code"], "nonzero_exit")

    def test_malformed_receipt_is_unconfirmed(self):
        self.write_manifest([self.task("bad")])
        result = self.run_batch(mode_env={"BATCH_FAKE_MODE_bad": "bad"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.statuses(), {"bad": "unconfirmed"})

    def test_missing_receipt_is_unconfirmed(self):
        self.write_manifest([self.task("gap")])
        result = self.run_batch(mode_env={"BATCH_FAKE_MODE_gap": "none"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.statuses(), {"gap": "unconfirmed"})

    def test_wrong_delegation_receipt_is_unconfirmed(self):
        self.write_manifest([self.task("t1")])
        result = self.run_batch(mode_env={"BATCH_FAKE_MODE_t1": "wrong_delegation"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.statuses(), {"t1": "unconfirmed"})
        self.assertNotIn("someone-else", json.dumps(self.summary()))

    def test_duplicate_run_receipt_is_unconfirmed(self):
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", scope=("b.txt",))])
        env = {"BATCH_FAKE_MODE_t1": "duplicate_run",
               "BATCH_FAKE_MODE_t2": "duplicate_run"}
        result = self.run_batch(mode_env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sorted(self.statuses().values()),
                         ["completed", "unconfirmed"])

    def test_summary_has_no_contract_or_scope_content(self):
        self.write_manifest([self.task("t1")])
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stderr)
        blob = (self.out / "batch-summary.json").read_text()
        self.assertNotIn("Do the subtask", blob)
        self.assertNotIn(str(self.work), blob)

    # --- interruption and cleanup ---------------------------------------------

    def test_interrupt_terminates_owned_group_and_leaves_queue_not_started(self):
        self.write_manifest([self.task("t1", scope=("a.txt",)),
                             self.task("t2", scope=("b.txt",))])
        env = dict(self.env)
        env["BATCH_FAKE_MODE_t1"] = "spawn_descendant"
        proc = subprocess.Popen(
            [sys.executable, "-c", ENTRYPOINT,
             *self.batch_args("--max-concurrency", "1")],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self._kill_if_alive, proc)
        pid_file = self.markers("t1") / "descendant-t1.pid"
        deadline = time.time() + 15
        while time.time() < deadline and not pid_file.exists():
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "descendant never started")
        descendant = int(pid_file.read_text().strip())
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 130, (out, err))
        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, "descendant process outlived the batch")
        self.assertEqual(self.statuses(), {"t1": "interrupted", "t2": "not_started"})

    # --- optional manifest task_group_id ---------------------------------------

    def test_profiles_file_forwarded_to_child(self):
        # The optional global --profiles-file is forwarded AS-IS to every child
        # (same path for dry validation and spawn: one argv builder); when
        # unset, no flag is forwarded and children use env/default.
        self.write_manifest([self.task("t1")])
        custom = self.base / "profiles-custom.json"
        # The forwarded file must itself be a usable configuration, so the copy
        # carries the same fake fixture table the session env provides.
        custom.write_text(FIXTURE.read_text())
        result = self.run_batch("--profiles-file", str(custom))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.argv_for("t1")
        self.assertIn("--profiles-file", argv)
        self.assertEqual(argv[argv.index("--profiles-file") + 1], str(custom))
        result = self.run_batch(out=self.base / "out2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--profiles-file", self.argv_for("t1", out=self.base / "out2"))

    def test_clean_machine_validate_only_without_profiles_fails_informative(self):
        # A fresh environment with no profiles anywhere: clean_env=True means
        # PI_WORKER_PROFILES_FILE is truly absent (not merged back from
        # self.env) and HOME is an isolated empty dir, so dry validation fails
        # with the informative no-enabled-profile error before any paid call,
        # spawn, marker, receipt, or record.
        (self.base / "no-home").mkdir()
        self.write_manifest([self.task("t1")])
        result = self.run_batch("--validate-only", clean_env=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("no enabled standard profile", result.stderr + result.stdout)
        self.assertFalse(self.out.exists())
        self.assertFalse((self.base / "records").exists())

    def test_validate_only_rejects_profiles_without_enabled_sub_role(self):
        # An enabled profile assigned only to the main role is not a worker
        # (sub) role profile, so a --validate-only launch must refuse it before
        # any output dir, marker, receipt, or record. Empty roles are equally
        # inert. The config is a synthetic temp file, never home config.
        for label, roles in (("main-only", ["main"]),
                             ("empty-roles", [])):
            with self.subTest(label=label):
                cfg = self.base / f"profiles-{label}.json"
                cfg.write_text(json.dumps({
                    "version": 2,
                    "profiles": [{
                        "provider": "synthetic-provider",
                        "model": f"synthetic-model-{label}",
                        "enabled": True,
                        "roles": roles,
                    }],
                }))
                out = self.base / f"out-{label}"
                self.write_manifest([self.task("t1")])
                result = self.run_batch(
                    "--validate-only", "--profiles-file", str(cfg), out=out)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("no enabled standard profile",
                              result.stderr + result.stdout)
                self.assertFalse(out.exists())
                self.assertFalse((self.base / "records").exists())

    def test_validate_only_accepts_shared_main_sub_profile(self):
        # The same profile assigned to both main and sub roles satisfies the
        # worker pool, so --validate-only succeeds and writes nothing.
        cfg = self.base / "profiles-shared.json"
        cfg.write_text(json.dumps({
            "version": 2,
            "profiles": [{
                "provider": "synthetic-provider",
                "model": "synthetic-model-shared",
                "enabled": True,
                "roles": ["main", "sub"],
            }],
        }))
        self.write_manifest([self.task("t1")])
        result = self.run_batch(
            "--validate-only", "--profiles-file", str(cfg))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "status": "validated", "tasks": ["t1"], "max_concurrency": 1})
        self.assertFalse(self.out.exists())
        self.assertFalse((self.base / "records").exists())

    def test_task_group_id_forwarded_to_child_and_summary(self):
        # A manifest group label is forwarded to the child argv (identically in
        # dry validation and spawn: one argv builder) and surfaced in the
        # summary entry only because it was supplied.
        self.write_manifest([self.task("t1", extra={"task_group_id": "Grp_01-a_B"})])
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.argv_for("t1")
        self.assertIn("--task-group-id", argv)
        self.assertEqual(argv[argv.index("--task-group-id") + 1], "Grp_01-a_B")
        entry = self.summary()["tasks"][0]
        self.assertEqual(entry["task_group_id"], "Grp_01-a_B")
        self.assertEqual(entry["status"], "completed")

    def test_task_group_id_absent_keeps_legacy_shape(self):
        # Without the manifest field no flag reaches the child and the summary
        # entry keeps the historical keys; no group is inferred from the
        # delegation id or any other string.
        self.write_manifest([self.task("t1")])
        result = self.run_batch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--task-group-id", self.argv_for("t1"))
        entry = self.summary()["tasks"][0]
        self.assertEqual(set(entry), {"id", "delegation_id", "status", "run_id",
                                      "review_status"})

    def test_task_group_id_validation_and_forwarding_agree(self):
        # Dry validation accepts exactly what spawn forwards: the same manifest
        # passes --validate-only and then runs.
        self.write_manifest([self.task("t1", extra={"task_group_id": "g-2"})])
        dry = self.run_batch("--validate-only")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(self.out.exists())
        live = self.run_batch(out=self.base / "out-live")
        self.assertEqual(live.returncode, 0, live.stderr)

    def test_invalid_task_group_id_rejected_before_output(self):
        self.write_manifest([self.task("t1", extra={"task_group_id": "bad group!"})])
        result = self.run_batch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("task_group_id", result.stderr)
        self.assertFalse(self.out.exists())
        # Bounded shape enforced: 64 chars ok, 65 rejected, empty rejected.
        ok_manifest = [self.task("t64", extra={"task_group_id": "a" * 64})]
        self.write_manifest(ok_manifest)
        self.assertEqual(self.run_batch("--validate-only").returncode, 0)
        for bad in ("a" * 65, "", "-lead", "_lead"):
            self.write_manifest([self.task("tbad", extra={"task_group_id": bad})])
            bad_result = self.run_batch()
            self.assertNotEqual(bad_result.returncode, 0, repr(bad))
            self.assertFalse(self.out.exists(), repr(bad))

    def _kill_if_alive(self, proc):
        if proc.poll() is None:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
