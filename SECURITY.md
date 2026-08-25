# Security Policy

## Sensitive data

Never commit API keys, cookies, provider credentials, private endpoints, source books, model translations, personal notes, or build logs containing private metadata. Keep credentials in `.env`, which is ignored by default.

The CLI redacts keys in diagnostics and `doctor` performs no network requests. A real build sends source chunks to the configured provider; users are responsible for confirming that the provider's privacy and data-retention terms are acceptable.

## Reporting a vulnerability

Do not open a public issue containing a secret or exploitable detail. Use GitHub's private vulnerability reporting for this repository, or contact the repository owner through a private channel.

Include the affected version, impact, reproduction steps, and a minimal synthetic test case. Do not attach copyrighted books or real credentials.

## Credential incident response

If a secret is exposed:

1. Revoke and rotate it immediately.
2. Stop active builds using the old credential.
3. Remove the secret from Git history, not only the latest commit.
4. Audit provider usage and billing.
5. Add a regression guard or ignore rule that prevents recurrence.
