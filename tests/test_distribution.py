"""Distribution/packaging checks for the pi-worker-skill Pi package.

These tests are intentionally static and offline. They:

* validate the explicit ``package.json`` manifest (identity, ``private``,
  license, the ``pi-package`` keyword, the gated resource manifest -- gate
  extension declared, root skills list empty so no skill auto-loads -- and the
  file whitelist);
* assert the manifest declares no default model, provider, profile, or auth
  keys (a generic "clean models" manifest check), that the host SDK is a
  type-only peer dependency (never a runtime copy), and that the test tooling
  dependencies are development-only;
* ask npm (when available) to list the files it would publish, both via
  ``npm pack --dry-run --json`` and a real ``npm pack`` into a temp directory
  outside the repo, and assert the tarball contains only whitelisted files and
  none of the sensitive/private paths (tests, fixtures, CI, caches, private
  config, credentials);
* run a conservative, heuristic static scan of the packaged text for absolute
  user-home paths and obvious live-credential shapes.

The credential scan is a best-effort heuristic over packaged text, not a proof
that the package contains no secrets. It deliberately ignores the synthetic
``SECRET-*`` sentinel constants used by the test suite (which are not packaged).
No provider/API call, network access, or private local configuration is used.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "package.json"
GITIGNORE = ROOT / ".gitignore"
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"

PACKAGE_NAME = "pi-worker-skill"
PACKAGE_VERSION = "0.1.0"

EXPECTED_FILES = [
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SKILL.md",
    "extensions/*.ts",
    "scripts/*.py",
    "references/*.md",
    "agents/*.yaml",
    "examples/profiles.example.json",
]

# Exact package-relative paths npm may ship. package.json is always included by
# npm; the docs are auto-included when present.
ALLOWED_EXACT = {
    "package.json",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SKILL.md",
}

# Substrings that must never appear in a shipped path.
FORBIDDEN_PATH_SUBSTRINGS = (
    "tests/",
    "fixtures/",
    "conftest.py",
    ".github/",
    "__pycache__",
    ".pyc",
    ".pyo",
    "profiles.json",
    "profiles.local.json",
    "settings.local.json",
    "auth",
    "history",
    "cache",
    "auth.json",
    "models.json",
    "settings.json",
    ".env",
    ".tgz",
    "node_modules/",
    "package-lock.json",
)

FORBIDDEN_MANIFEST_KEYS = {
    "defaultModel",
    "defaultModelId",
    "model",
    "models",
    "provider",
    "providers",
    "defaultProfile",
    "profiles",
    "apiKey",
    "apiKeys",
    "credentials",
}

SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk_live_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)

ABSOLUTE_USER_PATH_RE = re.compile(r"/Users/[^\s)\"'`]+")


def _npm_available() -> bool:
    return shutil.which("npm") is not None


def _allowed_pack_path(path: str) -> bool:
    if path in ALLOWED_EXACT:
        return True
    if path == "examples/profiles.example.json":
        return True
    parts = path.split("/")
    if len(parts) == 2:
        parent, name = parts
        if parent == "extensions" and name.endswith(".ts"):
            return True
        if parent == "scripts" and name.endswith(".py"):
            return True
        if parent == "references" and name.endswith(".md"):
            return True
        if parent == "agents" and (name.endswith(".yaml") or name.endswith(".yml")):
            return True
    return False


def _packaged_text_files() -> list[Path]:
    """Existing, text-like files that npm would ship, for static scanning."""
    candidates: list[Path] = []
    for entry in ALLOWED_EXACT:
        candidates.append(ROOT / entry)
    for pattern in (
        "scripts/*.py",
        "references/*.md",
        "agents/*.yaml",
        "extensions/*.ts",
    ):
        candidates.extend(sorted(ROOT.glob(pattern)))
    candidates.append(ROOT / "examples" / "profiles.example.json")
    return [p for p in candidates if p.is_file()]


class ManifestTests(unittest.TestCase):
    def test_manifest_parses_and_identity(self):
        self.assertTrue(MANIFEST.is_file(), "package.json is required")
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], PACKAGE_NAME)
        self.assertEqual(data["version"], PACKAGE_VERSION)
        self.assertIs(data.get("private"), True, "private:true blocks npm publish")
        self.assertEqual(data.get("license"), "MIT")
        self.assertIn("pi-package", data.get("keywords", []))

    def test_gated_manifest_prevents_unconditional_skill_auto_load(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        pi = data.get("pi", {})
        # The gate extension is the only declared extension; the root skill is
        # exposed by that extension only for exact enabled main-role pairs.
        self.assertEqual(
            pi.get("extensions"), ["extensions/profile-gated-skills.ts"]
        )
        # An empty skills list stops package skill auto-discovery. Declaring
        # the root skill here (e.g. ["."]) would bypass the gate entirely.
        self.assertEqual(pi.get("skills"), [])
        self.assertNotEqual(pi.get("skills"), ["."])

    def test_manifest_file_whitelist_is_explicit(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data.get("files"), EXPECTED_FILES)

    def test_manifest_has_no_default_model_or_provider_keys(self):
        text = MANIFEST.read_text(encoding="utf-8")
        data = json.loads(text)
        found: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in FORBIDDEN_MANIFEST_KEYS:
                        found.add(key)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        self.assertEqual(found, set(), f"unexpected model/provider keys: {found}")
        for token in ("defaultModel", "defaultProfile", "provider", '"model"', "apiKey"):
            self.assertNotIn(token, text, f"manifest text must not contain {token!r}")

    def test_manifest_host_sdk_peer_and_dev_dependencies(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        # No runtime dependency copies: the host SDK is a type-only import and
        # must be declared as a host-provided peer with a "*" range (see the
        # installed Pi packages docs), never bundled in dependencies.
        self.assertNotIn("dependencies", data)
        self.assertNotIn("optionalDependencies", data)
        self.assertEqual(
            data.get("peerDependencies"), {"@earendil-works/pi-coding-agent": "*"}
        )
        # Test tooling only; jiti loads the gate TypeScript in node:test.
        self.assertEqual(data.get("devDependencies"), {"jiti": "2.7.0"})
        self.assertEqual(
            data.get("scripts", {}).get("test"), "node --test tests/test_gate.mjs"
        )


class ToolingConfigTests(unittest.TestCase):
    def test_pyproject_is_tooling_only(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        # Ignore comments so prose about what we omit is not mistaken for config.
        config = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("[project]", config, "not a buildable Python distribution")
        self.assertIn("[tool.pytest.ini_options]", text)
        self.assertIn('testpaths = ["tests"]', text)
        self.assertIn('target-version = "py311"', text)
        self.assertIn('select = ["E9", "F63", "F7", "F82"]', text)
        # A pytest import-mode override would change existing test behavior.
        self.assertNotIn("importmode", text.replace("_", "").replace("-", "").lower())

    def test_requirements_dev_pins(self):
        text = REQUIREMENTS_DEV.read_text(encoding="utf-8")
        self.assertIn("pytest>=9,<10", text)
        self.assertIn("ruff>=0.14,<1", text)
        # pytest>=9 ships subtests in core; no separate plugin pin needed.
        pins = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("pytest-subtests", pins)


class LockfileTests(unittest.TestCase):
    """The committed package-lock.json pins dev tooling only, offline-safe.

    The lockfile exists so CI/dev installs are reproducible (frozen `npm ci`)
    while the host SDK peer stays host-supplied. It must never become part of
    the npm release (the pack whitelist above keeps it out) and must never
    encode auth, personal, or local-file URLs.
    """

    LOCK = ROOT / "package-lock.json"

    def _data(self) -> dict:
        self.assertTrue(
            self.LOCK.is_file(), "package-lock.json must be committed"
        )
        return json.loads(self.LOCK.read_text(encoding="utf-8"))

    def test_lockfile_v3_and_matching_identity(self):
        data = self._data()
        self.assertEqual(data.get("lockfileVersion"), 3)
        self.assertEqual(data.get("name"), PACKAGE_NAME)
        self.assertEqual(data.get("version"), PACKAGE_VERSION)
        self.assertTrue(data.get("requires", False), "lockfile must pin requires")

    def test_root_package_declares_dev_and_peer_only(self):
        data = self._data()
        root = data.get("packages", {}).get("")
        self.assertIsInstance(root, dict, "lockfile needs a root package entry")
        self.assertEqual(root.get("name"), PACKAGE_NAME)
        self.assertEqual(root.get("version"), PACKAGE_VERSION)
        self.assertNotIn("dependencies", root, "no runtime deps in lock root")
        self.assertNotIn("optionalDependencies", root)
        self.assertEqual(root.get("devDependencies"), {"jiti": "2.7.0"})
        self.assertEqual(
            root.get("peerDependencies"),
            {"@earendil-works/pi-coding-agent": "*"},
        )

    def test_jiti_pin_public_registry_url_and_integrity(self):
        data = self._data()
        packages = data.get("packages", {})
        self.assertEqual(
            sorted(packages), ["", "node_modules/jiti"],
            "lockfile must pin only the jiti dev tool, no SDK tree",
        )
        entry = packages["node_modules/jiti"]
        self.assertEqual(entry.get("version"), "2.7.0")
        self.assertIs(entry.get("dev"), True, "jiti must be marked dev-only")
        self.assertEqual(
            entry.get("resolved"),
            "https://registry.npmjs.org/jiti/-/jiti-2.7.0.tgz",
        )
        integrity = entry.get("integrity", "")
        self.assertTrue(
            integrity.startswith("sha512-"), "jiti entry needs sha512 integrity"
        )

    def test_host_sdk_node_modules_entry_absent(self):
        data = self._data()
        packages = data.get("packages", {})
        for key in packages:
            self.assertFalse(
                "pi-coding-agent" in key,
                f"host SDK must stay host-supplied, found lock entry: {key}",
            )
        self.assertNotIn(
            "node_modules/@earendil-works/pi-coding-agent",
            packages,
            "host SDK must not be resolved into the lockfile",
        )

    def test_no_auth_personal_or_file_urls(self):
        text = self.LOCK.read_text(encoding="utf-8")
        for bad in ("_authToken", "//registry:", "file:", "file://", "/Users/", "link:"):
            self.assertNotIn(bad, text, f"lockfile must not contain {bad!r}")
        for match in re.finditer(r'"resolved"\s*:\s*"([^"]+)"', text):
            url = match.group(1)
            self.assertTrue(
                url.startswith("https://registry.npmjs.org/"),
                f"non-public registry URL in lockfile: {url}",
            )

    def test_ci_uses_matching_frozen_npm_ci_command(self):
        ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        expected = (
            "npm ci --legacy-peer-deps --ignore-scripts --no-audit --no-fund"
        )
        self.assertIn(expected, ci_text, "CI must use the frozen npm ci command")
        self.assertIsNone(
            re.search(r"npm\s+install\b", ci_text),
            "CI must not fall back to npm install",
        )
        # Same command must be documented in CONTRIBUTING.md.
        contrib = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn(expected, contrib)


class GitignoreTests(unittest.TestCase):
    def test_gitignore_covers_required_private_and_cache_paths(self):
        lines = {
            line.strip()
            for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        required = {
            "__pycache__/",
            "*.py[cod]",
            ".pytest_cache/",
            ".ruff_cache/",
            ".venv/",
            "markers/",
            "worker-run-records/",
            "auth.json",
            "models.json",
            "settings.local.json",
            "profiles.local.json",
            "profiles.json",
            ".env",
            "*.tgz",
            "node_modules/",
            "build/",
            "dist/",
            "coverage/",
            "temp/",
        }
        missing = required - lines
        self.assertEqual(missing, set(), f"missing ignore rules: {missing}")

    def test_gitignore_does_not_blanket_ignore_all_json(self):
        for raw in GITIGNORE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            self.assertNotEqual(line, "*.json", "blanket *.json would hide fixtures")
            self.assertFalse(
                line.startswith("*.json"), f"blanket json ignore: {line!r}"
            )

    def test_gitignore_reincludes_tracked_fixture_and_example(self):
        lines = [
            line.strip()
            for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertIn("!tests/fixtures/profiles.json", lines)
        self.assertIn("!examples/profiles.example.json", lines)


@unittest.skipUnless(_npm_available(), "npm not available; CI installs Node")
class NpmPackTests(unittest.TestCase):
    def _run_npm(self, args, cwd=ROOT):
        return subprocess.run(
            ["npm", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_dry_run_pack_lists_only_whitelisted_paths(self):
        proc = self._run_npm(["pack", "--dry-run", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        paths = [entry["path"] for entry in data[0]["files"]]
        self.assertIn("package.json", paths)
        self.assertIn("SKILL.md", paths)
        self.assertIn("scripts/run_pi_worker.py", paths)
        self.assertIn("extensions/profile-gated-skills.ts", paths)
        for path in paths:
            self.assertTrue(
                _allowed_pack_path(path), f"unexpected packed path: {path!r}"
            )
            for bad in FORBIDDEN_PATH_SUBSTRINGS:
                self.assertNotIn(bad, path, f"sensitive path packed: {path!r}")

    def test_actual_tarball_contains_only_whitelisted_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_npm(["pack", "--json", "--pack-destination", tmp])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            tarballs = sorted(Path(tmp).glob("*.tgz"))
            self.assertEqual(len(tarballs), 1, "expected exactly one packed tarball")
            with tarfile.open(tarballs[0]) as archive:
                names = archive.getnames()
            self.assertTrue(names, "tarball must not be empty")
            packed = {
                name.split("/", 1)[1] if name.startswith("package/") else name
                for name in names
            }
            self.assertIn("extensions/profile-gated-skills.ts", packed)
            for name in names:
                path = name.split("/", 1)[1] if name.startswith("package/") else name
                self.assertTrue(
                    _allowed_pack_path(path), f"unexpected tarball member: {name!r}"
                )
                for bad in FORBIDDEN_PATH_SUBSTRINGS:
                    self.assertNotIn(bad, path, f"sensitive member packed: {name!r}")


class PackagedContentScanTests(unittest.TestCase):
    def test_no_absolute_user_home_paths_in_packaged_text(self):
        offenders: list[str] = []
        for path in _packaged_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in ABSOLUTE_USER_PATH_RE.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        self.assertEqual(offenders, [], f"absolute user paths: {offenders}")

    def test_no_obvious_live_credentials_in_packaged_text(self):
        offenders: list[str] = []
        for path in _packaged_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "SECRET-" in line:
                    continue  # synthetic test sentinel, not a real credential
                for regex in SECRET_RES:
                    if regex.search(line):
                        offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
                        break
        self.assertEqual(offenders, [], f"possible credential shapes: {offenders}")


if __name__ == "__main__":
    unittest.main()
