"""Tests des retards, du tri par échéance et de /suggest (fouettard/logic/tasks.py)."""

from datetime import date

from fouettard.logic.tasks import (build_priority_order, is_overdue, priority_rank,
                                   sort_by_due_date, suggest_issues)

TODAY = date(2026, 11, 10)


def make_issue(iid, due=None, labels=(), assignees=(), state="opened",
               created_at="2026-10-01T10:00:00Z"):
    """Fabrique une fausse issue avec seulement les champs utiles."""
    return {"iid": iid, "state": state, "due_date": due, "labels": list(labels),
            "assignees": [{"id": a} for a in assignees], "created_at": created_at}


# --- Retards ---------------------------------------------------------------

def test_overdue_when_due_date_is_past():
    assert is_overdue(make_issue(1, due="2026-11-09"), TODAY)


def test_not_overdue_on_due_date_itself():
    assert not is_overdue(make_issue(1, due="2026-11-10"), TODAY)


def test_not_overdue_without_due_date_or_when_closed():
    assert not is_overdue(make_issue(1), TODAY)
    assert not is_overdue(make_issue(1, due="2026-01-01", state="closed"), TODAY)


def test_sort_by_due_date_puts_issues_without_date_last():
    issues = [make_issue(1), make_issue(2, due="2026-12-01"), make_issue(3, due="2026-11-15")]
    assert [issue["iid"] for issue in sort_by_due_date(issues)] == [3, 2, 1]


# --- Priorités -------------------------------------------------------------

LABELS = [{"name": "prio::P2-important", "priority": None},
          {"name": "bloque", "priority": None},
          {"name": "prio::P1-PoC", "priority": None},
          {"name": "prio::P3-bonus", "priority": None}]


def test_priority_order_by_name():
    assert build_priority_order(LABELS) == ["prio::P1-PoC", "prio::P2-important", "prio::P3-bonus"]


def test_gitlab_priority_wins_over_name():
    labels = [{"name": "prio::P1-PoC", "priority": 5}, {"name": "prio::P2-important", "priority": 1}]
    assert build_priority_order(labels) == ["prio::P2-important", "prio::P1-PoC"]


def test_issue_without_priority_comes_last():
    order = build_priority_order(LABELS)
    assert priority_rank(make_issue(1, labels=["prio::P1-PoC"]), order) == 0
    assert priority_rank(make_issue(1), order) == 3


# --- /suggest --------------------------------------------------------------

def test_suggest_sorts_by_priority_then_due_date_then_age():
    order = build_priority_order(LABELS)
    issues = [
        make_issue(1, labels=["prio::P3-bonus"], due="2026-11-11"),
        make_issue(2, labels=["prio::P1-PoC"], due="2026-12-20"),
        make_issue(3, labels=["prio::P1-PoC"], due="2026-11-20"),
        make_issue(4, labels=["prio::P1-PoC"], created_at="2026-09-01T10:00:00Z"),
        make_issue(5, labels=["prio::P1-PoC"], created_at="2026-08-01T10:00:00Z"),
    ]
    result = suggest_issues(issues, order)
    # P1 d'abord (par échéance, puis les plus anciennes sans échéance), P3 ensuite
    assert [issue["iid"] for issue in result.issues] == [3, 2, 5, 4, 1]


def test_suggest_skips_assigned_issues():
    issues = [make_issue(1, assignees=[42]), make_issue(2)]
    assert [issue["iid"] for issue in suggest_issues(issues, []).issues] == [2]


def test_suggest_hides_unavailable_labels_by_default():
    issues = [make_issue(1, labels=["bloque"]), make_issue(2, labels=["Doing"]),
              make_issue(3, labels=["Doing"]), make_issue(4, labels=["To Do"])]
    result = suggest_issues(issues, [])
    assert [issue["iid"] for issue in result.issues] == [4]
    assert result.hidden == {"bloque": 1, "Doing": 2}


def test_suggest_can_include_a_hidden_label():
    issues = [make_issue(1, labels=["bloque"]), make_issue(2, labels=["Doing"])]
    result = suggest_issues(issues, [], included_labels={"bloque"})
    assert [issue["iid"] for issue in result.issues] == [1]
    assert result.hidden == {"Doing": 1}


def test_suggest_shows_at_most_five_issues():
    issues = [make_issue(i) for i in range(10)]
    assert len(suggest_issues(issues, []).issues) == 5
