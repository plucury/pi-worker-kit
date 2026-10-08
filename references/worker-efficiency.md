# Worker efficiency: telemetry, checkpoints, splitting, and timeout recovery

Practical guidance for reading `progress_metrics`, checkpointing long runs,
splitting dependent work, and recovering from a timeout. These are observations
and soft operating rules, not a quality score: the main agent still inspects the
actual diff and evidence before accepting anything.

## 0. Status of these controls — read before using the invocation sketch

**Implemented and verified (this package): phase collection and reporting, soft
checkpoint steering, and the per-tool bound.** The runner observes the RPC
messages it already receives and emits the optional flat `progress_metrics`
block in the terminal receipt (success *and* failure) and in the private
checkpoint sidecar. `scripts/worker_progress.py` holds the observer: it keeps
only a tool call id, its relative start offset, and a one-word classification,
and drops arguments, commands, results, and errors immediately. The same
observer backs the per-tool bound, so it runs on **every** path — including a
direct `run_rpc(..., progress=None)` API call, where no receipt is written.
`--tool-timeout-seconds` defaults to **180 s** per running tool; `0` (or any
non-positive value) disables the bound and is stated as disabled in the prompt.

`--allow-unchanged-timeout-retry` (implemented) is the deliberate escape hatch
described in section 4: without it, a byte-identical retry after a timeout is
refused before launch. Use the invocation sketch with the flag omitted for the
normal narrowed-contract retry.

## 1. What the stage telemetry observes — and what it does not

An optional flat `progress_metrics` block records *observed* timing relative to
the prompt. Every field is `null` when unobserved, and a missing field is never
rewritten to 0. Fields are monotonic: once a stage is observed, later
observations do not move it backwards.

- `first_tool_seconds` — first tool call observed.
- `first_edit_seconds` — first **successful** `edit`/`write` completion only: it
  needs a matching start and an explicit `isError: false`. A failed, denied, or
  status-less completion does not set it, and a bash command is never inferred
  to be an edit.
- `first_test_candidate_seconds` — a bash command that looks like a test
  invocation. This is a **candidate**, not a test result: the command may have
  failed, timed out, or run no assertions. It never means tests passed.
- `last_activity_seconds` / `last_tool_completion_seconds` — last observed
  prompt-relative activity / last completed tool call.
- `tool_seconds` — aggregate across tool calls and **can overlap** (parallel
  calls), so it is a coarse busy-time figure, not wall-clock.
- `retry_seconds` — automatic **agent-turn** retry time, including in-flight
  time before an abort; compaction retries are not counted.
- `retry_count`, and `checkpoint_notices` (a list of the fixed codes actually
  sent on this run: `investigation_checkpoint`, `first_delivery_checkpoint`,
  `wrap_up_checkpoint`; `[]` simply means no checkpoint was due yet, e.g. a short
  run that cannot reach the floors).

Distinguish observation from outcome:

- Tool/read/write/test-candidate timing shows *activity*, not deliverable.
- A run can stream many read-only tool events and still produce no usable
  artifact; streaming does not prove effective progress.
- A failed or timed-out run still retains the observations made before the
  failure. Use them to locate the slow stage, never to infer quality.
- Confirm real progress from the actual diff, changed files, and focused-test
  evidence, independent of the telemetry.

`scripts/review_worker_runs.py` aggregates the optional block per worker:
observation counts for each numeric field, medians for stage durations, retry
totals, and bounded counts of checkpoint and timeout codes. It never emits raw
contracts, commands, paths, or error text.

## 2. Checkpoints: implemented as soft steering

**Implemented.** The runner proposes at most one due checkpoint at a time as a
queued Pi `steer` on the task's RPC budget, and records the code it actually
sent in `progress_metrics.checkpoint_notices`. A checkpoint is a reminder to
reassess, not a kill switch, not a pause, and not proof of progress.

| Checkpoint | Fires at | Reassess |
|---|---|---|
| `investigation_checkpoint` | min 180 s or 20% of budget | Is the problem understood? Is there a concrete plan before more editing? |
| `first_delivery_checkpoint` | min 300 s or 35% of budget | Is there a reviewable increment yet? |
| `wrap_up_checkpoint` | 70% of budget | Stop new exploration, finalize evidence and the summary. |

- A `steer` is **queued and delivered after the current assistant turn finishes
  its tool calls**: it never interrupts a running tool, and the Pi docs do not
  promise delivery either. The runner therefore never awaits a response — a Pi
  build (or a test fake) may legitimately ignore it — and records a notice only
  when the request actually reached the worker's stdin. Re-check the receipt
  rather than assuming delivery.
