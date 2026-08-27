"""pytest bootstrap for the in-repo test suite.

Ensures the repo root (so `import cogs.*` resolves) and this tests dir (so
`from harness import ...` resolves) are importable no matter where pytest is
invoked from. Also skips suites whose target cog isn't present in the current
checkout, so the run never hard-errors on a version that lacks a feature.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# main.py calls colorama.init(autoreset=True) at import time (main.py:662), and
# that permanently replaces sys.stdout with a StreamWrapper bound to whatever
# stream was live at the moment it ran. Under pytest that stream is the capture
# object, so once capture ends the terminal reporter writes into a dead stream:
# the whole session prints NOTHING while still exiting 0. Any suite that imports
# main (tests/test_main_bootstrap.py does, at module scope) silences every other
# test in the same run. Neutralise the global wrap for the test session - main
# binds `init` by name at import, so this must land before collection, which is
# exactly when conftest is imported. Nothing under test needs ANSI translation.
try:
    import colorama

    colorama.init = lambda *args, **kwargs: None
except ImportError:  # colorama absent in a minimal checkout; nothing to disarm
    pass

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
# The tree under test. Defaults to the repo this suite lives in. A deploy gate
# sets WOS_TEST_TREE to an install root (e.g. Bot-runtime/) so the suite checks
# the payload that is about to be restarted rather than the source tree, which
# lags it: the two cogs/ trees drift by design and a green source run says
# nothing about the files systemd loads.
TREE = Path(os.environ.get("WOS_TEST_TREE") or _REPO).resolve()
# Later inserts land earlier, so TREE outranks _REPO for `import cogs.*` while
# _HERE stays first for `from harness import ...`.
for _p in (str(_REPO), str(TREE), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Skip suites whose target module isn't in this checkout (e.g. the attendance
# OCR parsers may live on a different branch/version). Keeps `pytest tests`
# green here while the suites auto-run wherever the feature exists.
collect_ignore = []
_REQUIRES = {
    "test_attendance_ocr_layer1.py": "cogs/attendance_ocr_parsers.py",
    "test_attendance_ocr_layer2.py": "cogs/attendance_ocr_parsers.py",
    "test_attendance_ocr_alias.py": "cogs/attendance_ocr_parsers.py",
    "test_attendance_ocr_fallback.py": "cogs/attendance_ocr_parsers.py",
    "test_attendance_history.py": "cogs/attendance_history.py",
    "test_layer1_parser.py": "cogs/bear_track.py",
    "test_layer2_ocr.py": "cogs/bear_track.py",
    "test_bear_name_matching.py": "cogs/bear_track.py",
    "test_bear_persist_no_deadlock.py": "cogs/bear_track.py",
    "test_ocr_auto_manage.py": "cogs/bear_track.py",
}
for _test_file, _needed in _REQUIRES.items():
    if not (TREE / _needed).exists():
        collect_ignore.append(_test_file)


@pytest.fixture
def make_templates_cog():
    """Builds a NotificationTemplates cog on a fresh in-memory database."""
    import importlib
    templates = importlib.import_module("cogs.notification_templates")

    def _build():
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE bear_notifications (id INTEGER PRIMARY KEY, event_type TEXT)")
        conn.commit()

        cog = templates.NotificationTemplates.__new__(templates.NotificationTemplates)
        cog.conn = conn
        cog.cursor = conn.cursor()
        cog._setup_database()
        return cog

    return _build
