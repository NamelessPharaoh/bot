"""
Trusted roster intake. A paired manager bot (the ACE Discord Manager) posts a
handover line in one configured channel when it approves an applicant, and this
cog registers that player exactly the way /register does: the game API decides
whether the ID is real and in the right state, and this bot is the only thing
that writes users.sqlite.

The manager bot never touches this bot's database. It asks; this cog validates
and decides. Nothing else in the bot accepts input from another bot, so the
listener is scoped to one channel, one application ID and one alliance.
"""
import discord
from discord.ext import commands
import sqlite3
import logging
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .pimp_my_bot import theme
from .permission_handler import PermissionManager
from .bot_level_mapping import parse_state
from .alliance import check_alliance_state
from .gift_state_resolver import verify_add_state

logger = logging.getLogger('alliance')

# Versioned on purpose: a format change gets a new version rather than a silent
# reinterpretation of a line the other bot already posted.
INTAKE_PATTERN = re.compile(
    r"^ACE_INTAKE\s+v1\s+fid=(\d{1,20})\s+discord=(\d{15,25})(?:\s+state=(\d{1,6}))?\s*$",
    re.IGNORECASE,
)

# How far back a restart looks for handovers that arrived while the bot was down.
REPLAY_WINDOW = timedelta(hours=24)
REPLAY_LIMIT = 50


