# Worker run records

Records are private append-only JSON Lines files under `~/.codex/worker-run-records`, partitioned by local calendar date. Appends use a global advisory lock and flush and sync before unlocking. Stable routing decisions remain in the private `.routing-state.json` sidecar.

## Schema v3

Every runner invocation that reaches routing appends one terminal event with `record_type: run` and `phase: completed`. A run contains identities and timestamps; worker and routing data; a hashed project ID; coarse assignment metadata; outcome, verification, usage, timing, failure, routing lesson, and aggregated `rpc_event_counts`. It never contains the contract, code, secrets, provider error text, or an absolute project path. `record_kind` is `production` (default) or `test`; the review report excludes `test` runs unless `--include-tests` is given.

The run initially has `review_verdict: pending`. Worker completion is not acceptance. After independent validation, `record_worker_review.py` appends a `record_type: review`, `phase: reviewed` event linked by `target_run_id`. Review verdicts map deterministically to results:

- `accepted` → `usable`
- `needs_rework` → `partial`
- `rejected` → `unusable`

Repeated reviews are allowed. Each event identifies the prior review with `supersedes_review_id`; readers apply the latest event and retain the immutable history.

### Optional `assignment.task_group_id`

A run may carry an opaque `assignment.task_group_id` string that ties several
runs of one user-visible feature together even when each phase has its own
`delegation_id` (investigation, implementation, verification, docs). The id is
opaque ASCII supplied by the main: readers group on it verbatim and never infer,
normalize, or synthesize a group id from a goal, scope, workdir, or time window.
Absent means ungrouped, and legacy records stay ungrouped — ungrouped runs are
counted, never merged into a synthetic bucket.

### Optional review-event `main_metrics`

A review event may carry one top-level `main_metrics` object with exactly these
six keys: `briefing_count`, `review_count`, `recovery_count`, `diff_bytes`,
`input_tokens`, `output_tokens`. Values are non-negative integers or `null`.

- The block is emitted **only** when the main supplied at least one value; when
  emitted, every one of the six keys is present and unsupplied ones are `null`.
- Every value measures **that review event only**. It is never cumulative and
  never inherited from an earlier review of the same run.
- These are the main agent's own counts for the work it did around this review
  (briefings written, review passes performed, recovery passes, and the main-side
  diff/token figures), supplied by the main through
  `record_worker_review.py --main-briefing-count`, `--main-review-count`,
  `--main-recovery-count`, `--main-diff-bytes`, `--main-input-tokens`, and
  `--main-output-tokens`. They are optional bookkeeping, not a measurement the
  runner can perform automatically.
- `null` means *unknown*, never 0. When main-side token attribution is not
  available, leave the token fields null instead of inventing values.
- Token attribution to the main session is manual and best-effort: it depends on
  the main recording counts it actually knows. `diff_bytes` is the main-side
  bytes it actually read, which is normally smaller than the total diff — it is
  not the size of the whole change.
- Nothing here replaces `assignment.gpt_tokens_saved`, which stays `null` unless
  separately measured. `main_metrics` is never presented as a token-saving claim.

Routing is sticky for ordinary retries. When `profile` is `auto`, the runner uniformly draws one of the enabled `sub`-role entries of the standard class from the user's profiles file (main-only, empty-role, and disabled rows never enter the pool), records `routing.random_policy: random_enabled_profile` and the 0-based `routing.random_draw` index into the eligible entries in configuration order, and stores the choice in the delegation sidecar so retries reuse it. An explicit `--profile` records `fixed_profile` with a null draw. This project ships no built-in profiles: which ids and models exist, and which roles they carry, comes entirely from the profiles file (`--profiles-file`, `PI_WORKER_PROFILES_FILE`, or `~/.config/pi-worker/profiles.json`), so a draw index only means something within one local configuration. An absent file, a fresh `{}` document, or a document with no enabled `sub` entry fails with `no_enabled_profile` rather than falling back to a default. Historical records and receipts naming removed or disabled profiles remain readable as data, but historical data is not execution config: a retry whose delegation is pinned to a profile that no longer exists is rejected as a removed profile with no silent redraw — start a new delegation ID, and change a profile's provider or model under a NEW profile id so identity stays sticky. The default routing class with no `--routing-class` argument is standard. `routing_class` is an internal launcher and CLI concept only; the profiles array carries roles, not routing classes.

