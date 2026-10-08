# pi-worker

`pi-worker` is a skill for agents that want to hand off bounded work to a
separate Pi worker process while keeping architecture, review, and final
acceptance in the main session. The main agent decides what to delegate, writes
a narrow contract, launches the worker, reviews the diff, records a verdict, and
tells the user what actually happened.

It ships no models and no profiles. You configure the providers and models your
account can actually reach, so an idle install makes no model calls at all.

## What you get

- `scripts/run_pi_worker.py` — launch one worker for one bounded package.
- `scripts/run_pi_worker_batch.py` — launch several independent, disjoint
  workers from one manifest.
- `scripts/record_worker_review.py` — append the main agent's verdict for a run.
- `scripts/review_worker_runs.py` — usage summary and read-only reconciliation.
- `scripts/worker_progress.py` — progress-telemetry helpers.
- `SKILL.md` and `references/` — the instructions the main agent follows.

## Requirements

| Requirement | Value |
|---|---|
| Platforms | macOS and Linux. Windows is not supported: record locking uses POSIX `fcntl`. |
| Python | 3.11 or newer |
| Pi | Baseline tested with Pi 1.0.4; newer versions are expected to work but are not claimed |
| Dependencies | Pi installed and authenticated; optionally an intercom extension you already installed and trust |
| Shell | POSIX `sh`/`bash` for the examples below |

Nothing is installed for you. There is no silent package installation: if a
command in this README reports a missing tool, install it yourself or stop.

## Install

Nothing is installed for you. This project never edits your Pi settings and
never installs itself: every block below is a command you run deliberately.

### Recommended: Pi package install (gated)