def parse_intake(content):
    """`ACE_INTAKE v1 fid=<id> discord=<id> [state=<n>]`.
    Returns (fid, discord_id, state) with state None when omitted, else None."""
    match = INTAKE_PATTERN.match((content or "").strip())
    if not match:
        return None
    state = int(match.group(3)) if match.group(3) else None
    return int(match.group(1)), int(match.group(2)), state


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_intake_config(guild_id):
    """(channel_id, trusted_app_id, alliance_id) for a guild, or None."""
    with closing(sqlite3.connect('db/settings.sqlite', timeout=30.0)) as db:
        return db.execute(
            "SELECT channel_id, trusted_app_id, alliance_id FROM roster_intake WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()


class RosterIntake(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # on_message fires for every message in every channel, so the pairing is
        # cached rather than read from SQLite per message. Misses are cached too;
        # only the slash command invalidates.
        self._config_cache = {}
        self.setup_database()

    def config_for(self, guild_id):
        if guild_id not in self._config_cache:
            try:
                self._config_cache[guild_id] = read_intake_config(guild_id)
            except Exception as e:
                logger.error(f"Could not read roster intake config for guild {guild_id}: {e}")
                return None
        return self._config_cache[guild_id]

    def setup_database(self):
        try:
            with closing(sqlite3.connect('db/settings.sqlite', timeout=30.0)) as conn, conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS roster_intake (
                        guild_id INTEGER PRIMARY KEY,
                        channel_id INTEGER NOT NULL,
                        trusted_app_id INTEGER NOT NULL,
                        alliance_id INTEGER NOT NULL,
                        updated_at TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            logger.error(f"Error setting up roster intake database: {e}")
            print(f"[ERROR] Roster intake database setup failed: {e}")

    # ── Roster writes, mirroring /register ─────────────────────────────────

    def _alliance_name(self, alliance_id):
        with closing(sqlite3.connect('db/alliance.sqlite', timeout=30.0)) as db:
            row = db.execute(
                "SELECT name FROM alliance_list WHERE alliance_id = ?", (alliance_id,)
            ).fetchone()
        return row[0] if row else None

    def _user_row(self, fid):
        with closing(sqlite3.connect('db/users.sqlite', timeout=30.0)) as db:
            return db.execute(
                "SELECT fid, discord_id, alliance, nickname FROM users WHERE fid = ?", (fid,)
            ).fetchone()

    def _attach_discord(self, fid, discord_id, server_id):
        with closing(sqlite3.connect('db/users.sqlite', timeout=30.0)) as db, db:
            db.execute(
                "UPDATE users SET discord_id = ?, discord_server_id = ?, "
                "discord_id_updated_at = ? WHERE fid = ?",
                (discord_id, server_id, _now_iso(), fid),
            )
            db.commit()

    def _insert_user(self, fid, alliance_id, kid, discord_id, server_id):
        with closing(sqlite3.connect('db/users.sqlite', timeout=30.0)) as db, db:
            db.execute(
                "INSERT INTO users (fid, nickname, furnace_lv, kid, stove_lv_content, "
                "alliance, discord_id, discord_server_id, discord_id_updated_at) "
                "VALUES (?, ?, 0, ?, NULL, ?, ?, ?, ?)",
                (fid, f"Player {fid}", kid, alliance_id, discord_id, server_id, _now_iso()),
            )
            db.commit()

    async def register_intake(self, guild, alliance_id, fid, discord_id, given_state):
        """Returns (status, message). Status is one of added, linked, already,
        conflict, other-alliance, state, alliance-missing, error."""
        alliance_name = self._alliance_name(alliance_id)
        if alliance_name is None:
            return "alliance-missing", f"Alliance `{alliance_id}` no longer exists."

        existing = self._user_row(fid)
        if existing:
            existing_discord = existing[1]
            existing_alliance = int(existing[2]) if existing[2] else None
            if existing_discord and int(existing_discord) != discord_id:
                return "conflict", (
                    f"ID `{fid}` is already linked to another Discord account. "
                    f"An admin has to unlink it first."
                )
            if existing_alliance is not None and existing_alliance != alliance_id:
                other = self._alliance_name(existing_alliance) or existing_alliance
                return "other-alliance", (
                    f"ID `{fid}` is already in `{other}`. Members are never moved "
                    f"automatically."
                )
            if existing_discord:
                return "already", f"ID `{fid}` is already registered in {alliance_name}."
            self._attach_discord(fid, discord_id, guild.id)
            return "linked", f"Linked existing ID `{fid}` to <@{discord_id}>."

        # A handover is machine input, so the ID is always probed against the
        # game API rather than trusted. The state the manager bot forwards is
        # only a fallback for an alliance that spans several states, where there
        # is no home state to probe against.
        gift_cog = self.bot.get_cog("GiftOperations")
        kid = None
        if gift_cog is not None:
            kid, _ = await verify_add_state(gift_cog, fid, alliance_id)
        if kid is None and given_state is not None:
            kid = parse_state(given_state)
        if kid is None:
            return "state", (
                f"Could not confirm ID `{fid}` in this alliance's state. Check the "
                f"ID, or add the member from Alliances -> Add Member."
            )

        state_error = check_alliance_state(alliance_id, kid)
        if state_error:
            return "state", state_error

        try:
            self._insert_user(fid, alliance_id, kid, discord_id, guild.id)
        except sqlite3.IntegrityError:
            return "already", f"ID `{fid}` was added by another process."
        return "added", (
            f"Added ID `{fid}` to {alliance_name} in state `{kid}` and linked it "
            f"to <@{discord_id}>."
        )

    # ── Channel listener ───────────────────────────────────────────────────

    def _icon(self, status):
        if status in ("added", "linked"):
            return theme.verifiedIcon
        if status in ("already", "other-alliance", "conflict"):
            return theme.warnIcon
        return theme.deniedIcon

    async def handle_intake_message(self, message):
        config = self.config_for(message.guild.id)
        if not config:
            return False
        channel_id, trusted_app_id, alliance_id = config
        if message.channel.id != channel_id or message.author.id != trusted_app_id:
            return False

        parsed = parse_intake(message.content)
        if parsed is None:
            return False
        fid, discord_id, given_state = parsed

        try:
            status, detail = await self.register_intake(
                message.guild, alliance_id, fid, discord_id, given_state
            )
        except Exception as e:
            logger.error(f"Roster intake failed for fid {fid}: {e}")
            status, detail = "error", f"Intake failed: {e}"

        logger.info(
            f"Roster intake {status}: fid={fid} discord={discord_id} "
            f"alliance={alliance_id} guild={message.guild.id}"
        )
        icon = self._icon(status)
        try:
            await message.add_reaction(icon)
            await message.reply(f"{icon} {detail}")
        except discord.HTTPException as e:
            logger.error(f"Could not acknowledge roster intake for fid {fid}: {e}")
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # The only place in the bot where a bot author is allowed through, and
        # only for the one application ID an admin has pinned to this channel.
        if not message.guild or message.author.id == self.bot.user.id:
            return
        try:
            await self.handle_intake_message(message)
        except Exception as e:
            logger.error(f"Error in roster intake listener: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        """Replay handovers that arrived while the bot was down. Every outcome
        is keyed on the FID, so reprocessing one is harmless; already-acknowledged
        messages are skipped so nobody gets a second reply."""
        for guild in self.bot.guilds:
            try:
                config = self.config_for(guild.id)
                if not config:
                    continue
                channel_id, trusted_app_id, _ = config
                channel = guild.get_channel(channel_id)
                if channel is None:
                    continue
                after = datetime.now(timezone.utc) - REPLAY_WINDOW
                async for message in channel.history(limit=REPLAY_LIMIT, after=after):
                    if message.author.id != trusted_app_id:
                        continue
                    if any(reaction.me for reaction in message.reactions):
                        continue
                    await self.handle_intake_message(message)
            except Exception as e:
                logger.error(f"Roster intake replay failed for guild {guild.id}: {e}")

    # ── Configuration ──────────────────────────────────────────────────────

    async def alliance_autocomplete(self, interaction: discord.Interaction, current: str):
        with closing(sqlite3.connect('db/alliance.sqlite', timeout=30.0)) as db:
            alliances = db.execute("SELECT alliance_id, name FROM alliance_list").fetchall()
        return [
            discord.app_commands.Choice(name=name, value=alliance_id)
            for alliance_id, name in alliances if (current or "").lower() in name.lower()
        ][:25]

    @discord.app_commands.command(
        name="roster_intake",
        description="Let a paired manager bot hand approved members to this bot's roster.",
    )
    @discord.app_commands.describe(
        channel="Channel the manager bot posts handovers in",
        application_id="The manager bot's application ID",
        alliance="Alliance the handed-over members join",
    )
    @discord.app_commands.autocomplete(alliance=alliance_autocomplete)
    async def roster_intake(self, interaction: discord.Interaction,
                            channel: discord.TextChannel, application_id: str, alliance: int):
        is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
        if not is_admin or not is_global:
            await interaction.response.send_message(
                f"{theme.deniedIcon} Only a Global Admin can pair another bot with this roster.",
                ephemeral=True,
            )
            return
        if not application_id.isdigit():
            await interaction.response.send_message(
                f"{theme.deniedIcon} The application ID is digits only.", ephemeral=True
            )
            return
        if self._alliance_name(alliance) is None:
            await interaction.response.send_message(
                f"{theme.deniedIcon} That alliance does not exist.", ephemeral=True
            )
            return

        with closing(sqlite3.connect('db/settings.sqlite', timeout=30.0)) as db, db:
            db.execute(
                "INSERT INTO roster_intake (guild_id, channel_id, trusted_app_id, alliance_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
                "channel_id = excluded.channel_id, trusted_app_id = excluded.trusted_app_id, "
                "alliance_id = excluded.alliance_id, updated_at = excluded.updated_at",
                (interaction.guild.id, channel.id, int(application_id), alliance, _now_iso()),
            )
            db.commit()
        self._config_cache[interaction.guild.id] = (channel.id, int(application_id), alliance)

        await interaction.response.send_message(
            f"{theme.verifiedIcon} Handovers posted in {channel.mention} by application "
            f"`{application_id}` will join **{self._alliance_name(alliance)}**.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(RosterIntake(bot))
