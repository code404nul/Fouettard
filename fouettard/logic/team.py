"""Vue d'ensemble de l'équipe : issues et dernière activité de chaque membre."""

import re
from dataclasses import dataclass
from datetime import date, datetime

from fouettard.logic.tasks import is_assigned_to, is_overdue
from fouettard.timeutils import parse_gitlab_datetime

# Période d'activité examinée (au-delà : "aucune activité depuis 30 jours")
ACTIVITY_LOOKBACK_DAYS = 30

# Comptes techniques créés par GitLab pour les tokens de projet/groupe
BOT_USERNAME = re.compile(r"^(project|group)_\d+_bot")


@dataclass
class MemberSummary:
    """Résumé d'un membre pour /team."""

    gitlab_id: int
    name: str
    username: str
    open_issues: int
    overdue: int
    at_risk: int                    # issues "à risque" (health status), 0 si non disponible
    last_activity: datetime | None  # None = aucune activité récente trouvée


def is_bot_account(member: dict) -> bool:
    return bool(BOT_USERNAME.match(member["username"]))


def last_activity_by_author(events: list[dict]) -> dict[int, datetime]:
    """Date de la dernière activité de chaque auteur (id GitLab -> date)."""
    latest: dict[int, datetime] = {}
    for event in events:
        author_id = event["author_id"]
        created_at = parse_gitlab_datetime(event["created_at"])
        if author_id not in latest or created_at > latest[author_id]:
            latest[author_id] = created_at
    return latest


def summarize_team(members: list[dict], open_issues: list[dict],
                   events: list[dict], today: date) -> list[MemberSummary]:
    """Un résumé par membre (hors comptes techniques), trié par nom."""
    last_activity = last_activity_by_author(events)
    summaries = []
    for member in members:
        if is_bot_account(member):
            continue
        mine = [issue for issue in open_issues if is_assigned_to(issue, member["id"])]
        summaries.append(MemberSummary(
            gitlab_id=member["id"],
            name=member["name"],
            username=member["username"],
            open_issues=len(mine),
            overdue=sum(1 for issue in mine if is_overdue(issue, today)),
            at_risk=sum(1 for issue in mine if issue.get("health_status") == "at_risk"),
            last_activity=last_activity.get(member["id"]),
        ))
    return sorted(summaries, key=lambda summary: summary.name.lower())
