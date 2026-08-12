# Release State

Use this document to distinguish implemented behavior from release targets. A target is not an implemented feature until its acceptance checks pass on a clean machine.

## Confirmed Product Direction

- The product is a thin Feishu control surface for a local Codex, not a new agent platform.
- The main value is reliable, ordered mobile input through Feishu, including messages sent while the computer is offline, followed by local processing after it comes back online.
- First-run setup should concentrate unavoidable human actions at the beginning. The desired experience is one natural-language request such as `把当前项目连接到我的飞书`, one QR scan or equivalent Feishu authorization, and automatic completion of the remaining safe setup.
- Codex may use computer-use or the signed-in browser to complete Feishu console configuration when needed. It must pause for QR scanning, account selection, consent, CAPTCHA, tenant-admin approval, paid upgrades, or other actions that require the user's identity or authority.
- Normal use stays in natural-language chat. Codex already owns planning, editing, tests, recovery, and multi-agent behavior; the bridge should not duplicate these as a dashboard or command system.
- Complex results may be delivered as private Feishu documents, with a concise group reply and a document link. Direct replies remain the default for simple results.
- Users do not need to understand internal project keys or thread IDs. A Feishu group is bound by its stable `chat_id` to an exact local working directory and persistent Codex thread; renaming the group must not affect the binding. The local folder name may be used only as an initial display name.
- Reuse one verified compatible Feishu application across additional groups. Before any app creation, inventory known user-level registries, Listener configurations, protected credential metadata, and the selected personal tenant's self-built apps; create another app only after recording exact incompatibility evidence or receiving an explicit isolation request. Do not consolidate working legacy apps as a side effect of new-project setup.
- Enable an hourly scheduled inspection by default. Describe it to users as `自动巡检`, not as a heartbeat: when the computer and Codex are available, it starts or checks the Listener, reconciles missed messages, and asks the Listener to process backlog. Let the user change its interval, pause it, resume it, or disable it in natural language.
- Send a formal explanation on the first inspection. On later unchanged runs, default to exactly three short lines: message/Listener state, Token plan usage, and a natural-language customization reminder. Let a user's later conversational change override this default.
- Start the demand-only Listener from a trusted user-level Codex `SessionStart` Hook on `startup|resume`. Do not describe it as OS login startup. Keep the hourly inspection for reports, reminders, and reconciliation rather than making it the only Listener starter.
- Support Chinese and English. Keep Chinese as the default system-message language and preserve its existing quality; ordinary Codex replies follow the current user's message or explicit language request.
- Require Python 3.10 or newer. This is an implementation compatibility baseline, not a user preference to ask during onboarding.
- Let users extend the scheduled inspection with natural-language reminders and other custom behavior. These are uses of the existing Codex automation surface, not a separate reminder dashboard.
- Do not add a web administration console, built-in updater, agent dashboard, or bridge-specific rollback system merely to resemble heavier competitors.

## Target First-Run Journey

1. The user installs the Skill and opens Codex in the local project to bind.
2. The user asks Codex to connect that project to Feishu.
3. The Skill runs a read-only preflight: operating system, Codex CLI, Python/runtime, Feishu login state, existing app or reusable credentials, working directory, and local listener conflicts.
4. The Skill shows one short checklist of unavoidable user actions and their reason. Whenever Feishu supports it, prefer a QR authorization flow over asking the user to copy IDs, secrets, scopes, or callback values.
5. After the user scans or authorizes, Codex completes safe configuration with API calls or computer-use, creates an isolated local environment, installs dependencies, configures private delivery, binds the exact working directory and language, and installs the per-user background launcher.
6. The Skill adds a user-level Codex `SessionStart` Hook without replacing other hooks. The user reviews and trusts the exact command once; the Skill never bypasses that trust gate.
7. The Skill runs an end-to-end test from Feishu message to Codex reply, enables the default hourly automatic inspection, and sends the concise user guide below. It reports granted permissions, Hook trust, background processes, storage locations, privacy state, and unresolved limitations in the setup summary rather than burdening the normal usage guide.

Never ask for all possible preferences before setup. Apply conservative reversible defaults, disclose them in the final setup summary, and offer natural-language changes later.

## Minimal User Guide

Keep the first Feishu message close to this form. Show the local folder name, not an internal project key or full private path.

> **已连接，可以直接使用**
>
> 本群已绑定本地项目 `项目名`。群名可以随时修改，不影响连接。
>
> - 直接连续发送任务；电脑离线时消息留在飞书，上线后按顺序处理
> - 简单结果直接回复，复杂结果可生成私有飞书文档
> - 直接说“从明天 10 点开始每小时提醒我……”或“已完成，取消提醒”
> **自动巡检默认每小时运行一次**：首次会正式说明；此后默认用三句报告状态、Token 用量和自然语言定制提示。你也可以直接要求调整内容或频率、暂停或恢复自动巡检。

Do not describe the group name as the binding key. Do not show commands, configuration variables, app IDs, chat IDs, or internal thread concepts unless the user is diagnosing a problem.

## Reminder Behavior To Reuse