- Each code is proposed at most once per run, even though time keeps passing.
- For read-only tasks, do not demand a diff: the task cannot be told to write.
- For write tasks, saving incremental work is **mandatory**, so a timeout still
  leaves a reviewable artifact instead of only tool telemetry.
- Checkpoints are advisory. A missed checkpoint is a prompt to narrow scope, not
  an automatic failure or an acceptance signal.
- The global `--timeout` and the per-tool bound are the only hard bounds; a
  checkpoint never fires either.

## 3. Split dependent stages; parallelize only disjoint work

Split a large package into `investigation -> implementation -> verification/docs`
stages. Later stages depend on the earlier snapshot, so run them in order and
give each stage the frozen artifact it needs; do not launch dependent stages in
parallel.

- **Decompose proactively, then delegate the routine work.** The main keeps the
  product and architecture decisions and hands over the bounded implementation,
  focused tests, and docs packages instead of writing them after it has already
  investigated. One package carries one narrow outcome and one check.
- **Sizing is a focus target, not a file-count rule.** Typical packages are 1-3
  files; aim for a focused change worth roughly 2-5 minutes of worker effort, and
  split anything wider before launching. Never treat a wall-clock deadline as a
  substitute for a smaller scope: narrow the contract instead.
- **Bounded fixes go back out.** When review finds a bounded defect, delegate it
  again with the same delegation id and a narrowed contract rather than doing the
  worker's job in the main out of impatience. The only main-side exception is
  explicitly authorized tiny emergency work, which must be recorded as
  `main_rework` on the review event so the report stays honest.

- Parallel workers are only for **independent and disjoint** scopes and
  resources; the default concurrency cap is **3**
  ([parallel-workers.md](parallel-workers.md)). The existing at-most-one rule
  already lets a disjoint batch preserve its scopes; do not overlap writers.
- Give full tests, screenshots, and environment setup an explicit per-tool
  timeout with `--tool-timeout-seconds` (default **180 s**; `0` disables the
  bound). A known lengthy build may opt into a longer override; unlimited is
  never the default.
- Implemented CLI defaults: `--tool-timeout-seconds 180`, `--max-auto-retries 2`,
  `--retry-budget-seconds 120`. Prefer fixing the slow stage over simply raising
  the overall `--timeout` (the old 15-20 minute blanket bump hides the bottleneck).
- The two retry bounds are **active**: the `(count + 1)`-th `auto_retry_start`
  fails the run with the sanitized code `retry_limit` (stage `rpc`), and reaching
  the aggregate observed retry time fails it with `retry_budget_exceeded`. Both
  end the run through the same bounded graceful abort as a per-tool timeout and
  both are enforced with or without a progress dict. Only agent-turn retries are
  counted (compaction retries are not). `--max-auto-retries 0` rejects every
  automatic retry; `--retry-budget-seconds 0` disables the *time* bound only.
  The time bound is the aggregate busy time of observed `auto_retry_start`/
  `auto_retry_end` spans, in-flight included, so overlapping retries can exceed
  wall-clock, a silent retry still trips it, and unrelated streaming cannot
  postpone it.
- A single running tool that exceeds the bound fails the run with the sanitized
  code `tool_timeout` (stage `rpc`) after the same bounded graceful abort used
  for the global timeout. The message carries only the configured bound — never
  a tool name, call id, arguments, or output — and the global `--timeout` still
  reports `timeout`, not `tool_timeout`.
- Implemented retry telemetry: `progress_metrics.retry_count` counts
  `auto_retry_start` events for the whole run and `progress_metrics.retry_seconds`
  is the aggregate observed retry time (in-flight included). One automatic-retry
  *episode* spans its `auto_retry_start` to its `auto_retry_end`, so the
  cumulative figure can be smaller than the sum of all network time in the run.
  A `auto_retry_end` with `success: true` clears a recovered prior provider
  error; a failed retry end leaves it in place.

## 4. Timeout recovery and retry discipline (retry bounds + digest gate active)

The automatic-retry count and time bounds are enforced (see section 3 and
`retry_limit` / `retry_budget_exceeded`), and the same-contract retry gate is
enforced too. Both rules below are live behaviour, not a proposal.

- After a timeout, the main agent first inspects the existing diff and evidence
  (including `progress_metrics` and `rpc_event_counts`), then narrows the
  contract — smaller scope, fewer files, tighter acceptance checks, or a smaller
  per-tool budget — before retrying with the same delegation ID and the pinned
  standard profile. There is no automatic blind same-contract loop.
