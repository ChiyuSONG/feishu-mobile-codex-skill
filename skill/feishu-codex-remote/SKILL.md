---
name: feishu-codex-remote
description: Connect an explicitly selected local project to a private Feishu group so the user can send ordered mobile tasks to a persistent local Codex thread, recover messages after offline periods, receive complex results as private Feishu documents, and use a default hourly automatic inspection. Use when the user asks to connect, configure, operate, inspect, repair, or uninstall Feishu Remote Codex on Windows or macOS. Do not trigger merely because Feishu or Codex is mentioned.
---

# Feishu Remote Codex

Provide a small, understandable mobile entrance to local Codex. Keep setup automatic, keep normal use conversational, and never expose internal project keys, thread IDs, credentials, or private paths unless diagnosis requires them.

## Start From One Request

Accept a request such as:

> 把当前项目连接到我的飞书。尽可能自动完成，只在需要我扫码、登录或授权时提醒我。

Treat the current canonical working directory as the project to bind. Never infer or switch to another directory from Feishu message text.

Before changing the machine, say briefly what will happen and that the user may need to scan a Feishu QR code. Then follow [setup.md](references/setup.md). Concentrate unavoidable human actions—QR login, consent, CAPTCHA, tenant-admin approval, or secure secret entry—at the beginning.

## Reuse The Feishu Application First

Before creating an application, inventory known user-level gateway and publisher registries, installed Listener configurations, protected credential metadata, and self-built applications in the selected personal tenant. Verify tenant, ownership, bot and long-connection capability, document delivery, required scopes, credential availability, and Listener topology. A new project or group is not a reason to create a new app.

Reuse one verified compatible app for additional groups whenever possible. Create another app only after recording why every candidate is unsafe or insufficient, or when the user explicitly requests isolation. Do not automatically consolidate already working legacy apps during unrelated setup, and do not attach independent group-specific long-connection clients to one app unless every client can route every event it may receive.

## User Experience

- Bind by stable `chat_id -> canonical working_directory -> persistent Codex thread_id`. The Feishu group name is display text only and may be renamed safely.
- Let the user send consecutive messages without waiting. Preserve each Feishu `message_id`, process durably and idempotently, and serialize work per group.
- Leave messages in Feishu while the computer is offline. Reconcile them when the Listener reconnects or automatic inspection runs.
- Reply directly for simple results. Send explicitly linked in-project images as native Feishu image replies and explicitly linked PDF/Excel deliverables as native Feishu file replies; use the bundled private Feishu publisher for long or structured results and fallback artifact links, then send a concise group summary and link. If publication, attachment delivery, or privacy hardening fails, keep the text result and state the artifact failure instead of silently dropping it.
- Keep normal interaction in natural language. Internal `/doc` and `/direct` overrides may remain available for diagnosis, but do not make users learn them.
- When the user asks how to use the system, summarize direct tasks, offline queueing, private documents, automatic inspection, reminders, Token plan reports, and how to pause or resume.

## Automatic Inspection

Enable one hourly Codex Automation by default unless the user opts out. Call it `自动巡检`, not heartbeat, in user-facing text.

Each run must:

1. start or confirm the demand-start Listener;
2. request startup/reconnect reconciliation;
3. let the Listener process backlog asynchronously;
4. send the default compact inspection report unless the user has changed its content.

Send the formal first-inspection explanation exactly once. On later unchanged runs, send one compact Feishu message containing exactly three short lines: current Listener/message state, Token plan usage, and a reminder that natural language can change the inspection or add reminders and other uses. Keep reminders off until requested. If the user changes the inspection through conversation, honor the new content instead of restoring the default.

Explain that inspection runs only while the computer and Codex are available and that each run consumes Codex usage. Let the user change, pause, resume, or disable it through natural language. Do not promise a schedule the installed Codex Automation cannot represent.

