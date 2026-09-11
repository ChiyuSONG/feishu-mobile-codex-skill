# Feishu Remote Codex

[中文](README.md)

Use Feishu on your phone to send tasks to local Codex, follow progress, and receive results. It is for people who want remote Codex access from an Android, Huawei, or other phone without exposing local projects to the public internet or running a separate management dashboard.

While the computer and Codex are running, tasks execute locally. When either is closed, Feishu still receives new messages and keeps them pending; Codex processes them in order when it opens again. Screenshots, Excel workbooks, PDFs, and other results can also be viewed or downloaded through Feishu.

This is a DIY-friendly local tool, not a managed service. When the environment is ready, installation starts with one prompt and aims to complete the first connection in about 15 minutes. The first setup may still require a QR scan, permission approval, or a few guided environment fixes.

<p align="center">
  <img src="assets/feishu-remote-codex-preview.png" alt="Example of sending a task to remote Codex and receiving a document and inspection status in Feishu" width="420">
</p>

## What it can do for you

- Use your phone to ask Codex to review code, edit files, run tests, or organize information;
- Send several ideas and requirements continuously and let Codex process them in order;
- Hand tasks to Codex through Feishu while away from the computer instead of saving them in a notes app for later;
- Receive short results in the group, screenshots as Feishu images, PDFs and Excel workbooks as attachments, and long or structured results as private Feishu documents;
- Keep one Feishu group connected to one local project, even if the group is renamed;
- Check message status and Codex usage, or create follow-up reminders through chat instructions.

These are the default capabilities, not a fixed feature ceiling. After installation, you can use chat instructions to:

- Give different Feishu groups their own reply styles, inspection content, and project conventions;
- Add project automations such as industry-news digests;
- Let colleagues submit work by mentioning the bot after defining the team account, group membership, and local permission boundary.

These extensions are not enabled by default. Codex confirms the relevant boundaries before adding team access, broader permissions, or external services.

## What you need before starting

- A Windows or Mac computer with Codex installed and signed in;
- A working Feishu account. For a first setup, a **personal Feishu account is the default and recommended choice** and usually avoids team-admin involvement. Use a team account only when you explicitly need a company group and accept the organizational visibility and administrator-approval implications;
- A local Project already created in Codex that you want to manage remotely through Feishu.

Windows is ready for trial use. macOS support is currently Beta, so pay attention to any system guidance Codex provides during the first setup. Python 3.10 or later is required; if it is missing, Codex explains the requirement and helps you handle it first.

## Installation

### Step 1: Give the Skill to Codex

Send this repository URL to Codex and say:

> Install the `skill/feishu-codex-remote` Skill from this repository.

Alternatively, copy [`skill/feishu-codex-remote`](skill/feishu-codex-remote) into the Codex Skills directory, then restart or refresh Codex. Do not copy the entire repository as one Skill.

### Step 2: Connect Feishu with one prompt

In Codex, find the project you want to manage remotely through Feishu, open a conversation within it, and send:

> Use `$feishu-codex-remote` to connect this project to my Feishu account. Automate everything possible and ask me only when I need to scan a QR code, sign in, or approve access.

**During setup: local Codex settings**

- We recommend **GPT-6 Astra** for installation and connection. Choose reasoning effort and Priority/Fast as needed; neither has a required setting.
- **Full Access is strongly recommended for both setup and remote use** and enabled by default for new remote connections to reduce approval interruptions. It permits reading and writing local files and running commands; mistakes or malicious instructions may affect data outside the project. Use it only on a computer and project you trust. You can lower permissions through chat instructions, but some operations may then require returning to the computer, limiting the remote experience.

**After connection: defaults for Feishu tasks**

- New projects default to **GPT-6 Astra** (`gpt-6-astra`) and **Full Access**. Reasoning effort and Priority/Fast inherit your local Codex configuration; existing explicit model, reasoning, and speed settings are preserved.
- Use chat instructions to adjust each project's model, reasoning effort, speed, and permissions for its task difficulty and usage budget. These need not match your installation settings.

**Plan recommendation**

**Plus is sufficient**. For frequent long tasks or multiple projects, we recommend **Pro 20×** for more usage headroom. See [OpenAI's current plan details](https://learn.chatgpt.com/docs/pricing).

Codex handles the remaining configuration.

**Avoid using the computer during configuration**: Codex uses CUA (Computer-Use Automation) to configure the browser and Feishu and may temporarily take control of the mouse and keyboard. Take over when prompted to scan a QR code, sign in, or approve access.

You normally only need to follow its prompts to:

1. Scan a QR code or sign in to Feishu;
2. Confirm that the Feishu developer console is using your personal account, the default choice for a first setup. If Codex/CUA detects a team or company account, it pauses before creating the app and asks you to switch. It continues only when your request explicitly targets a group under that account and you confirm the administrator-approval and organizational-visibility implications;
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

Each newly connected project gets an hourly automatic inspection by default unless you opt out during setup. It checks the connection and retrieves missed or pending messages. Inspection does not run while the computer or Codex is closed. When Codex opens again, the background connection starts and checks for messages without waiting for the next inspection.

For projects sharing one Feishu bot on the same computer, project status is reported separately while shared information stays in one conversation:

- **First project**: the first inspection explains its purpose; subsequent reports contain the three lines below.
- **Additional new projects**: only the first status line is sent, starting with their first inspection. Usage and tips are not repeated.

1. Whether any messages are pending, processing, or failed; active tasks also show elapsed time and the latest safe progress update;
2. How much of the Codex 5-hour and 7-day allowances remains;
3. That you can change inspection, set reminders, or add other uses through chat instructions.

Existing inspection customizations are preserved. Reminders and other DIY features stay in the conversation you specify and are not copied into new projects.

For example, say:

- `Notify me only when there are pending messages, failures, or other problems.`
- `Do not report Codex usage during inspection.`
- `Run automatic inspection every two hours.`
- `Pause automatic inspection.`

Each inspection starts a lightweight Codex run and consumes a small amount of the corresponding allowance. Pause it when you do not need it.

No reminder is created by default. When needed, inspection can become a small tool of your own. For example, say:

> Starting at 10 AM tomorrow, remind me during every inspection to submit the document until I say it is complete.

Codex remembers the item and keeps reminding you during inspections after that time. When it is done, simply say `The document has been submitted. Stop the reminder.` You can describe other inspection uses through chat instructions without following a fixed format.

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

- Default Full Access grants local execution capabilities, not permission to work across projects; you can switch to project-only access or per-action review through chat instructions;
- Your local project does not need to be exposed to the public internet;
- Feishu credentials are stored outside the repository;
- Public link sharing is disabled and verified for generated Feishu documents;
- Each group connects only to the local project you explicitly select;
- The repository does not store your credentials, conversations, project paths, or runtime history.

See [SECURITY.md](SECURITY.md) for more information.

## License

Released under the [MIT License](LICENSE). Personal use, commercial use, modification, and redistribution are allowed, provided the license and copyright notice are retained.

This is an unofficial community project and is not affiliated with or endorsed by OpenAI or Feishu.
