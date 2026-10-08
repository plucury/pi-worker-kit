# Pi-main intercom transport (mandatory)

Read this before any worker delegation when the main is a Pi session. Intercom
is mandatory for initial runs, retries, and rework. A Pi session
using a Codex/OpenAI model is still Pi-main. The default Codex/RPC path in
`SKILL.md` is unchanged; intercom is not required for Codex.

The shared CLI keeps its RPC default for compatibility, not as a Pi-main
fallback. Pi-main must always pass the three intercom flags explicitly.

For several independent/disjoint workers, the batch driver
(`scripts/run_pi_worker_batch.py`) forwards these same explicit intercom flags
to every child and launches each child in the background asynchronously; see
[parallel-workers.md](parallel-workers.md).

## Preconditions (all required)

- The main actually has a callable `intercom` tool with actions `list`,
  `status`, `send`, `ask`, `reply`. These are the actions used by this
  workflow; the `pending` action can disambiguate replies. There is no target
  action.
- The extension is installed and trusted; its absolute `.ts`/`.js` path is
  passed via `--intercom-extension`.
- The main knows its own full UUID from `intercom({action:'status'})`. That
  exact UUID is the `--supervisor` value. The CLI enforces a strict full UUID.
- Both sides share the same broker/scope runtime.
- The main can run the runner in the background and stay available to answer.

If any precondition fails, STOP delegation and explain the blocker. There is
NO plain-RPC fallback for Pi-main. If installed but not loaded in the main
session, request a Pi reload/restart. Do not pretend calls, silently install
anything, or bypass the requirement using direct subprocesses or another
launcher.

## Launch

Augment the normal runner invocation with `--transport intercom
--intercom-extension <path> --supervisor <full-uuid>`. Preserve the project
cwd BEFORE creating temp logs — do not `cd` into the log directory, or the
runner's `--workdir` breaks:

```bash
# workdir: the project the worker will edit
workdir="$(pwd -P)"
# derive the actual host-reported skill root first: set skill_dir to the
# directory containing the loaded SKILL.md for this installation
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
umask 077
logs="$(cd "$(mktemp -d)" && pwd -P)"
python3 "$skill_dir/scripts/run_pi_worker.py" \
  --workdir "$workdir" \
  --contract-file "$workdir/.pi-worker/contract.txt" \
  --routing-class standard --profile auto --thinking medium --tools write \
  --assignment-goal 'Implement the bounded change' --scope path/to/file.py \
  --acceptance-check 'focused tests pass' --task-kind implementation \
  --estimated-files 2 --risk low --acceptance-mode deterministic \
  --delegation-id 'stable-unique-id' --task-group-id 'opaque-group-id' \
  --timeout 900 \
  --receipt-file "$logs/terminal-receipt.json" \
  --transport intercom \
  --intercom-extension "$HOME/.pi/extensions/intercom/index.ts" \
  --supervisor '00000000-0000-0000-0000-000000000000' \
  >"$logs/receipt.json" 2>"$logs/error.log" < /dev/null &
echo $! > "$logs/pid"
```

Replace the supervisor placeholder with your own exact full UUID from
`intercom({action:'status'})` and the extension path with the absolute path of
the intercom extension you already installed and trust. `--task-group-id` is
optional; drop it for ungrouped work. Capturing `workdir` before creating the
log directory, and resolving it with `pwd -P`, keeps `--workdir` canonical
without assuming a platform's temporary-directory layout.

Keep the actual PID/job handle. How long the child survives after the main's
turn ends depends on the harness — verify with your own harness rather than
assuming persistence, and never claim a persistent/resident worker exists.
Private logs may contain metadata; do not print them. There is no worker
persistence in this integration: the child is one-shot.

`--task-group-id '<opaque-id>'` is optional. Supply one id for every phase of a
single user-visible feature — across separate launches with different
`delegation_id`s — so the report can aggregate them; omit it and the run stays
ungrouped. See [run-record-schema.md](run-record-schema.md).