Historical escalation markers (legacy data only): old records may carry `routing.random_policy: simple_failure_escalation`, `routing.escalated_from: simple-m3`, and a `previous_run_id` link from the former explicit simple-to-standard escalation (allowed only after the same delegation had a failed or interrupted `simple-m3` run, or its latest simple run had a rejected review). These fields are optional historical data: readers may still recognize and report them in old records, but the current runner produces no new `simple_failure_escalation` records and escalation routing is unsupported for new runs.

When a provider rejects a run for an account usage limit, the failure records `failure.code: provider_usage_limit` — sanitized wording only, never the raw provider message or HTTP body — and the runner auto-disables that profile in the private `profile-state.json` sidecar under the records directory. The failed run mirrors the decision in `routing.profile_auto_disabled` (`reason`, `disabled_at`, `expires_at`; a null expiry means the disable is cleared manually). A later run pinned to that profile is re-drawn among the remaining enabled profiles of the same class and records `routing.random_policy: auto_disabled_redraw`; routing fails with `no_enabled_profile` when the class has no profiles left.

Assignment metadata remains permissive. `assignment.metadata_quality` reports stable warnings—`generic_goal`, `missing_scope`, and `missing_acceptance_checks`—without blocking the worker. `gpt_tokens_saved` stays `null` unless separately measured. Failure records use sanitized category, code, and stage values rather than raw provider output.

## Optional progress telemetry

A receipt may carry an optional flat `progress_metrics` object. Older receipts
that omit it, and individual fields left `null`, remain valid; absent or `null`
means *unobserved* and is never rewritten to 0. Observed values are
prompt-relative and monotonic. Field meaning:

- `first_tool_seconds` — first tool call observed.
- `first_edit_seconds` — first **successful** `edit`/`write` completion only.
- `first_test_candidate_seconds` — a bash command heuristic for a test
  invocation; it is a candidate, not tests run or passed.
- `last_activity_seconds`, `last_tool_completion_seconds` — last observed
  activity / last completed tool call.
- `tool_seconds` — aggregate across tool calls and may overlap (parallel calls).
- `retry_seconds` — retry time including in-flight time before an abort;
  `retry_count` — number of retries.
- `checkpoint_notices` — a list drawn from the fixed codes
  `investigation_checkpoint`, `first_delivery_checkpoint`, `wrap_up_checkpoint`.

