# Setup And Repair

## One-Prompt Setup

1. Resolve the current working directory and show its folder name as the proposed project name.
2. Run `python scripts/bootstrap.py check`. Support Windows and macOS; stop with a clear prerequisite if the platform or Python is unsupported.
3. Run `python scripts/bootstrap.py install` to create the isolated runtime and install pinned dependencies.
4. Open the Feishu Open Platform in the user's signed-in browser. Prefer computer-use for navigation and let the user scan the QR code, complete CAPTCHA, select the intended tenant, and approve consent.
5. Before creating any app, produce a sanitized inventory from known user-level gateway/publisher registries, Listener configurations, protected credential metadata, and the selected personal tenant's self-built apps. Verify tenant, ownership, bot and long-connection capability, document delivery, required scopes, credential availability, and Listener topology. Reuse a verified compatible app and extend only the required capabilities. Create a new minimum app only after recording why every candidate is unsafe or insufficient, or after the user explicitly requests isolation. Do not migrate working legacy apps as a side effect. Enable `im.message.receive_v1` and grant only the operations used by the gateway: receive group messages, send/reply messages, read group history for reconciliation, download message resources, manage reactions, create/read the private group, import/manage private documents, and create/update Docx blocks plus upload Docx images/files (`docx:document` and `drive:drive`). Native media insertion must be read back and verified; if an existing app lacks Docx permission, preserve document delivery and expose the artifact as a private downloadable file rather than silently omitting it.
6. Add the localhost OAuth redirect URI `http://127.0.0.1:17321/callback`. Never request a public callback or public endpoint.
7. Store the App Secret without echo using `python scripts/feishu_publish.py configure --app-id <id>`. On macOS this uses Keychain; on Windows it uses DPAPI. Then run `python scripts/feishu_publish.py auth`; the browser authorization is the preferred QR/login moment for private-document delivery.
8. Register the exact working directory with `remote_gateway.py init-project --language <zh-CN|en>`. Infer the system-message language from the user's setup request, defaulting to Chinese when ambiguous. Default the Feishu group display name to `Codex · <folder-name>` and explain that it may be renamed. Create the private group or bind the explicitly selected existing group by stable `chat_id`.
9. Install the demand-start launcher with `python scripts/listener_control.py`. Do not add boot or logon triggers.
10. Install the user-level Codex SessionStart Hook with `python scripts/install_startup_hook.py install`. Preserve existing hooks. Ask the user to review and trust the exact `Starting Feishu Remote Listener` command when Codex prompts; never bypass trust.
    This step changes Listener startup only. Inventory existing project Automations before and after installation and verify their cadence, status, prompts, usage-report behavior, reminders, and domain-specific work are byte-for-byte unchanged.
11. Start the Listener, run an end-to-end message/reply test, send the Feishu welcome message, and install the active hourly Automation. Its sync command must include `--inspection-report`: the first real run sends the formal explanation once, and later unchanged runs send the default three-line report.
12. Report the granted Feishu permissions, credential backend, runtime directory, launcher, SessionStart Hook and trust state, Automation cadence and Codex-usage effect, document privacy setting, language, and anything that still needs manual work.

## Required User Actions

Ask only when the action is actually reached:

- scan/login to Feishu;
- choose the intended tenant/account;
- approve OAuth consent;
- review and trust the exact Codex SessionStart Hook;
- securely paste the App Secret if browser automation cannot transfer it without exposing it;
- obtain tenant-admin approval when the user's account cannot grant an app scope.

Never ask the user to invent project keys, chat IDs, thread IDs, state paths, listener commands, or routing variables.

## Welcome Surfaces

The Skill itself cannot assume an unsolicited post-install chat message. Provide the starting sentence in `agents/openai.yaml` and the GitHub README. After the first invocation, give a short Codex-side setup summary. After binding, send the Feishu welcome message. The first actual scheduled inspection sends its separate one-time explanation; later unchanged inspections send the compact three-line status, Token usage, and customization reminder.

## Uninstall

Map `断开当前项目` to project scope and `卸载飞书远程 Codex` to all-project scope. Run `python scripts/uninstall.py --scope project --project-key <key>` or `python scripts/uninstall.py --scope all` first without `--apply`; show its JSON preview in plain language and verify that the resolved targets belong to this Skill.

If no target message is pending or processing, run the same command with `--apply --confirm`. Project scope removes only that binding and its scheduled inspection, then restarts the shared Listener when other bindings remain. All-project scope removes every owned scheduled inspection, the owned SessionStart Hook, and the owned Listener launcher. Both preserve local recovery state by default. Remove the installed `feishu-codex-remote` Skill directory only when the preview identifies it and no project bindings remain; never remove the source repository or another Skill with the same name.

Use `--purge-local-data` only after the user explicitly asks to delete credentials, queue history, logs, and recovery state. Treat that option as irreversible. Never delete Feishu conversations, groups, or generated documents. If active work exists, stop and list the affected message IDs; use `--abandon-active` only after the user explicitly chooses to abandon them.

## Repair

Preserve messages and state. Diagnose config, OS credential backend, Listener launcher, events, durable queue, thread state, Feishu history, and document OAuth before changing anything. Reconcile the exact project after a repair. Apply a graceful cross-platform reload with `python scripts/reload_gateway.py`; use `--request-only` when the current Feishu turn is itself running inside the Listener. Never reset a cursor or delete pending/processing/failed messages to make status look healthy.
