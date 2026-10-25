"""Tests de /team (logic/team.py), des dates clés (logic/key_dates.py)
et de la lecture de config/deadlines.yaml."""

from datetime import date

import pytest

from fouettard.config import CONFIG_DIR, ConfigError, load_key_dates
from fouettard.logic.key_dates import KeyDate, days_until, is_past, is_this_week
from fouettard.logic.team import summarize_team

TODAY = date(2026, 11, 10)


# --- /team -----------------------------------------------------------------

def test_team_summary():
    members = [{"id": 1, "name": "Bob", "username": "bob"},
               {"id": 2, "name": "Alice", "username": "alice"},
               {"id": 3, "name": "Bot", "username": "project_12_bot_abc"}]
    open_issues = [
        {"state": "opened", "due_date": "2026-11-01", "assignees": [{"id": 1}], "health_status": "at_risk"},
        {"state": "opened", "due_date": None, "assignees": [{"id": 1}, {"id": 2}]},
    ]
    events = [{"author_id": 1, "created_at": "2026-11-08T10:00:00Z"},
              {"author_id": 1, "created_at": "2026-11-09T10:00:00Z"}]

    summaries = summarize_team(members, open_issues, events, TODAY)

    # Trié par nom, compte technique exclu
    assert [s.username for s in summaries] == ["alice", "bob"]
    alice, bob = summaries
    assert (bob.open_issues, bob.overdue, bob.at_risk) == (2, 1, 1)
    assert bob.last_activity.date() == date(2026, 11, 9)
    assert (alice.open_issues, alice.overdue) == (1, 0)
    assert alice.last_activity is None


# --- Dates clés ------------------------------------------------------------

SOUTENANCE = KeyDate("Soutenance 1", date(2027, 1, 4))  # un lundi


def test_days_until_key_date():
    assert days_until(SOUTENANCE, date(2026, 12, 21)) == 14


def test_key_date_week():
    assert is_this_week(SOUTENANCE, date(2027, 1, 4))   # lundi
    assert is_this_week(SOUTENANCE, date(2027, 1, 10))  # dimanche
    assert not is_past(SOUTENANCE, date(2027, 1, 10))
    assert is_past(SOUTENANCE, date(2027, 1, 11))


def test_real_deadlines_file_is_valid():
    key_dates = load_key_dates(CONFIG_DIR / "deadlines.yaml")
    assert len(key_dates) == 4
    # Toutes les dates doivent être des lundis
    assert all(key_date.week_start.weekday() == 0 for key_date in key_dates)


def test_invalid_deadlines_file(tmp_path):
    path = tmp_path / "deadlines.yaml"
    path.write_text("deadlines:\n  - name: Oral\n    week_start: bientot\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_key_dates(path)
