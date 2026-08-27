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

## 2. Build the ACE deploy automation

**What:** The Part A half of `~/.claude/plans/stateful-waddling-bachman.md`: a self-hosted GitHub
Actions runner as an unprivileged `ghrunner` user, `/usr/local/sbin/ace-deploy`, a sudoers entry
scoped to that one script, and `.github/workflows/deploy-ace.yml` triggered on push to main only.

**Why:** `ace-bot` still deploys by hand. Today's session was the proof: three commits merged to
main and the alliance still saw the removed form fields until a manual pull, because the bot runs
from a separate checkout at `/opt/ace` that nobody had updated.

**Blocked on two things only you can do:**
- A runner registration token (ACE repo -> Settings -> Actions -> Runners -> New self-hosted runner).
  Short-lived, so grab it when we start.
- A read-only deploy key: I generate the keypair as the `ace` user, you paste the public half into
  Settings -> Deploy keys. This also fixes manual deploys, which currently fail with
  `could not read Username` because the `ace` account has no GitHub credentials and the repo is
  private. Today that was worked around with a local bare mirror.

**Six fixes must be folded in** (from the eng review and the Codex pass, all verified in source):
- Pin `${GITHUB_SHA}` and deploy exactly the commit that was tested. Re-fetching `origin/main` can
  deploy an untested commit when two pushes land close together.
- Migration-aware rollback. `EventStore`'s constructor calls `migrate()` (`src/database.ts:88`) and
  `SchemaTooNewError` refuses to start against a newer schema (`src/migrations.ts:26-33`), so a plain
  code rollback after a migration crash-loops under `Restart=always`. Today's manual deploy applied
  migrations 8 and 9, so this is a live concern, not theory.
- Read the schema version with a direct `SELECT MAX(version) FROM schema_migrations`. `npm run
  migrate` is not a read-only probe - it *applies* migrations (`src/cli.ts:67`).
- Explicit CWD on every `npm` call. HANDOVER.md calls the working directory load-bearing.
- Timeout every CLI call: `connectClient()` awaits ready with no timeout (`src/client.ts:10-19`), so
  the health gate can hang forever.
- Accept `degraded` from `npm run health`; only `failed` sets a non-zero exit (`src/cli.ts:346`).
  The live bot is degraded today from pre-existing manifest drift (see item 3).

**Depends on:** the two GitHub-side items above.

---

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
