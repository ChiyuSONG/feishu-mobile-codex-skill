# Architecture And Acceptance Invariants

Use one narrow path:

`Feishu event -> durable idempotent inbox -> serialized Codex CLI run -> direct reply or private document`

Maintain `chat_id -> canonical working_directory -> persistent thread_id`. A group rename never changes this mapping. One project may have multiple intentionally isolated groups, but one group never selects a project from message text.

Use one verified compatible Feishu application across additional groups by default. Gate any new app behind a sanitized candidate inventory and explicit incompatibility evidence. Do not run independent group-specific long-connection clients against one app unless each client can safely route every event it may receive; prefer the shared dispatcher. Preserve working legacy apps until a separate migration is authorized and accepted.

## Reliability

- Real-time long-connection events are the normal path.
- Startup, reconnect, manual sync, and hourly automatic inspection reconcile missed messages.
- A trusted user-level Codex `SessionStart` Hook starts the demand-only Listener on session startup/resume; it never replaces unrelated hooks or creates OS login startup.
- Hook installation does not alter project-level hourly inspection, Token usage reporting, reminders, or other scheduled work. Those remain independently configured per workstream.
- Queue by Feishu `message_id`; ignore duplicate, bot, and system messages.
- Claim atomically, retry visibly, and retain terminal failures for diagnosis.
- Preserve pending work through reloads. Drain current work before graceful restart.
- Coalesce a short ordinary burst without losing source IDs. Keep forced isolation and conflicting response modes separate.

## User-Visible State

- Pending: no reaction.
- Processing: `Typing` reaction.
- Completed: remove the exact `Typing` reaction and add `CheckMark`.
- Failed: remove `Typing`, do not add `CheckMark`, and send a visible failure.

All user-visible requests, clarifications, results, failures, retries, and artifact links stay recoverable in Feishu. Hidden prompts, reasoning, and raw tool logs do not.

## Acceptance

Test Chinese and English direct replies and system messages, private-document routing and closed sharing, publisher fallback, duplicate events, burst ordering, offline recovery, Listener restart, SessionStart Hook merge/trust flow, first inspection idempotency, the recurring three-line report and Token fallback, continuous/one-time reminder delivery and completion, images/files, persisted-thread follow-up, tenant isolation, directory isolation, Windows Task Scheduler, macOS `launchd`, Windows DPAPI, macOS Keychain, and both read-only context bridges.
