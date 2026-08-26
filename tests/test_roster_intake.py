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
