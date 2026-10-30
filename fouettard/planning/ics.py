"""Lecteur générique de flux ICS (Zeus, Outlook publié...).

Les URL sont secrètes : aucun message d'erreur de ce fichier ne les contient.
Les exceptions d'origine sont volontairement coupées (« from None »), car celles
de httpx affichent l'adresse demandée.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx
import icalendar
import recurring_ical_events

from fouettard.timeutils import PARIS

FETCH_TIMEOUT = 30.0

# À ce niveau de détail, httpx écrirait l'adresse complète (donc le secret) dans les logs
logging.getLogger("httpx").setLevel(logging.WARNING)

# Préfixes qu'Outlook ajoute au titre des réunions annulées
CANCELLED_PREFIXES = ("annulé :", "annulé:", "annule :", "annule:", "canceled:", "cancelled:")


class IcsError(Exception):
    """Flux illisible ou inaccessible. Le message ne contient jamais l'URL."""


@dataclass(frozen=True)
class CalEvent:
    source: str
    key: str  # identifiant stable : UID + occurrence d'origine
    title: str
    start: datetime  # toujours en Europe/Paris
    end: datetime
    location: str = ""
    tentative: bool = False
    all_day: bool = False


async def fetch_ics(client: httpx.AsyncClient, url: str) -> str:
    """Télécharge un flux ICS. Lève IcsError sans jamais mentionner l'URL."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        response = await client.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise IcsError(f"connexion impossible ({exc.__class__.__name__})") from None
    if response.status_code != 200:
        raise IcsError(f"réponse HTTP {response.status_code}")
    if "BEGIN:VCALENDAR" not in response.text[:2000]:
        raise IcsError("la réponse n'est pas un calendrier ICS")
    return response.text


def _to_paris(value: date | datetime) -> tuple[datetime, bool]:
    """Convertit une valeur ICS en datetime de Paris. Renvoie aussi « journée entière »."""
    if isinstance(value, datetime):
        # Heure sans fuseau (« flottante ») : on la considère comme heure de Paris
        if value.tzinfo is None:
            return value.replace(tzinfo=PARIS), False
        return value.astimezone(PARIS), False
    return datetime.combine(value, time.min, tzinfo=PARIS), True


def _is_hidden(component) -> bool:
    """Vrai pour un événement annulé ou marqué « disponible »."""
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        return True
    if str(component.get("SUMMARY", "")).strip().lower().startswith(CANCELLED_PREFIXES):
        return True
    busy = str(component.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper()
    if busy:
        return busy == "FREE"
    return str(component.get("TRANSP", "")).upper() == "TRANSPARENT"


def _is_tentative(component) -> bool:
    return ("TENTATIVE" in (str(component.get("STATUS", "")).upper(),
                            str(component.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper()))


def parse_ics(text: str, source: str, first_day: date, last_day: date) -> list[CalEvent]:
    """Lit un flux ICS et renvoie les occurrences entre deux dates (répétitions et
    exceptions déjà appliquées), triées, sans les événements annulés ou « disponibles ».

    icalendar convertit lui-même les fuseaux Windows (« Romance Standard Time »).
    """
    try:
        calendar = icalendar.Calendar.from_ical(text)
        components = (recurring_ical_events.of(calendar, skip_bad_series=True)
                      .between(first_day, last_day + timedelta(days=1)))
    except Exception as exc:  # la bibliothèque peut lever beaucoup de types d'erreurs
        raise IcsError(f"calendrier illisible ({exc.__class__.__name__})") from None

    events = {}
    for component in components:
        if _is_hidden(component):
            continue
        start, all_day = _to_paris(component["DTSTART"].dt)
        end, _ = _to_paris(component["DTEND"].dt) if "DTEND" in component else (start, False)
        if end <= start:
            end = start + (timedelta(days=1) if all_day else timedelta(minutes=30))
        # L'occurrence d'origine reste la même si elle a été déplacée : c'est ce
        # qui permet de voir un déplacement plutôt qu'un ajout et une annulation.
        recurrence = component.get("RECURRENCE-ID")
        origin = _to_paris(recurrence.dt)[0] if recurrence else start
        is_recurring = bool(recurrence) or "RRULE" in component
        uid = str(component.get("UID", "")) or f"{component.get('SUMMARY')}@{start.isoformat()}"
        key = f"{uid}|{origin.isoformat()}" if is_recurring else uid
        events[key] = CalEvent(
            source=source, key=key,
            title=str(component.get("SUMMARY", "")).strip() or "(sans titre)",
            start=start, end=end,
            location=str(component.get("LOCATION", "")).strip(),
            tentative=_is_tentative(component), all_day=all_day,
        )
    return sorted(events.values(), key=lambda event: (event.start, event.title))
