# TODOS

Deferred work, newest first. This repo is public and the entries below name server paths and
service layout; that was a deliberate call, not an oversight. Never put a credential here.

The MCP control surface that items 0-4 refer to is designed, phased and reviewed in
[`docs/designs/mcp-control-surface.md`](docs/designs/mcp-control-surface.md). Nothing in it is
built yet.

---

## 0. Relocate the bot out of /root

**What:** Move the install from `/root/bot/Bot-runtime` to `/var/lib/wos-bot`, run `wos-bot.service`
as a dedicated service user, grant an MCP-reader group read access to `db/` and `log/`, and allow
one exact `systemctl restart wos-bot` through sudoers.

**Why:** `/root` is `drwx------`, so *no* unprivileged tooling can read `db/` or `log/` no matter
what their own modes are, and `/proc/<pid>/fd` is root-only. That single fact is the reason the MCP
control surface has to run as root with a forced SSH command as its entire security boundary. Found
by the outside voice during the MCP plan review: the plan is building a remote root API around a
deployment defect. Fixing this makes a stolen key read two directories instead of owning the box.

**Careful:** touches the systemd unit, `/usr/local/sbin/wos-deploy`, both install roots, and
`backups/`, on a live bot. Do it deliberately with its own plan, not as a prerequisite to something
smaller. **Depends on:** item 7 (exercise the rollback path first).

## 1. Proactive alerting: let Claude tell you when the bot is unhappy

**What:** A scheduled `claude -p` run that reads bot state through the MCP read tools every N minutes
and messages you only on an anomaly: queue stalled, a cog missing, a deploy rolled back, redemption
failures spiking.

**Why:** It inverts the MCP control surface from something you query into something that talks to
you, which is the actual 12-month goal that justified building the read plane.

**Deferred until** the read plane has run in production long enough to source real thresholds.
Thresholds tuned against tools nobody has used produce alerts that get muted within a week.
**Depends on:** MCP Phase 1 shipped and in use.

## 2. Deploy-history tool for the MCP surface

**What:** Read `/var/lib/wos-deploy/last-failed` and `Bot-runtime/backups/` so deploy outcomes are
legible from chat without SSH.

**Why:** `wos-deploy` is the most autonomous part of the stack and currently the least visible.

**Deferred because** `wos-deploy` is not in version control, so the tool would couple to paths
nothing tracks, with no test to catch a drift. `journalctl -u wos-deploy` answers the same question
at a terminal. Revisit after item 7's paths have settled.

## 3. Point the MCP server at ace-bot as a second target

**What:** Same host, same shape of problem, largely configuration rather than new code.

**Why:** ACE has no state visibility at all today, and its deploy automation is the less exercised
of the two.

**Deferred as** premature abstraction: generalizing to two targets before the first has run once
reliably produces the wrong seams. **Depends on:** items 9 and 10 (ACE manifest drift and the
credential rotation) being settled before an AI gets access to it.

## 4. Narrow bot_health.reload_cogs' catch-all exception handler

**What:** Replace `except Exception as e` at `cogs/bot_health.py:1375` with named exception classes.

**Why:** It collapses a cog syntax error, a missing dependency, a discord.py `ExtensionError`, and a
bug in the cog's own `setup()` into one undifferentiated string. MCP Phase 2's `reload_cog` reuses
this method, and its caller is an AI that will have to guess. Start by enumerating what
`load_extension` / `reload_extension` actually raise in discord.py 2.7.

**Not required** by Phase 2, just less legible without it.

## 5. Tree-wide em-dash audit and an invariant test

**What:** Audit the em-dashes in `cogs/` against the narrow rule (no em-dash as prose punctuation;
`-` as a column separator or empty-value placeholder is fine), fix the prose ones, and pin the
structural half in `tests/test_invariants.py`.

**Why:** `CLAUDE.md` calls this a hard constraint swept out in `138f7f7`, but nothing enforces it and
the tree has drifted to 298 occurrences across 294 lines, led by `bear_track.py` (87),
`attendance_ocr_review.py` (51) and `attendance_ocr_parsers.py` (34). Measured with
`grep -ro '\u2014' cogs/ | wc -l`. Right now the documented rule and the code disagree, which
misleads anyone who trusts the file.

**Careful:** a mechanical test can only pin the structural half; separating prose from structure
needs human judgement per site.

## 6. Two open findings from the codex review of the deploy scripts (2026-08-28)

Codex reviewed both root deploy scripts (they are not in version control, so that was their only
review). 15 findings, 9 of them P1; 13 are fixed and verified. Two remain:

**wos-deploy: rollback cannot restore the venv properly.** `pip install` mutates
`Bot-runtime/venv` in place, so a failure part-way leaves a half-changed environment that git
cannot revert. Rollback now reinstalls PREV's `requirements.txt` as a best effort, which is not
the same as a true restore. The real fix is versioned venvs: build `venv-<sha>` alongside, switch
a symlink atomically, keep the previous one for rollback.

**wos-deploy: the CI venv is not reproducible per candidate.** `/opt/ci/wos-venv` is long-lived and
`pip install -r` never removes packages, so a package left behind by an earlier candidate can make
a later one pass a gate it should fail. Fix by recreating (or `pip-sync`-ing) the venv from the
target's requirements for each candidate.

Neither blocks the automation, and both bots deploy correctly today.

---

## 7. Exercise wos-deploy's untested paths

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

## 8. Finish the ACE deploy automation - 2 steps left

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

## 9. ACE manifest drift

**What:** `npm run health` on the live ACE bot reports `degraded`: "2 missing resource(s) and 4
configuration difference(s)". Run `npm run preview` from `/opt/ace/ace-discord-manager` to see the
diff, then `npm run reconcile -- --apply` if the changes are wanted.

**Why:** It predates this session and nothing here caused it, but it is the reason the ACE deploy
gate has to accept `degraded` rather than requiring `healthy`. Clearing it would let the gate be
strict, which is a better gate.

**Careful:** `reconcile` mutates the live Discord server. Read the `preview` output first.

---

## 10. Consider rotating the ACE credentials that sat in ace-rollback-*

**What:** The deleted `ace-rollback-20260826T121736/` held a live `DISCORD_TOKEN` and
`ACE_BACKUP_KEY` in an `env.backup`, inside a working tree for a public repo.

**Why:** The directory is gone and it was never pushed (GitHub's push protection caught the earlier
attempt, and `ace-rollback-*` is gitignored now). Exposure looks contained to this host. Rotating is
the cheap way to stop reasoning about it - a Discord bot token is one click in the Developer Portal,
followed by updating `/opt/ace/ace-discord-manager/.env` and restarting `ace-bot`.

**Priority:** low, unless that host was ever shared.
