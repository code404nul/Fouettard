"""Lecture du GitLab J3D pour le planning : temps à réserver et points d'attention.

Réutilise la logique existante (logic/tasks.py) et ne parle jamais à GitLab directement.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fouettard.logic.tasks import due_date, is_assigned_to
from fouettard.timeutils import parse_gitlab_datetime


@dataclass
class J3dNeed:
    minutes: int = 0
    deadline: date | None = None
    deadline_name: str = ""
    impossible: bool = False  # plus aucun jour de travail avant l'échéance
    urgent: bool = False


def milestone_date(milestone: dict) -> date | None:
    text = milestone.get("due_date")
    return date.fromisoformat(text) if text else None


def issue_deadline(issue: dict) -> date | None:
    """Échéance de l'issue, ou à défaut celle de sa milestone."""
    return due_date(issue) or milestone_date(issue.get("milestone") or {})


def my_issues(open_issues: list[dict], gitlab_id: int | None) -> list[dict]:
    if gitlab_id is None:
        return []
    return [issue for issue in open_issues if is_assigned_to(issue, gitlab_id)]


def remaining_hours(issue: dict, default_hours: float) -> float:
    """Temps restant : estimation GitLab moins temps passé, sinon valeur par défaut."""
    stats = issue.get("time_stats") or {}
    estimate = stats.get("time_estimate") or 0
    if not estimate:
        return default_hours
    return max(0.0, (estimate - (stats.get("total_time_spent") or 0)) / 3600)


def compute_need(issues: list[dict], day: date, is_workday: Callable[[date], bool],
                 default_hours: float, urgent_hours: int) -> J3dNeed:
    """Temps J3D à réserver ce jour-là pour tenir toutes les échéances.

    Pour chaque échéance : (travail dû jusque-là) / (jours de travail restants).
    On garde le maximum. Un jour léger retire un jour de travail : son travail est
    donc naturellement avancé sur les jours précédents.
    """
    dated = sorted(((issue_deadline(issue), issue) for issue in issues
                    if issue_deadline(issue) is not None), key=lambda pair: pair[0])
    need = J3dNeed()
    cumulative = 0.0
    best_rate = 0.0
    for deadline, issue in dated:
        cumulative += remaining_hours(issue, default_hours)
        if cumulative <= 0:
            continue
        if deadline <= day + timedelta(hours=urgent_hours):
            need.urgent = True
        # Une échéance passée compte sur la journée : c'est du retard à rattraper
        span = max(0, (deadline - day).days)
        workdays = sum(is_workday(day + timedelta(days=offset)) for offset in range(span + 1))
        if workdays == 0:
            need.impossible = True
            workdays = 1
        rate = cumulative / workdays
        if rate > best_rate:
            best_rate = rate
            need.deadline = deadline
            need.deadline_name = issue.get("title", "")
    # Arrondi à la demi-heure supérieure
    need.minutes = math.ceil(best_rate * 2) * 30
    return need


def active_milestone(milestones: list[dict], day: date) -> dict | None:
    """Prochaine milestone à venir. Sert au temps minimal même sans issue attribuée."""
    upcoming = [milestone for milestone in milestones
                if milestone_date(milestone) and milestone_date(milestone) >= day]
    return min(upcoming, key=milestone_date) if upcoming else None


def slipping_milestones(milestones: list[dict], issues: list[dict], today: date) -> list[dict]:
    """Milestones dépassées dont il reste des issues ouvertes."""
    open_titles = {(issue.get("milestone") or {}).get("title") for issue in issues}
    return [milestone for milestone in milestones
            if milestone_date(milestone) and milestone_date(milestone) < today
            and milestone.get("title") in open_titles]


def stale_issues(issues: list[dict], now: datetime, stale_days: int) -> list[dict]:
    return [issue for issue in issues if issue.get("updated_at")
            and now - parse_gitlab_datetime(issue["updated_at"]) >= timedelta(days=stale_days)]


def merge_requests_to_review(merge_requests: list[dict], gitlab_id: int | None) -> list[dict]:
    if gitlab_id is None:
        return []
    return [mr for mr in merge_requests if not mr.get("draft")
            and any(reviewer["id"] == gitlab_id for reviewer in mr.get("reviewers") or [])]
