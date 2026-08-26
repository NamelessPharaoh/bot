"""Roster intake payload parsing.

The handover line is the trust boundary with the paired manager bot, so the
parser is deliberately strict: anything that is not exactly the versioned form
is ignored rather than guessed at.
"""
import pytest

from cogs.roster_intake import parse_intake


def test_parses_the_minimum_payload():
    assert parse_intake("ACE_INTAKE v1 fid=821103058 discord=701119021172523078") == (
        821103058, 701119021172523078, None
    )


def test_parses_an_optional_state():
    assert parse_intake(
        "ACE_INTAKE v1 fid=821103058 discord=701119021172523078 state=4562"
    ) == (821103058, 701119021172523078, 4562)


def test_tolerates_surrounding_whitespace():
    assert parse_intake("  ACE_INTAKE  v1   fid=1 discord=701119021172523078  \n") == (
        1, 701119021172523078, None
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
    cog._insert_user = lambda *args: cog.inserted.append(args)
    cog._attach_discord = lambda *args: cog.attached.append(args)
    return cog


class _Guild:
    id = 1541772698546606090


def _run(cog, fid=821103058, discord_id=701119021172523078, state=None):
    return asyncio.run(cog.register_intake(_Guild(), 1, fid, discord_id, state))


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
    status, _ = _run(cog)
    assert status == "added"
    assert cog.inserted == [(821103058, 1, 4562, 701119021172523078, _Guild.id)]


def test_a_multistate_alliance_falls_back_to_the_forwarded_state(monkeypatch):
    """With no home state there is nothing to probe against, so the state the
    manager bot forwarded is all there is."""
    monkeypatch.setattr(intake_module, "get_alliance_kid", lambda alliance_id: None)
    cog = _cog()
    assert _run(cog, state=245)[0] == "added"
    assert cog.inserted[0][2] == 245

    cog = _cog()
    assert _run(cog, state=None)[0] == "state"
    assert cog.inserted == []


def test_never_moves_a_member_between_alliances(monkeypatch):
    cog = _cog(existing_row=(821103058, None, "7", "Someone"))
    assert _run(cog)[0] == "other-alliance"
    assert cog.attached == []


def test_refuses_an_id_linked_to_a_different_discord_account(monkeypatch):
    cog = _cog(existing_row=(821103058, 382474155691343885, "1", "Someone"))
    assert _run(cog)[0] == "conflict"
    assert cog.attached == []


def test_links_an_existing_unlinked_row(monkeypatch):
    cog = _cog(existing_row=(821103058, None, "1", "Someone"))
    assert _run(cog)[0] == "linked"
    assert cog.attached == [(821103058, 701119021172523078, _Guild.id)]
