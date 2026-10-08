#!/usr/bin/env python3
"""Optional profile configuration for the Pi worker runner.

The runner ships with NO built-in provider, model, profile, or main-agent
model. Everything worker-related is host-controlled: a JSON configuration file
describes the profile table plus the ``record_retention_days`` setting. The
file is resolved with a fixed precedence and never discovered from the
repository or the project working directory:

1. explicit ``--profiles-file`` path (CLI flag),
2. ``PI_WORKER_PROFILES_FILE`` environment variable,
3. ``~/.config/pi-worker/profiles.json`` (the default host location).

An absent env/default file is not an error: the configuration is simply empty
(no profiles), so an auto launch fails with an informative ``no_enabled_profile``
error before any reservation, provider preflight, Pi process, or ledger write,
and there is no fallback provider anywhere. An explicit path that is missing,
unreadable, malformed, or invalid is an actionable configuration error raised
before any process side effect; raw file contents are never echoed in errors.

Canonical format (JSON root, schema version 2; ``version`` may be omitted for
an array or settings-only document)::

    {
      "version": 2,
      "profiles": [
        {
          "id": "<optional bounded ASCII id>",
          "provider": "<nonempty provider id>",
          "model": "<nonempty model id>",
          "enabled": true,
          "roles": ["main", "sub"]
        }
      ],
      "record_retention_days": 14
    }

``enabled`` must be a real boolean (required, no implicit default) and
``roles`` is a required list of unique ``main``/``sub`` members; an empty list
means unassigned/inert. A profile listed in both roles appears once in each
role filter. ``id`` is optional: without one, a stable bounded id is derived
deterministically from the canonical JSON of the provider/model pair (UTF-8),
independent of roles, ``enabled``, and array order. Duplicate provider/model
pairs and duplicate ids (including derived-id hash collisions) are rejected —
merge roles into one entry instead.

Legacy format (schema version 1; ``version`` is REQUIRED with a profiles
OBJECT keyed by profile id)::

    {
      "version": 1,
      "profiles": {
        "<profile-id>": {
          "provider": "<nonempty provider id>",
          "model": "<nonempty model id>",
          "enabled": true,
          "routing_class": "standard",
          "roles": ["sub"]
        }
      },
      "record_retention_days": 14
    }

In version 1, ``routing_class`` is optional (must be ``"standard"``) and
``roles`` is optional (defaults to ``["sub"]`` to preserve the old pure-worker
format). Validation is strict: unknown fields anywhere are rejected, root
fields are limited to ``profiles``/``version``/``record_retention_days``, and
credential/token/key/shell fields (``credential``, ``credential_shell_bridge``,
``apiKey``, ``token``, ...) are refused outright — configuration carries no
auth material of any kind; the runner inherits Pi's default auth storage and
environment unchanged. Secret values are never echoed in errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_PROFILES_FILE = Path.home() / ".config" / "pi-worker" / "profiles.json"
PROFILES_FILE_ENV = "PI_WORKER_PROFILES_FILE"

# Bounded ASCII profile ids: same shape as delegation/task ids elsewhere in the
# package, so historical worker labels stay lexically comparable.
PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ROUTING_CLASS = "standard"
ROLES = ("main", "sub")

_ROOT_FIELDS = frozenset({"version", "profiles", "record_retention_days"})
_CANONICAL_ENTRY_FIELDS = frozenset(
    {"id", "provider", "model", "enabled", "roles"})
_LEGACY_ENTRY_FIELDS = frozenset(
    {"provider", "model", "enabled", "roles", "routing_class"})
# Field names that must never appear anywhere: configuration carries ids and
# nonempty strings, never secrets, keys, tokens, credentials, or commands.
_FORBIDDEN_FIELDS = frozenset({
    "api_key", "apikey", "api-key", "apiKey", "token", "secret", "password",
    "commands", "command", "cmd", "script", "shell", "env", "env_values",
    "credential", "credential_env", "credential-env", "credentials",
    "credential_shell_bridge", "credential-shell-bridge", "credentialBridge",
    "shell_bridge"})


class ProfileConfigError(Exception):
    """Actionable configuration error. Never carries raw file contents."""


class ProfileConfig:
    """Validated configuration snapshot keyed by profile id.

    ``profiles`` is an internally ordered mapping
    ``id -> {provider, model, enabled, roles, routing_class}`` in document
    order. There are no credential attributes: auth is solely Pi's default.
    """

    __slots__ = ("profiles", "record_retention_days", "source", "path")

    def __init__(self, profiles: dict[str, dict[str, Any]], *,
                 record_retention_days: int = 14,
                 source: str = "absent", path: Path | None = None):
        self.profiles = profiles
        self.record_retention_days = record_retention_days
        self.source = source  # "explicit" | "env" | "default" | "absent"
        self.path = path

    def profiles_for_role(self, role: str, *,
                          enabled_only: bool = False) -> dict[str, dict[str, Any]]:
        """Ordered mapping of profiles assigned to ``role``.

        Disabled profiles are included by default so history, sticky pinning,
        and receipts keep resolving; pass ``enabled_only=True`` for selection
        pools. Unknown roles raise. A dual main/sub profile appears once in
        each role's result.
        """
        if role not in ROLES:
            raise ProfileConfigError(
                f"unknown role: {role!r} (expected one of {', '.join(ROLES)})")
        return {pid: entry for pid, entry in self.profiles.items()
                if role in entry["roles"]
                and (entry["enabled"] or not enabled_only)}


def _reject(**parts: str) -> ProfileConfigError:
    return ProfileConfigError("; ".join(f"{k}: {v}" for k, v in parts.items()
                                        if v))


def _refuse_forbidden(where: str, keys: Any) -> None:
    forbidden = sorted(set(keys) & _FORBIDDEN_FIELDS)
    if forbidden:
        raise ProfileConfigError(
            f"{where}: refused field(s) {', '.join(forbidden)}; "
            "configuration carries no credential/token/key/shell material "
            "(auth is solely Pi's default; values are never echoed)")


def _validate_roles(where: str, raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ProfileConfigError(f"{where}: roles must be a JSON array")
    roles: list[str] = []
    for member in raw:
        if not isinstance(member, str) or member not in ROLES:
            raise ProfileConfigError(
                f"{where}: roles members must be exactly "
                f"{' or '.join(repr(r) for r in ROLES)} (got {member!r})")
        if member in roles:
            raise ProfileConfigError(
                f"{where}: roles must be unique "
                f"(duplicate role {member!r})")
        roles.append(member)
    return roles


def _validate_core(where: str, raw: dict[str, Any], *, required: set[str]) -> tuple[str, str, bool, list[str]]:
    missing = sorted(required - set(raw))
    if missing:
        raise ProfileConfigError(f"{where}: missing field(s) "
                                 f"{', '.join(missing)}")
    provider, model = raw["provider"], raw["model"]
    for field, value in (("provider", provider), ("model", model)):
        if not isinstance(value, str) or not value.strip():
            raise ProfileConfigError(
                f"{where}: {field} must be a nonempty string")
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        # Never implicitly enabled: the field must be an explicit JSON
        # boolean (a "true"/"false" string or 1/0 is rejected).
        raise ProfileConfigError(f"{where}: enabled must be an explicit boolean")
    return provider, model, enabled, _validate_roles(where, raw["roles"])


def auto_profile_id(provider: str, model: str) -> str:
    """Stable bounded id derived from the canonical provider/model pair.

    Deterministic over the canonical JSON of the pair encoded as UTF-8:
    independent of roles, ``enabled``, and array/document ordering. Any two
    entries with the same pair map to the same id, so duplicates collide and
    are rejected rather than silently double-weighting the draw pool.
    """
    canonical = json.dumps({"model": model, "provider": provider},
                           sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "profile-" + digest[:48]


def _validate_canonical_entry(index: int, raw: Any) -> tuple[str, dict[str, Any]]:
    where = f"profiles[{index}]"
    if not isinstance(raw, dict):
        raise ProfileConfigError(f"{where} must be a JSON object")
    _refuse_forbidden(where, raw)
    unknown = sorted(set(raw) - _CANONICAL_ENTRY_FIELDS)
    if unknown:
        raise ProfileConfigError(
            f"{where}: unknown field(s) {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(_CANONICAL_ENTRY_FIELDS))})")
    provider, model, enabled, roles = _validate_core(
        where, raw,
        required={"provider", "model", "enabled", "roles"})
    if "id" in raw:
        pid = raw["id"]
        if not isinstance(pid, str) or not PROFILE_ID_RE.fullmatch(pid):
            # Sanitized: the invalid id value itself is never echoed.
            raise ProfileConfigError(
                f"{where}: invalid profile id; must match "
                "[A-Za-z0-9][A-Za-z0-9_-]{0,63} (bounded ASCII64)")
    else:
        pid = auto_profile_id(provider, model)
    return pid, {"provider": provider, "model": model, "enabled": enabled,
                 "roles": roles, "routing_class": ROUTING_CLASS}


def _validate_legacy_entry(profile_id: str, raw: Any) -> tuple[str, dict[str, Any]]:
    where = f"profile {profile_id!r}"
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
        raise ProfileConfigError(
            "profiles: invalid profile id "
            f"{profile_id!r} (must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}})")
    if not isinstance(raw, dict):
        raise ProfileConfigError(f"{where} must be a JSON object")
    _refuse_forbidden(where, raw)
    unknown = sorted(set(raw) - _LEGACY_ENTRY_FIELDS)
    if unknown:
        raise ProfileConfigError(f"{where}: unknown field(s) "
                                 f"{', '.join(unknown)}")
    # Old pure-worker format: entries without roles were worker-only, so
    # default to ["sub"] on a copy BEFORE core validation (which reads
    # ``raw["roles"]``). An explicit [] stays inert and ``null`` stays
    # invalid; canonical-array entries without roles still reject.
    core = raw if "roles" in raw else {**raw, "roles": ["sub"]}
    provider, model, enabled, roles = _validate_core(
        where, core, required={"provider", "model", "enabled"})
    routing_class = raw.get("routing_class", ROUTING_CLASS)
    if routing_class != ROUTING_CLASS:
        raise ProfileConfigError(
            f"{where}: routing_class must be {ROUTING_CLASS!r}")
    return profile_id, {"provider": provider, "model": model,
                        "enabled": enabled, "roles": roles,
                        "routing_class": ROUTING_CLASS}


def _install(profiles: dict[str, dict[str, Any]], pid: str,
             entry: dict[str, Any], derived: bool,
             pairs: dict[tuple[str, str], str]) -> None:
    # Duplicate pair FIRST: the same provider/model (including an
    # autogenerated id that is identical because the pair is identical) is
    # a duplicate pair, never a derived-id hash collision. Raw provider/
    # model values are not echoed: only the opaque registered id is cited.
    pair = (entry["provider"], entry["model"])
    if pair in pairs:
        raise ProfileConfigError(
            "profiles: duplicate provider/model pair already registered "
            f"as {pairs[pair]!r}; merge roles into one entry instead "
            "(duplicates are rejected so no profile gets duplicate draw "
            "weighting)")
    if pid in profiles:
        # Distinct pairs only reach here: a genuine duplicate id (explicit)
        # or a real derived-id hash collision (autogenerated).
        kind = ("derived-id hash collision" if derived else "duplicate id")
        raise ProfileConfigError(f"profiles: {kind} on {pid!r}; "
                                 "use distinct explicit ids or pairs")
    profiles[pid] = entry
    pairs[pair] = pid


def _validate_retention(document: dict[str, Any]) -> int:
    retention = document.get("record_retention_days", 14)
    if (isinstance(retention, bool) or not isinstance(retention, int)
            or retention < 0):
        raise ProfileConfigError(
            "record_retention_days must be a non-negative integer "
            "(0 disables automatic cleanup)")
    return retention


def _validate_document(document: Any) -> ProfileConfig:
    if isinstance(document, list) and not document:
        # Documented unconfigured shorthand: a fresh ``[]`` means no profiles.
        return ProfileConfig({})
    if not isinstance(document, dict):
        raise ProfileConfigError(
            "top level must be a JSON object (or an empty array)")
    if not document:
        # Empty ``{}``: settings-only shorthand, no profiles, no models.
        return ProfileConfig({})
    _refuse_forbidden("top level", document)
    unknown = sorted(set(document) - _ROOT_FIELDS)
    if unknown:
        raise ProfileConfigError(f"unknown top-level field(s): "
                                 f"{', '.join(unknown)} "
                                 f"(allowed: {', '.join(sorted(_ROOT_FIELDS))})")
    raw_profiles = document.get("profiles")
    if "version" in document:
        version = document["version"]
        # Reject ``true`` and ``2.0`` explicitly: Python equality would
        # accept them, but the schema requires an exact JSON integer.
        if (isinstance(version, bool) or not isinstance(version, int)
                or version not in (1, 2)):
            raise ProfileConfigError("version must be exactly 1 or 2")
    else:
        if isinstance(raw_profiles, dict):
            # A profiles OBJECT is legacy shape: it is invalid without the
            # explicit version 1 marker.
            raise ProfileConfigError(
                "legacy profiles object requires an explicit version 1 "
                "(array form may omit version)")
        version = 2  # array, settings-only, or empty shorthand
    profiles: dict[str, dict[str, Any]] = {}
    pairs: dict[tuple[str, str], str] = {}
    if version == 1:
        if not isinstance(raw_profiles, dict):
            raise ProfileConfigError(
                "version 1 requires profiles as a JSON object keyed by "
                "profile id")
        for profile_id, raw in raw_profiles.items():
            pid, entry = _validate_legacy_entry(profile_id, raw)
            _install(profiles, pid, entry, False, pairs)
    else:
        if raw_profiles is None:
            raw_profiles = []
        if not isinstance(raw_profiles, list):
            raise ProfileConfigError(
                "profiles must be a JSON array in version 2")
        for index, raw in enumerate(raw_profiles):
            pid, entry = _validate_canonical_entry(index, raw)
            _install(profiles, pid, entry, "id" not in (raw or {}), pairs)
    return ProfileConfig(profiles,
                         record_retention_days=_validate_retention(document))


def resolve_profiles_path(explicit: str | None, *,
                          env: dict[str, str] | None = None) -> tuple[Path | None, str]:
    """Return (path, source) for the configured file, or (None, "absent").

    Precedence: explicit > PI_WORKER_PROFILES_FILE > default location. The
    repository and the project working directory are never consulted.
    """
    if explicit:
        return Path(explicit), "explicit"
    env = os.environ if env is None else env
    from_env = env.get(PROFILES_FILE_ENV)
    if from_env:
        return Path(from_env), "env"
    return DEFAULT_PROFILES_FILE, "default"


def load_profile_config(explicit: str | None = None, *,
                        env: dict[str, str] | None = None,
                        require_explicit: bool = False) -> ProfileConfig:
    """Load and validate the configuration with the documented precedence.

    An absent env/default file yields an empty configuration (no profiles,
    14-day record retention). An explicit path is required to exist and to
    validate. Malformed/unreadable/invalid files raise ``ProfileConfigError``
    with an actionable message; raw file contents are never included. The
    ``profile_config`` module itself never reads any file at import time:
    loading happens only when a consumer explicitly calls this loader (the
    runner library also makes a best-effort import-time call, whose failures
    it swallows and re-reports). The strict CLI path calls this loader with
    the explicit ``--profiles-file`` value, so a broken local default file
    cannot prevent an explicit CLI override from taking effect.
    """
    path, source = resolve_profiles_path(explicit, env=env)
    if path is None or not path.is_file():
        if source == "explicit" or require_explicit:
            raise ProfileConfigError(
                f"profiles file not found: {path}" if path else
                "profiles file not found")
        return ProfileConfig({}, source=source, path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileConfigError(
            f"profiles file {path} is unreadable: "
            f"{type(exc).__name__}") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ProfileConfigError(
            f"profiles file {path} is not valid JSON ({type(exc).__name__}; "
            "contents are not echoed)") from exc
    try:
        config = _validate_document(document)
    except ProfileConfigError as exc:
        raise ProfileConfigError(f"profiles file {path}: {exc}") from exc
    config.source = source
    config.path = path
    return config


def parse_profile_document(document: Any) -> ProfileConfig:
    """Validate an already-parsed document (library/test entry point)."""
    return _validate_document(document)
