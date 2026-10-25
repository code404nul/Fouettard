"""Tests du récap des contributions (logic/recap.py) et de /checklist (logic/deliverables.py)."""

from datetime import date

from fouettard.logic.deliverables import ALMOST_EMPTY, MISSING, OK, check_deliverables
from fouettard.logic.recap import (build_recap, closed_in_period, find_commit_author,
                                   learn_commit_emails, normalize)

ALICE = {"id": 1, "name": "Alice Martin", "username": "alice.martin"}
FARES = {"id": 2, "name": "Farès Bentoumi", "username": "fares.bentoumi"}


def make_commit(sha, email, name="?", parents=1, created_at="2026-11-05T10:00:00Z"):
    return {"id": sha, "title": f"commit {sha}", "web_url": f"https://gitlab/c/{sha}",
            "author_email": email, "author_name": name, "created_at": created_at,
            "parent_ids": ["p"] * parents}


def push(author_id, commit_to):
    return {"author_id": author_id, "push_data": {"commit_to": commit_to}}


# --- Attribution des commits -----------------------------------------------

def test_normalize_removes_accents():
    assert normalize("  Farès ") == "fares"


def test_learn_emails_from_pushes_with_majority():
    commits = {"a": make_commit("a", "perso@gmail.com"), "b": make_commit("b", "perso@gmail.com"),
               "c": make_commit("c", "perso@gmail.com")}
    # 2 push par Alice, 1 par Farès (qui a poussé un commit d'Alice) : Alice gagne
    pushes = [push(1, "a"), push(1, "b"), push(2, "c")]
    assert learn_commit_emails(pushes, commits) == {"perso@gmail.com": 1}


def test_find_author_by_name_or_school_email():
    members = [ALICE, FARES]
    # Nom git sans accent = nom GitLab avec accent
    assert find_commit_author(make_commit("x", "z@z.fr", "Fares Bentoumi"), members, {}) == 2
    # Email "username@..." de l'école
    assert find_commit_author(make_commit("x", "alice.martin@epita.fr"), members, {}) == 1
    # Inconnu
    assert find_commit_author(make_commit("x", "pc@maison.local", "DESKTOP-42"), members, {}) is None


def test_build_recap():
    commits = [
        make_commit("a", "perso@gmail.com", "pseudo"),     # reconnu grâce au push
        make_commit("b", "fares.bentoumi@epita.fr"),       # reconnu par l'email
        make_commit("m", "alice.martin@epita.fr", parents=2),  # merge : exclu
        make_commit("z", "inconnu@x.fr", "inconnu"),       # non attribué
    ]
    closed = [{"iid": 7, "title": "t", "web_url": "u", "closed_at": "2026-11-06T10:00:00Z",
               "assignees": [], "closed_by": {"id": 1}}]

    recaps, unattributed = build_recap([ALICE, FARES], commits, [push(1, "a")], closed)

    alice, fares = recaps
    assert [c["id"] for c in alice.commits] == ["a"]
    assert [c["id"] for c in fares.commits] == ["b"]
    assert [i["iid"] for i in alice.issues] == [7]  # sans assigné : celle qui a fermé
    assert [c["id"] for c in unattributed] == ["z"]


def test_closed_in_period_uses_paris_time():
    issues = [
        # 30/11 à 23h30 UTC = 1er décembre à 00h30 à Paris : hors période
        {"state": "closed", "closed_at": "2026-11-30T23:30:00Z"},
        {"state": "closed", "closed_at": "2026-11-15T10:00:00Z"},
        {"state": "opened", "closed_at": None},
    ]
    result = closed_in_period(issues, date(2026, 11, 1), date(2026, 11, 30))
    assert result == [issues[1]]


# --- /checklist ------------------------------------------------------------

def test_check_deliverables():
    sizes = {
        "README.md": 3000,
        "docs/install.md": 50,          # quasi vide
        "docs/user_manual.md": None,    # absent
        "docs/architecture.md": 800,
        "docs/ai_journal.md": None,
        "docs/ai_journal.xlsx": 100,    # xlsx : seule la présence compte
    }
    statuses = {r.deliverable.name: r for r in check_deliverables(sizes, min_size=200)}
    assert statuses["README"].status == OK
    assert statuses["Guide d'installation"].status == ALMOST_EMPTY
    assert statuses["Manuel utilisateur"].status == MISSING
    assert statuses["Journal IA"].status == OK
    assert statuses["Journal IA"].path == "docs/ai_journal.xlsx"