Install the user-level Codex `SessionStart` Hook from `scripts/install_startup_hook.py` after the Listener launcher exists. Match `startup|resume` and call the idempotent Listener start command, so opening or resuming a Codex session does not wait for the hourly inspection. Never bypass Codex Hook trust review, overwrite unrelated hooks, or describe this as OS login startup. Keep the hourly inspection for reports, reminders, and reconciliation.

Treat Hook installation as a lifecycle-only change. Start every already registered project worker through the shared Listener, but never create, enable, disable, or rewrite a project's hourly inspection, usage report, reminder, or domain-specific scheduled task while installing the Hook.

Reminders are an optional use, not a dashboard. When implementing a reminder, keep it in a separate local append-only log, preserve explicit start time and time zone, and keep it active until completion or cancellation unless the user asks for one-off/pre-event behavior. Never import private records or reminder rules from another project.

Translate an unambiguous natural-language reminder into `python scripts/reminders.py add --project-key <key> --text <text> --start-at <ISO-8601-with-offset> [--mode continuous|once]`. Use `list` before completing or cancelling when the user's reference could match more than one reminder. Never infer a missing date, clock time, or time zone from message delivery time. Automatic inspection sends due reminders and records delivery; history remains append-only.

## Platform Behavior

Keep the Python gateway common across platforms.

- Windows: retain the existing `%LOCALAPPDATA%` state layout, DPAPI secrets, PowerShell wrappers, and demand-start Task Scheduler launcher.
- macOS: use `~/Library/Application Support`, macOS Keychain, the Python sync wrapper, and a demand-start per-user `launchd` agent.
- Reuse compatible Python 3.10+ and create the Skill-owned virtual environment with `scripts/bootstrap.py install`. If Python itself is absent, disclose the system package-manager/admin change before installing it.
- Never replace the tested Windows path merely to make macOS code look uniform. Dispatch at the platform boundary and run Windows regressions after every portability change.

## Language Behavior

Keep Chinese as the default and protect its existing welcome, inspection, reminder, and document-link quality. During setup, select `zh-CN` for a Chinese request and `en` for an English request. For ordinary remote turns, reply in the language of the current Feishu message or the language the user explicitly requests; do not force the stored setup language onto a user's later message.

## Context And Safety

- Do not mirror Desktop chats into Feishu or Feishu chats into Desktop automatically. Use the scoped `desktop-context` or `feishu-context` bridge only when the user explicitly asks to continue or retrieve work from the other surface.
- Keep credentials and runtime state outside the repository in the OS-specific user data directory. Store secrets in DPAPI or Keychain; never print or commit them.
- Verify the selected Feishu tenant before creating chats, sending messages, or publishing documents.
- Keep generated documents private. Close link sharing, read the setting back, and withhold the URL if verification fails.
- Prohibit public endpoints and anonymous/public links by default.
- Stop for tenant/account admin elevation, cross-project or cross-tenant access, credential disclosure, disabling core protections, irreversible history destruction, bulk/out-of-scope deletion, or a new external paid lifecycle.
- Allow routine work inside the explicitly bound project under its existing Codex approval policy.

## Conversational Uninstall

Treat natural-language requests such as `卸载飞书远程 Codex`, `断开当前项目`, or their English equivalents as uninstall intents. Read [setup.md](references/setup.md#uninstall) and use `scripts/uninstall.py`; do not improvise deletion commands.

Always preview the exact project binding, Automation, Hook, launcher, Skill directory, and local data affected. Refuse to continue while target messages are pending or processing unless the user explicitly asks to abandon them. A normal uninstall removes active integration components but preserves a local recovery backup. Delete credentials, queues, logs, or other local state only when the user explicitly asks to clear all local data. Never delete Feishu conversations, groups, or generated documents.

Read [architecture.md](references/architecture.md) for queue, routing, reaction, reload, and acceptance invariants. Read [setup.md](references/setup.md) for installation or repair. Read [release-state.md](references/release-state.md) only when assessing unfinished release work; never present a planned item as implemented.

After structural edits, run the Skill validator and the complete unit test suite. Do not modify or reload an installed live gateway while editing the package source.
