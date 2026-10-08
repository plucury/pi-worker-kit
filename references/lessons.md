# Operational Lessons

General operational lessons distilled from past pi-worker runs, written so a
future routing or review decision does not have to rediscover them. They are
stated generally on purpose: no private project names, account identifiers, or
per-project measurements appear here. Keep this file stable and
evidence-shaped; update it when a new sample contradicts a lesson.

## Read records as measurements, not prices

Receipt `cost_usd` is a nominal per-token figure, not the real bill: plans,
discounts, and provider tiers do not show up in it. A cheaper-looking worker is
not automatically a cheaper run. Never make routing decisions by comparing
per-token prices across profiles, and never present a receipt number to the user
as a saving.

Judge a worker on bounded-package fit: completion, rework rounds, interruption
rate, and wasted runs.

## Reusable lessons

1. **Escalate on waste, not on verdict alone.** A run that burns a large
   fraction of its budget and returns no deliverable (no changed files, no
   verification evidence) is a stronger signal than a single negative review:
   reject it and re-delegate the same package with a tighter contract. Zero-output
   runs did not converge on a second attempt with the same contract, while
   converged ones nearly always returned an actual diff.
2. **Rework is normal when there is a real diff.** `needs_rework` reviews that
   included changed files usually converged on a second attempt; runs with no
   output and no evidence usually did not. Re-delegate the latter with a narrowed
   contract instead of retrying unchanged.
3. **Package size matters more than file count alone.** Effective delegations
   were 1-3 files with a deterministic check. Larger packages repeatedly timed
   out or reworked. Split before delegating; file count is a signal, not a rule.
4. **Timeouts happen; reuse the delegation id.** After a timeout, retrying the
   same delegation id on a pinned profile converged, given a realistic
   `--timeout` for the package size. See lesson 11 before retrying an unchanged
   contract.
5. **Provider errors are free retries.** A run rejected for a provider-side
   reason usually cost nothing and converged on a same-id rerun. Do not treat it
   as evidence about the model, and do not switch models over it.
6. **A usage limit retires one worker, not the run.** A
   `provider_usage_limit` failure auto-disables that profile for the cooldown
   window and re-draws pinned delegations among the remaining enabled workers of
   the same class, so one throttled provider does not stop the queue. Check
   `--show-profile-state` before calling a missing worker a configuration bug,
   and clear the entry with `--reset-profile-state <profile-id>` once the
   provider limit lifts. If everything is disabled, routing fails with
   `no_enabled_profile` — that is a configuration state, not a silent success.
7. **Observation is not outcome.** `progress_metrics` reports observed stages
   only: `first_test_candidate_seconds` is a bash-command heuristic and never
   means tests passed, and sustained read-only tool traffic is not a
   deliverable. Judge progress from the diff and focused-test evidence. A failed
   or timed-out run keeps the observations it made; use them to find the slow
   stage, not to score the worker.
8. **Use checkpoints to catch a stuck run, not to police it.** Soft checkpoint
   steering is implemented: notices fire at `investigation_checkpoint` (min 180 s
   / 20% of budget), `first_delivery_checkpoint` (min 300 s / 35%), and
   `wrap_up_checkpoint` (70%). They are queued `steer` messages delivered after
   the current turn finishes its tool calls, so they never interrupt a running
   tool and delivery is not guaranteed; the codes actually sent land in
   `checkpoint_notices` (`[]` means none was due). A read-only task cannot be
   told to write, so do not demand a diff from it; for write tasks, saving
   incremental work is mandatory so a timeout still leaves a reviewable artifact.
9. **Split dependent stages; parallelize only disjoint work.** Run
   investigation, then implementation, then verification/docs, in order against
   frozen snapshots. Parallel workers are for independent, disjoint scopes and
   resources only (default cap 3; see [parallel-workers.md](parallel-workers.md)).
   Give full test suites, screenshots, and setup builds an explicit per-tool
   timeout with `--tool-timeout-seconds`; a known lengthy build may opt into a
   longer value, never an unlimited default. The default is **180 s** and `0`
   disables the bound. The automatic-retry bounds are active: `--max-auto-retries
   2` and `--retry-budget-seconds 120` (aggregate observed retry time, in-flight
   included); the `(count + 1)`-th agent-turn retry fails the run with
   `retry_limit`, and an exhausted time budget with `retry_budget_exceeded`. Fix
   the slow stage instead of raising the overall deadline.
