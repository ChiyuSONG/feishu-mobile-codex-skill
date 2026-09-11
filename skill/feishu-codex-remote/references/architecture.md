# Architecture And Acceptance Invariants

Use one narrow path:

`Feishu event -> durable idempotent inbox -> serialized main turn or one-shot # branch -> direct reply or private document`

Maintain `chat_id -> canonical working_directory -> persistent thread_id`. A group rename never changes this mapping. One project may have multiple intentionally isolated groups, but one group never selects a project from message text.

Use one verified compatible Feishu application across additional groups by default. Gate any new app behind a sanitized candidate inventory and explicit incompatibility evidence. Do not run independent group-specific long-connection clients against one app unless each client can safely route every event it may receive; prefer the shared dispatcher. Preserve working legacy apps until a separate migration is authorized and accepted.

## Reliability

- Real-time long-connection events are the normal path.
- Startup and manual sync reconcile missed messages. A confirmed SDK reconnect wakes an immediate reconciliation for every bound group; the existing hourly inspection remains the periodic catch-up fallback only for workstreams where it is enabled. Reconciliation does not start Codex when no new message is found. Serialize concurrent history pulls without holding the lock during task execution.
- Each scheduled inspection reads the current durable `processing` batch without waiting for it. Both full and status-only reports place elapsed time and the latest safe user-visible Codex update in the status line (see [Automatic Inspection](../SKILL.md#automatic-inspection) for registration defaults); a customized inspection with that report disabled sends one hourly-idempotent progress-only message while active. Idle and opted-out projects receive no progress-only message.
- A trusted user-level Codex `SessionStart` Hook starts the demand-only Listener on session startup/resume; repeated starts are idempotent and never kill an already-running Listener, and the Hook never replaces unrelated hooks or creates OS login startup.
- Hook installation does not alter project-level hourly inspection, Token usage reporting, reminders, or other scheduled work. Those remain independently configured per workstream.
- Queue by Feishu `message_id`; ignore duplicate, bot, and system messages.
- Claim atomically, retry visibly, and retain terminal failures for diagnosis.
- Do not impose a gateway wall-clock timeout on the serialized `codex exec` process. Multi-hour work is valid; wait for Codex to exit or report its own terminal failure.
- Preserve pending work through reloads. Close main and hash admission under the store lock, drain active runs, finish atomic queue writes, and gracefully restart. Reconcile retained arrivals at startup; reopen admission if reload fails.
- Queue interaction contract: wait for 15 seconds of silence from the same group and sender before claiming an ordinary prefix. Persist immediately; new user messages reset the unstarted wait, duplicates and bots do not. Waiting continues while another batch runs, so a ready backlog has no extra wait, default message-count cap, or adjacent-time cutoff. A first non-whitespace `*` ends the preceding wait, runs alone, and starts a new waiting prefix afterward. Preserve source IDs and conflicting response-mode separation. Do not claim another ordinary main batch or inject messages into an active turn. Hash branches bypass the ordinary quiet window and never join its batch. Pending messages take precedence over separately retried historical failures. Reconstruct waiting from durable receipt timestamps after restart; use platform time only for legacy records without a receipt timestamp.

A first non-whitespace `#` copies the current main context into one persistent child for that message and runs independently alongside main and other hash work. Strip only the leading marker; retain attachments and `/doc` or `/direct` routing. Keep the main thread mapping unchanged. Register before launch to prevent duplicate claims; retries reuse confirmed children. If thread creation may have been submitted but its result is unknown, preserve the exact message for reconciliation instead of creating another child or resuming the parent. Confirmed pre-submission failures retain ordinary retry behavior. The metadata RPC helper has a configurable 30-second deadline, separate from unlimited model-turn duration; branch discovery uses event wakeups and one-second local polling.

For native capacity failure and the narrowly scoped history-fork compatibility
path, use [runtime-recovery.md](runtime-recovery.md). Its acceptance matrix covers
terminal notices, concurrent recovery and maintenance alongside the baseline.

## User-Visible State

- Pending: no reaction.
- Processing: add `Typing` only for the atomically claimed batch immediately before Codex processing, after preparing attachments; queueing, waiting, and download preparation do not add it.
- Completed: remove the exact `Typing` reaction and add `CheckMark`.
- Failed: remove `Typing`, do not add `CheckMark`, and send a visible failure.

All user-visible requests, clarifications, results, failures, retries, and artifact links stay recoverable in Feishu. Progress snapshots may reuse a user-visible `agent_message` or a generic event category, but hidden prompts, commands, reasoning, raw tool output, internal IDs, and local paths do not.

## Acceptance

Test Chinese and English direct replies and system messages, default `full-access` command construction (`danger-full-access` plus `approval_policy="never"` before `resume`), both downgrade modes, first-run risk disclosure, private-document routing and closed sharing, publisher fallback, duplicate events, burst ordering, offline recovery, Listener restart, SessionStart Hook merge/trust flow, first inspection idempotency, the recurring full report and Token fallback, additional-project status-only reports including idle state and no usage query, non-blocking active-task progress with hourly deduplication and safe redaction, continuous/one-time reminder delivery and completion, images/files, persisted-thread follow-up, tenant isolation, directory-scope instructions, Windows Task Scheduler, macOS `launchd`, Windows DPAPI, macOS Keychain, and both read-only context bridges. Native-image acceptance must use a real Feishu image message with `im:resource` (or the current upload equivalent); a document fallback or mocked upload does not satisfy this gate.
