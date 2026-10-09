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

## Execution And Recovery Contract

`scripts/task_contract.py` owns the shared generation/recovery instructions and
result receipt format. `run_codex` loads this contract for every run; project
prompts reference it rather than maintaining competing copies. Applicable domain
criteria remain project-owned; do not install another reviewer or nutrition rules.
For existing project reviewers, forward criteria, evidence and prior findings
together. Distinguish objective invariants from flexible semantic judgments.

Codex plans compound work inside its turn. The gateway preserves source order,
not a new intent classifier or separate model call per subtask. A partial result
uses the supplied per-run `outcome.json`: completed, failed, dependency-blocked
work, evidence, readable cause and next step. Recovery receives the prior result;
incomplete tasks retain the existing attempt budget and get no completion mark.
Fully completed replies need no extra receipt or review call. This is an execution
report, not independent proof: the Agent must verify effects with project tools.
Restricted agents can use the transport fallback defined in `task_contract.py`;
the gateway removes its control block before delivery. Do not broaden permissions
just to save an outcome receipt.

Before network delivery, the store saves the output, source batch and hash.
Delivery-only recovery reuses the output without another model turn; new messages
cannot merge into that prepared reply. Completion is committed for the whole batch
before best-effort reaction updates; late failures cannot downgrade it. Terminal
failure notices use the durable lifecycle outbox and the last source-message anchor.
The user sees the failed phase, known completed/unfinished/blocked portions and
next step. Say when the detailed cause is unknown instead of inventing one.
Optional safe drafts are labeled unapproved; raw exceptions stay local.

`OUTCOME_FIELDS` in `task_contract.py` is the source for prompt field types and
both file/transport validation. Present malformed fields are reported together;
legacy receipts may omit optional fields. Do not force ordinary successful
answers into a new schema or add a model call merely to validate transport.
If a usable safe draft does not exist, explain that via
`draft_unavailable_reason`; never copy raw logs into user-facing failure text.

For an explicitly requested repair of a different failed message, use the local
read-only `recovery-context --project-key <key> --message-id <original>` command.
It reads only specified originals from that registered inbox, including their
source fingerprint and prior completed effects. Do not scan sibling projects or
turn historical findings into new requirements. The optional `resolutions` field
is defined by `task_contract.py`; bind each claim to the unchanged original and
current verification artifacts. The original becomes completed only after the
repair's final result is delivered and the linked state change is committed.
Unrelated successes, a code fix alone, or a stale test log never resolve it.
These checks establish provenance and integrity, not independent semantic truth:
actual effects and any required domain review remain the project's responsibility.

Terminal failures project `CrossMark`; pending/provider-wait/maintenance states
do not. Remove the failure mark on requeue, and replace it with `CheckMark` only
after verified original-task recovery and delivery. Persist reaction receipts;
reconcile missing/error receipts after restart without repeating completed work.
Main and branch failures use the existing lifecycle outbox with stable identity.
Cancel an unsent failure notice when its original is no longer failed.

Background work without a source message uses the same Listener-owned outbox,
not a fabricated source ID or a second state writer. The internal `task-notice
--project-key <key> --key <stable-run-identity> --text-file <safe-text>` command
persists a request; it reports queued, not delivered. Omit `--message-id` for
background notices. Keep existing schedules, notification preferences and domain
behavior unchanged. Never include raw exceptions or private diagnostics in the
text file. Notification failure alone must not be called business-task failure.

## Native Provider Failures

