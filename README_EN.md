# Feishu Mobile Codex Skill

[中文](README.md)

A DIY-friendly Codex mobile assistant: connect to Codex on your computer through Feishu and configure it around your personal workflow. Start installation with one prompt and, when the environment is ready, aim to complete the first connection in about 15 minutes. Once connected, keep adjusting it to suit how you work.

Want to use Codex remotely from an Android or Huawei phone, or tired of waiting for ChatGPT to reconnect every time you send a task? This Skill provides a simple path: send tasks to Codex on your computer and receive results through Feishu, without exposing the project on your computer to the public internet or running a separate management dashboard.

<p align="center">
  <img src="assets/feishu-remote-codex-preview.png" alt="Example of sending a task to remote Codex and receiving a document and inspection status in Feishu" width="420">
</p>

## What it can do for you

- Review code, edit files, run tests, or organize information remotely from your phone;
- Send several ideas and requirements continuously and let Codex process them in order;
- Hand tasks to Codex through Feishu while away from the computer instead of saving them in a notes app for later;
- Receive short results directly; screenshots appear as native Feishu images, explicitly delivered PDFs and Excel workbooks arrive as chat attachments, and long tutorials or complex layouts use private Feishu documents;
- Check message status and Codex usage, or create follow-up reminders through chat instructions.

These are the basic features available after installation and connection, not a fixed feature ceiling. After installation, simply talk to Codex to adjust existing behavior or add features that fit your workflow. For example:

- Collect and post a daily industry-news digest;
- Let colleagues submit work questions by mentioning the bot after agreeing on the team tenant, group membership, and local permission boundary;
- Give each project group its own response format, inspection content, working conventions, and scheduled work.

These are opt-in DIY extensions, not default features, and Codex implements and verifies them within the relevant project, permission, and security boundaries.

## What you need before starting

- A Windows or Mac computer with Codex installed and signed in;
- A working Feishu account. For a first setup, a **personal Feishu account is the default and recommended choice** because it has the lowest organizational risk and usually avoids team-admin involvement. Use a team account only when your explicit goal is to connect a group under that company account and you accept the organizational visibility and administrator-approval implications;
- A project already created in Codex on your computer that you want to manage remotely through Feishu.

## Installation

It automates setup wherever practical, but it is not a fully managed service. Differences in Feishu accounts, permission approval, and computer environments may require you to scan, confirm, or follow Codex through a few setup issues.

**Model and permission settings**

For installation, we recommend **GPT-6 Astra** and **strongly recommend enabling Full Access**; newly connected projects also default to these settings. Choose reasoning effort and Fast mode as needed; remote tasks inherit your local configuration. Existing explicit model, reasoning, and speed settings are preserved, and each project can be adjusted separately through chat instructions.

> **Full Access warning — applies to both installation and remote use: it permits reading and writing local files and running commands; mistakes or malicious instructions may affect data outside the project. Use it only on a computer and project you trust. You can switch to project-only access or per-action review through chat instructions, but some operations may then require returning to the computer. Full Access remains strongly recommended for smooth remote work.**

### Step 1: Give the Skill to Codex

Copy this instruction and send it to Codex:

> Install the `skill/feishu-codex-remote` Skill from https://github.com/ChiyuSONG/feishu-mobile-codex-skill.

Alternatively, copy [`skill/feishu-codex-remote`](skill/feishu-codex-remote) into the Codex Skills directory, then restart or refresh Codex. Do not copy the entire repository as one Skill.

### Step 2: Connect Feishu with one prompt

In Codex, find the project you want to manage remotely through Feishu, open a conversation within it, and send:

> Use `$feishu-codex-remote` to connect this project to my Feishu account. Automate everything possible and ask me only when I need to scan a QR code, sign in, or approve access.

**Avoid using the computer during configuration**: Codex uses CUA (Computer-Use Automation) to configure the browser and Feishu and may temporarily take control of the mouse and keyboard.

You normally only need to follow its prompts to:

