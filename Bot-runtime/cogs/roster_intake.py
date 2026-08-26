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
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .pimp_my_bot import theme
from .permission_handler import PermissionManager
from .bot_level_mapping import parse_state, parse_furnace_level
from .alliance_member_edit import is_placeholder_name, apply_member_edit
from .alliance import check_alliance_state
from .gift_state_resolver import verify_add_state, get_alliance_kid

logger = logging.getLogger('alliance')

# Versioned on purpose: a format change gets a new version rather than a silent
# reinterpretation of a line the other bot already posted.
# The name is last and runs to the end of the line, because an in-game name is
# free text and cannot be delimited from the fields that follow it.
INTAKE_PATTERN = re.compile(
    r"^ACE_INTAKE\s+v1\s+fid=(\d{1,20})\s+discord=(\d{15,25})"
    r"(?:\s+state=(\d{1,6}))?(?:\s+fc=([A-Za-z0-9-]{1,16}))?"
    r"(?:\s+name=(.{1,40}))?\s*$",
    re.IGNORECASE,
)

# How far back a restart looks for handovers that arrived while the bot was down.
REPLAY_WINDOW = timedelta(hours=24)
REPLAY_LIMIT = 50


def parse_intake(content):
    """`ACE_INTAKE v1 fid=<id> discord=<id> [state=<n>] [fc=<level>] [name=<name>]`.
    Returns (fid, discord_id, state, furnace_lv, name), None for anything
    omitted or unreadable, or None if the line is not a handover."""
    match = INTAKE_PATTERN.match((content or "").strip())
    if not match:
        return None
    state = int(match.group(3)) if match.group(3) else None
    # This bot owns what a level means: "30", "FC10" and "FC10-2" all resolve
    # here, and anything else is dropped rather than stored as a number.
    furnace_lv = parse_furnace_level(match.group(4)) if match.group(4) else None
    # A forwarded name is self-reported, exactly like the one a member types
    # into /register, so it is cleaned rather than trusted for formatting.
    name = (match.group(5) or "").replace("`", "").strip() or None
    return int(match.group(1)), int(match.group(2)), state, furnace_lv, name


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
                "SELECT fid, discord_id, alliance, nickname, furnace_lv FROM users WHERE fid = ?",
                (fid,),
            ).fetchone()

    def _attach_discord(self, fid, discord_id, server_id):
        with closing(sqlite3.connect('db/users.sqlite', timeout=30.0)) as db, db:
            db.execute(
                "UPDATE users SET discord_id = ?, discord_server_id = ?, "
                "discord_id_updated_at = ? WHERE fid = ?",
                (discord_id, server_id, _now_iso(), fid),
            )
            db.commit()

    def _insert_user(self, fid, alliance_id, kid, discord_id, server_id, nickname=None, furnace_lv=None):
        with closing(sqlite3.connect('db/users.sqlite', timeout=30.0)) as db, db:
            db.execute(
                "INSERT INTO users (fid, nickname, furnace_lv, kid, stove_lv_content, "
                "alliance, discord_id, discord_server_id, discord_id_updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (fid, nickname or f"Player {fid}", furnace_lv or 0, kid, alliance_id,
                 discord_id, server_id, _now_iso()),
            )
            db.commit()

    async def register_intake(self, guild, alliance_id, fid, discord_id, given_state,
                              name=None, furnace_lv=None):
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
            # A row that never got a real name or level is worth more with one,
            # whichever path put it there. Both go through the edit that records
            # the change in history rather than a silent write.
            edits = {}
            if name and is_placeholder_name(existing[3], fid):
                edits["nickname"] = name
            if furnace_lv and not existing[4]:
                edits["furnace_lv"] = furnace_lv
            named = bool(await asyncio.to_thread(apply_member_edit, fid, **edits)) if edits else False
            if existing_discord:
                return "already", (
                    f"ID `{fid}` is already registered in {alliance_name}."
                    + (f" Filled in its {' and '.join(edits)}." if named else "")
                )
            self._attach_discord(fid, discord_id, guild.id)
            return "linked", (
                f"Linked existing ID `{fid}` to <@{discord_id}>."
                + (f" Filled in its {' and '.join(edits)}." if named else "")
            )

        # A handover is machine input, so the ID is proved against the game API
        # rather than trusted. An alliance with a home state must probe clean:
        # anything other than a match, including an API error, is a refusal, so
        # a mistyped ID can never enter the roster on the strength of a state
        # the other bot forwarded.
        home_state = await asyncio.to_thread(get_alliance_kid, alliance_id)
        if home_state is not None:
            gift_cog = self.bot.get_cog("GiftOperations")
            kid, verified = (None, False)
            if gift_cog is not None:
                kid, verified = await verify_add_state(gift_cog, fid, alliance_id)
            if not verified or kid is None:
                return "state", (
                    f"Could not confirm ID `{fid}` in state `{home_state}`. Check the "
                    f"ID, or add the member from Alliances -> Add Member."
                )
        else:
            # The alliance spans several states, so there is no home state to
            # probe against and the forwarded state is the only thing to go on.
            kid = parse_state(given_state) if given_state is not None else None
            if kid is None:
                return "state", (
                    f"This alliance has members in several states, so ID `{fid}` "
                    f"needs a state. Add the member from Alliances -> Add Member."
                )

        state_error = check_alliance_state(alliance_id, kid)
        if state_error:
            return "state", state_error

        try:
            self._insert_user(fid, alliance_id, kid, discord_id, guild.id,
                              nickname=name, furnace_lv=furnace_lv)
        except sqlite3.IntegrityError:
            return "already", f"ID `{fid}` was added by another process."
        display = name or f"Player {fid}"
        return "added", (
            f"Added `{display}` (ID `{fid}`) to {alliance_name} in state `{kid}` "
            f"and linked it to <@{discord_id}>."
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
        fid, discord_id, given_state, furnace_lv, name = parsed

        try:
            status, detail = await self.register_intake(
                message.guild, alliance_id, fid, discord_id, given_state, name, furnace_lv
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