`scripts/provider_failure.py` classifies only the final native JSONL outcome.
Intermediate errors allow Codex's native retry to finish; a later success wins.
Terminal `at_capacity`, `rate_limit`, model transport and workspace-routing failures return the exact work to Pending, clear
Typing, retain its thread and attachments, and consume no ordinary failure attempt.
The gateway adds no outer provider retries or model switching. A transient internal
`provider_wait` prevents immediate busy-loop retries; normal startup/reconnect,
manual or hourly reconciliation clears it even when no new messages are pulled.
Send one stable, retryable unavailable notice, never a completion mark or resend
request. Authentication failures are identified safely but retain the ordinary
failure budget: a 401 alone does not prove permanent credential expiry and does
not introduce an unlimited retry or mandatory manual-unlock policy.
Genuine execution failures keep their existing bounded recovery budget;
this rule must not reset them indiscriminately. Pending branches stay resumable,
not archived. Native retry latency must be explained from actual event evidence.

## Listener Crash And Original Run Ownership

`scripts/run_ownership.py` records the exact batch, run ID, Listener identity,
launch intent, event/final paths and child PID plus OS creation identity in the
existing message store. A restart never turns all processing work into Pending.
The existing control cycle observes surviving original processes; it neither
starts a duplicate model nor kills a long-running task. Windows process handles,
macOS libproc start identity and Linux boot/start identity distinguish PID reuse.

An exact live child retains ownership. A stopped original with a verified native
completion, matching thread and nonempty final artifact may resume the existing
review/delivery path without another model turn. This is saved as
`recovered_model_result`, not a review-approved `prepared_reply`. An incomplete
result preserves its evidence and resumes the existing execution path and budget;
it must not become a permanent delivery-only retry. A verified prepared reply follows
the same path. Only a durable claim known not to have submitted a model may resume
execution. Missing or ambiguous identity/terminal evidence remains outcome-unknown
for diagnosis; do not manufacture success or blindly replay effects. Main/branch
identity and maintenance ownership remain intact. Closed admission may safely
hand off a verified-ready claim during reload; live/unknown/resuming owners still
block quiescence, and snapshots keep their actual processing status. This adds no runtime deadline,
retry budget or extra scheduled process.

## Formal Reply Receipts And Attachment Integrity

`scripts/delivery_receipts.py` checkpoints the unchanged answer delivery plan,
uploaded keys/private document link and remote message IDs on the existing reply
anchor. Text, all chunks, image/file replies, document links and fallback warnings
require valid remote receipts. Delivery-only retry skips confirmed parts and keeps
the original UUID/payload for unconfirmed parts; the model is not rerun. A failed
send or local checkpoint after document preparation cannot switch the same UUID
to a different fallback text. Completion requires every planned receipt.

Remote creation and local persistence are not one atomic transaction. A crash
after a remote effect but before its receipt is saved still depends on Feishu UUID
deduplication for message retry; upload/document creation has no caller UUID here.
Reuse saved resource IDs, but do not promise perfect exactly-once remote creation
or invent receipts for historical deliveries.

`scripts/attachment_archive.py` keeps the current download timing and per-message
location. Downloads verify declared length, publish complete same-volume temporary
files without replacing another writer, and store immutable identity/SHA256/size
receipts. Verified cache works offline. A conflicting or corrupted resource is
retained as evidence and rejected as model input, not silently overwritten. Old
unverified files stay available to old run references; they are not trusted as
verified new downloads. No eager-download policy, new storage service or business
review gate is added.

## Persistent Thread Writer Conflicts

Treat a persistent-thread writer conflict as an ownership diagnostic, not permission
to erase or replay the inbox. Preserve thread ID, queue, cursor and run evidence;
identify whether the existing owner is active, stale or outcome-unknown. Do not
kill unrelated processes or automatically replace the conversation. If rebinding
is necessary, use maintenance, preserve recovery evidence and obtain explicit
approval before replacing the main thread. Resume only unfinished work after
checking actual effects and verify ordered reply delivery.

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

## Regression Maintenance

Keep this section as the maintenance-test owner; project acceptance criteria
remain in their existing canonical sources. Use the existing test suite and
case catalog rather than creating a parallel policy or testing framework.

- When a behavior changes, review its code, prompt, references and tests together.
  Replace obsolete assertions with current behavioral checks; do not preserve an
  old restriction merely to keep a test green, or delete a failing test without
  preserving coverage of the still-required capability.