The public package identity is unchanged: name `pi-worker-skill`, version
`0.1.0`, `private: true`. The repository is
[plucury/pi-worker-kit](https://github.com/plucury/pi-worker-kit). Install from a
local checkout, or use a published git tag:

```bash
# workdir: your shell, outside any repository
pi install /path/to/this/checkout
# requires the maintainer to publish the v0.1.0 tag first
pi install git:github.com/plucury/pi-worker-kit@v0.1.0
```

The tagged command requires the source and `v0.1.0` tag to be available remotely;
this document does not claim that the tag or an npm release has already been
published. `private: true` prevents accidental npm publication, not git/local Pi
installation. You decide when and where to publish.

The package ships the bundled gate: `pi.extensions` declares
`extensions/profile-gated-skills.ts` and `pi.skills` is `[]`, so the root skill
is never auto-discovered. The gate resolves `../SKILL.md` relative to its own
file and contributes the skill only when its catalog names the exact
provider/model pair enabled with the `main` role. Nothing is hardcoded, no
model is ever selected or switched, and a missing, invalid, or empty catalog
hides the skill. The gate reads only `PI_WORKER_PROFILES_FILE`, else
`~/.config/pi-worker/profiles.json` — never the working directory or a
delegated repository — and does no network, process, or auth work. It has no
`--profiles-file` flag, so set the env var when the runner should share the
gate's catalog; an explicit runner `--profiles-file` may intentionally name a
different catalog. The gate accepts the canonical v2 array, empty, and
settings-only documents and rejects legacy `version: 1`; the runner keeps its
documented v1 compatibility.

To trial the gate for one invocation without writing settings:

```bash
# workdir: your shell, outside any repository
pi -e /path/to/this/checkout/extensions/profile-gated-skills.ts
```

Then bootstrap your private profiles file (next section), enable one `main` row
for this host's provider/model and `sub` rows for worker draws, and restart or
reload Pi. Slash commands register at startup/reload; a model or catalog edit
changes advertised availability from the next turn, and gating cannot erase
instructions or conversation already in context — it is discovery, not a
permission boundary (see [SECURITY.md](SECURITY.md)).

If you already loaded a local gate extension of your own, do not run it
alongside the bundled gate: after choosing your migration path, remove or
disable the legacy extension yourself; nothing here edits your settings.

### Manual gated install (explicit paths)

Copy the checkout to a directory outside Pi's auto-scan roots, then load the
gate explicitly:

```bash
# workdir: your shell, outside any repository
checkout="/path/to/this/checkout"
skill_home="$HOME/.pi/agent/gated-skills"
skill_dir="$skill_home/pi-worker"
mkdir -p "$skill_home"
if [ -e "$skill_dir" ] || [ -L "$skill_dir" ]; then
  printf 'kept existing %s; move or delete it yourself, nothing was copied\n' "$skill_dir"
else
  cp -R "$checkout" "$skill_dir"
fi
```

The guard matters: an existing installed copy may be a directory you have
edited, and a plain `cp -R` over it would merge the shipped files into your
local state. The recipe therefore copies only when the destination is absent
and prints a message instead of overwriting. Remove or rename the destination
yourself if you really do want a fresh copy.

Load the gate for one invocation with
`pi -e "$skill_dir/extensions/profile-gated-skills.ts"`, or add that path to
the `extensions` array in `~/.pi/agent/settings.json` yourself (user-managed;
this project never writes it). Do not copy the whole skill beneath
`~/.pi/agent/skills` while expecting it to be gated: auto-scan would load the
skill ungated.

### Ungated manual install (intentional opt-out)

Only choose this when you deliberately want no gate. Copy into the auto-scanned
skills directory; this is an intentional UNGATED install, not the gated
package:

```bash
# workdir: your shell, outside any repository
checkout="/path/to/this/checkout"
skill_home="$HOME/.pi/agent/skills"
skill_dir="$skill_home/pi-worker"
mkdir -p "$skill_home"
if [ -e "$skill_dir" ] || [ -L "$skill_dir" ]; then
  printf 'kept existing %s; move or delete it yourself, nothing was copied\n' "$skill_dir"
else
  cp -R "$checkout" "$skill_dir"
fi
```

`pi --skill <path/to/SKILL.md>` and a `settings.skills` entry are equally
intentional bypasses of the gate. Package settings filters (the `extensions`
and `skills` arrays on a package entry) can only narrow resources the package
declared; they cannot re-enable the undeclared root skill.

### Setting skill_dir

Pi reports where a loaded skill lives. Obtain the actual `SKILL.md` path from
your host and set `skill_dir` once to its containing directory — ordinary,
gated, and Pi-managed installs are all handled the same way (a Pi-managed
install may live anywhere; the resource directory is chosen by Pi). If you
used one of the install recipes above, that block already set `skill_dir` and
you can keep it; otherwise substitute the placeholder below with the actual
host-reported path for your installation — it is not a shared default. Every
example after this section validates that variable instead of assuming a
fixed path:

```bash
# workdir: your shell; substitute the path your host reports for this skill
skill_dir="/absolute/path/to/directory-containing-SKILL.md"
python3 "$skill_dir/scripts/run_pi_worker.py" --help
```

## Configuration

### Authentication

Authentication is Pi's own, and this package adds nothing to it. Register your
model in Pi the usual way:

- run `/login` inside Pi and complete the provider login, or
- export the environment variables your provider documents, in your own shell.

The runner uses whatever Pi already has stored or in the environment. There are
no custom auth variables to set, no login-shell bridge, no login step to run
here, and this package never writes an auth setting. Do not put keys on a command
line, and never write a real key into a configuration file that might be
committed.

To see which provider/model identifiers Pi can reach, use the read-only catalog
check. It lists models without calling one:

```bash
pi --list-models
```

This only reports the catalog. It does not prove a model answers, that your
account is funded, or that a run will succeed.

### The profiles file

Routing is configuration, so this repository ships an example and nothing else.
Copy it to your own home directory and edit it. The recipe never replaces a file
that is already there, including a symlink:

```bash
# workdir: your shell
umask 077
src="$skill_dir/examples/profiles.example.json"
dst_dir="$HOME/.config/pi-worker"
dst="$dst_dir/profiles.json"
if [ -e "$dst" ] || [ -L "$dst" ]; then
  printf 'kept existing %s; edit it in place, nothing was replaced\n' "$dst"
else
  mkdir -p "$dst_dir"
  cp "$src" "$dst"
  chmod 600 "$dst"
  printf 'created %s; edit it before any paid run\n' "$dst"
fi
```

`umask 077` applies to what this shell creates; the new directory is created
`0700` and the new file `0600`. Nothing else is touched: no existing ancestor is
re-permissioned, and nothing is unlinked or overwritten. Keep using the same
variable names (`src`, `dst_dir`, `dst`) in the rest of this README.

Your real profiles file lives outside this repository, at `$dst`. It is covered
by `.gitignore` (`profiles.json` and friends) and it is not in the npm `files`
allowlist, so a published tarball cannot carry it. The runner reads it; it never
uploads, copies, or writes it anywhere else. Installing or using this skill does
not modify a pre-existing Pi main-agent configuration, login, or settings file:
auth stays with Pi's own `/login`, and this package ships no default models and
no stored auth of its own. The tracked `examples/profiles.example.json` is
disabled placeholders only and never names a real provider or model.

Resolution order, first match wins:

1. `--profiles-file /path/to/profiles.json`
2. `PI_WORKER_PROFILES_FILE=/path/to/profiles.json`
3. `~/.config/pi-worker/profiles.json`

A profiles file is trusted only when it comes from one of those three sources.
The runner never auto-discovers a profiles file from the working directory or
from inside a delegated repository, so a checked-in file cannot silently take
over routing.

There are no shipped profiles and no default models: `{}`, `{"profiles": []}`,
and a default with only `record_retention_days` are all the same empty
configuration, so no main or sub model is ever chosen for you. An absent env or
default file therefore loads that empty configuration, and a document that
parses but has no enabled `sub` row still refuses to launch a worker with the
sanitized code `no_enabled_profile`. An explicit `--profiles-file` path that does
not exist is a different failure: the
strict load before launch reports an actionable configuration error naming the
path, never `no_enabled_profile`. The current main model is never implicitly
used as a worker fallback. A provider/model pair is eligible for workers only
when it is explicitly enabled with the `sub` role, and a shared main/sub row may
be used for either role. The `main` role governs host eligibility; it never
automatically switches the current main.

```json
{
  "profiles": [
    {
      "id": "my-main",
      "provider": "your-provider",
      "model": "your-main-model",
      "enabled": false,
      "roles": ["main", "sub"]
    },
    {
      "id": "my-worker",
      "provider": "your-provider",
      "model": "your-sub-model",
      "enabled": false,
      "roles": ["sub"]
    }
  ]
}
```

The shipped example mirrors this: every entry starts `enabled: false` with
placeholder provider and model values and no real pair, so it cannot route a
paid run until you edit your own copy.

- `profiles` is an **array** of rows. Each row needs `provider` and `model` as
  non-null, non-empty strings, an explicit `enabled` boolean, and a `roles`
  array containing only `"main"` and/or `"sub"`, with no duplicates.
- `roles` is required. `[]` is valid and means unassigned: the row stays in the
  catalog but is inert. A row with both roles is eligible once per role, not
  twice per draw.
- `id` is optional and is your own alias, up to 64 ASCII characters. When it is
  absent, identity is a stable hash derived from the provider/model pair alone,
  so it does not depend on row order, roles, or the enabled flag; existing ids
  from an older config survive unchanged when you carry them over.
- Two rows with the same `provider`/`model` pair are rejected; merge their roles
  into one row instead. There is no weight, priority, or preference column. A
  duplicate `id` is rejected the same way. An unknown role member rejects the
  whole DOCUMENT: that configuration is schema-invalid and nothing in it
  loads. A config with no enabled `sub` row is NOT schema-invalid — a
  main-only document is valid and can unlock the host skill through the gate
  — what is rejected is a worker LAUNCH, with the sanitized code
  `no_enabled_profile`.
- Launching a worker needs at least one row `enabled` with the `sub` role;
  otherwise the runner reports an informative configuration error before any
  Pi or model call.
- `auto` selection is a uniform draw among the **enabled `sub`-role entries**
  only. The main-only or empty-role rows are ignored even when they are enabled,
  and a disabled sub row is never drawn.
- Profile identity is sticky per delegation, and that stickiness is a protocol
  rule you must keep, not an automatic guard: keep the provider and model of an
  existing profile id immutable, and introduce a **new profile id** (with a new
  delegation id) for any change, so a retry cannot silently run on a different
  model than the one you reviewed. The runner does not detect a mutated
  configuration, and it does not re-read a mutable file to protect you here.
- The `main` role is a declaration for host integration: it lists which
  provider/model pairs you allow as the main model for this host, and the gate
  opens the host skill only for an exact enabled `main` pair. It never selects
  or changes the main model — the host chooses that, and this configuration
  cannot switch it for you. `main` and `sub` are independent: an enabled `main`
  row opens the host skill, an enabled `sub` row permits draws, and a merged
  `["main","sub"]` row does both.
- Legacy `version: 1` dictionary configs are still recognized for an existing
  worker-only setup and default every row to the `sub` role; the runner accepts
  them, while the bundled gate rejects v1 and hides the skill until you
  migrate. Legacy auth fields are rejected rather than executed, so
  migrate such a file to the array form; nothing here needs a secret copy or a
  login script.

Configuration fields at the document root:

| Field | Default | Meaning |
|---|---|---|
| `profiles` | `[]` | Array of profile rows as described above. |
| `version` | `2` | Optional document marker. Omit it or set `2`; only a legacy `1` document is read differently. |
| `record_retention_days` | `14` | Retention window for run, review, lifecycle, and notification records, in local calendar days: today plus the previous `N-1`. A prune runs after **every** successful ledger append (run, review, lifecycle, or notification), so nothing waits for a "next launch". `0` disables the cleanup. |

Note: the gate parses this in JavaScript, where the value must be an exact
integer ≤ `9007199254740991` (2^53−1); anything larger fails closed (the gate
hides the skill) even though the Python runner may accept it.

## Quickstart

Write a narrow contract, then dry-run the launch before spending anything:

```bash
# workdir: the project the worker will edit
workdir="$(pwd -P)"
logs="$(cd "$(mktemp -d)" && pwd -P)"
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
python3 "$skill_dir/scripts/run_pi_worker.py" \
  --profiles-file "$HOME/.config/pi-worker/profiles.json" \
  --workdir "$workdir" \
  --contract-file "$workdir/.pi-worker/contract.txt" \
  --tools write \
  --assignment-goal 'Implement the bounded change' \
  --scope path/to/file.py \
  --acceptance-check 'focused tests pass' \
  --task-kind implementation --estimated-files 2 --risk low \
  --acceptance-mode deterministic \
  --delegation-id 'docs-001' \
  --timeout 900 \
  --validate-only
```

Drop `--validate-only` to actually launch, add `--profile auto` (or the `id` alias
of your own `sub`-role entry) to choose routing, and add
`--receipt-file "$logs/receipt.json"` to keep a private copy of the terminal
receipt. `--profile` only ever resolves against `sub`-role entries, so a `main`
row is never a worker target. There is no flag that sets or switches the main
model; that stays with the host.

```bash
# workdir: the project the worker will edit
workdir="$(pwd -P)"
logs="$(cd "$(mktemp -d)" && pwd -P)"
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
python3 "$skill_dir/scripts/run_pi_worker.py" \
  --profiles-file "$HOME/.config/pi-worker/profiles.json" \
  --workdir "$workdir" \
  --contract-file "$workdir/.pi-worker/contract.txt" \
  --tools write \
  --delegation-id 'docs-001' \
  --timeout 900 \
  --receipt-file "$logs/receipt.json" \
  >"$logs/stdout.json" 2>"$logs/stderr.log"
```

`workdir="$(pwd -P)"` resolves symlinks so the path you pass is the canonical
one; `logs="$(cd "$(mktemp -d)" && pwd -P)"` does the same for the log directory
instead of assuming a particular platform's temporary-directory layout.

Review the diff yourself, then record the verdict with the `run_id` from the
receipt:

```bash
# workdir: anywhere
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
python3 "$skill_dir/scripts/record_worker_review.py" \
  --run-id 'run-id-from-receipt' \
  --verdict accepted \
  --summary 'Focused and integration checks passed' \
  --changed-file path/to/file.py \
  --verification 'focused tests pass' \
  --main-rework none
```

## Help and inspection

```bash
# workdir: anywhere
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
python3 "$skill_dir/scripts/run_pi_worker.py" --help
python3 "$skill_dir/scripts/run_pi_worker.py" --show-profile-state
python3 "$skill_dir/scripts/review_worker_runs.py" --days 30
python3 "$skill_dir/scripts/review_worker_runs.py" --reconcile --days 30
```

`--show-profile-state` prints the private auto-disable state as JSON and exits
without running anything; `--reset-profile-state <profile-id>` clears one entry.
Record routing follows the launcher, so run these from the same side that
produced the records.

## Tests

The suite is offline and uses a fake Pi runner; it never calls a provider.

```bash
# workdir: a scratch checkout of this repository
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
python3 -m ruff check scripts tests
```

The bundled gate's synthetic tests need Node.js 20 or newer and the local npm
dev tooling:

```bash
# workdir: a scratch checkout of this repository
npm ci --legacy-peer-deps --ignore-scripts --no-audit --no-fund
npm test
```

Commit `package-lock.json` to freeze development dependencies. The matching
`--legacy-peer-deps` policy avoids installing Pi's host-supplied SDK peer;
`npm ci` checks the lock instead of updating it. The lock is not shipped in the
npm release artifact. `npm test` runs `node --test tests/test_gate.mjs` (offline,
synthetic only).
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full setup and for the
documentation syntax checks.

## Uninstall

This skill and its extension never write configuration and never install
themselves; removing them is your deliberate choice. Uninstalling never edits
your local configuration, your Pi login, or the main agent's own
configuration, and never removes records or receipts for you.

For a package install, `pi remove <source>` — the same local path or git
source you installed from — removes the package and its settings declaration;
Pi manages that declaration itself. A gate path you added to
`settings.extensions` by hand is yours to remove as well.

For a manual install, choose the one actual `skill_dir` you copied into and
delete exactly that directory, guarded:

```bash
# workdir: your shell; the single directory you installed into
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
rm -rf "$skill_dir"
```

The guard stops an unset variable, not a wrong value: point `skill_dir` at the
one destination you used (the recipes above set it), never at both paths.

Local state you created stays where it is until you delete it yourself:

| Path | What it holds | Remove it with |
|---|---|---|
| `~/.config/pi-worker/profiles.json` | your profiles | `rm` it after you no longer need it; it lists your provider/model pairs |
| the record directory (`PI_WORKER_RECORD_DIR`, else the launcher-side default) | append-only run and review ledger | `rm -r` it, or let `record_retention_days` prune it |
| `--receipt-file` destinations and log directories | terminal receipts and stderr logs | `rm -r` them |

Uninstalling the skill does not cancel anything a provider has already billed.

## Data retention

- Run, review, lifecycle, and notification records are appended to a private
  ledger on your machine with owner-only permissions (`0600`), and created
  parent directories are `0700`.
- `record_retention_days` defaults to `14` local calendar days: today plus the
  previous `13`, so a configured positive `N` keeps today plus the previous
  `N-1`. A prune runs after **every** successful append (run, review, lifecycle,
  or notification) under the same exclusive append lock, which also happens when
  you append a review yourself; `0` keeps everything. There is no daemon and no
  CLI prune flag.
- Reports and cross-delegation reviews only cover the retention window that
  still exists, so a pruned run cannot appear in later analysis.

## Security

Read [SECURITY.md](SECURITY.md) before pointing a worker at a repository. Short
version: `--approve` grants the worker real operations, allowlists and scopes are
instructions rather than a filesystem sandbox, and delegated repositories can
contain text crafted to influence an agent.

## Support

| Item | Status |
|---|---|
| Version | 0.1.0 |
| Platforms | macOS, Linux (no Windows support) |
| Python | 3.11+ |
| Pi baseline | 1.0.4 tested; later versions not claimed |
| License | MIT, see [LICENSE](LICENSE) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |

Licensing note: this repository ships its own MIT license. Pi and the optional
intercom extension are external tools you install yourself and are **not**
bundled here; no audit of their licenses or dependencies is claimed, and you are
responsible for checking the terms of anything you install alongside this
project.
