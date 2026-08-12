# Feishu Remote Codex

[中文](README.md)

Turn Feishu into a mobile entrance to your local Codex.

Send tasks to Codex from Feishu at any time without staying at your computer or waiting for the previous task to finish. Messages remain in Feishu while the computer is offline, and Codex continues processing them in order the next time it opens.

## What it can do for you

- Review code, edit files, run tests, or organize information remotely from your phone;
- Send several ideas and requirements continuously and let Codex process them in order;
- Leave tasks in Feishu while away from the computer instead of recording them in another app;
- Receive short results directly and long tutorials, tables, or complex results as private Feishu documents;
- Keep one Feishu group connected to one local project, even if the group is renamed;
- Check message status and Codex usage, or create follow-up reminders in natural language.

## What you need before starting

- A Windows or Mac computer with Codex installed and signed in;
- A working Feishu account;
- A local project folder you want Codex to work with.

Windows is ready for trial use. macOS support is currently Beta, so pay attention to any system guidance Codex provides during the first setup. Python 3.10 or later is required; if it is missing, Codex explains the requirement and helps you handle it first.

## Installation

### Step 1: Give the Skill to Codex

Send this repository URL to Codex and say:

> Install the `skill/feishu-codex-remote` Skill from this repository.

If the repository is private, first make sure the computer running Codex is signed in and has permission to access it on GitHub.

Alternatively, copy [`skill/feishu-codex-remote`](skill/feishu-codex-remote) into the Codex Skills directory, then restart or refresh Codex. Do not copy the entire repository as one Skill.

### Step 2: Connect Feishu with one prompt

Open the local project you want to connect in Codex, then send:

> Use `$feishu-codex-remote` to connect this project to my Feishu account. Automate everything possible and ask me only when I need to scan a QR code, sign in, or approve access.

Codex handles the remaining configuration. You normally only need to follow its prompts to:

1. Scan a QR code or sign in to Feishu;
2. Select the correct Feishu account and approve access;
3. Copy a Feishu app credential once when necessary;
4. Confirm a Codex security prompt during the first startup.

The Feishu group receives a welcome message when the connection succeeds.

## How to use it in Feishu

Send messages just as you normally talk to Codex:

- `Review the latest project changes and run the tests.`
- `I will send three requirements in a row. Process all of them in order after receiving them.`
- `Put the complete result into a Feishu document.`
- `Record this for now and wait until I send the remaining information.`

You can continue sending messages while a task is running; new messages wait in the queue. Tasks do not run while the computer or Codex is closed, but messages are not lost. Processing continues after Codex opens again.

## What automatic inspection does

Automatic inspection runs once per hour by default. It confirms that the connection is healthy, finds missed or pending messages, and reports Codex usage.

The first inspection explains its purpose. Later, unless you change it, the report briefly tells you:

1. Whether any messages are pending, processing, or failed;
2. How much of the Codex 5-hour and 7-day allowances remains;
3. That you can change inspection in natural language.

For example, say:

- `Notify me only when there are pending messages, failures, or other problems.`
- `Do not report Codex usage during inspection.`
- `Run automatic inspection every two hours.`
- `Pause automatic inspection.`

Each inspection starts a lightweight Codex run and consumes a small amount of the corresponding allowance. Pause it when you do not need it.

## Use inspection for reminders

You can also ask Codex to remember something that needs follow-up and remind you during an inspection after the specified time arrives.

For a continuing reminder, say:

> Starting at 10 AM tomorrow, remind me during every inspection to submit the document until I say it is complete.

When it is complete, say:

> The document has been submitted. Stop the reminder.

For a one-time reminder, say:

> After 3 PM tomorrow, remind me during the next inspection to call my friend back. Remind me only once.

This is not an exact-time alarm. Reminders depend on the computer, Codex, and automatic inspection. If the computer is not running at the specified time, the reminder arrives during the next inspection after it resumes. Use Feishu Calendar or a phone alarm for anything that must happen at an exact time.

## Frequently asked questions

### Does the computer need to stay on?

No. You can keep sending messages in Feishu while the computer is off, but tasks wait until the computer and Codex are running again.

### Does renaming the Feishu group break the connection?

No. The connection does not depend on the group name.

### Can I connect multiple projects?

Yes. Use a separate Feishu group for each project so their contexts do not mix.

### Does every reply become a Feishu document?

No. Simple replies are sent directly in the group. Only long or structurally complex content, or content you explicitly request as a document, is organized into a private Feishu document.

## Uninstall

No command is required. Tell Codex:

> Uninstall Feishu Remote Codex. Keep my Feishu conversations and documents, tell me what will be removed first, and then complete the uninstall automatically.

Codex first lists what it plans to remove and waits for confirmation. Uninstalling does not affect unrelated Codex tasks and never deletes Feishu conversations, groups, or generated documents.

A recovery backup is kept by default. To also delete local credentials, cache, and historical state, explicitly add: `Also delete all local data.`

## Privacy and security

- Your local project does not need to be exposed to the public internet;
- Feishu credentials are stored outside the repository;
- Public link sharing is disabled and verified for generated Feishu documents;
- Each group connects only to the local project you explicitly select;
- The repository does not store your credentials, conversations, project paths, or runtime history.

See [SECURITY.md](SECURITY.md) for more information.

## License

Released under the [MIT License](LICENSE). Personal use, commercial use, modification, and redistribution are allowed, provided the license and copyright notice are retained.

This is an unofficial community project and is not affiliated with or endorsed by OpenAI or Feishu.
