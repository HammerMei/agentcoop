# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| `main` branch | ✅ |
| Older releases | ❌ |

We currently support only the latest version on the `main` branch.

## Reporting a Vulnerability

**Please do not report security vulnerabilities via GitHub Issues.**

If you discover a security vulnerability, please report it privately via:

- **GitHub Security Advisories:** [Report a vulnerability](https://github.com/HammerMei/agentcoop/security/advisories/new)

Include as much detail as possible:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within **72 hours**. We ask that you give us reasonable time to address the issue before any public disclosure.

## Security Considerations

`coop` runs AI agents with access to tools (Bash, file I/O, web fetch, etc.) on your infrastructure. Please review:

- **[Permission & RBAC Reference](docs/permission-reference.md)** — How role-based access control and tool approval work
- **Tool allow-lists** — Configure `owner_allowed_tools` and `guest_allowed_tools` to limit blast radius
- **`skip_owner_approval`** — Only set this to `true` in trusted, sandboxed environments
- **Working directory** — Agents operate within a configured `working_directory`; keep it scoped

## Scope

The following are **in scope** for security reports:

- Authentication or authorization bypasses (e.g. RBAC circumvention, permission approval bypass)
- Tool allow-list bypass via crafted inputs
- Path traversal vulnerabilities in file tool matching
- SSRF via WebFetch tool rules
- Credential exposure in logs or state files

The following are **out of scope**:

- Vulnerabilities in third-party dependencies (report to their maintainers)
- Issues requiring physical access to the server
- Social engineering attacks
