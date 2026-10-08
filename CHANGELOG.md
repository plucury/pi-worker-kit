# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First open-source release.

### Added

- Bounded worker delegation CLI (`scripts/run_pi_worker.py`): one worker per
  delegated package, with contract file, workdir, tool mode, task metadata,
  delegation and task-group identifiers, run deadline, per-tool timeout,
  automatic-retry count and retry time bounds, and an opt-in
  unchanged-timeout-retry override.
- User-owned profiles file instead of shipped built-in profiles. The document
  holds a `profiles` **array** of rows (`provider`, `model`, `enabled`, required
  `roles` of `main`/`sub`, optional `id` alias up to 64 ASCII characters), with
  optional root `version` and `record_retention_days`. A configuration with no
  enabled `sub` profile refuses to launch with `no_enabled_profile`. Resolution
  order is `--profiles-file`, then `PI_WORKER_PROFILES_FILE`, then
  `~/.config/pi-worker/profiles.json`, never the working directory. A profiles
  file is trusted only when named explicitly, by environment, or under the
  user's home directory.
- Legacy `version: 1` dictionary documents are still recognized for an existing
  worker-only setup and default every row to the `sub` role.
- `main` is a declaration for host integration: enabled `main` provider/model
  pairs are read as an eligibility catalog, while the main model itself stays
  whatever the host already chose.
- Sticky profile identity per delegation, with uniform `auto` selection among the
  enabled `sub`-role entries as the documented default strategy.
- Private append-only run ledger and terminal receipts written `0600`, plus an
  optional `--receipt-file` copy validated before routing is reserved.
- Review recording (`scripts/record_worker_review.py`), usage and reconciliation
  reporting (`scripts/review_worker_runs.py`), and progress telemetry helpers
  (`scripts/worker_progress.py`).
- Batch driver (`scripts/run_pi_worker_batch.py`) for independent, disjoint
  workers with manifest schema v1, conflict detection, and per-child receipts.
- Optional intercom transport for a Pi session acting as main, using an
  explicitly provided extension path, plus a best-effort `RUN_FINISHED`
  terminal notification.
- `record_retention_days` configuration, default `14`, pruning run, review,
  lifecycle, and notification records; `0` disables the cleanup.
- Authentication is Pi's own throughout: `/login` or the environment, with no
  auth field, custom auth variable, login-shell bridge, or login script shipped
  here, and no auth setting ever written by this package. Legacy documents that
  carry the removed auth fields are rejected instead of executed.

- Bundled portable gate as the default package behavior: `pi.extensions`
  declares `extensions/profile-gated-skills.ts` and `pi.skills` is `[]`, so
  there is no unconditional root-skill auto-discovery. The gate resolves
  `../SKILL.md` relative to its own file, advertises the skill only for an
  exact enabled `main` provider/model pair, accepts canonical v2, empty, and
  settings-only documents, and rejects legacy `version: 1` while the runner
  keeps its v1 compatibility.
- Skill frontmatter (`name: pi-worker`, license, compatibility) and portable,
  host-derived paths throughout: docs reference a host-reported `skill_dir`
  instead of any fixed install location.

### Development

- Committed npm development lockfile and frozen `npm ci` installation in CI,
  without installing Pi's host-supplied SDK peer.

### Documentation

- README, SECURITY, CONTRIBUTING, CHANGELOG, this skill's instructions, and the
  `references/` guides.

[0.1.0]: https://github.com/plucury/pi-worker-kit/releases/tag/v0.1.0
