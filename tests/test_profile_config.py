"""Profile configuration module tests: canonical array schema, roles, and
legacy version 1 dictionary support.

``scripts/profile_config.py`` ships no built-in provider, model, or profile: an
absent environment/default file is an empty configuration, an explicit file
must exist and strictly validate, and raw file contents are never echoed in
errors. Auth is solely Pi's default, so configuration carries no credential
field. Only the committed test fixture and temp files are used; the real
private ``~/.config/pi-worker/profiles.json`` is never read.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "profiles.json"

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "profile_config_test", SCRIPTS / "profile_config.py")
profile_config = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(profile_config)


def _write(document) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        if isinstance(document, str):
            fh.write(document)
        else:
            json.dump(document, fh)
        return fh.name


def _ids(selection):
    """Normalize a ``profiles_for_role`` result to an ordered id list.

    The frozen interface returns the role pool in table order; the exact
    container shape (dict subset, id list, or entry list) is accepted here so
    the assertions read the same pool either way.
    """
    if isinstance(selection, dict):
        return list(selection)
    out = []
    for item in selection:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(item.get("id"))
        else:  # (id, entry) pair
            out.append(item[0])
    return out


class ProfileConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(self, explicit, env=None):
        # Hermetic: the default home location is redirected into the temp dir
        # so the real private ~/.config file is never consulted.
        with tempfile.TemporaryDirectory() as empty_home, \
                mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(profile_config, "DEFAULT_PROFILES_FILE",
                                  Path(empty_home) / "none.json"):
            return profile_config.load_profile_config(explicit)

    def test_fixture_loads_canonical_array_with_role_pools(self):
        config = self.load(str(FIXTURE))
        self.assertEqual(list(config.profiles),
                         ["standard-glm", "standard-deepseek", "standard-glm-zai",
                          "standard-minimax", "standard-mimo", "test-main-only"])
        # Registered SUB pool: all five sub profiles, disabled included.
        self.assertEqual(_ids(config.profiles_for_role("sub")),
                         ["standard-glm", "standard-deepseek", "standard-glm-zai",
                          "standard-minimax", "standard-mimo"])
        # Active (enabled-only) SUB pool stays exactly four.
        self.assertEqual(_ids(config.profiles_for_role("sub", enabled_only=True)),
                         ["standard-glm", "standard-deepseek", "standard-minimax",
                          "standard-mimo"])
        # MAIN pool includes the both-role standard-glm and the main-only profile.
        self.assertEqual(_ids(config.profiles_for_role("main")),
                         ["standard-glm", "test-main-only"])
        self.assertEqual(_ids(config.profiles_for_role("main", enabled_only=True)),
                         ["standard-glm", "test-main-only"])
        glm = config.profiles["standard-glm"]
        self.assertEqual(sorted(glm["roles"]), ["main", "sub"])
        self.assertTrue(glm["enabled"])
        self.assertEqual(glm["routing_class"], "standard")
        self.assertNotIn("credential", glm)
        self.assertNotIn("credential", config.profiles["standard-glm-zai"])
        self.assertFalse(config.profiles["standard-glm-zai"]["enabled"])
        self.assertEqual(config.profiles["standard-glm-zai"]["roles"], ["sub"])
        self.assertEqual(config.profiles["test-main-only"]["roles"], ["main"])
        self.assertEqual(config.record_retention_days, 14)
        self.assertEqual(config.source, "explicit")
        self.assertFalse(hasattr(config, "credential_shell_bridge"))

    def test_absent_env_and_default_yields_empty_configuration(self):
        config = self.load(None)
        self.assertEqual(config.profiles, {})
        self.assertEqual(_ids(config.profiles_for_role("sub")), [])
        self.assertEqual(_ids(config.profiles_for_role("main")), [])
        self.assertEqual(config.record_retention_days, 14)
        self.assertEqual(config.source, "default")

    def test_explicit_precedence_over_env(self):
        other = _write({"version": 2, "profiles": []})
        self.addCleanup(os.unlink, other)
        config = self.load(other, {profile_config.PROFILES_FILE_ENV: str(FIXTURE)})
        self.assertEqual(config.profiles, {})
        self.assertEqual(config.source, "explicit")

    def test_env_precedence_over_default(self):
        config = self.load(None, {profile_config.PROFILES_FILE_ENV: str(FIXTURE)})
        self.assertIn("standard-mimo", config.profiles)
        self.assertEqual(config.source, "env")

    def test_missing_explicit_file_is_actionable(self):
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "not found"):
            self.load(str(Path(self.temp.name) / "absent.json"))
        # But an absent env/default file is just an empty configuration.
        self.assertEqual(self.load(None).profiles, {})

    def test_malformed_json_is_rejected_without_echoing_contents(self):
        path = _write("{not json at all secretvalue")
        self.addCleanup(os.unlink, path)
        with self.assertRaises(profile_config.ProfileConfigError) as ctx:
            self.load(path)
        self.assertNotIn("secretvalue", str(ctx.exception))

    def test_unknown_fields_rejected(self):
        path = _write({"version": 2, "profiles": [{
            "provider": "prov", "model": "mod", "enabled": True,
            "roles": ["sub"], "extra": 1}]})
        self.addCleanup(os.unlink, path)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "extra"):
            self.load(path)
        root = _write({"version": 2, "profiles": [], "bogus": True})
        self.addCleanup(os.unlink, root)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "bogus"):
            self.load(root)
        # The removed credential field is now an unknown profile field.
        credential = _write({"version": 2, "profiles": [{
            "provider": "prov", "model": "mod", "enabled": True,
            "roles": ["sub"], "credential": "SOME_KEY"}]})
        self.addCleanup(os.unlink, credential)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "credential"):
            self.load(credential)

    def test_secret_like_fields_are_never_accepted(self):
        for field in ("apiKey", "token", "commands", "credential"):
            document = {"version": 2, "profiles": [{
                "provider": "prov", "model": "mod", "enabled": True,
                "roles": ["sub"], field: "value"}]}
            path = _write(document)
            self.addCleanup(os.unlink, path)
            with self.assertRaisesRegex(profile_config.ProfileConfigError, field):
                self.load(path)

    def test_enabled_must_be_explicit_boolean(self):
        for bad in ("true", 1, None):
            document = {"version": 2, "profiles": [{
                "provider": "prov", "model": "mod", "enabled": bad,
                "roles": ["sub"]}]}
            path = _write(document)
            self.addCleanup(os.unlink, path)
            with self.assertRaisesRegex(profile_config.ProfileConfigError,
                                        "enabled must be an explicit boolean"):
                self.load(path)

    def test_profile_fields_and_ids_are_bounded(self):
        bad_id = _write({"version": 2, "profiles": [{
            "id": "bad id!", "provider": "p", "model": "m", "enabled": True,
            "roles": ["sub"]}]})
        self.addCleanup(os.unlink, bad_id)
        with self.assertRaisesRegex(profile_config.ProfileConfigError,
                                    "invalid profile id"):
            self.load(bad_id)
        long_id = _write({"version": 2, "profiles": [{
            "id": "a" * 65, "provider": "p", "model": "m", "enabled": True,
            "roles": ["sub"]}]})
        self.addCleanup(os.unlink, long_id)
        with self.assertRaises(profile_config.ProfileConfigError):
            self.load(long_id)
        empty_model = _write({"version": 2, "profiles": [{
            "provider": "prov", "model": " ", "enabled": True, "roles": ["sub"]}]})
        self.addCleanup(os.unlink, empty_model)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "model"):
            self.load(empty_model)

    def test_roles_are_required_unique_and_known(self):
        missing = _write({"version": 2, "profiles": [{
            "provider": "prov", "model": "mod", "enabled": True}]})
        self.addCleanup(os.unlink, missing)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "roles"):
            self.load(missing)
        for bad in ("sub", {"main": True}, ["worker"], ["main", "main"],
                    ["main", "sub", 1]):
            path = _write({"version": 2, "profiles": [{
                "provider": "prov", "model": "mod", "enabled": True,
                "roles": bad}]})
            self.addCleanup(os.unlink, path)
            with self.assertRaisesRegex(profile_config.ProfileConfigError, "roles"):
                self.load(path)
        # An explicit empty role list is valid but offers no pool.
        empty = _write({"version": 2, "profiles": [{
            "provider": "prov", "model": "mod", "enabled": True, "roles": []}]})
        self.addCleanup(os.unlink, empty)
        config = self.load(empty)
        self.assertEqual(len(config.profiles), 1)
        self.assertEqual(_ids(config.profiles_for_role("sub")), [])
        self.assertEqual(_ids(config.profiles_for_role("main")), [])

    def test_duplicate_profile_id_or_provider_model_pair_rejected(self):
        dup_id = _write({"version": 2, "profiles": [
            {"id": "dup", "provider": "p1", "model": "m1", "enabled": True,
             "roles": ["sub"]},
            {"id": "dup", "provider": "p2", "model": "m2", "enabled": True,
             "roles": ["sub"]}]})
        self.addCleanup(os.unlink, dup_id)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "duplicate"):
            self.load(dup_id)
        dup_pair = _write({"version": 2, "profiles": [
            {"provider": "p1", "model": "m1", "enabled": True, "roles": ["sub"]},
            {"provider": "p1", "model": "m1", "enabled": False, "roles": ["main"]}]})
        self.addCleanup(os.unlink, dup_pair)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "duplicate"):
            self.load(dup_pair)

    def test_omitted_ids_derive_from_provider_model_and_stay_independent(self):
        def derived(provider, model, enabled, roles):
            path = _write({"version": 2, "profiles": [{
                "provider": provider, "model": model, "enabled": enabled,
                "roles": roles}]})
            self.addCleanup(os.unlink, path)
            config = self.load(path)
            self.assertEqual(len(config.profiles), 1)
            return next(iter(config.profiles))

        first = derived("prov", "mod", True, ["sub"])
        second = derived("prov", "mod", False, ["main"])
        self.assertEqual(first, second)  # id is independent of enabled/roles/order
        self.assertNotEqual(first, derived("prov", "other", True, ["sub"]))
        explicit = _write({"version": 2, "profiles": [{
            "id": "kept-history", "provider": "prov", "model": "mod",
            "enabled": True, "roles": ["sub"]}]})
        self.addCleanup(os.unlink, explicit)
        self.assertIn("kept-history", self.load(explicit).profiles)

    def test_legacy_version1_dictionary_is_supported(self):
        path = _write({"version": 1, "profiles": {
            "legacy": {"provider": "legacy-prov", "model": "legacy-mod",
                       "enabled": True, "routing_class": "standard"}}})
        self.addCleanup(os.unlink, path)
        config = self.load(path)
        entry = config.profiles["legacy"]
        self.assertEqual(entry["roles"], ["sub"])  # legacy default role
        self.assertEqual(entry["provider"], "legacy-prov")
        self.assertEqual(entry["routing_class"], "standard")
        self.assertNotIn("credential", entry)
        self.assertEqual(_ids(config.profiles_for_role("sub")), ["legacy"])
        # Explicit legacy roles are honored; credential is no longer a field.
        roles = _write({"version": 1, "profiles": {
            "legacy": {"provider": "p", "model": "m", "enabled": True,
                       "roles": ["main"]}}})
        self.addCleanup(os.unlink, roles)
        self.assertEqual(self.load(roles).profiles["legacy"]["roles"], ["main"])
        credential = _write({"version": 1, "profiles": {
            "legacy": {"provider": "p", "model": "m", "enabled": True,
                       "credential": "SOME_KEY"}}})
        self.addCleanup(os.unlink, credential)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "credential"):
            self.load(credential)

    def test_dict_without_version_is_invalid(self):
        path = _write({"profiles": {"p": {
            "provider": "prov", "model": "mod", "enabled": True}}})
        self.addCleanup(os.unlink, path)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "version"):
            self.load(path)

    def test_canonical_version2_or_absent_list(self):
        for version in (2, None):
            document = {"profiles": [{
                "provider": "prov", "model": "mod", "enabled": True,
                "roles": ["sub"]}]}
            if version is not None:
                document["version"] = version
            path = _write(document)
            self.addCleanup(os.unlink, path)
            config = self.load(path)
            self.assertEqual(_ids(config.profiles_for_role("sub")),
                             list(config.profiles))
        # Mismatched shapes are rejected: version 1 must be a dictionary and
        # version 2 must be an array.
        v1_list = _write({"version": 1, "profiles": []})
        self.addCleanup(os.unlink, v1_list)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "version"):
            self.load(v1_list)
        v2_dict = _write({"version": 2, "profiles": {}})
        self.addCleanup(os.unlink, v2_dict)
        with self.assertRaisesRegex(profile_config.ProfileConfigError, "profiles"):
            self.load(v2_dict)

    def test_array_bool_float_and_unknown_versions_rejected(self):
        for bad in (True, False, 1.0, 0, 3, "2"):
            with self.subTest(version=bad):
                path = _write({"version": bad, "profiles": []})
                self.addCleanup(os.unlink, path)
                with self.assertRaisesRegex(profile_config.ProfileConfigError,
                                            "version"):
                    self.load(path)

    def test_settings_only_default_v2_and_empty_object(self):
        for document in ({}, {"record_retention_days": 0},
                         {"version": 2, "profiles": []}):
            config = profile_config.parse_profile_document(document)
            self.assertEqual(config.profiles, {})
        settings = profile_config.parse_profile_document(
            {"record_retention_days": 7})
        self.assertEqual(settings.record_retention_days, 7)
        # The ``{}`` shorthand is accepted through the file loader too.
        path = _write({})
        self.addCleanup(os.unlink, path)
        loaded = self.load(path)
        self.assertEqual(loaded.profiles, {})
        self.assertEqual(loaded.source, "explicit")

    def test_retention_validation_and_zero_disables(self):
        for bad in (-1, "14", 1.5, True):
            path = _write({"version": 2, "profiles": [],
                           "record_retention_days": bad})
            self.addCleanup(os.unlink, path)
            with self.assertRaisesRegex(profile_config.ProfileConfigError,
                                        "record_retention_days"):
                self.load(path)
        zero = _write({"version": 2, "profiles": [], "record_retention_days": 0})
        self.addCleanup(os.unlink, zero)
        self.assertEqual(self.load(zero).record_retention_days, 0)

    def test_root_auth_fields_are_rejected(self):
        for field in ("credential", "credential_shell_bridge", "_bridge_shell",
                      "CREDENTIAL_BRIDGE_TIMEOUT_SECONDS"):
            path = _write({"version": 2, "profiles": [], field: False})
            self.addCleanup(os.unlink, path)
            with self.assertRaisesRegex(profile_config.ProfileConfigError, field):
                self.load(path)


RUNNER = SCRIPTS / "run_pi_worker.py"


class InitialProfileLoadTests(unittest.TestCase):
    """The best-effort import-time load in run_pi_worker honors the USER

    default home configuration (it is not a built-in model default): a local
    file with ``record_retention_days`` 0 is picked up at import with
    PI_WORKER_PROFILES_FILE unset. Verified in a subprocess with a temp HOME
    and an explicit fake record dir; the real private ~/.config file is never
    touched and a read-only review append observes the same local values.
    """

    def test_import_time_load_honors_local_retention_zero(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as records:
            cfg_dir = Path(home) / ".config" / "pi-worker"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "profiles.json").write_text(json.dumps({
                "version": 2, "profiles": [], "record_retention_days": 0}))
            env = {k: v for k, v in os.environ.items()
                   if k != profile_config.PROFILES_FILE_ENV}
            env["HOME"] = home
            env["PI_WORKER_RECORD_DIR"] = str(records)
            code = (
                "import sys\n"
                f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
                "import run_pi_worker as mod\n"
                "assert mod.RECORD_RETENTION_DAYS == 0, mod.RECORD_RETENTION_DAYS\n"
                "assert mod.PROFILES == {}\n"
                "assert not hasattr(mod, 'CREDENTIAL_SHELL_BRIDGE')\n")
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            # The explicit fake record dir was never written by the import.
            self.assertEqual(list(Path(records).iterdir()), [])


class UnconfiguredCliGuardTests(unittest.TestCase):
    """A fresh ``{}`` profiles file is the documented unconfigured shorthand.

    It must load as an empty table (no schema error) and then let the strict
    ``--validate-only`` path refuse an auto launch with the informative
    ``no_enabled_profile`` failure before any provider preflight, Pi process,
    ledger record, routing state, or receipt write.
    """

    def test_validate_only_empty_document_refuses_before_side_effects(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            home = root / "home"
            home.mkdir()
            records = root / "records"
            work = root / "work"
            work.mkdir()
            fresh = root / "fresh.json"
            fresh.write_text("{}", encoding="utf-8")
            pi_argv = root / "pi-argv.json"
            receipt = root / "receipt.json"
            env = {k: v for k, v in os.environ.items()
                   if k != profile_config.PROFILES_FILE_ENV}
            env.update({
                "HOME": str(home),
                "PI_WORKER_RECORD_DIR": str(records),
                "PI_WORKER_PI": str(ROOT / "tests" / "fake_pi.py"),
                "FAKE_PI_ARGV_FILE": str(pi_argv),
            })
            cmd = [sys.executable, str(RUNNER), "--workdir", str(work),
                   "--contract", "Do it", "--profile", "auto",
                   "--routing-class", "standard", "--thinking", "medium",
                   "--tools", "write", "--delegation-id", "guard-fresh-empty",
                   "--profiles-file", str(fresh),
                   "--receipt-file", str(receipt), "--validate-only"]
            result = subprocess.run(cmd, env=env, text=True,
                                    capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("no enabled standard profile", result.stderr)
            # No provider check / Pi process ran: its argv counter is absent.
            self.assertFalse(pi_argv.exists())
            # No ledger record, routing/profile state, or receipt was written.
            self.assertEqual(list(records.glob("*.jsonl")), [])
            self.assertFalse((records / ".routing-state.json").exists())
            self.assertFalse((records / "profile-state.json").exists())
            self.assertFalse(receipt.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
