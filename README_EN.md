# Feishu Remote Codex

[中文](README.md)

Turn Feishu into a lightweight mobile entrance to your local Codex.

You can send tasks continuously from your phone without waiting for the previous one to finish. Messages remain in Feishu while the computer is offline, and Codex processes them in order after it returns. Simple results are sent directly; complex results can be organized into private Feishu documents.

## What you can do

- Send tasks to local Codex directly from a Feishu group;
- Keep one group reliably bound to one local project, even if the group is renamed;
- Send several messages continuously and have Codex process them in order;
- Leave tasks in Feishu while the computer is offline instead of recording them elsewhere;
- Receive short results directly and long tutorials, tables, or complex results as private Feishu documents;
- Adjust automatic inspection, view Codex usage, or set reminders in natural language;
- Talk in Chinese or English.

Codex still understands requests, edits the project, runs tools, and completes the work. Feishu is simply a smooth and reliable mobile entrance.

## Installation

### 1. Install the Skill

Give this repository URL to Codex and ask it to install `skill/feishu-codex-remote` from the repository.

Alternatively, copy [`skill/feishu-codex-remote`](skill/feishu-codex-remote) into your Codex Skills directory, then restart or refresh Codex. Do not copy the entire repository as one Skill.

### 2. Start setup with one prompt

Open the local project you want to connect in Codex, then send:

> Use `$feishu-codex-remote` to connect this project to my Feishu account. Automate everything possible and ask me only when I need to scan a QR code, sign in, or approve access.

Codex checks the environment, creates an isolated Python environment, helps configure the Feishu app, binds the group, installs the Listener and startup Hook, and tests the complete message path.

You normally only need to:

1. Scan a QR code or sign in to Feishu;
2. Select the correct Feishu account or tenant and approve access;
3. Paste the App Secret into a secure input location when necessary;
4. Review and trust `Starting Feishu Remote Listener` when the Hook security prompt first appears.

Codex handles internal settings such as the project key, chat ID, thread ID, and Listener path automatically.

## Start using it

After the connection succeeds, simply tell Codex what you want in the Feishu group:

- `Review the latest project changes and run the tests.`
- `I will send three requirements in a row. Process all of them in order after receiving them.`
- `Put the complete result into a Feishu document.`
- `Record this for now and wait until I send the remaining information.`

Tasks do not run while the computer or Codex is closed, but the messages remain in Feishu. The startup Hook starts the Listener the next time Codex opens or resumes, and pending messages continue processing.

## Automatic inspection

Automatic inspection runs once per hour by default. It checks the Listener, retrieves Feishu messages that may have been missed, moves pending tasks forward, and reports Codex usage.

The first inspection explains its purpose in full. Later inspections use only three lines unless you change them:

> Status: Listener healthy, 0 pending, 0 processing, 0 failed.
>
> Codex usage: 5-hour allowance remaining ...; 7-day allowance remaining ...
>
> Tip: Use natural language to change inspection, create reminders, or add other uses.

Each inspection starts a lightweight Codex run and consumes the corresponding allowance. You can change it directly:

- `Notify me only when there are pending messages, failures, or other problems.`
- `Do not report Codex usage.`
- `Run automatic inspection every two hours.`
- `Pause automatic inspection.`

## Use inspection for reminders

Reminders are useful for something you need to follow up later. Codex saves the item and reminds you through automatic inspection after the specified time arrives.

For example, to keep following up on a document, say:

> Starting at 10 AM tomorrow, remind me during every inspection to submit the document until I say it is complete.

Later, say:

> The document has been submitted. Stop the reminder.

For a one-time reminder, say:

> After 3 PM tomorrow, remind me during the next inspection to call my friend back. Remind me only once.

This is not an independent exact-time alarm. Reminders depend on the computer, Codex, and automatic inspection. If the computer is not running at the specified time, the reminder arrives during the next inspection after it resumes. Use Feishu Calendar or a phone alarm when exact timing is required.

## Uninstall

No command is required. Tell Codex:

> Uninstall Feishu Remote Codex. Keep my Feishu conversations and documents, tell me what will be removed first, and then complete the uninstall automatically.

Codex first lists what it plans to remove. After confirmation, it removes the Listener, startup Hook, automatic inspection, and local Skill added by this project without affecting unrelated tasks.

A recovery backup is kept by default, and Feishu conversations, groups, and generated documents are never deleted. To also delete local credentials, cache, and historical state, explicitly add: `Also delete all local data.`

## Supported environments

- Windows: local automated tests are available;
- macOS: adaptation and simulated tests are complete, but support remains experimental until validation on a real Mac;
- Python: version 3.10 or later is required. If it is missing, Codex explains what needs to be installed first.

## Privacy and security

- No public callback or public service is required;
- App Secrets and OAuth Tokens stay outside the repository, using DPAPI on Windows and Keychain on macOS;
- Public link sharing is disabled and verified for generated Feishu documents;
- Each group binds only to an explicitly selected local directory; the project is never guessed from the group name or message content;
- The repository does not store private paths, Feishu IDs, credentials, conversations, or runtime state.

See [SECURITY.md](SECURITY.md) for more information.

## License

Released under the [MIT License](LICENSE): personal use, commercial use, modification, and redistribution are allowed, provided the license and copyright notice are retained.

This is an unofficial community project and is not affiliated with or endorsed by OpenAI or Feishu.
