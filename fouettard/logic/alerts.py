"""Détection des alertes automatiques.

Chaque fonction regarde les données GitLab et renvoie la liste des alertes
qui "devraient" partir. C'est ensuite le cog des alertes qui décide, grâce à
l'anti-spam de la base de données, si elles ont déjà été envoyées.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from fouettard.logic.key_dates import KeyDate, days_until
from fouettard.logic.tasks import due_date, is_overdue, issue_link, markdown_link
from fouettard.logic.team import is_bot_account, last_activity_by_author
from fouettard.timeutils import parse_gitlab_datetime


@dataclass
class Alert:
    """Une alerte à envoyer dans le salon.

    key          : identifiant unique de l'alerte (pour l'anti-spam)
    repeat_after : délai avant de pouvoir renvoyer la même alerte (None = jamais)
    text         : le message ; "{mentions}" sera remplacé par les personnes concernées
    people       : personnes à mentionner, sous la forme {"id": ..., "name": ...}
    """

    key: str
    repeat_after: timedelta | None
    text: str
    people: list[dict] = field(default_factory=list)


def plural_days(days: int) -> str:
    return f"{days} jour" + ("s" if days > 1 else "")


# --- Échéances des issues --------------------------------------------------

def overdue_alerts(open_issues: list[dict], today: date, repeat_days: int) -> list[Alert]:
    """Issues ouvertes dont l'échéance est passée (rappel tous les repeat_days jours)."""
    alerts = []
    for issue in open_issues:
        if not is_overdue(issue, today):
            continue
        late = (today - due_date(issue)).days
        alerts.append(Alert(
            key=f"overdue:issue:{issue['iid']}",
            repeat_after=timedelta(days=repeat_days),
            text=f"⚠️ {{mentions}} : l'issue {issue_link(issue)} est en retard "
                 f"de {plural_days(late)}.",
            people=issue.get("assignees") or [],
        ))
    return alerts


def due_tomorrow_alerts(open_issues: list[dict], today: date) -> list[Alert]:
    """Issues ouvertes dont l'échéance est demain (un seul rappel)."""
    tomorrow = today + timedelta(days=1)
    alerts = []
    for issue in open_issues:
        if due_date(issue) != tomorrow:
            continue
        alerts.append(Alert(
            # La date fait partie de la clé : si l'échéance change, on refera un rappel
            key=f"due_tomorrow:issue:{issue['iid']}:{tomorrow.isoformat()}",
            repeat_after=None,
            text=f"⏰ {{mentions}} : l'issue {issue_link(issue)} est à rendre **demain**.",
            people=issue.get("assignees") or [],
        ))
    return alerts


# --- Inactivité ------------------------------------------------------------

def inactivity_alerts(members: list[dict], events: list[dict], now: datetime,
                      inactivity_days: int, lookback_days: int) -> list[Alert]:
    """Membres sans aucune activité GitLab depuis inactivity_days jours.

    events : l'activité du projet sur les lookback_days derniers jours.
    """
    last_activity = last_activity_by_author(events)
    alerts = []
    for member in members:
        if is_bot_account(member):
            continue
        last = last_activity.get(member["id"])
        if last is None:
            since = f"plus de {plural_days(lookback_days)}"
        elif now - last >= timedelta(days=inactivity_days):
            since = plural_days((now - last).days)
        else:
            continue  # actif récemment
        alerts.append(Alert(
            key=f"inactive:{member['id']}",
            repeat_after=timedelta(days=inactivity_days),
            text=f"👋 {{mentions}}, on n'a pas vu d'activité de ta part sur GitLab depuis "
                 f"{since}. Tout va bien ? Si tu bloques sur quelque chose, n'hésite pas "
                 f"à demander un coup de main à l'équipe 🙂",
            people=[member],
        ))
    return alerts


# --- Merge requests --------------------------------------------------------

def waiting_merge_request_alerts(open_mrs: list[dict], approved_iids: set[int],
                                 now: datetime, review_days: int) -> list[Alert]:
    """Merge requests qui attendent une review depuis plus de review_days jours.

    On ignore les brouillons (draft) et les MR déjà approuvées.
    L'attente est comptée depuis la création de la MR.
    """
    alerts = []
    for mr in open_mrs:
        if mr.get("draft") or mr["iid"] in approved_iids:
            continue
        waiting = now - parse_gitlab_datetime(mr["created_at"])
        if waiting <= timedelta(days=review_days):
            continue

        mr_link = markdown_link(f"!{mr['iid']} {mr['title']}", mr["web_url"])
        reviewers = mr.get("reviewers") or []
        if reviewers:
            text = (f"🔍 {{mentions}} : la merge request {mr_link} attend votre review "
                    f"depuis {plural_days(waiting.days)}.")
            people = reviewers
        else:
            # Personne n'est désigné : on prévient l'auteur pour qu'il en choisisse un
            text = (f"🔍 {{mentions}} : ta merge request {mr_link} attend depuis "
                    f"{plural_days(waiting.days)} et n'a aucun reviewer. "
                    f"Demande à quelqu'un de la relire !")
            people = [mr["author"]]
        alerts.append(Alert(key=f"mr_waiting:{mr['iid']}", repeat_after=timedelta(days=1),
                            text=text, people=people))
    return alerts


# --- Dates clés de la SAE --------------------------------------------------

def key_date_alerts(key_dates: list[KeyDate], today: date,
                    reminder_days: list[int]) -> list[Alert]:
    """Rappels J-14, J-7, J-2 (selon reminder_days) avant chaque date clé.

    Si le bot était éteint le jour exact, le rappel part dès que possible :
    à J-6 on envoie le rappel "J-7" (une seule fois), pas celui de J-14.
    """
    alerts = []
    for key_date in key_dates:
        days = days_until(key_date, today)
        if days <= 0:
            continue
        # Le plus petit palier encore valable (ex : à 6 jours -> palier 7)
        reached = [threshold for threshold in reminder_days if days <= threshold]
        if not reached:
            continue
        threshold = min(reached)
        alerts.append(Alert(
            key=f"key_date:{key_date.name}:{key_date.week_start.isoformat()}:J-{threshold}",
            repeat_after=None,
            text=f"📅 **{key_date.name}** : c'est la semaine du "
                 f"{key_date.week_start:%d/%m/%Y}, dans **{plural_days(days)}** !",
        ))
    return alerts
