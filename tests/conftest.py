"""Test-session configuration for the pi-worker package.

Every test session explicitly injects the committed FAKE profiles fixture
(``tests/fixtures/profiles.json``) via ``PI_WORKER_PROFILES_FILE`` so test
modules and runner subprocesses resolve the configured profile table without
touching the real private ``~/.config/pi-worker/profiles.json``, global Pi
settings, auth, or any host-local configuration. The fixture is a test-only
stand-in and is never a production fallback: with the variable unset (fresh
home) the runner ships no built-in profiles at all.
"""

from pathlib import Path
import os

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "profiles.json"
os.environ["PI_WORKER_PROFILES_FILE"] = str(FIXTURE)
