"""Compréhension des messages privés, sans IA : mots-clés et analyseur de dates."""

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from fouettard.planning.scheduler import LEGER, OFF, SOIREE
from fouettard.planning.textutils import normalize

WEEKDAYS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
            "vendredi": 4, "samedi": 5, "dimanche": 6}
MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}
_WEEKDAY_PATTERN = "|".join(WEEKDAYS)
_MONTH_PATTERN = "|".join(MONTHS)

# Intentions
CANCEL_LIGHT = "cancel_light"
WEEK = "week"
MAILS = "mails"
HELP = "help"
DONE = "done"
POSTPONE = "postpone"
PROGRESS = "progress"
ADD_TASK = "add_task"
ADD_EVENT = "add_event"
LIGHT = "light"
SET_GOAL = "set_goal"
SET_MIN_BLOCK = "set_min_block"
ADD_CLUB_SLOT = "add_club_slot"
REMOVE_CLUB_SLOT = "remove_club_slot"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class DateSpec:
    dates: tuple[date, ...] = ()
    weekly: int | None = None  # jour de la semaine si récurrent
    evening: bool = False


@dataclass
class Intent:
    kind: str
    text: str = ""  # titre, étape, élément visé...
    dates: DateSpec | None = None
    level: str | None = None
    moment: time | None = None
    end_moment: time | None = None
    project: str | None = None
    number: float | None = None
    extra: dict = field(default_factory=dict)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_with_day(day_number: int, today: date, month: int | None = None) -> date | None:
    """Prochaine date portant ce numéro (dans le mois donné s'il est précisé)."""
    if month is not None:
        for year in (today.year, today.year + 1):
            found = _safe_date(year, month, day_number)
            if found and found >= today:
                return found
        return None
    year, current_month = today.year, today.month
    for _ in range(13):
        found = _safe_date(year, current_month, day_number)
        if found and found >= today:
            return found
        current_month += 1
        if current_month == 13:
            year, current_month = year + 1, 1
    return None


def _next_weekday(weekday: int, today: date, skip_today: bool = False) -> date:
    days = (weekday - today.weekday()) % 7
    if days == 0 and skip_today:
        days = 7
    return today + timedelta(days=days)


def _date_range(first: date, last: date) -> tuple[date, ...]:
    if last < first:
        return ()
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def parse_dates(text: str, today: date) -> DateSpec | None:
    """Trouve une date ou une période dans une phrase. None si aucune."""
    text = normalize(text)
    evening = bool(re.search(r"\bce soir\b|\bsoir(ee)?\b", text))

    match = re.search(rf"\b(?:tous les|toutes les|chaque)\s+({_WEEKDAY_PATTERN})s?\b", text)
    if match:
        return DateSpec(weekly=WEEKDAYS[match.group(1)], evening=evening)

    match = re.search(rf"\bdu\s+(\d{{1,2}})(?:/(\d{{1,2}}))?\s+au\s+(\d{{1,2}})(?:/(\d{{1,2}})|\s+({_MONTH_PATTERN}))?\b",
                      text)
    if match:
        first_day, first_month, last_day, last_month, month_name = match.groups()
        end_month = int(last_month) if last_month else MONTHS.get(month_name)
        begin_month = int(first_month) if first_month else end_month
        first = _next_with_day(int(first_day), today, begin_month)
        if first is None:
            return None
        last = _safe_date(first.year, end_month or first.month, int(last_day))
        if last and last < first:  # à cheval sur le mois ou l'année suivante
            month = first.month % 12 + 1
            last = _safe_date(first.year + (first.month == 12), end_month or month, int(last_day))
        return DateSpec(dates=_date_range(first, last), evening=evening) if last else None

    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if match:
        day_number, month, year = match.groups()
        if year:
            found = _safe_date(int(year) + (2000 if len(year) == 2 else 0), int(month), int(day_number))
        else:
            found = _next_with_day(int(day_number), today, int(month))
        return DateSpec(dates=(found,), evening=evening) if found else None

    match = re.search(rf"\b(\d{{1,2}})\s+({_MONTH_PATTERN})(?:\s+(\d{{4}}))?\b", text)
    if match:
        day_number, month_name, year = match.groups()
        if year:
            found = _safe_date(int(year), MONTHS[month_name], int(day_number))
        else:
            found = _next_with_day(int(day_number), today, MONTHS[month_name])
        return DateSpec(dates=(found,), evening=evening) if found else None

    if re.search(r"\bapres[- ]demain\b", text):
        return DateSpec(dates=(today + timedelta(days=2),), evening=evening)
    if re.search(r"\bdemain\b", text):
        return DateSpec(dates=(today + timedelta(days=1),), evening=evening)
    if re.search(r"\b(aujourd'hui|ce soir|ce matin|cet apres-midi)\b", text):
        return DateSpec(dates=(today,), evening=evening)

    match = re.search(rf"\b({_WEEKDAY_PATTERN})\b(\s+prochain)?", text)
    if match:
        found = _next_weekday(WEEKDAYS[match.group(1)], today, skip_today=bool(match.group(2)))
        return DateSpec(dates=(found,), evening=evening)

    match = re.search(r"\ble\s+(\d{1,2})\b(?!\s*[h:])", text)
    if match:
        found = _next_with_day(int(match.group(1)), today)
        return DateSpec(dates=(found,), evening=evening) if found else None

    if evening:
        return DateSpec(dates=(today,), evening=True)
    return None