- Hard checks cover objective transport/state invariants, such as receipt types,
  source identity and artifact integrity. Synonyms, explanatory labels and item
  meaning belong to an existing project semantic reviewer, not a new keyword,
  alias or item-count veto in the gateway. Retain both equivalent-expression
  positives and genuinely incomplete/conflicting negatives. Transport tests
  establish lossless handoff, not semantic correctness.
- For a real failure, retain the original input and relevant pre-run state in the
  project's private test assets: source order, grouping/entrypoint, attachments,
  prior committed effects and code/criteria version. Reconstruct only necessary
  state and disclose material differences from production. Never publish private
  replay data or credentials; public fixtures are explicitly synthetic.
- Test small components before affected integration/replay paths. Run the complete
  fast suite for package releases. For costly project model replays, prioritize
  changed behavior, recent/repeated failures and representative workflows; follow
  the approved task scope and budget. A changed semantic prompt needs multiple
  representative positive/negative cases, not a phrase-presence assertion or a
  single successful answer.
- Record code/criteria versions, selected cases, omitted coverage and outcomes.
  Separate examples used for tuning from unseen acceptance. Distinguish component
  mocks, real local subprocess tests, isolated model replay and live delivery;
  one does not prove the others. Do not relabel failed, skipped or unrun checks
  as passed. Fix obsolete fixtures without weakening their required assertions.

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
| Native singleton ownership, functional shared health and SDK callback wiring | `test_listener_control.py`, `test_transport_recovery.py`, `test_functional_transport.py` |
| Durable per-group reconciliation, realtime/history race and component recovery | `test_catchup_health.py` |
| Startup/running DNS outage and endpoint 503: automatic connection, fresh application PONG, durable catch-up and unchanged live work without manual wake-up | `test_network_flap_recovery.py` (real local HTTP/WebSocket/protobuf; fixture-only shortened SDK cadence) |
| Dead worker recovery, active owner preservation and unknown outcomes | `test_worker_health.py` |
| Quiet window, leading/trailing star barriers, hash priority, attachments and retained source IDs | `test_queue_batching.py` and `test_star_separators.py` |
| Maintenance arrival/stop/restart/notice retry; temporary cleanup | `test_gateway_lifecycle.py` |
| Checkpoint/communication exceptions, stop failure, real hidden child reaping and subsequent execution | `test_gateway_lifecycle.py` |
| Partial result, prepared reply replay, late failure, acceptance evidence and notification receipts | `test_task_contract.py` |
| Failure reactions, schema errors, original-task resolution, no-source notices, crash/restart and stale evidence | `test_generic_recovery.py` |
| Wording-preserving delivery, declared omissions and prior review handoff (transport only) | `test_generic_recovery.py` |
| Listener death with live child, PID reuse, ambiguous outcome, retained final, main/branch and maintenance | `test_run_ownership.py` |
| Terminal model network/routing/authentication classification and native subprocess replay | `test_provider_failure.py`, `test_provider_transport_failure.py`, `test_provider_worker_recovery.py` |
| Partial reply receipts, lost local completion, media/document/fallback and restart without model replay | `test_delivery_receipts.py` |
| Real local HTTP short read, offline attachment reuse, concurrent immutable publication and corruption | `test_attachment_archive.py` |
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
After running the applicable regression and exit/catch-up tests, record their
evidence in a local acceptance JSON. Pass it to `notify-complete` using
`--acceptance-file <path>`. Required fields are `release_id`, `maintenance_id`
(the current maintenance ID, or null if no maintenance was needed), `scope`,
empty `remaining_work` and `conflicts`, `catch_up_verified: true`, and `checks`.
Each check contains `passed: true`, an `artifact` path relative to this JSON and
that file's `sha256`. Include changed-source fingerprints and test logs, not just
a prose assertion. `required_message_ids` is optional: list only messages that
the repair explicitly must complete, never unrelated new arrivals. The Listener
checks artifact integrity, generation and required message completion before
queuing the notification. This validates evidence integrity, not the truth of
arbitrary test claims; the maintainer remains responsible for meaningful tests.
New maintenance cancels an unsent obsolete completion notice. Exit still uses
the existing catch-up/worker wake-up path, without another worker or scheduled job.
Skill source updates alone do not authorize changing live schedules or profiles.

