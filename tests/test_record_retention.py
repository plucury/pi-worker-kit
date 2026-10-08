"""Retention tests: 14-local-calendar-day window for worker run records.

Policy under test (scripts/run_pi_worker.py):
- Keep today plus the previous 13 local calendar days (14 days total).
- Eligible for deletion: regular non-symlink direct child files named exactly
  ``YYYY-MM-DD.jsonl`` whose encoded date is < today - 13 days (age >= 14).
- mtime is never consulted; malformed names, other extensions, future dates,
  state/lock files, ``.checkpoints``, directories and symlinks are untouched.
- Dry run mutates nothing (including ``.append.lock``); a missing directory
  returns an empty result without creation; actual pruning holds the same
  exclusive ``.append.lock`` flock as ``append_record``.
Only temp directories are used; the default home ledgers are never touched.
"""

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_pi_worker.py"

import importlib.util
spec = importlib.util.spec_from_file_location("runner", RUNNER)
runner = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(runner)

FROZEN_TODAY = dt.date(2026, 10, 7)
LAST_KEPT = FROZEN_TODAY - dt.timedelta(days=13)   # 2026-09-24: retained
FIRST_EXPIRED = FROZEN_TODAY - dt.timedelta(days=14)  # 2026-09-23: deleted


def _child_prune(directory: str, today: dt.date) -> None:
    result = runner.prune_record_files(Path(directory), today=today)
    assert result["removed"] == [f"{today - dt.timedelta(days=30)}.jsonl"], result


class RecordRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.records = self.base / "records"
        self.records.mkdir()

    def seed(self, name: str, content: str = "{}\n") -> Path:
        path = self.records / name
        path.write_text(content)
        return path

    def test_boundary_retains_14_days_and_deletes_15(self):
        self.seed(f"{FROZEN_TODAY}.jsonl")
        self.seed(f"{LAST_KEPT}.jsonl")
        expired = self.seed(f"{FIRST_EXPIRED}.jsonl")
        result = runner.prune_record_files(self.records, today=FROZEN_TODAY)
        self.assertEqual(result, {"removed": [expired.name],
                                  "eligible": [expired.name],
                                  "failed_count": 0})
        self.assertFalse(expired.exists())
        self.assertTrue((self.records / f"{FROZEN_TODAY}.jsonl").exists())
        self.assertTrue((self.records / f"{LAST_KEPT}.jsonl").exists())

    def test_mtime_is_never_used(self):
        fresh_mtime_old_date = self.seed(f"{FIRST_EXPIRED}.jsonl")  # mtime = now
        old_mtime_recent = self.seed(f"{LAST_KEPT}.jsonl")
        os.utime(old_mtime_recent, (946684800.0, 946684800.0))  # year 2000
        result = runner.prune_record_files(self.records, today=FROZEN_TODAY)
        self.assertEqual(result["removed"], [fresh_mtime_old_date.name])
        self.assertFalse(fresh_mtime_old_date.exists())
        self.assertTrue(old_mtime_recent.exists())  # retained despite old mtime

    def test_state_files_dirs_symlinks_and_other_names_untouched(self):
        outside = self.base / "outside.jsonl"
        outside.write_text("keep\n")
        kept = self.seed(f"{LAST_KEPT}.jsonl")
        expired = self.seed(f"{FIRST_EXPIRED}.jsonl")
        symlink_like_expired = self.records / f"{FIRST_EXPIRED - dt.timedelta(days=1)}.jsonl"
        os.symlink(outside, symlink_like_expired)  # symlink named like a date file
        (self.records / ".routing-state.json").write_text("{}")
        (self.records / "profile-state.json").write_text("{}")
        (self.records / ".append.lock").write_text("")
        checkpoints = self.records / ".checkpoints"
        checkpoints.mkdir()
        (checkpoints / "c.json").write_text("{}")
        (self.records / f"{FIRST_EXPIRED}.jsonl.bak").write_text("{}")
        (self.records / "2026-13-45.jsonl").write_text("{}")   # invalid month/day
        (self.records / "2026-1-01.jsonl").write_text("{}")    # not zero padded
        self.seed("notes.txt")
        as_dir = self.records / f"{FIRST_EXPIRED - dt.timedelta(days=2)}.jsonl"
        as_dir.mkdir()  # directory with an eligible-looking name

        result = runner.prune_record_files(self.records, today=FROZEN_TODAY)

        self.assertEqual(result["removed"], [expired.name])
        self.assertEqual(result["failed_count"], 0)
        self.assertTrue(symlink_like_expired.is_symlink())
        self.assertEqual(outside.read_text(), "keep\n")
        self.assertTrue(kept.exists())
        for name in (".routing-state.json", "profile-state.json", ".append.lock",
                     f"{FIRST_EXPIRED}.jsonl.bak", "2026-13-45.jsonl",
                     "2026-1-01.jsonl", "notes.txt"):
            self.assertTrue((self.records / name).exists(), name)
        self.assertTrue((checkpoints / "c.json").exists())
        self.assertTrue(as_dir.is_dir())

    def test_future_dated_files_are_retained(self):
        future = self.seed("2027-01-01.jsonl")
        result = runner.prune_record_files(self.records, today=FROZEN_TODAY)
        self.assertEqual(result, {"removed": [], "eligible": [], "failed_count": 0})
        self.assertTrue(future.exists())

    def test_dry_run_has_no_side_effects_including_no_lock(self):
        expired = self.seed(f"{FIRST_EXPIRED}.jsonl")
        kept = self.seed(f"{LAST_KEPT}.jsonl")
        before = sorted(p.name for p in self.records.iterdir())
        result = runner.prune_record_files(self.records, today=FROZEN_TODAY,
                                           dry_run=True)
        self.assertEqual(result, {"removed": [], "eligible": [expired.name],
                                  "failed_count": 0})
        self.assertEqual(sorted(p.name for p in self.records.iterdir()), before)
        self.assertTrue(expired.exists() and kept.exists())
        self.assertFalse((self.records / ".append.lock").exists())

    def test_missing_directory_returns_empty_without_creation(self):
        missing = self.base / "nope"
        for dry in (False, True):
            result = runner.prune_record_files(missing, today=FROZEN_TODAY,
                                               dry_run=dry)
            self.assertEqual(result, {"removed": [], "eligible": [],
                                      "failed_count": 0})
        self.assertFalse(missing.exists())

    def test_default_resolution_follows_active_record_dir_override(self):
        # No directory argument: the active resolver (PI_WORKER_RECORD_DIR) is
        # used, never a hardcoded home ledger.
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}):
            expired = self.seed(f"{FIRST_EXPIRED}.jsonl")
            result = runner.prune_record_files(today=FROZEN_TODAY)
        self.assertEqual(result["removed"], [expired.name])
        self.assertFalse(expired.exists())

    def test_default_today_comes_from_local_day_source(self):
        expired = self.seed(f"{FIRST_EXPIRED}.jsonl")
        with mock.patch.object(runner, "local_day", return_value="2026-10-07"):
            result = runner.prune_record_files(self.records)
        self.assertEqual(result["removed"], [expired.name])

    def test_append_automatically_prunes_expired_files(self):
        expired = self.seed("2026-09-01.jsonl")
        state = self.seed(".routing-state.json", '{"d": {"profile": "x"}}')
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
                mock.patch.object(runner, "local_day", return_value="2026-10-07"):
            path = runner.append_record({"n": 1})
        self.assertFalse(expired.exists())
        self.assertTrue(state.exists())
        self.assertEqual(path.name, "2026-10-07.jsonl")
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual(lines, [{"n": 1}])

    def test_backdated_append_is_protected_until_next_write(self):
        # A manual backdated append must never delete the file it just
        # returned, even when its date is already past the window; the next
        # real write re-evaluates and removes it.
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
                mock.patch.object(runner, "local_day", return_value="2026-10-07"):
            backdated = runner.append_record({"n": 1}, day="2026-09-01")
            self.assertTrue(backdated.exists())
            self.assertEqual(
                [json.loads(x) for x in backdated.read_text().splitlines()],
                [{"n": 1}])
            runner.append_record({"n": 2})
        self.assertFalse(backdated.exists())
        self.assertTrue((self.records / "2026-10-07.jsonl").exists())

    def test_prune_failure_does_not_mask_a_valid_append(self):
        self.seed("2026-09-01.jsonl")
        with mock.patch.dict(os.environ,
                             {"PI_WORKER_RECORD_DIR": str(self.records)}), \
                mock.patch.object(runner, "local_day", return_value="2026-10-07"), \
                mock.patch.object(runner, "_retention_scan",
                                  side_effect=RuntimeError("boom")):
            path = runner.append_record({"n": 1})
        self.assertEqual(
            [json.loads(x) for x in path.read_text().splitlines()], [{"n": 1}])

    @unittest.skipUnless(hasattr(os, "fork") and os.geteuid() != 0,
                         "needs unlink permissions to bite")
    def test_unlink_permission_failure_is_counted_not_fabricated(self):
        self.seed(f"{FIRST_EXPIRED}.jsonl")
        lock = self.seed(".append.lock", "")
        self.records.chmod(0o500)  # unlink needs write on the directory
        self.addCleanup(self.records.chmod, 0o700)
        result = runner.prune_record_files(self.records, today=FROZEN_TODAY)
        self.records.chmod(0o700)
        self.assertEqual(result["eligible"], [f"{FIRST_EXPIRED}.jsonl"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["failed_count"], 1)
        self.assertTrue((self.records / f"{FIRST_EXPIRED}.jsonl").exists())
        self.assertTrue(lock.exists())

    def test_configurable_retention_period_and_disabled_zero(self):
        # The retention period is host configuration: an explicit
        # ``retention_days`` override widens/narrows the window, and ``0``
        # explicitly disables cleanup (nothing eligible, nothing deleted, even
        # for a dry run).
        day = FROZEN_TODAY - dt.timedelta(days=21)
        self.seed(f"{day.isoformat()}.jsonl")
        self.assertEqual(
            runner.prune_record_files(self.records, today=FROZEN_TODAY,
                                      retention_days=21)["removed"],
            [f"{day.isoformat()}.jsonl"])
        self.seed(f"{day.isoformat()}.jsonl")
        self.assertEqual(
            runner.prune_record_files(self.records, today=FROZEN_TODAY,
                                      retention_days=30)["eligible"], [])
        self.seed(f"{day.isoformat()}.jsonl")
        for dry in (False, True):
            self.assertEqual(
                runner.prune_record_files(self.records, today=FROZEN_TODAY,
                                          dry_run=dry, retention_days=0),
                {"removed": [], "eligible": [], "failed_count": 0})
            self.assertTrue((self.records / f"{day.isoformat()}.jsonl").exists())

    def test_result_shape_is_compact_filenames_only(self):
        self.seed(f"{FIRST_EXPIRED}.jsonl")
        result = runner.prune_record_files(self.records, today=FROZEN_TODAY)
        self.assertEqual(set(result), {"removed", "eligible", "failed_count"})
        self.assertTrue(all(isinstance(n, str) for n in result["removed"]))
        self.assertTrue(all(isinstance(n, str) for n in result["eligible"]))
        self.assertIsInstance(result["failed_count"], int)

    def test_prune_serializes_with_append_on_the_append_lock(self):
        # A prune must block on the exclusive .append.lock while another
        # process holds it, so a concurrent append is never deleted mid-write.
        expired = self.seed(f"{FROZEN_TODAY - dt.timedelta(days=30)}.jsonl")
        lock = self.seed(".append.lock", "")
        with open(lock, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            import multiprocessing
            context = multiprocessing.get_context("fork")
            child = context.Process(target=_child_prune,
                                    args=(str(self.records), FROZEN_TODAY))
            child.start()
            time.sleep(0.5)
            self.assertIsNone(child.exitcode)  # still blocked on the flock
            self.assertTrue(expired.exists())  # nothing deleted while locked
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        child.join(timeout=10)
        self.assertEqual(child.exitcode, 0)
        self.assertFalse(expired.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
