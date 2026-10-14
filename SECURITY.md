# Security

## Reporting a vulnerability

Do not open a public issue with the details. Contact the maintainer through their GitHub profile to
arrange a private channel; GitHub's private vulnerability reporting for this repository is on the
owner's list to enable. Reports are acknowledged, and a fix lands with a test that fails if the
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

## Reviews of the public repository (2026-10-14)

Two independent passes were made when the repository's visibility and licensing were reconciled.

**Local scan** of the 56 commits then on `main`: no home-directory paths, e-mail addresses, keys or
cloud ARNs, no fleet or host names, no Slack tokens, no customer names.

**Independent review** from a fresh clone: 66 commits, 496 blobs, every path, the pin tag, the
issue and pull-request bodies with their edit histories, the repository events, Actions logs,
releases and wiki; 94 real secret values from the origin host were compared against every object,
without running any script from the tree. Result: **no secret, credential, cloud or host identifier
anywhere; attribution clean** (every commit authored, committed and signed off by the maintainer,
no co-authors). Four release-gate items, none exploitable, all remediated or recorded:

| # | Severity | Finding | Remediation |
|---|---|---|---|
| 1 | medium, people | pull-request bodies named a collaborator, linked their private repository and quoted their requests verbatim | bodies edited to neutral wording; the earlier revisions can only be deleted in the GitHub web UI, which is the owner's step |
| 2 | medium, license | the README said the repository was private with licensing deferred, contradicting `LICENSE` | the README states the Apache License 2.0 |
| 3 | low, infrastructure | one commit message and pull-request bodies named a private review archive, evidence directories and an internal session | bodies edited; the commit message stays as history unless the owner decides on a rewrite (new shas, re-pin, re-tag) |
| 4 | low, infrastructure | `docs/PROVENANCE.md` listed the origin host's tools by directory name | reworded to generic descriptions; the names joined the local denylist, so the gate now refuses them |

Three other commit messages quote a collaborator's requests under their first name. They are
history, left in place for the same reason; the denylist refuses the name in any new commit or
pull-request text through the hooks above.

**Planted controls** the independent review ran against its own scanner before trusting the clean
result, each caught: a Slack bot-token shape; an AWS access key id in the documented example shape;
a home-directory path; a 12-digit account id. Three in-process controls also fired: a Slack id
shape, host identifiers, exact secret values. This repository's own gates carry their controls in
the tree: `scripts/sanitize-gate.sh --self-test` plants one secret of every class, and
`tests/test_sanitize_text.py` refuses a commit message carrying a synthetic token shape while a
clean one passes, including through the installed commit-msg hook.

**Not verified** by that review: only the latest CI log of each workflow (of 163 runs) was read;
commits force-pushed away before the repository went public, or outside the 22-event public feed,
cannot be seen from a clone.