Prefer `maintenance exit --project-key <key> --release-id <version>
--acceptance-file <path>` when acceptance is ready: the Listener first completes
catch-up, then durably stores exit and completion intent together. Its existing
lifecycle loop restores a missing outbox entry after a crash. A plain exit is
still supported when no completion claim is ready; it does not invent acceptance.
`notify-complete` remains available after later verification. Before sending or
retrying completion parts, recheck the same acceptance-file hash, artifact hashes,
maintenance generation and required original outcomes. Changed evidence holds the
completion notice without blocking unrelated failure notices. Never relabel an
old acceptance file to approve different inputs or code.

This reuses the transactional-outbox/idempotent-consumer approach rather than a
new scheduler, external queue or model-based recovery service. Reference:
https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html


## Listener Lifecycle Regression Gate

For every relevant change, rerun the complete suite above, not only the formerly
failing case. Retain the tests with the package. `test_patrol_entrypoint.py`
executes the real Windows wrapper against isolated Scheduler/child fixtures
(including spaces and non-ASCII paths): stopped/running startup, denied access,
trigger rejection, nonblocking inspection, manual drain and error propagation.
`test_startup_reactions.py` verifies independent project startup and durable
completion-reaction reuse; missing/error receipts stay repairable. On non-Windows
hosts report Windows-specific skips, not Windows acceptance.

The Windows launch chain uses a demand-only Task Scheduler action pointing to
`pythonw.exe supervisor.py`, with hidden children and durable diagnostic output.
The sync/reload wrappers share `windowless_process.ps1` and wait for the actual
child output/exit status. A native process-lifetime lease also excludes consumers
started outside Task Scheduler; duplicate `run` must exit before opening workers.
The Windows and macOS SessionStart entrypoints queue all bound groups for startup reconciliation even if the launcher is already running. Ordinary sync asks gateway_transport.Client for a fresh same-generation Feishu application PONG, then checks worker/component ownership and retained catch-up failures. Successful REST history alone cannot acknowledge functional health. `transport-health.json` is PID- and timestamp-bound diagnostic evidence of that check; an old receipt is not current health and even a fresh application PONG is not full user-message/model/reply proof.
SDK reconnect callbacks request asynchronous history reconciliation for all groups,
independently of hourly opt-outs. Do not substitute direct worker launch after an access-denied error. Retain exact
launcher ownership checks and one shared consumer. Repeated starts must preserve
active work; macOS kickstart must not use the destructive `-k` option.

Before declaring a live patrol repair complete, pair each scheduled turn's own
permissions, timestamps, command, result and final status. A UI preference, a
config file, a different turn or a maintenance agent's probe does not prove the
patrol's effective permission. Verify the exact Hook's current trust state.
After safe drain and confirmed child exit, test actual scheduled recovery from a
stopped Listener for each affected independent patrol. Observe one process tree,
reconciliation acknowledgement, asynchronous progress handling and no visible
console window. An earlier empty-queue snapshot is not authority to kill a
worker. If controlled recovery fails, restore the existing launcher and report
failure; do not add another consumer, permission bypass or schedule.

`command_invocation.py` owns the generated shell transport. Windows uses the
documented UTF-16LE PowerShell `EncodedCommand` from a noninteractive, hidden
`cmd.exe` invocation; literal arguments avoid cross-shell quoting ambiguity.
The prompt retains the readable command plus exact tool arguments. This changes
transport only, not permission or failure handling. `test_command_invocation.py`
executes a real command with Unicode, spaces and shell-special characters and
checks argument and exit-code preservation; do not reinstate arbitrary prompt
length caps that discard the executable contract. Reference:
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1