def parse_times(text: str) -> list[time]:
    """Heures écrites dans la phrase : « 18h », « 18 h 30 », « 18:30 »."""
    moments = []
    for hours, minutes in re.findall(r"\b(\d{1,2})\s*(?:h|:)\s*(\d{2})?", normalize(text)):
        if int(hours) < 24 and int(minutes or 0) < 60:
            moments.append(time(int(hours), int(minutes or 0)))
    return moments


def _project_in(text: str) -> str | None:
    if re.search(r"\b(voiture|awake)\b", text):
        return "voiture"
    if re.search(r"\b(domotique|local)\b", text):
        return "domotique"
    if re.search(r"\bj3d\b", text):
        return "j3d"
    return None


def understand(message: str, today: date) -> Intent:
    """Transforme un message privé en intention."""
    text = normalize(message)
    if text in ("semaine", "voir la semaine", "ma semaine"):
        return Intent(WEEK)
    if text in ("mail", "mails", "mes mails", "voir mes mails", "email", "emails", "mes emails", "voir mes emails"):
        return Intent(MAILS)
    if text in ("aide", "help", "?"):
        return Intent(HELP)

    if re.search(r"elle (ne )?vient (pas|plus)", text) or re.search(
            r"\b(annule|supprime)\b.*\b(jour|journee)\s+(leger|legere|off)\b", text):
        return Intent(CANCEL_LIGHT, dates=parse_dates(text, today))

    match = re.match(r"(fait|c'est fait|termine|fini)\b\s*(.*)", text)
    if match:
        return Intent(DONE, text=match.group(2))
    match = re.match(r"(reporte|reporter|decale)\b\s*(.*)", text)
    if match:
        return Intent(POSTPONE, text=match.group(2))

    match = re.search(r"j'?\s?en suis (?:a|au|aux)\b\s*(.*)", text)
    if match:
        # On garde le texte d'origine (accents, majuscules) pour la prochaine étape
        original = re.search(r"(?i)j['’]?\s?en suis (?:à|a|au|aux)\b\s*(.*)", message.strip())
        step = original.group(1) if original else match.group(1)
        return Intent(PROGRESS, text=step.strip(), project=_project_in(text))

    match = re.match(r"objectif(?: club)?\s+(\d+(?:[.,]\d+)?)\s*h?\b", text)
    if match:
        return Intent(SET_GOAL, number=float(match.group(1).replace(",", ".")))
    match = re.match(r"bloc(?: minimum| min)?\s+(\d+)\b", text)
    if match:
        return Intent(SET_MIN_BLOCK, number=float(match.group(1)))

    match = re.match(rf"(supprime|retire|enleve) (?:le )?creneau(?: club)?\s+({_WEEKDAY_PATTERN})", text)
    if match:
        return Intent(REMOVE_CLUB_SLOT, number=WEEKDAYS[match.group(2)])
    match = re.match(rf"creneau(?: club)?\s+({_WEEKDAY_PATTERN})\b", text)
    if match:
        moments = parse_times(text)
        if len(moments) >= 2 and moments[0] < moments[1]:
            return Intent(ADD_CLUB_SLOT, number=WEEKDAYS[match.group(1)],
                          moment=moments[0], end_moment=moments[1])
        return Intent(UNKNOWN)

    if text.startswith("point de suivi"):
        moments = parse_times(text)
        dates = parse_dates(text, today)
        return Intent(ADD_EVENT, text="Point de suivi AWAKE", dates=dates,
                      moment=moments[0] if moments else None, project=_project_in(text))

    if text.startswith("ajoute"):
        original = re.sub(r"(?i)^ajoute[rz]?\s+", "", message.strip())
        title = re.split(r"(?i)\s+(?:pour|avant|d'ici)\s+", original)[0].strip()
        return Intent(ADD_TASK, text=title, dates=parse_dates(text, today))

    if re.search(r"copine|elle vient|\bleger(e)?\b|\boff\b", text):
        dates = parse_dates(text, today)
        if re.search(r"\boff\b", text):
            level = OFF
        elif dates and dates.evening:
            level = SOIREE
        else:
            level = LEGER
        return Intent(LIGHT, dates=dates, level=level)

    return Intent(UNKNOWN)
