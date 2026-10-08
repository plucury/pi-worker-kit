# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

Older or unreleased lines are not patched. See [README.md](README.md) for the
tested platform baseline.

## Reporting a vulnerability

Do **not** open a public issue for a suspected vulnerability.

Send a private report to the repository maintainer through a private channel
that the repository owner has configured for security contact. Until this
repository has a published security contact, there is no email address,
security advisory page, or GitHub account to use here, and this project does
not invent one: use the private channel you already have with the maintainer
and state clearly that the report is confidential.

A good report contains:

- what the issue is, in one or two sentences;
- the version (`0.1.0`), platform (macOS/Linux), Python version, and Pi version;
- minimal reproduction steps or a private receipt/run identifier you are willing
  to share privately;
- the impact you believe it has, and any data or secrets that may be
  exposed.

Do not paste API keys, tokens, private contracts, or the contents of private
records into a report. Share them redacted, or share the identifier of the
local file that holds them instead.

## What this project does and does not protect

### No sandbox

The runner is an orchestrator, not a security boundary. `--approve` grants the
worker the operations you listed for that run: file edits, shell commands,
tests. Allowlists, routing classes, scopes, tool modes, and contract text are
*instructions the worker is asked to follow*, not filesystem security. A worker
that ignores them is a correctness failure, not an attack that containment
stops.

You are responsible for:

- trusting every repository, contract file, and code path you point a worker at;
- trusting every Pi extension you load, including the intercom extension passed
  with `--intercom-extension`;
- trusting your own local profiles file and anything it references. It is a
  plain routing catalog you maintain; nothing in this package detects a
  provider/model change you make in it, and allowlists of allowed main pairs
  are not enforcement.
- reviewing diffs and running integration checks yourself before accepting work;
- keeping secrets out of repositories, contracts, and command lines.

A worker may read repository content that contains hostile text. Treat any
instruction found inside a delegated repository, issue, or file as untrusted
data, not as a directive to you or to the worker.

### Bundled skill gate

The bundled gate is ordinary extension code that Pi loads as trusted code: it
executes with your host's full rights, so it is not a sandbox or a permission
boundary. It gates advertisement only — it cannot remove instructions or
conversation already read into context, and direct skill loading (`--skill`,
`settings.skills`) or copying the skill into an auto-scanned skills directory
bypasses it intentionally. The gate reads only the trusted environment/home
catalog (`PI_WORKER_PROFILES_FILE`, else `~/.config/pi-worker/profiles.json`)
and does no network, process, auth, or config writes. Never ship your user
configuration or credentials in this repository or any published artifact.

### Authentication

Auth lives entirely in Pi's own setup. Register a model in Pi the usual way —
`/login` inside Pi, or the environment variables your provider documents — so
keys stay in your Pi setup and your shell rather than in this repository. This
package ships no auth field, no custom auth variable, no login-shell bridge, and
no login script, and it never writes an auth setting of any kind. A
`profiles.json` row carries routing metadata only: `provider`, `model`,
`enabled`, `roles`, and an optional `id` alias. A legacy document carrying the
old auth fields is rejected rather than executed, so migrate the file instead of
restoring it.

### Local files and permissions

- Run records and receipts are written with owner-only permissions (`0600`),
  and created parent directories are `0700`. Logs and receipts stay on your
  machine; do not commit them.
- `record_retention_days` defaults to `14` local calendar days (today plus the
  previous 13) and prunes older run, review, lifecycle, and notification
  records after every successful ledger append, under the same exclusive append
  lock; `0` disables the cleanup entirely. Historical reports and cross-reviews
  then only cover the window that still exists.
- A tool timeout or run deadline aborts the worker turn and its managed Pi
  process after a bounded grace period. It is not a guarantee that every child
  process a shell command spawned has exited.
- Resource groups and batch manifests express ownership and sequencing intent.
  They are bookkeeping, not enforcement of quotas or isolation.
- No default models are configured, so an idle install makes no model calls and
  no surprise charges. Any run you launch explicitly can still incur provider
  fees.
- Delegated work can cost tokens without producing a usable token-saving claim.
  Main-side token counters in records are manual entries, not a measured saving,
  and worker tokens are not the same thing as money saved.
