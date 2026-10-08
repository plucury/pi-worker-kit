# Parallel worker batches (independent, disjoint subtasks)

Run several Pi workers concurrently with
`scripts/run_pi_worker_batch.py` when subtasks are **independent and
disjoint** (no write/write or write/read scope overlap, no shared exclusive
resources). One child worker per subtask, one `delegation_id` per child; the
main agent keeps final review of every child receipt. No nesting: a child
never launches further workers. Use a single worker instead whenever subtasks
are coupled.

## Manifest (schema version 1)

```json
{
  "version": 1,
  "tasks": [
    {
      "id": "t1",
      "delegation_id": "parallel-subagents-t1",
      "workdir": "/absolute/path/to/alpha",
      "contract_file": "/absolute/path/to/contracts/t1.txt",
      "routing_class": "standard",
      "profile": "auto",
      "thinking": "medium",
      "tools": "write",
      "scope": ["src/a.py", "tests/test_a.py"],
      "resources": [],
      "timeout": 900
    },
    {
      "id": "t2",
      "delegation_id": "parallel-subagents-t2",
      "task_group_id": "user-feature-001",
      "workdir": "/absolute/path/to/alpha",
      "contract_file": "/absolute/path/to/contracts/t2.txt",
      "tools": "read-only",
      "scope": ["docs/design.md"],
      "timeout": 900
    }
  ]
}
```

Rules: `version` is exactly `1`; task `id` and `scope` entries are required and
unique; scopes are relative to `workdir`, absolute paths, `..`, globs, and
symlink escapes to other workdirs are rejected; overlaps are compared after
canonical absolute resolution, including ancestor/descendant and symlink
aliases across different workdirs. Resolve paths to canonical absolute form
before writing the manifest (for example with `pwd -P`), so an alias such as a
symlinked temporary directory cannot look disjoint. `resources` keys are
exclusive: two tasks listing the same key are rejected. Read/read overlaps are
allowed. Routing uses the user's profiles file like every other launch: there are
no built-in profiles, and `profile: "auto"` means a uniform draw among the
enabled `sub`-role entries of the standard class. Unsupported routing classes
(including the removed `simple` class) are rejected. `routing_class` here is a
launcher/CLI field of the manifest; a profiles row declares roles instead.

`task_group_id` is an optional opaque string recorded as
`assignment.task_group_id` on every child run. Use one id for all phases of a
single user-visible feature even when each phase gets its own `delegation_id`;
the batch must not invent ids, and tasks without the field stay ungrouped.

## Sizing, dependencies, and timeouts

Only independent, disjoint subtasks belong in one batch. Split a larger package
into `investigation -> implementation -> verification/docs` stages and run the
dependent stages in order against frozen snapshots; never parallelize a stage
that consumes another stage's output. The default concurrency cap is 3.

Decompose more proactively in the main: it owns the product and architecture
choices and delegates the routine implementation, tests, and docs work instead of
doing them itself after investigating. Keep one narrow outcome and one check per
package. Typical packages are 1-3 files, but that is a signal, not a hard
file-count rule: aim for a focused change of about 2-5 minutes of worker effort
rather than a blind deadline kill, and split anything wider before launching.

Give full tests, screenshots, and environment setup an explicit per-tool
timeout with `--tool-timeout-seconds` (default **180 s**; `0` disables it), and
opt into a longer bound only for a known lengthy build; unlimited is never the
default. A tool that exceeds the bound fails its child with the sanitized code
`tool_timeout`. The automatic-retry bounds are active: `--max-auto-retries 2`
(the `(count + 1)`-th agent-turn `auto_retry_start` fails the child with the
sanitized code `retry_limit`) and `--retry-budget-seconds 120` (aggregate
observed retry time, in-flight included, fails it with
`retry_budget_exceeded`); `0` rejects every retry / disables the time bound
only. After any child times
out, the main inspects the existing diff and evidence and narrows the child
contract before a same-delegation retry; the runner enforces that in the single
runner by blocking a byte-identical contract digest after a timeout with the
sanitized code `unchanged_timeout_retry` unless
`--allow-unchanged-timeout-retry` is passed deliberately with a user-visible
reason. That flag belongs to the single-runner invocation; a batch child uses
the runner defaults and does not accept the flag through its own task schema. See
[worker-efficiency.md](worker-efficiency.md) for the full rules.

## Invocation

- Default concurrency cap: **3** (limit is `min(3, task count)`); override with
  `--max-concurrency <positive int>`. With more tasks than slots the batch
  queues and slots are reused.
- Dry-run: `--validate-only` checks the manifest, output dir, and every child
  argv through the real runner without creating the output directory.
- Pi-main invocation is **explicit and async**: `--transport intercom
  --intercom-extension <path> --supervisor <full-uuid>` (plus the usual
  `--intercom-cli` / `--pi` if desired) with `</dev/null &`; Pi-main cannot use
  plain RPC transport. RPC hosts omit intercom flags entirely.
- Output: a fresh (nonexistent or empty) `--output-dir`; the batch writes
  `batch-summary.json` and one subdirectory per task.

## Receipts

Each child writes its own private terminal receipt (default
`<output>/<task-id>/terminal.json`). A batch exit code of 0 only means all
children **completed execution** — it is never acceptance. Every run is
`pending_main_review` until the main agent reviews each child's diff and
record; rejected or rework-requiring children get delegated again. Unchecked
child exit paths are classified (`unconfirmed` for missing/malformed/
wrong-delegation/duplicate-run receipts, `failed` for nonzero exit or watchdog
timeout); pending tasks left in the queue are `not_started`.

## Reading a batch back cheaply

Announce a batch **once** before launch, then report the actual confirmed models
when the runner confirms them instead of echoing per-child updates. The main
reads the compact per-child handoff plus receipt, and reads diffs on demand; it
does not repeatedly reread whole files or full logs, and a worker's summary is
never a substitute for the main's own verification. A bounded fix the main finds
is delegated again — only explicitly authorized tiny emergency work stays in the
main, and that work must be recorded as `main_rework` on the review event.
