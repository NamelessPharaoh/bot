"""Roster intake payload parsing.

The handover line is the trust boundary with the paired manager bot, so the
parser is deliberately strict: anything that is not exactly the versioned form
is ignored rather than guessed at.
"""
import pytest

from cogs.roster_intake import parse_intake


def test_parses_the_minimum_payload():
    assert parse_intake("ACE_INTAKE v1 fid=821103058 discord=701119021172523078") == (
        821103058, 701119021172523078, None, None, None
    )


def test_parses_an_optional_state():
    assert parse_intake(
        "ACE_INTAKE v1 fid=821103058 discord=701119021172523078 state=4562"
    ) == (821103058, 701119021172523078, 4562, None, None)


def test_parses_an_in_game_name():
    """Without it every handover lands as `Player <id>` and someone renames it
    by hand, which is what happened to the first two members."""
    assert parse_intake(
        "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 state=4562 name=NOUR"
    ) == (816479488, 701119021172523078, 4562, None, "NOUR")
    # A name is free text: spaces and non-Latin scripts are ordinary.
    assert parse_intake(
        "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 name=Lord Ahmed 99"
    )[4] == "Lord Ahmed 99"
    assert parse_intake(
        "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 name=نور"
    )[4] == "نور"
    # Backticks would break the embed the name is rendered into.
    assert parse_intake(
        "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 name=`NOUR`"
    )[4] == "NOUR"


def test_tolerates_surrounding_whitespace():
    assert parse_intake("  ACE_INTAKE  v1   fid=1 discord=701119021172523078  \n") == (
        1, 701119021172523078, None, None, None
    )


@pytest.mark.parametrize("content", [
    "",
    None,
    "hello",
    "821103058",
    # A future version must not be read with today's meaning.
    "ACE_INTAKE v2 fid=821103058 discord=701119021172523078",
    # Missing or malformed fields.
    "ACE_INTAKE v1 fid=821103058",
    "ACE_INTAKE v1 discord=701119021172523078",
    "ACE_INTAKE v1 fid=abc discord=701119021172523078",
    # A Discord snowflake is never this short, so this is not an account id.
    "ACE_INTAKE v1 fid=821103058 discord=42",
    # Trailing junk could hide a second instruction.
    "ACE_INTAKE v1 fid=821103058 discord=701119021172523078 and drop the roster",
])
def test_rejects_anything_else(content):
    assert parse_intake(content) is None


# ── Validation: a handover is proved, never trusted ────────────────────────

import asyncio

from cogs import roster_intake as intake_module
from cogs.roster_intake import RosterIntake


class _StubCog:
    pass


def _cog(existing_row=None, alliance_name="ArabChampEmpire"):
    """A RosterIntake with the database and Discord layers replaced. __init__ is
    skipped so no db/ directory is created next to the tests."""
    cog = RosterIntake.__new__(RosterIntake)
    cog.bot = type("Bot", (), {"get_cog": staticmethod(lambda name: _StubCog())})()
    cog._config_cache = {}
    cog.inserted = []
    cog.attached = []
    cog._alliance_name = lambda alliance_id: alliance_name
    cog._user_row = lambda fid: existing_row
    cog._insert_user = lambda *args, **kwargs: cog.inserted.append((args, kwargs))
    cog._attach_discord = lambda *args: cog.attached.append(args)
    return cog


class _Guild:
    id = 1541772698546606090


def _run(cog, fid=821103058, discord_id=701119021172523078, state=None, name=None, furnace=None):
    return asyncio.run(cog.register_intake(_Guild(), 1, fid, discord_id, state, name, furnace))


def test_refuses_an_id_the_game_api_will_not_confirm(monkeypatch):
    """The bug this guards: a forwarded state must never stand in for a probe
    that did not match, or a mistyped ID enters the roster as a real member."""
    monkeypatch.setattr(intake_module, "get_alliance_kid", lambda alliance_id: 4562)

    async def no_match(cog, fid, alliance_id):
        return None, False
    monkeypatch.setattr(intake_module, "verify_add_state", no_match)

    cog = _cog()
    status, message = _run(cog, fid=999999999, state=4562)
    assert status == "state"
    assert "999999999" in message
    assert cog.inserted == []


