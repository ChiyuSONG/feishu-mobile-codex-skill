# Feishu Remote Codex

[中文](README.md)

Turn Feishu into a mobile entrance to your local Codex.

Send tasks to Codex continuously from Feishu without waiting for the previous task to finish. The computer and Codex must remain running for tasks to execute and return results. When either is closed, Feishu can still receive new messages and keep them pending; Codex processes them in order after it opens again. Remote outputs such as screenshots, Excel workbooks, and PDFs can also be viewed or downloaded through Feishu.

## What it can do for you

- Review code, edit files, run tests, or organize information remotely from your phone;
- Send several ideas and requirements continuously and let Codex process them in order;
- Leave tasks in Feishu while away from the computer instead of recording them in another app;
- Receive short results directly, while long tutorials, tables, screenshots, Excel workbooks, PDFs, and other artifacts are delivered through private Feishu documents for viewing or download on your phone;
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

While the computer and Codex are running, automatic inspection runs once per hour by default. It confirms that the connection is healthy, finds missed or pending messages, and reports Codex usage. Inspection does not run while the computer or Codex is closed.

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

Inspection can also become a small tool of your own. For example, say:

> Starting at 10 AM tomorrow, remind me during every inspection to submit the document until I say it is complete.

Codex remembers the item and keeps reminding you during inspections after that time. When it is done, simply say `The document has been submitted. Stop the reminder.` You can describe other inspection uses in natural language without following fixed commands.

These reminders depend on the computer, Codex, and automatic inspection; they are not exact-time alarms. Use Feishu Calendar or a phone alarm for anything that must happen at an exact time.

## Frequently asked questions

### Do the computer and Codex need to stay on?

Yes, if you want tasks to be processed promptly. The computer must be on and Codex must be running. When either is closed, Feishu still receives new messages and keeps them pending, but no task executes until Codex opens again.

### Does renaming the Feishu group break the connection?

No. The connection does not depend on the group name.

### Can I connect multiple projects?

Yes. Use a separate Feishu group for each project so their contexts do not mix.

### Does every reply become a Feishu document?

No. Simple replies are sent directly in the group. Private Feishu documents are used for long or structurally complex content and for delivering screenshots, Excel workbooks, PDFs, or other files so you can view or download local artifacts remotely.

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