Two optional assignment fields support deliberate timeout retries:
`assignment.contract_digest` is a bare SHA-256 hex digest of the exact contract
text handed to the worker (no contract content, no path; taken before any
intercom appendix is appended) and `assignment.unchanged_timeout_retry_override`
is a boolean recording an explicit `--allow-unchanged-timeout-retry`. Both are
present on every terminal receipt, successful or failed, whether or not the gate
would have fired. A retry whose delegation's **latest prior actual RUN record**
(lifecycle/review/notification records ignored; legacy schema-v2 runs without
`record_type` still count; only the caller's own `record_kind` is considered)
failed with `timeout` or `tool_timeout` *and* recorded the same nonempty digest
is rejected before launch — dry-run included — unless the override boolean is
set from that flag. Legacy receipts without a digest are allowed, a non-timeout
prior failure is allowed, and a revised contract under the same delegation ID is
allowed and stays pinned. The digest proves byte-text equality only; it is not
evidence of a narrower or better scope. Timeout-class failures use the sanitized
codes `timeout`, `tool_timeout`, `retry_limit`, and `retry_budget_exceeded`; the
last two come from the active `--max-auto-retries` (default 2) and
`--retry-budget-seconds` (default 120) automatic-retry bounds. Records that
predate these fields stay readable, and old receipts are never rewritten. All of
this is observational; none of it proves deliverable quality or tests passed.

`scripts/review_worker_runs.py` aggregates the optional block read-only and
per worker: observation counts for every numeric field, stage-duration medians,
retry total count/time, fixed checkpoint-code counts, and timeout-cause counts
keyed by `failure.code`. It never emits raw contracts, commands, paths, or error
text, and workers without the block keep the previous summary shape.

The same script aggregates review-event `main_metrics` into `main_cost`. For each
run that has at least one attached review event it materializes
`main_cost.review_event_count`, `main_cost.observations` (per-key count of valid
observations) and `main_cost.totals` (per-key sum, `null` when nothing was
observed):

- Valid observations from **every** genuine review of the run are retained,
  including a later-superseded review's real cost; the latest review still
  decides the outcome, but measured history is never replaced by the verdict.
- A `review_id` recorded twice for the same run is one real cost event.
- Review events are counted independently of the final verdict: a run reviewed
  twice and finally accepted reports two events and one outcome.
- Boolean, negative, fractional, NaN, infinite, and non-numeric values are
  ignored as unobserved, never rewritten to 0; an integral float (e.g. `1500.0`)
  is accepted.
- Reviews of excluded records (`record_kind: test`, detected `fake-session`
  fixtures) never contribute, and unmeasured totals stay `null`, so a legacy
  uninstrumented report reads *unmeasured*, never cheap.

## Terminal receipt file (optional)

`--receipt-file <absolute path>` writes the same private UTF-8 JSON receipt object produced on stdout for every terminal run — success, failure, timeout, and interrupt. The destination is validated before routing reservation (including on `--validate-only`, which never creates it): `..` components and a symlink destination are refused; the path must be absolute and, when it exists, a regular file that is not group/other-writable; and each existing parent must be a directory that is not group/other-writable unless it is sticky. Parent symlinks are canonicalized under an explicit alias policy (macOS `/tmp` -> `/private/tmp`; pass the canonical path), so an alias is accepted only when its resolved target passes the same checks and is refused when the target is unsafe. At write time missing parents are created `0700`, the checks run again right before writing to narrow the validate/write race, and the receipt is written atomically with mode `0600`; pre-existing parents are never chmodded. A receipt-file failure never claims success and never masks the original execution failure; the append-only ledger remains authoritative, so `--receipt-file` is a convenience copy, not the source of truth.

## Lifecycle and notification events

Schema v3 also defines two coarse append-only event kinds linked to a run by `run_id`:

- `record_type: "lifecycle"`, `phase: "started"`, `event: "run_started"` — written once the receipt identity is reserved and before provider preflight. It records `created_at`, an ISO `deadline`, `deadline_scope: "preflight_included"` (the timeout budget starts at this event, so both the auth/catalog preflight subprocesses and the RPC worker are clamped to the same remaining budget), `worker_type`, `transport`, and `record_kind`. It carries no contract, path, or payload. A killed runner leaves this start evidence.
- `record_type: "notification"`, `phase: "finished"`, `event: "run_finished"` — written for intercom runs after the terminal ledger and receipt file, with `attempts`, `status`, `delivery` (`delivered`/`failed`), `delivered`, and the terminal-evidence flags `ledger_written`, `receipt_required`, and `receipt_written`. Only a zero-exit intercom CLI whose JSON has both `ok: true` and `delivered: true` counts as routing confirmation; a contradictory, malformed, or raw-text result never does. That is delivery, never acceptance or read acknowledgment. Notification is best-effort and never changes the run's execution status, and an uncatchable SIGKILL cannot notify. The evidence flags distinguish an authoritative terminal write from an unconfirmed one: any false flag means the ledger or a required receipt file is not durable, so the main must not assume a receipt exists and must match the receipt file's `run_id` against the notification to avoid trusting a stale previous receipt. Bounded retries (at most two attempts of at most five seconds) plus the stable `run_id` let the receiver dedup RUN_FINISHED notifications.

A bounded private checkpoint sidecar (`.checkpoints/<run_id>.json`, mode `0600`) records the last coarse RPC event name, an event count, a session id when known, and a timestamp. It is throttled, removed only when the terminal ledger record is durable, and survives both an uncatchable kill and a failed terminal ledger append for reconciliation. A bounded pre-prompt `get_session_stats` RPC captures the session id early when Pi supports it, so a failed or timed-out run can still preserve the actual `worker.session_id`; an unsupported or slow lookup falls back without failing the run and never runs before the intercom preflight or makes a model call. Failed runs preserve accumulated `rpc_event_counts`, `worker.session_id` when known, and `timing.worker_seconds` in the terminal receipt; no raw RPC payloads, free-form summaries, or paths are stored.

`--record-kind test` tags an explicitly test record so the default production report and reconciliation skip it; the default also skips narrowly detected `fake-session` fixtures (only the exact legacy `session_id: "fake-session"`), and `--include-tests` restores both while the counts stay in diagnostics.

## Retention: a 14-local-day window by default

Run-record files are kept for **`record_retention_days` local calendar days**, default `14`: today plus the previous 13, using the same local day appends use (`local_day()`, not UTC). A configured positive `N` keeps today plus the previous `N-1`, so a file is eligible for deletion only when the date in its exact `YYYY-MM-DD.jsonl` name is at least `N` days old; file mtime is never consulted. `0` disables pruning entirely and nothing is ever eligible. Retained files are still append-only and are never rewritten, reordered, or edited.

- **Automatic trigger, no daemon:** every successful ledger write — run, review, lifecycle, or notification — prunes once after the flush + fsync, under the same exclusive `.append.lock` flock as the append itself, with the current write's file protected. There is no cron job and no midnight process: the next write after a file ages out removes it. A backdated append (or a quiet period) leaves an aged-out file in place until the next write.
- **Manual cleanup:** `prune_record_files(directory=None, today=None, dry_run=False, retention_days=None)` in `scripts/run_pi_worker.py` is the public helper. With no argument it resolves the active record directory (so the same window applies to whichever ledger is live); `today` overrides the reference day; `retention_days` overrides the configured window for that one call, and an explicit `0` disables the cleanup for that call (nothing eligible, nothing deleted) while any other value uses the configured default when omitted. `dry_run=True` changes nothing and creates nothing (not even `.append.lock`); a missing directory returns an empty result without being created. It returns `{"removed": [...], "eligible": [...], "failed_count": N}` — filename lists only, never file contents — and a real prune takes the same exclusive lock so a concurrent append is never deleted. Deletion is best-effort; failures are counted, never reported as successes. There is **no CLI prune flag**; the helper is the only manual entry point.
- **What is never deleted:** state files, `.checkpoints`, lock files, receipts, subdirectories, symlinks, files with malformed or future-dated names, and anything not named exactly `YYYY-MM-DD.jsonl`. Only regular, non-symlink, direct-child files are scanned; the directory is never traversed.
- **Cost:** history older than the configured window (14 days by default) is **irrecoverable** once pruned. Keep anything that must survive outside the ledger before it ages out.
- **Readers:** reports and the retry digest gate see only retained history. A cross-partition lookup may miss an aged-out target and surface it as an orphan review or unknown run, and the digest gate can only consider the newest prior run **that is still retained** — an aged-out prior timeout is not seen and therefore does not block a retry.

## Compatibility and reporting

Schema v2 entries are legacy run records. Readers accept them without requiring `record_type` or `phase`, preserve their pending review state, and never infer acceptance. The new `lifecycle` and `notification` event kinds are recognized and attached to their run (latest notification materialized) instead of inflating unknown-record diagnostics. Unknown versions, invalid entries, and orphan reviews are excluded from run metrics and surfaced as diagnostics.

Date filters select runs by invocation date and then apply the latest known review, even when that review was appended later. Reports distinguish worker completion rate from main-agent acceptance rate and separately count pending reviews, rework, missing usage, completed runs with zero usage, and incomplete assignment metadata. `--reconcile` adds a read-only lifecycle view that reports started runs past their recorded deadline without a terminal receipt as stale/unconfirmed, completed runs pending review, failed or interrupted runs without closure review, and missing/failed terminal notifications for intercom runs that carry the new lifecycle markers. It never rewrites the ledger, never relabels a failed run as completed or rejected, and never treats a legacy run that predates lifecycle markers as a known notification delivery failure. Historical `fake-session` fixtures and `--record-kind test` runs are excluded from default metrics and counted separately; `--include-tests` includes them. Because the lifecycle view has no run-level filters, `--reconcile` fails clearly when `--worker`, `--status`, or `--project` is supplied rather than silently ignoring it. Reconciliation never edits historical ledger entries. Reconciliation output contains only compact IDs, statuses, dates, and ages.

## Lifecycle reporting

`scripts/review_worker_runs.py` summarizes records at two granularities and they answer different questions. Run-level acceptance counts each completed receipt individually; a delegation that needed one rework before being accepted contributes one accepted run and one needs_rework run. Delegation-level metrics group runs by `delegation_id` and treat the chain as a unit:

- `delegations.total` counts distinct delegations (a run without a usable `delegation_id` is its own delegation keyed by `run_id`, so unrelated legacy runs never merge).
- The final state comes from the last run in input order: a completed run uses its effective `review_verdict`; a completed run with no terminal review is `pending_review`; otherwise the state is `failed` or `interrupted`.
- `delegations.accepted`, `needs_rework`, `rejected`, `pending_review`, `failed`, and `interrupted` count delegations by their final state.
- `delegations.first_pass_accepted` increments only when the delegation's first run is completed and effectively accepted.
- `final_acceptance_rate` and `first_pass_acceptance_rate` use `total` as the denominator; both are `null` when there are no delegations.

A delegation can be finally accepted while still failing first-pass acceptance when one rework was needed. Use run-level acceptance to measure per-receipt outcomes and delegation-level metrics to measure delegation quality.

## Measured main cost in the report

Per worker, `workers.<type>.main_cost` appears only when that worker has review
events, and reports `review_events`, `runs_with_reviews`,
`runs_with_observations`, the fixed per-key `observations` and `totals` (null
totals mean unobserved), plus the existing run-level proxies `main_rework_runs`,
`unresolved_reviews` (no terminal verdict yet), and `timeout_runs`. All existing
summary fields, counts, and rates are unchanged. The text report adds **one
compact line per worker, only when observations exist** (`main_cost <worker>:
events=… briefing_count=1/1 …`), where `total/observations` is shown per key and
`unmeasured` marks a key with no observation. No model ranking, winner, or
overall cost verdict is generated from it: these are the main's own counts, and
they never include a path, contract, diff, command, or error text.

## Task groups in the report

`task_groups` is always present: `group_count`, `ungrouped_runs`, and `groups`
keyed by the opaque `assignment.task_group_id` verbatim. Each group reports
`runs`, distinct `delegations`, `models` plus the bounded `worker_types` list,
`review_events`, `accepted`/`needs_rework`/`rejected`/`unresolved`, `reworked`,
`timeouts`, `launches_by_status` (every observed launch and its status),
`runs_with_observations`, and the fixed `observations`/`totals` cost pairs.

A group aggregates across delegation ids and models, so several new delegation
ids inside one group are phases of the same work — not several fresh successes
and not a history reset. Because phases declare no dependency graph, the report
never claims a group is logically complete or first-pass clean from its last run;
read `launches_by_status` and the per-phase counts instead. Cost totals cover
only what was measured, and `runs_with_observations` states that coverage
explicitly. Runs with no explicit group id stay ungrouped and are only counted.

## Largest runs

The summary also exposes `largest_runs`, one compact descriptor per metric (`cost_usd`, `total_tokens`, `worker_seconds`). Each descriptor contains only `run_id`, `delegation_id`, `worker_type`, and the metric value, so it surfaces coarse resource outliers without revealing the contract, code, assignment goal, changed files, provider errors, or absolute paths. The descriptor is `null` when no valid observation exists. Boolean, negative, NaN, and infinite values are ignored; on ties the first run in input order wins.
