"""Tests de la détection des alertes (logic/alerts.py) et de leur texte final."""

from datetime import date, datetime, timedelta

from fouettard.formatting import render_alert
from fouettard.logic.alerts import (Alert, due_tomorrow_alerts, inactivity_alerts, key_date_alerts,
                                    overdue_alerts, waiting_merge_request_alerts)
from fouettard.logic.key_dates import KeyDate
from fouettard.timeutils import PARIS

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=PARIS)
ALICE = {"id": 1, "name": "Alice", "username": "alice"}
BOB = {"id": 2, "name": "Bob", "username": "bob"}


def make_issue(iid, due=None, assignees=(ALICE,), state="opened"):
    return {"iid": iid, "title": f"Tâche {iid}", "web_url": f"https://gitlab/i/{iid}",
            "state": state, "due_date": due, "assignees": list(assignees)}


def make_mr(iid, created_days_ago, draft=False, reviewers=(BOB,)):
    created = (NOW - timedelta(days=created_days_ago)).isoformat()
    return {"iid": iid, "title": f"MR {iid}", "web_url": f"https://gitlab/mr/{iid}",
            "draft": draft, "created_at": created, "reviewers": list(reviewers), "author": ALICE}


# --- Échéances -------------------------------------------------------------

def test_overdue_alert():
    alerts = overdue_alerts([make_issue(1, due="2026-11-07"), make_issue(2, due="2026-11-20")],
                            TODAY, repeat_days=2)
    assert len(alerts) == 1
    assert alerts[0].key == "overdue:issue:1"
    assert alerts[0].repeat_after == timedelta(days=2)
    assert "3 jours" in alerts[0].text


def test_due_tomorrow_alert_is_sent_once_and_depends_on_date():
    alerts = due_tomorrow_alerts([make_issue(1, due="2026-11-11"), make_issue(2, due="2026-11-12")],
                                 TODAY)
    assert [alert.key for alert in alerts] == ["due_tomorrow:issue:1:2026-11-11"]
    assert alerts[0].repeat_after is None


# --- Inactivité ------------------------------------------------------------

def test_inactivity_alert():
    events = [{"author_id": 1, "created_at": "2026-11-09T10:00:00Z"},  # Alice : hier
              {"author_id": 2, "created_at": "2026-11-02T10:00:00Z"}]  # Bob : il y a 8 jours
    bot_account = {"id": 3, "name": "Bot", "username": "project_5_bot_x"}
    alerts = inactivity_alerts([ALICE, BOB, bot_account], events, NOW,
                               inactivity_days=5, lookback_days=30)
    assert [alert.key for alert in alerts] == ["inactive:2"]
    assert "7 jours" in alerts[0].text  # 7 jours et quelques heures


def test_member_without_any_event_is_inactive():
    alerts = inactivity_alerts([ALICE], [], NOW, inactivity_days=5, lookback_days=30)
    assert "plus de 30 jours" in alerts[0].text


# --- Merge requests --------------------------------------------------------

def test_waiting_merge_requests():
    mrs = [make_mr(1, created_days_ago=3),               # en attente -> alerte
           make_mr(2, created_days_ago=1),               # trop récente
           make_mr(3, created_days_ago=5, draft=True),   # brouillon
           make_mr(4, created_days_ago=5)]               # déjà approuvée
    alerts = waiting_merge_request_alerts(mrs, approved_iids={4}, now=NOW, review_days=2)
    assert [alert.key for alert in alerts] == ["mr_waiting:1"]
    assert alerts[0].people == [BOB]  # on mentionne le reviewer


def test_merge_request_without_reviewer_mentions_author():
    alerts = waiting_merge_request_alerts([make_mr(1, 3, reviewers=())], set(), NOW, 2)
    assert alerts[0].people == [ALICE]
    assert "aucun reviewer" in alerts[0].text


# --- Dates clés ------------------------------------------------------------

SOUTENANCE = KeyDate("Soutenance 1", date(2027, 1, 4))


def keys_at(day):
    return [alert.key.split(":")[-1] for alert in key_date_alerts([SOUTENANCE], day, [14, 7, 2])]


def test_key_date_reminders():
    assert keys_at(date(2026, 12, 20)) == []        # J-15 : rien
    assert keys_at(date(2026, 12, 21)) == ["J-14"]  # J-14
    assert keys_at(date(2026, 12, 28)) == ["J-7"]   # J-7
    assert keys_at(date(2027, 1, 2)) == ["J-2"]     # J-2
    assert keys_at(date(2027, 1, 4)) == []          # le jour même : plus de rappel


def test_key_date_reminder_catches_up_after_downtime():
    # Bot éteint à J-7 : à J-6, on envoie quand même le rappel "J-7"
    assert keys_at(date(2026, 12, 29)) == ["J-7"]


# --- Texte final -----------------------------------------------------------

def test_render_alert_mentions_linked_people_only():
    alert = Alert("k", None, "⚠️ {mentions} : retard", people=[ALICE, BOB])
    assert render_alert(alert, {1: 999}) == "⚠️ <@999>, **Bob** : retard"


def test_render_alert_without_people():
    alert = Alert("k", None, "⚠️ {mentions} : retard")
    assert "personne n'est assigné" in render_alert(alert, {})