Generalize the proven reminder interaction without importing project-specific health data or rules:

- Store reminders separately from project business records in a local append-only event log.
- Unless the user asks for a one-off or pre-event reminder, keep an active reminder eligible at each scheduled inspection until the user explicitly completes, cancels, or stops it.
- Preserve an explicit `start_at`; remain silent before it. Never substitute message time for an unstated event or start time.
- Support natural-language changes to time, cadence, wording, completion, cancellation, and whether Token plan usage should be reported.
- When several active reminders could match “完成了” or “取消它”, ask which one rather than closing one by guess.
- Keep reminder content scoped to the bound workstream and never copy private records from another project.

## Current Release Baseline (2026-08-12)

| Capability | Current state |
| --- | --- |
| Durable ordered Feishu inbox, offline recovery, persistent Codex thread | Implemented and covered by deterministic tests |
| Direct Feishu replies and private-document routing | Implemented; explicit in-project Markdown images are sent as native image replies and explicit PDF/Excel deliverables as native file replies after tenant verification; private documents and private-file links remain the fallback for complex content and other artifacts |
| One-prompt installer | Skill workflow and runtime bootstrap implemented; real clean-machine/CUA onboarding not yet accepted |
| QR/login-based Feishu authorization | Browser OAuth with a localhost callback is implemented and can use Feishu's QR login when offered; real clean-account testing remains |
| CUA/browser-assisted Feishu console setup | Procedure defined; real console navigation not yet accepted |
| Hourly automatic inspection and default three-line Token/status report | Implemented with a one-time formal explanation and hourly idempotent recurring report |
| Listener start on Codex session startup/resume | Implemented as a merge-safe user-level SessionStart Hook; real Desktop trust/startup test pending |
| Chinese and English | Chinese remains the default; English welcome, inspection, reminder, usage and document-link text are implemented; real bilingual Feishu test pending |
| Deterministic natural-language changes to inspection schedule | Not implemented; do not promise a specific sub-hour cadence until Codex Automation support is verified |
| General natural-language reminder persistence | Implemented as a project-scoped append-only log with continuous and one-time modes; real Feishu end-to-end testing remains |
| Windows listener lifecycle | Preserved with PowerShell and Windows Task Scheduler |
| macOS listener lifecycle | Implemented with a Python wrapper and per-user demand-start `launchd`; real Mac test pending |
| Portable credential storage | Implemented as Windows DPAPI or macOS Keychain; real Mac test pending |
| Conversational uninstall | Implemented with preview, active-work refusal, component-scoped removal, recovery-by-default, and explicit local-data purge; clean-machine test pending |
| Clean-machine packaging and public installation test | Not completed |

The package is therefore a Release candidate, not yet a clean-machine-accepted public release.

## Cross-Platform Boundary

The Python gateway core is mostly portable, but installing Python alone does not make the current version work on macOS. Release work should keep one shared gateway and isolate platform-specific adapters:

- runtime/state directories: `%LOCALAPPDATA%` on Windows and `~/Library/Application Support` on macOS, preferably resolved through one cross-platform helper;
- secrets: Windows Credential Manager or DPAPI on Windows, macOS Keychain on macOS;
- lifecycle: a current-user demand-start Task Scheduler task on Windows and a current-user `launchd` agent on macOS;
- Codex lifecycle bridge: a trusted user-level `SessionStart` Hook starts the demand-only launcher on `startup|resume`; no OS login trigger is required;
- executable discovery: locate `codex`, `python` or `python3`, and browsers without assuming `.exe` paths;
- bootstrap: reuse a compatible Python when present, create a Skill-owned virtual environment, and install pinned Python packages there.

If no compatible Python exists, Codex may guide or automate installation, but it must disclose system-level package-manager changes, administrator prompts, downloads, and lifecycle effects. It must not claim that invoking a Skill grants silent authority to install system software.

## Proposed Defaults Requiring Confirmation

These are design candidates only; none is a finished product rule yet.

- Support Windows first and macOS in the same public release only after both clean-machine acceptance suites pass. Otherwise label macOS as experimental or postpone it.
- Keep bridge-specific commands minimal. Natural language remains primary; reserve only essential out-of-band recovery controls if testing proves they are needed.
- Prefer one QR authorization. If Feishu's official flow cannot grant all required bot/event/document permissions, show the smallest additional action list rather than hiding manual steps.

## Release Acceptance Gates

- A new consumer can start from the public package and one simple prompt without reading internal design documents.
- All unavoidable manual actions appear in one initial checklist, and each completed step is automatically verified.
- No app secret, token, tenant ID, chat ID, local username, private path, or conversation content exists in the package or logs by default.
- Direct reply, native image reply, private-document reply, offline queue recovery, duplicate-event handling, restart recovery, and uninstall instructions pass on a clean Windows account.
- The same suite passes on a clean macOS account before macOS is called supported.
- The generated Feishu app uses least privilege, generated documents remain non-public, and no public endpoint is required unless the user separately approves and secures it.
- Uninstall removes only Skill-owned launchers, environments, and state after showing the exact targets; it never deletes Feishu conversations or generated documents.
