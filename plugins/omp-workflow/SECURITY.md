# Security

This plugin modifies the current user's omp configuration and copies workflow files only after confirmation. Review the dry-run plan and the templates before installation. It relies on the `omp` executable on PATH and trusts the current user's account and profile directory. Configuration backups and state may contain sensitive configuration; restrict access to them.

Report security issues using the repository's [security policy](../../SECURITY.md).
