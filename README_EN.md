# Feishu Remote Codex

[中文](README.md)

Turn Feishu into a mobile entrance to your local Codex. Start installation with one prompt and, when the environment is ready, aim to complete the first connection in about 15 minutes.

Send tasks to Codex continuously from Feishu without waiting for the previous task to finish. The computer and Codex must remain running for tasks to execute and return results. When either is closed, Feishu can still receive new messages and keep them pending; Codex processes them in order after it opens again. Remote outputs such as screenshots, Excel workbooks, and PDFs can also be viewed or downloaded through Feishu.

If you are looking for a practical way to use remote Codex from an Android or Huawei phone, or to connect Feishu to local Codex without depending on the ChatGPT/Codex mobile app, this small tool provides a simple path. It does not require exposing your local project to the public internet or running a separate management dashboard.

<p align="center">
  <img src="assets/feishu-remote-codex-preview.png" alt="Example of sending a task to remote Codex and receiving a document and inspection status in Feishu" width="420">
</p>

## What it can do for you

- Review code, edit files, run tests, or organize information remotely from your phone;
- Send several ideas and requirements continuously and let Codex process them in order;
- Leave tasks in Feishu while away from the computer instead of recording them in another app;
- Receive short results directly; screenshots appear as native Feishu images, explicitly delivered PDFs and Excel workbooks arrive as chat attachments, and long tutorials or complex layouts use private Feishu documents;
- Keep one Feishu group connected to one local project, even if the group is renamed;
- Check message status and Codex usage, or create follow-up reminders in natural language.

These are ready-to-use foundations, not a fixed feature ceiling. After installation, simply talk to Codex to adjust existing behavior, add features that fit your workflow, or give different Feishu groups their own reply style, inspection content, and working conventions. Codex makes and verifies those changes within the relevant project, permission, and security boundaries, without requiring you to learn configuration files or commands first.

## What you need before starting

- A Windows or Mac computer with Codex installed and signed in;
- A working personal Feishu account. **Create the app under your personal account.** Team or company accounts commonly require administrator approval. Do not use one unless you explicitly understand and accept creating the app there;
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

> **Before setup:** The default remote model is **GPT-6 Astra** (`gpt-6-astra`); existing explicit models, reasoning levels, and Priority/Fast settings are preserved. Set local Codex to **Full Access** to avoid repeated permission pauses during setup and remote tasks. ChatGPT Plus is sufficient to install and use the Skill. If you frequently run long tasks or connect several projects, Pro 5x or a higher allowance offers more headroom; Pro mainly increases Codex usage limits and does not make the same model smarter. For roughly the next 15 minutes, Codex uses CUA (Computer-Use Automation) to configure the browser and Feishu and may temporarily take control of the mouse and keyboard, so avoid using the computer at the same time; take over only when asked to scan, sign in, or approve access. Full Access is highly privileged—use it only on a computer and project you trust. You can later switch to project-only access or per-action review in natural language.

Codex handles the remaining configuration. You normally only need to follow its prompts to:

1. Scan a QR code or sign in to Feishu;
2. Confirm that the Feishu developer console is using your personal account. If Codex/CUA detects a team or company account, it pauses before creating the app and asks you to switch. It continues there only after you explicitly accept the administrator-approval and organizational implications;
3. Approve the permissions needed for messages, images/files, and private documents;
4. Copy a Feishu app credential once when necessary;
5. Confirm a Codex security prompt during the first startup.

Codex first sends a real test image to the bound group. The connection is successful only after you can see that image in Feishu; the group then receives the welcome message. If a permission is missing, Codex asks for it at this stage instead of treating a failed upload as completed setup.

## How to use it in Feishu

Send messages just as you normally talk to Codex:

- `Review the latest project changes and run the tests.`
- `I will send three requirements in a row. Process all of them in order after receiving them.`
- `Put the complete result into a Feishu document.`
- `Record this for now and wait until I send the remaining information.`

You can continue sending messages while a task is running; new messages wait in the queue. Tasks do not run while the computer or Codex is closed, but messages are not lost. Processing continues after Codex opens again.

Consecutive queued messages may be combined while preserving every request. Start with `*` to queue one separately, or `#` to process one message in parallel using a copy of the main conversation context. Later ordinary messages still use the main conversation.

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

### Can I edit or recall a message after sending it by mistake?

Do not rely on edits or recalls. The current version treats only new messages as reliable instructions: an edit or recall is not handled as a correction or cancellation of queued work. Send a new correction instead, for example: `Correction to my previous message: update only the README, not the code.` If the earlier task has already started, the correction waits for the next turn; it cannot live-steer the running turn or automatically undo completed actions.

### Does every reply become a Feishu document?

No. Simple replies are sent directly in the group; private Feishu documents are used only for long or structurally complex content. Screenshots appear directly in the group. PDFs and Excel workbooks explicitly delivered by Codex arrive as downloadable chat attachments and may retain backup links in the full document. Delivery failures are reported instead of silently dropping an artifact.

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
