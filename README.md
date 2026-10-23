# miogit-sync

MioGit (miogit.300031.xyz) ⇄ GitHub organisation **mio-co**, every repository, both directions.
This directory is published as the private repository `mio-co/miogit-sync`; the source of truth is
`sync/github/` in the MioGit repository (the Worker's `POST /~sync/api/github/setup` writes it there).

## How it flows

| Change | Noticed by | Then |
|---|---|---|
| push to MioGit | Worker `onPush` (skips the sync token's own pushes, repos with sync off) | `repository_dispatch` **miogit-push** |
| push to GitHub, repo created / made public / private | organisation webhook → Worker `POST /~sync/github` (HMAC `X-Hub-Signature-256`; skips a push MioGit already has) | `repository_dispatch` **github-push** |
| anything a missed event left behind | `schedule` every 6 h | full reconcile |

`sync.py` then works on the whole repository, not just the refs in the event: `git ls-remote` both
sides, and only when they differ fetch both into a cached bare repository and push.

## Rules

- **Names**: same name on both sides; GitHub names are case-insensitive. `sync.json` `aliases`
  maps a MioGit name to a different GitHub name (`MioGit` ↔ `miogit`). `exclude`: GitHub names never touched.
- **New repositories**: a MioGit repository without a GitHub twin is created in mio-co with the same
  visibility and the `miogit-sync` topic. A GitHub repository without a MioGit twin is created on
  MioGit only if it was created after `since` (or carries the topic) – older GitHub-only repositories are reported, not imported.
- **Existing GitHub repository of the same name**: linked only if it already has the topic, is empty,
  or shares history (a common sha / merge base). Otherwise it is left alone and the run goes red.
  A name that GitHub redirects to a renamed repository is never touched.
- **Branches**: fast-forward only, never forced. Diverged → skipped, run red.
- **Tags**: never moved. Same tag on different objects → skipped, run red.
- **Deleting a branch / tag**: copied only while the other side still has exactly the sha of the
  last sync (push with `--force-with-lease=<ref>:<sha>`); otherwise red. Default branches are never deleted.
- **Deleting a repository**: never copied, and the remaining copy is not re-created on the side
  where it was deleted (notice only).
- **Visibility**: the side that changed since the last sync wins; unknown / both changed → private.
- **Switch**: MioGit console → repository → Settings → General → “同步到 GitHub”. Off = both directions stop.
- **Loops**: the workflow pushes to MioGit with the sync token, whose pushes the Worker never
  dispatches; its pushes to GitHub come back as webhooks the Worker drops because MioGit already has that sha.

State (last synced sha per ref, visibility, link) is stored in MioGit (Registry settings) through
`/~sync/api/state/<repo>`.

## Secrets

| Where | Name | What |
|---|---|---|
| this repository (Actions) | `GH_SYNC_PAT` | fine-grained PAT for mio-co (Administration, Contents, Workflows, Secrets r/w; org Webhooks r/w) |
| this repository (Actions) | `MIOGIT_SYNC_TOKEN` | MioGit sync token (admin; its pushes are not echoed) |
| Worker `git-cells` | `SYNC_GITHUB_TOKEN`, `SYNC_GIT_TOKEN`, `SYNC_WEBHOOK_SECRET` | the same PAT, the same sync token, the webhook HMAC secret |

The Worker writes the two Actions secrets itself (sealed box) and creates the org webhook:
`POST /~sync/api/github/setup` with the sync token.

## Run by hand

Actions → sync → Run workflow: empty repo = reconcile all; `dry_run` prints the plan only.
Locally: `MIOGIT_SYNC_TOKEN=… GH_SYNC_PAT=… python3 sync.py reconcile --dry-run`.
Tests: `python3 -m unittest -v test_sync.py`.