- **Gate:** every terminal receipt records `assignment.contract_digest`, the
  SHA-256 hex digest of the exact contract text handed to the worker, taken
  before any intercom appendix is appended and before launch (the file is read
  once, so the digest always describes the sent bytes). The runner inspects the
  **latest prior actual RUN record** for that delegation (lifecycle, review, and
  notification records are ignored; a schema-v2 run written before
  `record_type` existed still counts; only records with the caller's own
  `record_kind` are considered). If that run failed with `timeout` or
  `tool_timeout` and recorded the same nonempty digest, the launch is refused
  with `RunnerError(code="unchanged_timeout_retry", stage="routing")` before
  reserving routing, before provider preflight, before the Pi process, and before
  any ledger or lifecycle write — `--validate-only` enforces the same gate with
  no side effects. There is no automatic rerun.
- **Permitted without the flag:** an older receipt with no digest (legacy runs),
  a prior *non-timeout* failure, a delegation with no prior run, and a **revised**
  contract under the same delegation ID (which stays pinned to the same profile).
  An old timeout that a later successful run has already superseded does not
  block anything: only the newest actual run is inspected, and no record is
  mutated.
- **What the digest does not prove:** equality is byte-text equality of the
  assignment only. An identical digest is not evidence that the scope, checks,
  or budget are better than the run that timed out. Rewording the contract
  without narrowing anything passes the gate and proves nothing.
- **Override:** `--allow-unchanged-timeout-retry` (default off) allows exactly
  that unchanged retry. It changes no model, profile, or routing class, does not
  change sticky identity, and is recorded as
  `assignment.unchanged_timeout_retry_override: true` in the terminal receipt
  whether or not the gate would otherwise have fired. Disclose the override and
  its reason to the user when you use it; no justification flag exists. This
  override applies to the single runner's flags only.
- Non-timeout provider failures may retain the same contract and there is no
  automatic model switching. Whether such a failure is *free* (unbilled, not
  counted against any quota) is **not guaranteed**: do not assume it. Re-running
  the same delegation ID reuses the pinned profile.
- A worker never delegates nested workers.

## 4b. Retention: the ledger only holds the configured window (14 local days by default)

Run-record files are pruned to the configured `record_retention_days` window — a **14-local-calendar-day** window by default (today plus the previous 13; a configured positive `N` keeps today plus the previous `N-1`, and `0` keeps everything). So `--days 30` or any wider report window can only ever show what the configured window still holds — with the default that is 14 days of ledger history, the extra range empty rather than missing by accident. Do not assume a full month of evidence is available.

- Pruning is automatic and happens after every successful ledger append (run, review, lifecycle, notification) under the append lock, not only on a worker launch, so appending your own review can prune as well; there is no cron daemon. The public helper `prune_record_files(directory=None, today=None, dry_run=False, retention_days=None)` in `scripts/run_pi_worker.py` does a one-off manual cleanup (`dry_run=True` mutates nothing; an explicit `retention_days=0` disables that call, and omitting it uses the configured window); there is no CLI prune flag.
- Practical effect on retry discipline: the same-contract timeout digest gate can only see prior runs that are still retained. An aged-out prior timeout is invisible to the gate, so the gate protects less than the ledger's whole history — never treat "the gate did not fire" as proof that no earlier timeout existed.
- A cross-partition lookup can lose its target to pruning and report an orphan review or unknown run. That is a retention effect, not necessarily a bookkeeping bug.
- History beyond the window is **irrecoverable**; copy anything that must outlive the configured window (14 days by default) out of the ledger before it ages out. Full rules (what is never deleted, dry-run API) are in `references/run-record-schema.md`.

## 3b. Quiet communication: what not to send

Cheap delegation is also a communication cost. Default to silence unless the
message changes someone's decision.

- Main reads the compact handoff plus the receipt, and opens diffs only when the
  verdict needs them. Do not reread a whole file or a full log repeatedly, and do
  not treat a worker's summary as independent verification — confirm from the
  diff and the focused check yourself.
- On an ordinary `READY` or routine progress message the main sends **no echo**
  user update and no extra worker acknowledgment. Pre-launch disclosure and the
  actual model once the runner confirms it stay mandatory — announce a batch once
  rather than once per child. Blockers, a confirmed model change, and final
  acceptance stay mandatory too.
- Quiet workers keep exactly one short `READY` that names the exact supervisor
  proof they were required to send, and stay silent otherwise; they send an
  actionable `ask` or `PROGRESS` only when the main asked for one.