Runner behavior in intercom mode: loads only `-e <extension>` alongside
`--no-extensions`, appends `intercom` to the allowed tools, runs a
`get_commands` preflight (a failed/malformed response fails with
`intercom_unavailable` before any prompt; a missing response becomes the run
timeout), and appends the communication contract to the task text. The receipt
records only `worker.transport: "intercom"` — never the supervisor UUID,
extension path, or contract.

`--receipt-file` additionally writes the terminal receipt atomically (`0600`)
for success, failure, timeout, and interrupt; the path is validated (no `..` or
symlink destination, no group/other-writable non-sticky parent) and platform
parent aliases such as `/tmp` are resolved to their canonical target, so pass
canonical paths (`pwd -P` output, or a `mktemp -d` result resolved the same way)
rather than an alias. After the terminal ledger and
receipt exist, the runner sends a best-effort `RUN_FINISHED` notification over
the intercom CLI (`node <cli.mjs> send --to <exact supervisor> --text <compact
sanitized JSON> --json --name <unique runner name>`), resolving `cli.mjs` from a
sibling of `--intercom-extension` or from an explicit `--intercom-cli`. At most
two attempts of at most five seconds are made. Only a zero-exit CLI whose JSON
has both `ok: true` and `delivered: true` confirms routing; a contradictory or
malformed result never does. It is not acceptance or a read acknowledgment.
Notification failure never changes execution status, SIGKILL cannot notify, and
the runner records a coarse `notification` event (`attempts`, `delivery`, plus
the terminal-evidence flags) so a gap is visible.

## Intent mappings for routing features

The CLI uses strict enums for routing features; only canonical values are accepted
and invalid aliases are rejected by argparse. Common intents map as follows:

- `--task-kind`: review and root-cause debugging → `investigation`; implementation
  debugging that edits files → `implementation`.
  Canonical values: `investigation`, `implementation`, `test`, `docs`, `config`,
  `refactor`, `other`. Invalid aliases such as `review` or `debugging` are rejected.
- `--acceptance-mode`: deterministic checks combined with human judgment →
  `mixed`; mostly human acceptance → `manual`.
  Canonical values: `deterministic`, `mixed`, `manual`. Invalid aliases such as
  `judgment` or `judgmental` are rejected.

When the intent does not match a canonical value, pick the closest one above
rather than inventing a new alias; these fields are coarse routing features
recorded in the receipt and never used to make routing decisions.

## Validating inputs (dry-run)

Add `--validate-only` to the launch invocation above to dry-run the inputs
before reserving a delegation. The runner performs the same input checks as a
real launch — required fields, workdir, contract UTF-8 decodability, AGENTS.md
overrides, profile/thinking resolution, transport/intercom arguments,
non-mutating routing/profile compatibility, and sticky-reservation
compatibility against any existing delegation — but never reserves or changes
routing state, checks a provider, starts Pi, performs a random draw, or
appends a run/review record. On success it prints a small JSON validation
result and exits 0; the result intentionally omits the supervisor UUID,
extension path, and contract content, so it is safe to surface to the user
when announcing that a delegation is ready to launch.

## Worker contract (appended by the runner)

`intercom` is a tool: `intercom({action:'list'|'status'|'send'|'ask'|'reply',...})`.
`list` exposes shortened roster IDs only; `status` shows the caller's own full
ID, not peers. The worker: lists presence, then sends READY to the exact full
supervisor UUID the runner provided. A short roster match is not identity.
Delivery is a routing confirmation only — not authentication and not proof the
recipient processed anything. The supervisor UUID is trusted runner input; no
UUID claimed in a message proves identity. The delegation ID appears in every
READY/ask/PROGRESS/DONE. READY failure means the worker stops without edits.
DONE is a summary, not acceptance.

A quiet worker sends exactly one short READY carrying the exact supervisor
proof it was required to send, plus only the actionable `ask` or `PROGRESS` the
main asked for. `DONE` is fixed-size: at most **1200 characters**, relative paths
only, check summaries, known limits, and only IDs the main already knows.
`RUN_FINISHED` from the runner is the authoritative terminal signal, so there is
no duplicate completion ack and no bulky recap; neither proves the work is good.

