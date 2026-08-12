# Security

Feishu Remote Codex handles local project access, Feishu credentials, messages, and generated documents.

- Never commit App Secrets, OAuth tokens, tenant/chat/user IDs, local runtime state, private paths, or conversation content.
- Never open a public callback, public storage bucket, anonymous document link, or public service endpoint during setup.
- Use Windows DPAPI or macOS Keychain for secrets.
- Verify the selected tenant and exact local working directory before binding or sending.
- Close and read back document sharing settings before returning a link.
- Stop before tenant/account administrator elevation, cross-project or cross-tenant access, credential disclosure, disabling core protections, or irreversible/bulk deletion.

When the GitHub repository is published, enable private vulnerability reporting. Do not post credentials, private conversation text, tenant identifiers, or exploitable private links in a public issue.