10. **A timeout retry needs a narrowed contract.** Inspect the existing diff and
    evidence first, then retry the same delegation id and pinned profile with a
    tighter contract; do not loop the same contract automatically. The runner
    enforces this: a byte-identical `assignment.contract_digest` after a timeout
    is rejected with the sanitized code `unchanged_timeout_retry` (stage
    `routing`, dry run included) unless `--allow-unchanged-timeout-retry` is
    passed deliberately with a user-visible reason. The gate is byte equality
    only — it never proves a reworded contract is a better scope — and only the
    latest actual run is inspected, so a repaired history never blocks new work.
    Non-timeout provider failures may keep the same contract with no automatic
    model switching, and a worker never delegates nested workers.
11. **Changing routing underneath a delegation is a silent change.** Profile
    identity is sticky per delegation. If you edit a profile's provider or model,
    use a new profile id and a new delegation id, otherwise a retry can land on
    a model you never reviewed. `auto` is a uniform draw among the *enabled*
    `sub`-role entries, so enabling or disabling a row changes the pool a later
    draw sees, and a main-only row is never drawn; a shared main/sub row enters
    the sub pool exactly once when enabled.
12. **The main's own spend is real but only manually attributable.** Nothing
    measures the main session's tokens, so main-side counters on a review event
    (`--main-briefing-count`, `--main-review-count`, `--main-recovery-count`,
    `--main-diff-bytes`, `--main-input-tokens`, `--main-output-tokens`) are
    numbers the main knows, not an instrumented total: leave a field out when it
    is unknown instead of inventing it, because a report with `null` totals
    reads *unmeasured* while a fabricated `0` reads *cheap*. `--main-diff-bytes`
    is the diff volume the main actually read, normally well under the total
    diff. These counters are bookkeeping — never a tokens-saved claim and never a
    basis for a per-model ranking.
13. **Group the phases of one feature, and read the group as a whole.** When a
    feature is delegated as investigation, implementation, verification, and docs
    with separate delegation ids, pass one opaque `--task-group-id` (manifest
    `task_group_id` for batch children) to every phase. Otherwise each phase looks
    like a brand-new first-pass success and the rework history disappears; with
    the group id the report aggregates across delegation ids and models. Even
    grouped, read `launches_by_status`, review-event counts, and cost coverage per
    phase — phases declare no dependency graph, so a group's last run proves
    nothing about logical completeness or first-pass quality. Retention windows
    also bound what a group can still show: pruned records are simply absent.
14. **Silence is the default channel.** One combined pre-launch disclosure (per
    batch, not per child), one model confirmation once the runner reports it, and
    then only blockers and the final acceptance message. Echoing ordinary
    `READY`/progress traffic back to the user, or acknowledging each worker, adds
    tokens without changing a decision. Workers send one short `READY` carrying
    the exact supervisor identity, only the asks they were asked for, and one
    `DONE` capped at 1200 characters with relative paths and check summaries —
    the runner's `RUN_FINISHED` is already the authoritative terminal signal, so
    duplicate completion acknowledgments and bulky recaps are pure overhead.
15. **Delegate the routine work; do not absorb it after investigating.** The
    cheapest main turn is one where a bounded package left the process. Routine
    implementation, focused tests, and docs still belong in a worker package even
    when the main already understands the problem; keep one narrow outcome and
    one check per package, treat 1-3 files as a signal rather than a hard rule,
    and aim for a focused change of roughly 2-5 minutes of worker effort instead
    of raising a deadline. Re-delegate bounded fixes instead of editing the
    worker's deliverable yourself, and record any authorized tiny emergency main
    edit as `main_rework` so the delegation record stays truthful.
16. **Test evidence must be isolated.** Run the project's offline suite in a
    scratch or virtual-environment context, keep fixtures free of real
    secrets, and never let a verification step spend tokens or hit a paid
    provider on the user's behalf. An offline suite that cannot run is not
    evidence.