## Main-side protocol

- Launch, then end your turn. Do not block a tool call on the whole task and do
  not ask the worker synchronously (it may ask back).
- Stay quiet otherwise: an ordinary `READY` or routine progress message gets **no
  echo user update and no extra acknowledgment**. Mandatory main messages are
  pre-launch disclosure (one combined notice for a batch, not one per child), the
  actual model once the runner confirms it, blockers, a confirmed model change,
  and the final acceptance decision. Read the compact handoff plus receipt and
  open diffs on demand; never repeatedly reread whole files or full logs, and
  never accept a worker summary in place of your own verification.
- Answer worker `ask` via `intercom({action:'reply',...})` with exact
  thread/pending disambiguation. The sender metadata on READY is the worker's
  session ID, not the delegation ID.
- DONE may arrive before the receipt is written. Wait briefly and re-check the
  receipt without blocking replies to other pending asks.
- The runner also emits `RUN_FINISHED` after the terminal ledger and receipt,
  carrying `run_id`, `delegation_id`, `status`, `failure_code` when present,
  `followup_required`, and the terminal-evidence flags `ledger_written`,
  `receipt_required`, and `receipt_written`. React to it by reading the receipt
  from `--receipt-file`, then review. A false `ledger_written` means the terminal
  ledger is not durable; a false `receipt_written` (when `receipt_required` is
  true) means the required receipt file is not durable. Either makes the
  evidence unconfirmed: the runner keeps the checkpoint and exits nonzero, and
  the main must not claim an authoritative receipt exists. Before trusting a
  receipt file, match its `run_id` to the notification, because a stale previous
  receipt may still be on disk. Dedup `DONE` and `RUN_FINISHED` by
  `run_id`/`delegation_id` plus the receipt; a bounded number of retries is
  expected and harmless because the run id is stable. `RUN_FINISHED` presence or
  absence is never proof the work is good, and a failed notification never
  changes the recorded execution status.
- Run periodic bounded reconciliation
  (`review_worker_runs.py --reconcile`) to catch a dead runner or a notification
  that never arrived. It reports stale/unconfirmed starts, completed runs
  pending review, failed/interrupted runs without closure review, and
  missing/failed intercom notifications. Test-tagged records and `fake-session`
  fixtures are excluded by default (use `--include-tests`), and `--reconcile`
  rejects `--worker`/`--status`/`--project` because the lifecycle view has no
  run-level filters. Never rewrite the ledger and never turn `pending_review`
  into `rejected`.
- Main wakes only via messages, so arrange bounded monitoring of runs that
  fail without DONE (a background facility or explicit periodic checks). Do not
  infinite-poll. If DONE never arrives, check the actual receipt and the
  recorded job/child status before concluding anything: an absent receipt
  means the run status is unconfirmed/interrupted pending investigation —
  never fabricate a receipt and never treat `pending_review` as rejected.
  A crash does not always produce no receipt; inspect whatever terminal
  receipt exists, and surface missing evidence rather than assuming an
  outcome.
- On READY-failure reports, stop and report the blocker in your final answer.
  The receipt may still end completed/pending because the runner sees settling;
  do not claim the receipt automatically recorded a delivery failure. Review
  is never accepted merely because a receipt's status reads completed —
  acceptance comes only from the main's own review.

## Review, rework, lifecycle

- Review is the existing append-only helper (`record_worker_review.py`); the
  original receipt is never mutated.
- Use `--main-rework none` unless the main genuinely edited files. Only
  explicitly authorized tiny emergency work may stay in the main, and it must be
  recorded as `--main-rework minor|major|rewrite` so the report shows it.