Keep per-run logs, source fingerprints and acceptance receipts outside the Skill.
Document live vs simulated coverage and platform limits. Updating package code
alone must not rewrite existing project Automations, models, permissions,
reporting modes or domain-owned systems. Source changes to provisioning defaults
apply to new registrations, not retroactive enforcement on existing projects.

### Functional reception recovery

`gateway_transport.py` adapts pinned lark-oapi without editing its installation. Endpoint discovery runs off-loop with the existing gateway HTTP connect/read inactivity timeout; this is not a DNS/total wall-clock guarantee. Application PING/PONG verification uses the existing WebSocket Pong budget and the server heartbeat/reconnect cadence. Only one receiver and reconnect owner operate per connection generation. Durable event callbacks complete before ACK; a callback from an old connection cannot ACK onto a replacement.

`gateway_recovery.py` retains per-project catch-up intent in `catchup-recovery.json`. Only complete history pagination plus durable enqueue commits the existing history cursor. Realtime arrivals never advance it. One failed group does not discard other groups or consume recovery intent; an in-flight scan cannot erase a newer reconnect revision. Retry cadence follows the SDK, without a second automation or consumer.

Existing control polling supervises worker ownership. Dead workers reuse their Store and queue, while live main/branch tasks stay intact. A processing message with no live owner is reported as outcome_unknown, not automatically replayed. Auxiliary startup reaction repair still precedes that project worker startup; its failure now always releases startup, and it does not block other projects or the shared receiver. Slow live business calls are not killed solely for elapsed time.


### Shared core for independent Listeners

`gateway_transport.py`, `gateway_recovery.py` and `gateway_primitives.py` form the generic transport/recovery source. The primitives module owns the existing HTTP connect/read inactivity timeout and atomic JSON helpers; importing this core must not load a project registry, credentials, user directories or business workers. `gateway_common` retains its existing public helper names by re-exporting them.

Independent Listeners may deploy the same verified core as a fixed-version managed copy with its upstream source, version and SHA256 manifest. Treat the selected Skill as the authoritative generic implementation: return generic fixes here and selectively port to the public edition, then update consumers through their own validation and safe deployment. Do not dynamically load an unverified editing tree or maintain a second divergent SDK repair. Each consumer retains its own singleton, configuration, credentials, durable state, queue, admission rules and domain logic.

The thin project adapter wires reconnect callbacks to durable catch-up intent and wakes its existing recovery/worker loop. Only full history pagination and durable message/resource acceptance may acknowledge that revision; a normal return containing failed records is not complete recovery. A newer reconnect during an in-flight pull must remain pending. Maintenance may continue reception and catch-up while admission still prevents model execution. Keep the SDK/server reconnect cadence and the existing project execution rules.

Acceptance must run the common real local HTTP/WebSocket/protobuf transport scenarios against the deployed core and verify the consumer's own callbacks, persistence and admission adapter. Shared Store fixtures alone do not prove an independent consumer. Check startup and running DNS failure, endpoint failure, fresh application PONG, receiver death, generation-isolated ACK/PONG, partial catch-up failure, repeated reconnect during catch-up, and no duplicate work or new model launch during closed admission. Record each consumer's source hashes, SDK versions, isolated results and live receipt separately.

Catch-up completion uses publish-before-clear: a failed state write retains the pending revision in memory and on disk. A newly requested intent that cannot be written remains visible as desired work in memory and the error propagates; it must not be labeled durable. The adapter must wake its existing recovery loop in a finally block even on that failure, so it retries through the established cadence without a manual poke. Restart still performs the existing unconditional startup reconciliation; an unsuccessful disk write cannot guarantee persistence through a process crash. See `test_recovery_commit.py` and the failed-request wakeup case in `test_catchup_health.py`.
