# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | ✅ Active          |

## Reporting a Vulnerability

If you discover a security vulnerability in RapidWebs-SysStable, please report it
privately. **Do not** disclose it publicly until we've had a chance to address it.

To report a vulnerability:

1. **Email**: dev@rapidwebs.com with subject "Security: [brief description]"
2. **Include**:
   - Affected version(s)
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We will acknowledge receipt within 48 hours and provide a timeline for resolution.

## Security Considerations

### Socket Permissions
The unix socket (`~/.cache/sysstable/sysstable.sock`) is created with mode `0o600`.
Only the owning user can communicate with the daemon.

### State File
The Hermes plugin reads `state.json` only — it never executes commands.
The daemon writes state.json to `~/.hermes/plugins/rapidwebs-sysstable/state.json`
with restrictive permissions.

### Event Dispatch
Shell hooks must be explicitly installed in `~/.config/sysstable/hooks.d/` and be
executable. The daemon only runs scripts it can execute — it does not download or
install anything automatically.