- Optionally record what the main itself spent on this review event with
  `--main-briefing-count`, `--main-review-count`, `--main-recovery-count`,
  `--main-diff-bytes`, `--main-input-tokens`, and `--main-output-tokens`. Each is
  a non-negative integer for **that review event only**, omitted/null when
  unknown; the block is written only when at least one value is supplied.
  Attribution to the main session is manual — the runner measures no main tokens
  — so omit what you cannot know rather than inventing values. `--main-diff-bytes`
  is the main-side diff volume actually read, usually smaller than the whole diff.
  These counters are bookkeeping and never a `gpt_tokens_saved` claim; the report
  shows them per review event, per worker, and per `--task-group-id` group, and it
  never derives a model ranking from them.
- No same-session rework this iteration: the child is one-shot. After
  `needs_rework`/`rejected`, wait for receipt + child exit, record the verdict,
  then launch a new runner with the same delegation ID and a narrow updated
  contract. The new invocation MUST retain `--transport intercom`,
  `--intercom-extension`, and the current main's full `--supervisor` UUID. The
  pinned standard profile is reused (an auto-disabled pin re-draws among the
  remaining enabled standard profiles); no model switching via intercom.
  Historical `simple-m3` escalation chains exist in old records as data only;
  the simple class has been removed, so no new simple launch or escalation can
  start.
- Cancel via intercom is advisory. The runner enforces `--timeout` and aborts.
  Before retrying, confirm the runner AND your own child actually exited; PID
  reuse makes a stale `kill -0` probe unreliable, so prefer waiting on the
  recorded handle. Never claim the runner still monitors after it has itself
  died, and never assume closing stdin stopped the worker.

## Efficiency and timeout recovery

Pi-main delegates only over intercom and launches async (see [Launch](#launch));
model selection stays fixed — no automatic switching. The per-tool bound
`--tool-timeout-seconds` defaults to **180 s** (`0` disables it); a tool that
exceeds it fails the worker with the sanitized code `tool_timeout` after the
same bounded graceful abort as the global timeout. The automatic-retry bounds are
active: `--max-auto-retries 2` (the `(count + 1)`-th agent-turn `auto_retry_start`
fails the worker with the sanitized code `retry_limit`) and
`--retry-budget-seconds 120` (aggregate observed retry time, in-flight included,
fails it with `retry_budget_exceeded`); `0` rejects every retry / disables the
time bound only. After a timeout, read the
receipt, inspect the existing diff and evidence, then narrow the contract before
retrying the same delegation ID and pinned profile. The runner enforces that: a
byte-identical `contract_digest` after the latest run timed out is rejected
before launch (dry-run included) with the sanitized code
`unchanged_timeout_retry` at stage `routing`, unless
`--allow-unchanged-timeout-retry` is passed deliberately with a user-visible
reason; it changes no model or profile and only records the override in the
receipt. Equal digests mean equal text, not a better scope. Non-timeout provider failures may keep the same contract.
A worker never
delegates nested workers. The runner aborts the whole worker on the tool-wait or
global timeout; that is not a promise of a hard subprocess kill or proof of
quality. The optional `progress_metrics` block is observation only — it never
replaces a diff and test review. See
[worker-efficiency.md](worker-efficiency.md) for the full guidance.

Use the self-contained [six-field contract](worker-efficiency.md#5-the-compact-six-field-contract-and-the-short-rework-contract),
with only the fix and necessary retained facts on rework. Intercom appends
communication rules, not task background. Follow the
[script-first review order](worker-efficiency.md#6-independent-review-order-cheap-without-losing-acceptance):
match the receipt and pre-launch snapshot, run trusted focused checks after any
necessary execution-safety inspection, then review all changed hunks even when
green. Broaden only for failures, risk-critical changes, or unresolved semantics;
passing tests never auto-accept. Keep full logs private outside the repo.

## Trust boundaries

The intercom contract is instructions, not a security sandbox. Tool allowlists
(`--tools read-only` still excludes bash/edit/write), filesystem permissions,
and the deadline remain the real boundaries. The runner sets no pi-subagents
bridge env; it uses generic `intercom` send/ask only. The `get_commands`
preflight proves the extension loaded, not that the supervisor is connected.
