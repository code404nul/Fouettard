"""Mise en forme des messages (texte seulement, sans Discord)."""

import re
from datetime import date, datetime, time, timedelta

from fouettard.planning.agenda import (AJOUT, ANNULATION, CONGE, DEPLACEMENT, EXAM, INFO, RATTRAPAGE,
                                       SALLE, Change, day_window, kind_of)
from fouettard.planning.config import MAIL, OUTLOOK, ZEUS
from fouettard.planning.ics import CalEvent
from fouettard.planning.scheduler import (CLUB_KINDS, DOMOTIQUE, LEGER, NORMAL, OFF, SOIREE, VOITURE,
                                          Block)

WEEKDAY_NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTH_NAMES = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
               "septembre", "octobre", "novembre", "décembre"]
SOURCE_MARKERS = {ZEUS: "🎓", OUTLOOK: "📅"}
SOURCE_NAMES = {ZEUS: "Zeus", OUTLOOK: "Outlook", MAIL: "La boîte mail"}
LEVEL_NAMES = {SOIREE: "soirée libérée", LEGER: "journée légère", OFF: "journée off",
               NORMAL: "journée normale"}
LEVEL_EFFECTS = {
    SOIREE: "Soirée libérée, journée normale.",
    LEGER: "Pas de club, une révision de 15 min au plus.",
    OFF: "Rien, sauf urgence J3D.",
    NORMAL: "Planning normal.",
}
MAX_ROOMS_SHOWN = 3

HELP_TEXT = """**Ce que tu peux m'écrire**
- « fait », « fait maths », « reporte »
- « j'en suis à monter le châssis (voiture) »
- « ajoute réviser thermo pour lundi »
- « point de suivi AWAKE le 12/01 à 18h »
- « semaine », « mails »
- « elle vient samedi », « elle vient ce soir », « journée off demain », « léger tous les mercredis »
- « finalement elle vient pas »
- « objectif club 6h », « bloc minimum 45 », « créneau club jeudi 18h-20h », « supprime créneau club jeudi »"""


def safe_text(text: str, limit: int = 90) -> str:
    """Rend inoffensif un texte venu de l'extérieur (sujet ou expéditeur d'un mail)."""
    text = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return re.sub(r"([\\*_~`|>\[\]()#@:])", r"\\\1", text)


def hour(moment: time | datetime) -> str:
    return f"{moment.hour} h {moment.minute:02d}" if moment.minute else f"{moment.hour} h"


def time_range(start: datetime, end: datetime) -> str:
    return f"{hour(start)}-{hour(end)}"


def day_name(day: date) -> str:
    return f"{WEEKDAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]}"


def short_day(day: date) -> str:
    return f"{WEEKDAY_NAMES[day.weekday()][:3]}. {day.strftime('%d/%m')}"


def location_text(location: str) -> str:
    rooms = [room.strip() for room in location.split(",") if room.strip()]
    if not rooms:
        return ""
    return "plusieurs salles" if len(rooms) > MAX_ROOMS_SHOWN else ", ".join(rooms)


def event_line(event: CalEvent, day: date) -> str | None:
    """Ligne d'un événement dans le brief (avec son marqueur de source), ou None."""
    kind = kind_of(event)
    if kind == INFO:
        return None
    if kind == CONGE:
        return f"🏖️ {event.title}"
    start, end = day_window(event, day)
    when = "Toute la journée" if event.all_day else time_range(start, end)
    line = f"{when} {SOURCE_MARKERS[event.source]} {event.title}"
    room = location_text(event.location)
    if room:
        line += f" ({room})"
    if kind == EXAM:
        line = "📝 " + line
    if kind == RATTRAPAGE:
        line += " — seulement si tu es concerné"
    if event.tentative:
        line += " ⏳ provisoire"
    return line


def change_line(change: Change) -> str:
    marker = SOURCE_MARKERS[change.source]
    when = f"{short_day(change.when.date())} à {hour(change.when)}"
    if change.kind == ANNULATION:
        return f"{marker} ❌ Annulé : {change.title}, {when}"
    if change.kind == AJOUT:
        label = "Partiel ajouté" if change.is_exam else "Ajouté"
        return f"{marker} ➕ {label} : {change.title}, {when}"
    if change.kind == DEPLACEMENT:
        return f"{marker} 🔁 Déplacé : {change.title}, maintenant {when} ({change.detail})"
    if change.kind == SALLE:
        return f"{marker} 🚪 Salle changée : {change.title}, {when} ({change.detail})"
    return f"{marker} {change.title}, {when}"


def block_line(block: Block) -> str:
    return f"{time_range(block.start, block.end)} : {block.label}"


def is_club(block: Block) -> bool:
    return block.kind in CLUB_KINDS


def countdown(day: date, today: date) -> str:
    days = (day - today).days
    if days == 0:
        return "aujourd'hui"
    if days == 1:
        return "demain"
    return f"J-{days}" if days > 0 else f"dépassé de {-days} j"


def project_name(project: str) -> str:
    return {"j3d": "J3D", VOITURE: "Voiture AWAKE", DOMOTIQUE: "Domotique"}.get(project, project)


def week_of(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)
