"""Sens des événements de calendrier : type, matière, doublons, changements.

Le flux Zeus est celui d'un groupe entier : on y trouve des cours parallèles
(Anglais ACF / ADP), les jours fériés, des examens sans matière (« EXAMEN 2 SUP »)
et des événements de plusieurs jours (« ASSERTIVITE », du lundi au vendredi).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher

from fouettard.planning.config import OUTLOOK, ZEUS, PlanningConfig
from fouettard.planning.ics import CalEvent
from fouettard.planning.textutils import normalize

COURS = "cours"
EXAM = "examen"
RATTRAPAGE = "rattrapage"
CONGE = "conge"
INFO = "info"
RDV = "rdv"

# Clé de exam_distances pour un examen sans matière : il concerne toutes les matières
ALL_SUBJECTS = "*"

_EXAM_PATTERN = re.compile(r"\b(examen|partiel|controle|qcm|ds)\b")
_MAX_DUPLICATE_GAP = timedelta(minutes=15)
_TITLE_SIMILARITY = 0.6


def kind_of(event: CalEvent) -> str:
    if event.source == OUTLOOK:
        return RDV
    title = normalize(event.title)
    if title.startswith("pas d'examen"):
        return INFO
    if "rattrapage" in title:
        return RATTRAPAGE
    if re.search(r"\b(ferie|vacances|conge)\b", title):
        return CONGE
    if _EXAM_PATTERN.search(title):
        return EXAM
    return COURS


def subject_of(title: str, config: PlanningConfig) -> str | None:
    """Matière reconnue dans le titre (« CM PROGRAMMATION » -> Programmation), ou None."""
    text = normalize(title)
    for alias, subject in config.subject_aliases.items():
        if re.search(rf"\b{re.escape(normalize(alias))}\b", text):
            return subject
    for subject in config.review_subjects:
        if re.search(rf"\b{re.escape(normalize(subject))}\b", text):
            return subject
    return None


def is_ignored(event: CalEvent, config: PlanningConfig) -> bool:
    """Vrai si l'événement est un cours d'un autre sous-groupe (ignored_subjects)."""
    title = normalize(event.title)
    return any(title == normalize(ignored) for ignored in config.ignored_subjects)


def is_blocking(event: CalEvent, config: PlanningConfig) -> bool:
    kind = kind_of(event)
    return kind in (COURS, EXAM, RDV) or (kind == RATTRAPAGE and config.rattrapages_block)


def day_window(event: CalEvent, day: date) -> tuple[datetime, datetime] | None:
    """Plage occupée par l'événement ce jour-là, ou None.

    Un événement de plusieurs jours (du lundi 8 h au vendredi 20 h) occupe les mêmes
    heures chaque jour, pas les nuits. Une journée entière occupe tout le jour.
    """
    start, end = event.start, event.end
    last_day = (end - timedelta(microseconds=1)).date()
    if not start.date() <= day <= last_day:
        return None
    if start.date() == last_day and not event.all_day:
        return start, end
    midnight = datetime.combine(day, time.min, tzinfo=start.tzinfo)
    if event.all_day or start.time() >= end.time():
        return midnight, midnight + timedelta(days=1)
    return (datetime.combine(day, start.time(), tzinfo=start.tzinfo),
            datetime.combine(day, end.time(), tzinfo=start.tzinfo))


def events_on(events: list[CalEvent], day: date) -> list[CalEvent]:
    found = [event for event in events if day_window(event, day)]
    return sorted(found, key=lambda event: day_window(event, day)[0])


def is_vacation(events: list[CalEvent], day: date) -> bool:
    return any(kind_of(event) == CONGE for event in events_on(events, day))


def exam_distances(events: list[CalEvent], today: date, horizon_days: int,
                   config: PlanningConfig) -> dict[str, int]:
    """Nombre de jours avant le prochain examen, par matière (ALL_SUBJECTS si non précisée)."""
    distances: dict[str, int] = {}
    for event in events:
        if kind_of(event) != EXAM:
            continue
        days = (event.start.date() - today).days
        if 0 <= days <= horizon_days:
            subject = subject_of(event.title, config) or ALL_SUBJECTS
            distances[subject] = min(days, distances.get(subject, days))
    return distances


# --- Doublons Zeus / Outlook -----------------------------------------------

def _similar_titles(first: str, second: str) -> bool:
    first, second = normalize(first), normalize(second)
    if not first or not second:
        return False
    if first in second or second in first:
        return True
    return SequenceMatcher(None, first, second).ratio() >= _TITLE_SIMILARITY


def is_duplicate(event: CalEvent, references: list[CalEvent]) -> bool:
    """Vrai si l'événement existe déjà dans références (même horaire et titre proche)."""
    return any(abs(event.start - other.start) <= _MAX_DUPLICATE_GAP
               and abs(event.end - other.end) <= _MAX_DUPLICATE_GAP
               and _similar_titles(event.title, other.title)
               for other in references)