1. Scan a QR code or sign in to Feishu;
2. Confirm that the Feishu developer console is using the account you intend to connect;
3. Approve the permissions needed for messages, images/files, and private documents;
4. Copy a Feishu app credential once when necessary;
5. Confirm a Codex security prompt during the first startup;
6. If your computer is missing runtime dependencies, follow the prompts to approve installing Python 3.10 or later and any other necessary dependencies.

The connection is complete when you receive the test image and welcome message in Feishu.

## How to use it in Feishu

Send messages just as you normally talk to Codex:

- `Review the latest code changes, fix any issues you find, run the tests, and send me the results.`
- `Summarize this sales sheet by product, identify the largest sales declines, and send me an Excel workbook.`
- `Remind me every Friday at 5 PM to submit my weekly report, and keep reminding me until I say it is submitted.`

You can send messages continuously in the same Feishu group. Codex uses the context to identify follow-up details for the same task and combine them for processing, while different tasks are queued and processed sequentially. Start a message with `*` to force separate processing without automatically merging it with other messages, or `#` to start an independent context inherited from the current conversation and begin processing in parallel immediately. Automatic inspection checks connection and task status hourly by default. Tasks do not run while the computer or Codex is closed, but messages stored in Feishu are not lost; processing resumes automatically when Codex opens again.

## Frequently asked questions

### How can I use Codex remotely from an Android or Huawei phone?

After installing this tool and connecting Feishu on your computer, send tasks to the bound group from Feishu on your phone. Results return to Feishu as well. Your phone must be able to use Feishu normally, and the computer and Codex must remain running with working access to the service.

### Do I still need the ChatGPT app on my phone when using Feishu?

No. Sending tasks and receiving results through this tool only requires Feishu on your phone, not the ChatGPT mobile app. You still need Codex installed and signed in on your computer, with working network access.

### Do the computer and Codex need to stay on?

Yes, if you want tasks to be processed promptly. The computer must be on and Codex must be running. When either is closed, Feishu still receives new messages and keeps them pending, but no task executes until Codex opens again.

### Does renaming the Feishu group break the connection?

No. The connection does not depend on the group name.

### Can I connect multiple projects?

Yes. Each Feishu group stays connected to one project on your computer. Use different groups for different projects so their contexts do not mix.

### Can I edit or recall a message after sending it by mistake?

Do not rely on edits or recalls to correct or cancel queued tasks. Send a new correction instead, for example: `Correction to my previous message: update only the README, not the code.` If the earlier task has already started, the correction waits for the next turn; it cannot live-steer the running turn or automatically undo completed actions.

### Does every reply become a Feishu document?

No. Simple replies are sent directly in the group; private Feishu documents are used only for long or structurally complex content. Image or attachment delivery failures are reported explicitly.

## Uninstall

No command is required. Tell Codex:

> Uninstall Feishu Mobile Codex Skill. Keep my Feishu conversations and documents, tell me what will be removed first, and then complete the uninstall automatically.

Codex first lists what it plans to remove and waits for confirmation. Uninstalling does not affect unrelated Codex tasks and never deletes Feishu conversations, groups, or generated documents.

A recovery backup is kept by default. To also delete local credentials, cache, and historical state, explicitly add: `Also delete all local data.`

## Privacy and security

- Default Full Access grants local execution capabilities, not permission to work across projects; you can switch to project-only access or per-action review through chat instructions;
- The project on your computer does not need to be exposed to the public internet;
- Feishu credentials are stored outside the repository;
- Public link sharing is disabled and verified for generated Feishu documents;
- Each group connects only to the project on your computer that you explicitly select;
- The repository does not store your credentials, conversations, project paths, or runtime history.

See [SECURITY.md](SECURITY.md) for more information.

## License

Released under the [MIT License](LICENSE). Personal use, commercial use, modification, and redistribution are allowed, provided the license and copyright notice are retained.

This is an unofficial community project and is not affiliated with or endorsed by OpenAI or Feishu.
