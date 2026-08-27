# TODOS

Deferred work, newest first. This repo is public and the entries below name server paths and
service layout; that was a deliberate call, not an oversight. Never put a credential here.

---

## 1. Exercise wos-deploy's untested paths

**What:** Deliberately drive `/usr/local/sbin/wos-deploy` down the four branches that have never
run: rollback, `last-failed` suppression, the requirements-changed reinstall, and retention pruning.

**Why:** The happy path is proven (a real `7343295 -> b98fce8` deploy: both gates, 13 database
backups, fast-forward, restart, `31/31 modules loaded`, 2m12s). The failure paths are only *written*.
Rollback is the one that matters: it fires exactly when something is already wrong, and if it is
broken it turns a failed deploy into a bot that will not start.

**How:** Push a commit to `main` that passes the gates but fails readiness (e.g. a cog that raises
on load, so the bot prints `30/31 modules loaded`). Confirm the script resets to the previous commit,
restarts, exits non-zero, and that the bot comes back on the old code. Then confirm the same SHA is
recorded in `/var/lib/wos-deploy/last-failed` and skipped on the next timer tick instead of being
retested every 5 minutes. Retention needs 11 deploys, or just drop 11 dated stub directories into
`Bot-runtime/backups/` and run the script once.

**Do it on a quiet evening** - the alliance sees a restart either way.

**Depends on:** nothing. **Risk if skipped:** a failed deploy leaves the bot down instead of rolled
back.

---

## 2. Finish the ACE deploy automation - 2 steps left

**Built and verified on 2026-08-27.** `/usr/local/sbin/ace-deploy` (root, 0755) with all six review
fixes, the `ghrunner` user, a sudoers entry scoped to that one script, an ed25519 deploy key with
GitHub's host key pinned (fingerprint checked against the published
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`), runner 2.337.0 staged at
`/opt/actions-runner-ace` with dependencies installed, and `.github/workflows/deploy-ace.yml`
committed in the ACE repo as `2e2f1e9` (NOT pushed - see step 2).

Verified: `ghrunner` cannot read `.env`, `/var/lib/ace/`, or the deploy key, can invoke `ace-deploy`
through sudo, and is refused `systemctl` and `bash`.

**Step 1 - register the deploy key.** Paste this into the ACE repo -> Settings -> Deploy keys ->
Add deploy key. Leave "Allow write access" UNCHECKED:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINuazk5hVSBhJ2s0+B5DSFzTH4KR1fhpLYgzhULv2Gjy ace-deploy@ubuntu-4gb-hel1-3
```

Then confirm: `sudo -u ace git -C /opt/ace fetch origin main` (currently fails with "Please make
sure you have the correct access rights" - that is expected until the key is added).

**Step 2 - register the runner, then push the workflow.** Get a token from the ACE repo ->
Settings -> Actions -> Runners -> New self-hosted runner (short-lived, so grab it immediately
before running this):

```
sudo -u ghrunner /opt/actions-runner-ace/config.sh \
  --url https://github.com/NamelessPharaoh/WOS-discord-manager \
  --token <TOKEN> --labels self-hosted,ace --unattended
cd /opt/actions-runner-ace && sudo ./svc.sh install ghrunner && sudo ./svc.sh start
```

The workflow is held back deliberately: pushing it before a runner exists queues a job that never
runs. Push it once the runner shows Idle.

**Untested until the first real run:** the whole path, including whether `npm ci` builds
better-sqlite3 natively in the runner's workspace. The rollback path is worth exercising
deliberately, same as item 1.

## 3. ACE manifest drift

**What:** `npm run health` on the live ACE bot reports `degraded`: "2 missing resource(s) and 4
configuration difference(s)". Run `npm run preview` from `/opt/ace/ace-discord-manager` to see the
diff, then `npm run reconcile -- --apply` if the changes are wanted.

**Why:** It predates this session and nothing here caused it, but it is the reason the ACE deploy
gate has to accept `degraded` rather than requiring `healthy`. Clearing it would let the gate be
strict, which is a better gate.

**Careful:** `reconcile` mutates the live Discord server. Read the `preview` output first.

---

## 4. Consider rotating the ACE credentials that sat in ace-rollback-*

**What:** The deleted `ace-rollback-20260826T121736/` held a live `DISCORD_TOKEN` and
`ACE_BACKUP_KEY` in an `env.backup`, inside a working tree for a public repo.

**Why:** The directory is gone and it was never pushed (GitHub's push protection caught the earlier
attempt, and `ace-rollback-*` is gitignored now). Exposure looks contained to this host. Rotating is
the cheap way to stop reasoning about it - a Discord bot token is one click in the Developer Portal,
followed by updating `/opt/ace/ace-discord-manager/.env` and restarting `ace-bot`.

**Priority:** low, unless that host was ever shared.