def merge_sources(events_by_source: dict[str, list[CalEvent]],
                  config: PlanningConfig) -> list[CalEvent]:
    """Tous les événements à afficher. Zeus est la référence pour les cours."""
    zeus = [event for event in events_by_source.get(ZEUS, []) if not is_ignored(event, config)]
    # On compare à tout Zeus : un cours ignoré présent dans Outlook reste un doublon
    all_zeus = events_by_source.get(ZEUS, [])
    outlook = [event for event in events_by_source.get(OUTLOOK, [])
               if not is_duplicate(event, all_zeus)]
    return sorted(zeus + outlook, key=lambda event: (event.start, event.title))


# --- Changements -----------------------------------------------------------

AJOUT = "ajout"
ANNULATION = "annulation"
DEPLACEMENT = "deplacement"
SALLE = "salle"


@dataclass(frozen=True)
class Change:
    kind: str
    source: str
    title: str
    when: datetime  # date concernée : la nouvelle si l'événement a bougé
    detail: str = ""
    is_exam: bool = False


def _when_text(moment: datetime) -> str:
    return moment.strftime("%d/%m à %H:%M").replace(":", " h ", 1).replace(" h 00", " h")


def diff_events(old: list[CalEvent], new: list[CalEvent], now: datetime) -> list[Change]:
    """Compare deux versions d'un calendrier. Les événements passés sont ignorés."""
    old_by_key = {event.key: event for event in old}
    new_by_key = {event.key: event for event in new}

    # Si le flux a changé ses identifiants, un événement identique n'est pas un changement
    identity = lambda event: (event.start, event.end, normalize(event.title))
    removed = [event for key, event in old_by_key.items() if key not in new_by_key]
    added = [event for key, event in new_by_key.items() if key not in old_by_key]
    removed_identities = {identity(event) for event in removed}
    added_identities = {identity(event) for event in added}
    removed = [event for event in removed if identity(event) not in added_identities]
    added = [event for event in added if identity(event) not in removed_identities]

    changes = []
    for event in removed:
        if event.end > now:
            changes.append(Change(ANNULATION, event.source, event.title, event.start,
                                  is_exam=kind_of(event) == EXAM))
    for event in added:
        if event.end > now:
            changes.append(Change(AJOUT, event.source, event.title, event.start,
                                  is_exam=kind_of(event) == EXAM))
    for key, event in new_by_key.items():
        before = old_by_key.get(key)
        if before is None or (before.end <= now and event.end <= now):
            continue
        if (before.start, before.end) != (event.start, event.end):
            changes.append(Change(DEPLACEMENT, event.source, event.title, event.start,
                                  f"était le {_when_text(before.start)}",
                                  kind_of(event) == EXAM))
        elif before.location != event.location:
            changes.append(Change(SALLE, event.source, event.title, event.start,
                                  f"{before.location or '?'} → {event.location or '?'}",
                                  kind_of(event) == EXAM))
    return sorted(changes, key=lambda change: change.when)


def is_alert_worthy(change: Change, today: date) -> bool:
    """Alerte immédiate seulement pour le jour même ou le lendemain."""
    return today <= change.when.date() <= today + timedelta(days=1)
