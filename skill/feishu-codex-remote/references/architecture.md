# Architecture And Acceptance Invariants

Use one narrow path:

`Feishu event -> durable idempotent inbox -> serialized Codex CLI run -> direct reply or private document`

Maintain `chat_id -> canonical working_directory -> persistent thread_id`. A group rename never changes this mapping. One project may have multiple intentionally isolated groups, but one group never selects a project from message text.

Use one verified compatible Feishu application across additional groups by default. Gate any new app behind a sanitized candidate inventory and explicit incompatibility evidence. Do not run independent group-specific long-connection clients against one app unless each client can safely route every event it may receive; prefer the shared dispatcher. Preserve working legacy apps until a separate migration is authorized and accepted.

## Reliability

- Real-time long-connection events are the normal path.
- Startup, reconnect, manual sync, and hourly automatic inspection reconcile missed messages.
- Each scheduled inspection reads the current durable `processing` batch without waiting for it. The default three-line report places elapsed time and the latest safe user-visible Codex update in its first line; a customized inspection with that report disabled sends one hourly-idempotent progress-only message while active. Idle and opted-out projects receive no progress-only message.
- A trusted user-level Codex `SessionStart` Hook starts the demand-only Listener on session startup/resume; it never replaces unrelated hooks or creates OS login startup.
- Hook installation does not alter project-level hourly inspection, Token usage reporting, reminders, or other scheduled work. Those remain independently configured per workstream.
- Queue by Feishu `message_id`; ignore duplicate, bot, and system messages.
- Claim atomically, retry visibly, and retain terminal failures for diagnosis.
- Do not impose a gateway wall-clock timeout on the serialized `codex exec` process. Multi-hour work is valid; wait for Codex to exit or report its own terminal failure.
- Preserve pending work through reloads. Drain current work before graceful restart.
- Queue interaction contract: wait for 15 seconds of silence from the same group and sender before claiming an ordinary prefix. Persist immediately; new user messages reset the unstarted wait, duplicates and bots do not. Waiting continues while another batch runs, so a ready backlog has no extra wait, default message-count cap, or adjacent-time cutoff. A first non-whitespace `*` ends the preceding wait, runs alone, and starts a new waiting prefix afterward. Preserve source IDs and conflicting response-mode separation. Do not claim new work while a batch is processing. Pending messages take precedence over separately retried historical failures. Reconstruct waiting from durable receipt timestamps after restart; use platform time only for legacy records without a receipt timestamp.

## User-Visible State

- Pending: no reaction.
- Processing: add `Typing` only for the atomically claimed batch immediately before Codex processing, after preparing attachments; queueing, waiting, and download preparation do not add it.
- Completed: remove the exact `Typing` reaction and add `CheckMark`.
- Failed: remove `Typing`, do not add `CheckMark`, and send a visible failure.

All user-visible requests, clarifications, results, failures, retries, and artifact links stay recoverable in Feishu. Progress snapshots may reuse a user-visible `agent_message` or a generic event category, but hidden prompts, commands, reasoning, raw tool output, internal IDs, and local paths do not.

## Acceptance

Test Chinese and English direct replies and system messages, default `full-access` command construction (`danger-full-access` plus `approval_policy="never"` before `resume`), both downgrade modes, first-run risk disclosure, private-document routing and closed sharing, publisher fallback, duplicate events, burst ordering, offline recovery, Listener restart, SessionStart Hook merge/trust flow, first inspection idempotency, the recurring three-line report and Token fallback, non-blocking active-task progress with hourly deduplication and safe redaction, continuous/one-time reminder delivery and completion, images/files, persisted-thread follow-up, tenant isolation, directory-scope instructions, Windows Task Scheduler, macOS `launchd`, Windows DPAPI, macOS Keychain, and both read-only context bridges. Native-image acceptance must use a real Feishu image message with `im:resource` (or the current upload equivalent); a document fallback or mocked upload does not satisfy this gate.
