# Runtime Repair And Acceptance

Use this reference for gateway repairs and for porting verified fixes between
editions. It supplements existing queue and maintenance behavior, not product
scope or a project's domain rules.

## Maintain One Effective Implementation

- Inspect the current edition, baseline tests, affected entrypoints and existing
  policy owners before editing. Plan the smallest repair and its acceptance
  scenarios first; do not replace working features to make a new test pass.
- Port generic mechanics explicitly. Never copy project records, identifiers,
  nutrition logic, domain review gates, live state or credentials into the Skill.
  Preserve edition-specific platform, permissions, language and inspection policy.
- Reuse existing queue and lifecycle-notice stores. Code and references use local
  Git history; runtime claims use short store transactions, not a task-long lock.
  Do not use Git merges to coordinate live queue writes or create a second ledger.
- Change the existing owner and retire superseded rules in the same revision.
  Inspect code, prompts, Skill references and tests together, including hidden
  enforcement; conflict checks and real scenario regressions are release criteria.
  Keep historical evidence outside routine prompts. Before generation, supply
  the task's actual criteria and relevant current evidence, not a history dump.
  On rejection, preserve exact failure reasons and completed side effects in the
  handoff. Reuse verified work; do not invent success, repeat completed work, or
  create a new retry budget merely because the error text changed.
  Domain-specific review gates and model-routing budgets remain project-owned.

## Native Capacity And Quota Errors

`scripts/provider_failure.py` classifies only the final native JSONL outcome.
Intermediate errors allow Codex's native retry to finish; a later success wins.
Terminal `at_capacity` and `rate_limit` return the exact work to Pending, clear
Typing, retain its thread and attachments, and consume no ordinary failure attempt.
The gateway adds no outer provider retries or model switching. A transient internal
`provider_wait` prevents immediate busy-loop retries; normal startup/reconnect,
manual or hourly reconciliation clears it even when no new messages are pulled.
Send one stable, retryable unavailable notice, never a completion mark or resend
request. Genuine execution failures keep their existing bounded recovery budget;
this rule must not reset them indiscriminately. Pending branches stay resumable,
not archived. Native retry latency must be explained from actual event evidence.

## Persistent Thread Writer Conflicts

Treat an `active writer` or equivalent persistent-thread ownership error as a
thread diagnostic, not permission to delete or replay the Feishu inbox. Preserve
the current thread ID, queue state, cursor, run receipt and event log. First
identify the live owner and determine whether the previous turn is still active,
stale, or has an unknown outcome. Do not kill an unrelated Codex process, reset
the cursor, erase message history, or automatically create a replacement thread.

If a replacement thread is necessary, pause admission through the existing
maintenance lifecycle, retain a recovery copy of the old thread ID and state,
and require explicit user approval before rebinding the main conversation. Resume
only the messages whose prior outcome is known not to have completed, then verify
ordering and reply delivery before reopening admission. This conservative repair
prevents duplicated side effects and silent loss of conversation continuity.

## Paginated Fork Recovery

Try native `thread/fork` first. `excludeTurns=true` reduces the returned metadata
payload; it does not truncate inherited context.

Only the exact `-32603` pre-creation paginated-history ordinal rejection permits
the compatibility import in `scripts/codex_thread_fork.py`. All other RPC errors,
EOFs and timeouts keep the existing conservative outcome-unknown behavior.

`scripts/history_fork_snapshot.py` reads the exact parent index entry and a fixed
complete-line byte extent of its rollout. Validate identity and records; reject
unresolved `history_base` references. Change only the copied session's storage
mode to `legacy`, leaving original history and Codex databases untouched. Each
concurrent import has a unique local file under
`$CODEX_HOME/feishu-fork-snapshots` (default `~/.codex`), never in a project repo.
Delete that copy only after confirmed native fork success; retain ambiguous
imports for reconciliation, never blind-refork or substitute the main thread.

This compatibility path uses the experimental `thread/fork.path` API and the
local `state_5.sqlite` index layout. Verify the installed Codex schema when
upgrading Codex. If unsupported or inconsistent, preserve the request and report
the branch failure. Do not claim compatibility with every future Codex version.
Original native forks and main turns do not depend on this fallback succeeding.

## Acceptance Evidence

Run from the selected Skill root:

```text
python -m unittest discover -s scripts -p "test_*.py"
```

| Scenario | Executable coverage |
| --- | --- |
| Native retry then success; terminal capacity; unrelated/user text | `test_runtime_recovery.py` |
| Batch/branch capacity, no extra model call, next main claim | `test_runtime_recovery.py` |
| Failure notice offline/restart/dedup; stale run cannot overwrite new | `test_runtime_recovery.py` |
| Known fork rejection, saved context preserved, busy main unaffected, reply and archive | `test_runtime_recovery.py` |
| Partial/corrupt/referenced history, concurrent imports, ambiguous outcome | `test_runtime_recovery.py` |
| Native metadata protocol, timeout/EOF, hidden child process cleanup | `test_codex_thread_fork.py` |
| Ordinary/hash overlap, duplicate claims, bootstrap, reload | `test_parallel_hash.py` |
| Quiet window, star barriers, retained source IDs | `test_queue_batching.py` |
| Maintenance arrival/stop/restart/notice retry; temporary cleanup | `test_gateway_lifecycle.py` |
| Delivery, attachments, scope, context bridge and existing policies | `test_remote_gateway.py` and edition-specific tests |

Use isolated temporary fixtures. Distinguish simulated model/Feishu tests from
a real native Codex canary and real Feishu delivery. Test success does not prove
clean-machine onboarding, a different OS, or future upstream availability.

After a Codex upgrade, run `python scripts/check_fork_protocol.py --codex <executable>`.
It verifies native path-import and archive against synthetic history in a temporary
Codex home without credentials or model turns. Codex CLI 0.153.4 passed this probe;
an empty `thread/start` alone need not persist a source rollout, so it is not a
sufficient import fixture. Full gateway fault routing remains covered separately
by the deterministic tests above.

For major live maintenance, retain arrivals as Pending with an upgrade notice;
never complete them or require resending. `gateway_lifecycle.py` owns maintenance
state, durable notices and temporary-thread cleanup. Ordinary feature maintenance
ends after regression and exit/catch-up tests pass, then the existing Listener
reconciles and resumes the backlog. When repairing those very pending requests,
or when the user asks to process them during repair, include their completion in
acceptance and use one exclusive processing owner; never race a second worker.
Do not publish a completion notice until the whole approved scope is verified.
Use the existing `maintenance exit` and `notify-complete` actions, not extra jobs.
Skill source updates alone do not authorize changing live schedules or profiles.
