# Privacy and security

Display Desk runs locally as your desktop user. It invokes Niri's local IPC commands and writes display profiles and recovery files in your XDG configuration/state directories. It does not send this data to a server.

Profiles contain monitor serial numbers, setup names, display settings, and a machine identifier. Configuration backups and preview records may also contain local paths or other private configuration. Do not post those files unchanged in GitHub issues.

For a bug report, start with the app version, Niri version, Ubuntu version, and a description of the problem. Use fictional monitor names/serials in examples. Crop or redact any real screenshot that contains personal information, and review logs before attaching them.

Report a security concern through the repository's private vulnerability-reporting feature when available. Avoid publishing credentials or private configuration in a public issue.

The included screenshot is generated with synthetic data. Build scripts package an explicit allowlist and do not collect your display configuration.
