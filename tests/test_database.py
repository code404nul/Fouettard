"""Tests de la base SQLite : liaisons et anti-spam."""

from datetime import datetime, timedelta

import pytest

from fouettard.database import Database
from fouettard.timeutils import PARIS


@pytest.fixture
def db():
    """Base temporaire en mémoire, recréée pour chaque test."""
    database = Database(":memory:")
    yield database
    database.close()


NOW = datetime(2026, 11, 10, 9, 0, tzinfo=PARIS)


def test_save_and_read_link(db):
    db.save_link(discord_id=111, gitlab_id=42, gitlab_username="alice")
    link = db.get_link_by_discord(111)
    assert link.gitlab_username == "alice"
    assert db.get_link_by_gitlab_id(42).discord_id == 111
    assert db.get_link_by_discord(999) is None


def test_relinking_replaces_old_link(db):
    db.save_link(111, 42, "alice")
    db.save_link(111, 43, "alice2")
    assert db.get_link_by_discord(111).gitlab_username == "alice2"
    assert len(db.get_all_links()) == 1


def test_new_alert_is_not_sent_yet(db):
    assert not db.was_alert_sent_recently("overdue:issue:1", NOW, timedelta(days=2))


def test_alert_is_blocked_until_delay_is_over(db):
    db.mark_alert_sent("overdue:issue:1", NOW)
    # 1 jour plus tard : toujours bloquée
    assert db.was_alert_sent_recently("overdue:issue:1", NOW + timedelta(days=1), timedelta(days=2))
    # 2 jours plus tard : on peut la renvoyer
    assert not db.was_alert_sent_recently("overdue:issue:1", NOW + timedelta(days=2), timedelta(days=2))


def test_one_time_alert_is_never_resent(db):
    db.mark_alert_sent("due_tomorrow:issue:1", NOW)
    assert db.was_alert_sent_recently("due_tomorrow:issue:1", NOW + timedelta(days=365), None)


def test_alerts_are_independent(db):
    db.mark_alert_sent("overdue:issue:1", NOW)
    assert not db.was_alert_sent_recently("overdue:issue:2", NOW, None)