def test_adds_an_id_the_game_api_confirms(monkeypatch):
    monkeypatch.setattr(intake_module, "get_alliance_kid", lambda alliance_id: 4562)

    async def match(cog, fid, alliance_id):
        return 4562, True
    monkeypatch.setattr(intake_module, "verify_add_state", match)

    cog = _cog()
    status, _ = _run(cog, name="NOUR", furnace=80)
    assert status == "added"
    # Name and level ride along, so the member is not filed as Player <id> at 0.
    assert cog.inserted == [
        ((821103058, 1, 4562, 701119021172523078, _Guild.id), {"nickname": "NOUR", "furnace_lv": 80})
    ]


def test_a_multistate_alliance_falls_back_to_the_forwarded_state(monkeypatch):
    """With no home state there is nothing to probe against, so the state the
    manager bot forwarded is all there is."""
    monkeypatch.setattr(intake_module, "get_alliance_kid", lambda alliance_id: None)
    cog = _cog()
    assert _run(cog, state=245)[0] == "added"
    assert cog.inserted[0][0][2] == 245

    cog = _cog()
    assert _run(cog, state=None)[0] == "state"
    assert cog.inserted == []


def test_never_moves_a_member_between_alliances(monkeypatch):
    cog = _cog(existing_row=(821103058, None, "7", "Someone", 0))
    assert _run(cog)[0] == "other-alliance"
    assert cog.attached == []


def test_refuses_an_id_linked_to_a_different_discord_account(monkeypatch):
    cog = _cog(existing_row=(821103058, 382474155691343885, "1", "Someone", 0))
    assert _run(cog)[0] == "conflict"
    assert cog.attached == []


def test_links_an_existing_unlinked_row(monkeypatch):
    cog = _cog(existing_row=(821103058, None, "1", "Someone", 0))
    assert _run(cog)[0] == "linked"
    assert cog.attached == [(821103058, 701119021172523078, _Guild.id)]


def test_reads_a_furnace_level_this_bot_understands():
    """ACE forwards the level as typed with the spaces squeezed out, and this
    bot is what decides FC10-2 means 82."""
    line = "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 state=4562 fc=FC10-2 name=NOUR"
    assert parse_intake(line) == (816479488, 701119021172523078, 4562, 82, "NOUR")
    plain = "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 fc=30"
    assert parse_intake(plain)[3] == 30
    # Unreadable is dropped, not stored as a number.
    assert parse_intake("ACE_INTAKE v1 fid=816479488 discord=701119021172523078 fc=banana")[3] is None
    # A name containing "fc=" is still a name, because the name runs last.
    assert parse_intake(
        "ACE_INTAKE v1 fid=816479488 discord=701119021172523078 name=fc=weird"
    )[4] == "fc=weird"


def test_fills_in_a_placeholder_row_from_a_handover(monkeypatch):
    """The row exists with no real name and no level, which is what the first
    handovers produced before either was carried."""
    recorded = {}

    def fake_edit(fid, **fields):
        recorded.update(fields)
        return list(fields)
    monkeypatch.setattr(intake_module, "apply_member_edit", fake_edit)

    cog = _cog(existing_row=(816479488, None, "1", "Player 816479488", 0))
    status, message = _run(cog, fid=816479488, name="NOUR", furnace=80)
    assert status == "linked"
    assert recorded == {"nickname": "NOUR", "furnace_lv": 80}

    # A member who already has a real name and level keeps them.
    recorded.clear()
    cog = _cog(existing_row=(816479488, None, "1", "Nour", 27))
    _run(cog, fid=816479488, name="SOMETHING ELSE", furnace=80)
    assert recorded == {}
