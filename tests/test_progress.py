"""Tests du calcul d'avancement (fouettard/logic/progress.py)."""

from fouettard.logic.progress import (compute_progress, compute_progress_by, domain_name,
                                      iteration_name, milestone_name, progress_bar,
                                      should_use_weights, uses_domain_labels)


def make_issue(state="opened", weight=None, milestone=None):
    """Fabrique une fausse issue avec seulement les champs utiles."""
    issue = {"state": state, "weight": weight}
    if milestone:
        issue["milestone"] = {"title": milestone}
    return issue


def test_progress_counts_closed_issues():
    issues = [make_issue("closed"), make_issue("closed"), make_issue("opened"), make_issue("opened")]
    progress = compute_progress(issues)
    assert progress.done == 2
    assert progress.total == 4
    assert progress.percent == 50.0


def test_progress_without_issues_is_zero():
    assert compute_progress([]).percent == 0.0


def test_weighted_progress():
    issues = [make_issue("closed", weight=3), make_issue("opened", weight=1)]
    progress = compute_progress(issues, use_weights=True)
    assert (progress.done, progress.total) == (3, 4)
    assert progress.percent == 75.0


def test_issue_without_weight_counts_as_one():
    issues = [make_issue("closed", weight=None), make_issue("opened", weight=3)]
    progress = compute_progress(issues, use_weights=True)
    assert (progress.done, progress.total) == (1, 4)


def test_should_use_weights():
    with_weight = [make_issue(weight=2), make_issue()]
    without_weight = [make_issue(), make_issue()]
    assert should_use_weights(with_weight, weights_available=True)
    # Licence gratuite : jamais de pondération
    assert not should_use_weights(with_weight, weights_available=False)
    # Licence payante mais personne n'a mis de poids : pas de pondération
    assert not should_use_weights(without_weight, weights_available=True)


def test_progress_by_milestone():
    issues = [
        make_issue("closed", milestone="Sprint 2"),
        make_issue("opened", milestone="Sprint 2"),
        make_issue("closed", milestone="Sprint 1"),
        make_issue("opened"),
    ]
    groups = compute_progress_by(issues, milestone_name)
    # Tri par nom, "Sans milestone" à la fin
    assert list(groups) == ["Sprint 1", "Sprint 2", "Sans milestone"]
    assert groups["Sprint 1"].percent == 100.0
    assert groups["Sprint 2"].percent == 50.0
    assert groups["Sans milestone"].percent == 0.0


def test_iteration_without_title_uses_dates():
    issue = {"iteration": {"title": None, "start_date": "2026-11-02", "due_date": "2026-11-15"}}
    assert iteration_name(issue) == "2026-11-02 → 2026-11-15"
    assert iteration_name({}) == "Sans itération"


def test_domain_from_label():
    assert domain_name({"labels": ["type::bug", "domaine::gameplay"]}) == "gameplay"
    assert domain_name({"labels": ["type::bug"]}) == "Sans domaine"
    assert uses_domain_labels([{"labels": []}, {"labels": ["domaine::son"]}])
    assert not uses_domain_labels([{"labels": ["bloque"]}])


def test_progress_bar():
    assert progress_bar(0, width=10) == "░" * 10
    assert progress_bar(50, width=10) == "█" * 5 + "░" * 5
    assert progress_bar(100, width=10) == "█" * 10