- `DONE` is fixed-size: at most **1200 characters**, relative paths only, check
  summaries, known limits, and only the IDs the main already knows. `RUN_FINISHED`
  from the runner is the authoritative terminal signal, so a worker sends no
  duplicate completion ack and no bulky recap. The main never sends a second
  "done" acknowledgment for a settled worker.

## 3c. Recording what the main itself spent

The review helper accepts six optional main-side counters per review event:
`--main-briefing-count`, `--main-review-count`, `--main-recovery-count`,
`--main-diff-bytes`, `--main-input-tokens`, and `--main-output-tokens`. Each is a
non-negative integer measured **for that review event only**, or omitted/null when
unknown; the block appears only when at least one value is supplied.

- Record the counts you actually know. If main-side token attribution is not
  available, omit the token flags — an invented number is worse than an unknown
  one, and unknown is reported as `null`, never 0.
- These are manual main-attribution counters, not an automatic measurement: the
  runner cannot observe the main's own tokens. They describe the main's work
  around the review, not the worker's usage.
- `--main-diff-bytes` is the main-side diff volume it actually read, which is
  usually smaller than the whole diff. Do not report the total diff size as if
  the main read all of it.
- They are bookkeeping, not a token-saving claim: `gpt_tokens_saved` stays `null`
  unless separately measured, and no report derives a per-model cost winner from
  these numbers.
- To see one feature across several newly delegated phases, give every phase's
  run the same `--task-group-id '<opaque-id>'` (manifest `task_group_id` for a
  batch child). The group then aggregates across delegation ids and models; it
  never claims the feature is logically complete or first-pass clean, because
  phases declare no dependency graph.

## Invocation sketch (Pi-main must use intercom and launch async)

```bash
# workdir: the project the worker will edit
workdir="$(pwd -P)"
# derive the actual host-reported skill root first: set skill_dir to the
# directory containing the loaded SKILL.md for this installation
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
logs="$(cd "$(mktemp -d)" && pwd -P)"
python3 "$skill_dir/scripts/run_pi_worker.py" \
  --workdir "$workdir" \
  --contract-file "$workdir/.pi-worker/contract.txt" \
  --routing-class standard --profile auto --thinking medium --tools write \
  --delegation-id 'stable-unique-id' --timeout 900 \
  --transport intercom \
  --intercom-extension "$HOME/.pi/extensions/intercom/index.ts" \
  --supervisor '00000000-0000-0000-0000-000000000000' \
  --receipt-file "$logs/terminal-receipt.json" \
  >"$logs/receipt.json" 2>"$logs/error.log" < /dev/null &
```

The supervisor value is a placeholder: use your own exact full UUID, and point
`--intercom-extension` at the absolute path of the extension you installed.

This is the invocation that works today. `--tool-timeout-seconds`,
`--max-auto-retries`, and `--retry-budget-seconds` can all be added; the
same-contract retry gate of section 4 is enforced unless
`--allow-unchanged-timeout-retry` is passed deliberately:

```bash
workdir="$(pwd -P)"
# derive the actual host-reported skill root first: set skill_dir to the
# directory containing the loaded SKILL.md for this installation
: "${skill_dir:?Set skill_dir to the directory containing the loaded SKILL.md}"
logs="$(cd "$(mktemp -d)" && pwd -P)"
python3 "$skill_dir/scripts/run_pi_worker.py" \
  --workdir "$workdir" \
  --contract-file "$workdir/.pi-worker/contract.txt" \
  --routing-class standard --profile auto --thinking medium --tools write \
  --delegation-id 'stable-unique-id' --timeout 900 \
  --tool-timeout-seconds 180 \
  --max-auto-retries 2 \
  --retry-budget-seconds 120 \
  --transport intercom \
  --intercom-extension "$HOME/.pi/extensions/intercom/index.ts" \
  --supervisor '00000000-0000-0000-0000-000000000000' \
  --receipt-file "$logs/terminal-receipt.json" \
  >"$logs/receipt.json" 2>"$logs/error.log" < /dev/null &
```

Add the line below only for a disclosed, deliberate retry of a byte-identical
contract after a timeout; it is never part of the normal command:

```bash
  --allow-unchanged-timeout-retry
```

The enforced bounds today are the overall `--timeout`, the default 180 s
per-tool bound (`0` disables it), the automatic-retry count/time bounds
(`--max-auto-retries 2`, `--retry-budget-seconds 120`), the same-contract retry
gate, and the soft checkpoints; no metric, flag, or notice guarantees
deliverable quality.
