# Security

## Reporting a vulnerability

Do not open a public issue with the details. Contact the maintainer through their GitHub profile to
arrange a private channel. Reports are acknowledged, and a fix lands with a test that fails if the
property is removed, like every other property here.

## What the repository promises

- **No secrets, ever.** No token, key, password, account id, chat id or host identifier is tracked —
  not even as a scan pattern. Auth in `settings.json` is by environment-variable NAME (`env:…`); a
  literal token in a settings file is refused at load.
- **Loopback only.** Every listener binds `127.0.0.1`; the one override that redirects the Slack
  adapter (`COMMS_SLACK_API_BASE`) accepts a loopback address only.
- **Nothing sends on its own.** The outbox ladder stages, and a send fires only on an explicit human
  action on the exact staged text, for a bot token only. Default policy denies.

## The gates

| Gate | Where it runs | What it refuses |
|---|---|---|
| `scripts/sanitize-gate.sh` | pre-commit and pre-push hooks, CI, and `--self-test` on demand | the pattern classes listed in the script (token, key, private-key, assigned-secret, platform-id, 12-digit account-id and non-loopback IPv4 shapes; `--self-test` plants one of each) over every tracked file, plus a **local denylist** of host-specific literals that is never committed |
| `scripts/sanitize-text.sh` | the commit-msg hook (every commit message), and on demand over pull-request titles and bodies (`--pr N …`) or a commit range (`--commits RANGE`) | the same classes and denylist, applied to text that never enters the tree; an empty scan is an error, not a pass |
| `tests/netguard/` | the whole test suite (`PYTHONPATH=tests/netguard`), CI and the pre-push hook | any non-loopback connect, bind, send or name lookup from a test — refused with a `BaseException` nothing can swallow |
| `tests/mutation_check.sh` | pre-push hook and CI | a load-bearing property whose removal does **not** turn the suite red (every entry must be caught) |

Install the hooks with `scripts/install-hooks.sh`; `docs/RUNBOOK.md` says how to tell they are
running. Note that the tree gate cannot see commit messages or pull-request text — that is what
`sanitize-text.sh` is for; messages are history once pushed, so the commit-msg hook runs before
the commit exists.

## This repository

This public repository carries release snapshots of the engine, one commit per release.
Development history is not included. Third-party licenses: [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES).
