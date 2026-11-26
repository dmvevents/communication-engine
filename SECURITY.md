# Security

## Reporting a vulnerability

Do not open a public issue with the details. Contact the maintainer through their GitHub profile to
arrange a private channel. Reports are acknowledged, and a fix lands with a test that fails if the
property is removed, like every other property here.

## What the repository promises

- **No secrets, ever.** No token, key, password, account id, chat id or host identifier is tracked —
  not even as a scan pattern. Auth in `settings.json` is by environment-variable NAME (`env:…`); a
  literal-looking secret in a settings file is refused at load.
- **Read-only by default.** Every target starts at reply policy `never`; the outbox stage-gate lives
  in core and adapters cannot bypass it.
- **No listening ports.** The engine makes outbound calls only; the dashboard wrapper
  (`scripts/dashboard-serve.sh`) binds `127.0.0.1`.

## The gates

| Gate | Where it runs | What it refuses |
|---|---|---|
| `scripts/sanitize-gate.sh` | pre-commit and pre-push hooks, CI, and `--self-test` on demand | the pattern classes listed in the script (token, key, private-key, assigned-secret, platform-id, 12-digit account-id and non-loopback IPv4 shapes; `--self-test` plants one of each) over every tracked file, plus a **local denylist** of host-specific literals that is never committed |
| unit tests (`python3 -m unittest discover -s tests`) | pre-push hook and CI | any regression of a load-bearing property |
| `tests/mutation_check.sh` | pre-push hook and CI | a load-bearing property whose removal does **not** turn the suite red (every entry must be caught) |

Install the hooks with `scripts/install-hooks.sh`; `docs/RUNBOOK.md` says how to tell they are
running.

## This repository

This public repository is a single-commit snapshot of the engine. Its development history is not
included.
