"""Unified-role parsing tests: legacy roles defaults, duplicate precedence,
derived-id stability, and role mapping.

Pure library tests: documents are parsed in memory via the existing
``parse_profile_document``/``auto_profile_id`` entry points plus one fake
env load. HOME and the shared fixture are never read, no auth keys exist or
are synthesized, and no other module's global profile table is touched.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "roles_config_test", SCRIPTS / "profile_config.py")
pc = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(pc)

_OMIT = object()


def entry(provider="prov", model="mod", enabled=True, roles=("sub",),
          pid=_OMIT):
    item = {"provider": provider, "model": model, "enabled": enabled}
    if roles is not _OMIT:
        item["roles"] = list(roles) if isinstance(roles, tuple) else roles
    if pid is not _OMIT:
        item["id"] = pid
    return item


def parse(document):
    return pc.parse_profile_document(document)


def ids(selection):
    """Ordered id list from a ``profiles_for_role`` result (dict or list)."""
    return list(selection)


class LegacyRolesTest(unittest.TestCase):
    def test_legacy_omitted_roles_default_to_sub(self):
        config = parse({"version": 1, "profiles": {
            "w": entry(roles=_OMIT)}})
        self.assertEqual(config.profiles["w"]["roles"], ["sub"])
        self.assertEqual(ids(config.profiles_for_role("sub")), ["w"])
        self.assertEqual(ids(config.profiles_for_role("main")), [])

    def test_legacy_explicit_roles_preserved(self):
        config = parse({"version": 1, "profiles": {
            "a": entry(model="m-one", roles=()),
            "b": entry(model="m-two", roles=("main",), enabled=False)}})
        self.assertEqual(config.profiles["a"]["roles"], [])
        self.assertEqual(ids(config.profiles_for_role("sub")), [])
        self.assertEqual(ids(config.profiles_for_role("main")), ["b"])

    def test_legacy_null_roles_invalid(self):
        with self.assertRaisesRegex(pc.ProfileConfigError,
                                    "roles must be a JSON array"):
            parse({"version": 1, "profiles": {"w": entry(roles=None)}})

    def test_canonical_array_missing_roles_rejected(self):
        with self.assertRaisesRegex(pc.ProfileConfigError, "roles"):
            parse({"profiles": [entry(roles=_OMIT)]})


class DuplicateInstallTest(unittest.TestCase):
    def test_duplicate_pair_reported_before_generated_id(self):
        doc = {"profiles": [
            entry(provider="hid-den", roles=("main",)),
            entry(provider="hid-den", enabled=False, roles=("sub",))]}
        with self.assertRaises(pc.ProfileConfigError) as ctx:
            parse(doc)
        message = str(ctx.exception)
        self.assertIn("duplicate", message)
        self.assertIn("merge roles", message)
        self.assertNotIn("hash collision", message)
        self.assertNotIn("hid-den", message)  # raw pair values never echoed

    def test_duplicate_pair_wins_over_same_explicit_id(self):
        doc = {"profiles": [entry(pid="dup", roles=("main",)),
                            entry(pid="dup", enabled=False, roles=("sub",))]}
        with self.assertRaisesRegex(pc.ProfileConfigError,
                                    "duplicate provider/model pair"):
            parse(doc)

    def test_duplicate_id_for_distinct_pairs_rejected(self):
        doc = {"profiles": [
            entry(provider="p1", pid="dup"),
            entry(provider="p2", pid="dup")]}
        with self.assertRaisesRegex(pc.ProfileConfigError, "duplicate id"):
            parse(doc)

    def test_real_derived_hash_collision_rejected(self):
        doc = {"profiles": [
            entry(provider="p1", roles=("main",)),
            entry(provider="p2", roles=("sub",))]}
        with mock.patch.object(pc, "auto_profile_id",
                               return_value="profile-clash"):
            with self.assertRaisesRegex(
                    pc.ProfileConfigError,
                    "derived-id hash collision"):
                parse(doc)


class DerivedIdStabilityTest(unittest.TestCase):
    def test_generated_ids_stable_across_order_enabled_and_roles(self):
        base = parse({"profiles": [
            entry(provider="pa", model="ma", roles=("main",)),
            entry(provider="pb", model="mb", enabled=False,
                  roles=("main", "sub"))]})
        other = parse({"profiles": [
            entry(provider="pb", model="mb", enabled=True, roles=("sub",)),
            entry(provider="pa", model="ma", enabled=False,
                  roles=("main", "sub"))]})
        self.assertEqual(set(base.profiles), set(other.profiles))
        for provider, model in (("pa", "ma"), ("pb", "mb")):
            expected = pc.auto_profile_id(provider, model)
            self.assertEqual(base.profiles[expected]["provider"], provider)
            self.assertEqual(other.profiles[expected]["model"], model)


class RoleMappingTest(unittest.TestCase):
    def setUp(self):
        self.config = parse({"profiles": [
            entry(provider="main-only", roles=("main",)),
            entry(provider="off-main", roles=("main",), enabled=False),
            entry(provider="dual", roles=("main", "sub"))]})
        # Instance-local config: no other module's globals are mutated.

    def test_main_mapping_excludes_main_only_from_sub(self):
        pool = self.config.profiles_for_role("sub")
        self.assertEqual(len(pool), 1)
        self.assertEqual(next(iter(pool)), pc.auto_profile_id("dual", "mod"))
        self.assertEqual(len(self.config.profiles_for_role("main")), 3)

    def test_enabled_only_excludes_disabled_and_dual_counts_once(self):
        main = self.config.profiles_for_role("main", enabled_only=True)
        sub = self.config.profiles_for_role("sub", enabled_only=True)
        self.assertEqual(len(main), 2)
        self.assertEqual(len(sub), 1)
        dual = pc.auto_profile_id("dual", "mod")
        self.assertEqual([k for k in main].count(dual), 1)
        self.assertEqual([k for k in sub].count(dual), 1)
        self.assertNotIn(pc.auto_profile_id("off-main", "mod"), main)

    def test_unknown_role_rejected(self):
        with self.assertRaises(pc.ProfileConfigError):
            self.config.profiles_for_role("worker")


class ValidationAndDefaultsTest(unittest.TestCase):
    def test_unknown_and_duplicate_roles_rejected(self):
        for bad in (["worker"], ["main", "main"]):
            with self.assertRaises(pc.ProfileConfigError):
                parse({"profiles": [entry(roles=bad)]})

    def test_empty_document_is_unconfigured(self):
        for document in ({}, []):
            config = parse(document)
            self.assertEqual(config.profiles, {})
            self.assertEqual(config.profiles_for_role("main"), {})

    def test_no_auth_apis_and_environment_inheritance_untouched(self):
        config = parse({"profiles": [entry()]})
        for name in ("api_key", "apiKey", "token", "credential",
                     "credential_shell_bridge", "shell", "auth"):
            self.assertFalse(hasattr(config, name), name)
        before = dict(os.environ)
        parse({"profiles": [entry()]})
        self.assertEqual(dict(os.environ), before)

    def test_fake_env_load_without_home_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent.json")
            env = {pc.PROFILES_FILE_ENV: missing}
            config = pc.load_profile_config(None, env=env)
            self.assertEqual(config.source, "env")
            self.assertEqual(config.profiles, {})


if __name__ == "__main__":
    unittest.main()
