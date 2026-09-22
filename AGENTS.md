# Release Workspace Rules

- Treat this repository as a public, consumer-facing package. Never add maintainer-specific paths, account or tenant identifiers, chat or project IDs, private conversation content, credentials, or assumptions about another installed copy.
- Keep setup and removal conversational: a consumer should be able to connect or uninstall with one clear natural-language request while Codex performs deterministic checks and asks only for identity, authority, or destructive-data confirmation.
- Do not mutate an installed gateway, Hooks, scheduled tasks, credentials, or runtime state while editing or testing this repository. Use temporary directories and mocks unless the user explicitly requests a live operation.
- Preserve the small product boundary: Feishu is a mobile entrance to local Codex, not a separate agent platform or management dashboard.
- Before release, validate the Skill, run the complete test suite, and scan the package for personal data and secrets.
- Every relevant edit must rerun retained lifecycle and recovery regressions as part of the complete suite; follow the live/simulated acceptance distinction in `skill/feishu-codex-remote/references/runtime-recovery.md`. Never replace a failing behavioral test with a source-text assertion.
