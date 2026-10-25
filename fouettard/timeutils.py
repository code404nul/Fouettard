"""Petites fonctions autour des dates, toujours dans le fuseau Europe/Paris."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


def now() -> datetime:
    """Date et heure actuelles à Paris."""
    return datetime.now(PARIS)


def today() -> date:
    """Date du jour à Paris."""
    return now().date()


def parse_gitlab_datetime(text: str) -> datetime:
    """Convertit une date GitLab ("2026-10-24T08:15:00.000Z") en heure de Paris."""
    return datetime.fromisoformat(text).astimezone(PARIS)


def parse_user_date(text: str) -> date:
    """Lit une date tapée par un utilisateur : "24/10/2026" ou "2026-10-24".

    Lève ValueError si le format n'est pas reconnu.
    """
    text = text.strip()
    for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass
    raise ValueError(f"date invalide : {text!r} (formats acceptés : JJ/MM/AAAA ou AAAA-MM-JJ)")


def start_of_day(day: date) -> datetime:
    """Minuit (début de journée) à Paris."""
    return datetime.combine(day, time.min, tzinfo=PARIS)


def end_of_day(day: date) -> datetime:
    """23:59:59 (fin de journée) à Paris."""
    return datetime.combine(day, time.max, tzinfo=PARIS)
