"""Compréhension des messages privés, sans IA : mots-clés et analyseur de dates."""

import re
import unicodedata
from difflib import get_close_matches
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
MAYBE_TASK = "maybe_task"  # tâche devinée : le bot demande avant de l'ajouter
TODO = "todo"
REVISE = "revise"          # conseils de révision avant les contrôles
CONFIDENCE = "confidence"  # « confiance maths 2 », ou « confiance » pour tout voir
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


NUMBER_WORDS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6,
                "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "quinze": 15}
_NUMBER_PATTERN = r"\d{1,3}|" + "|".join(NUMBER_WORDS)


def _number(word: str) -> int:
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


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

    match = re.search(rf"\bdans\s+({_NUMBER_PATTERN})\s+(jours?|semaines?)\b", text)
    if match:
        count = _number(match.group(1))
        days = count * 7 if match.group(2).startswith("semaine") else count
        return DateSpec(dates=(today + timedelta(days=days),), evening=evening)
    if re.search(r"\bsemaine prochaine\b", text):
        return DateSpec(dates=(_next_weekday(0, today, skip_today=True),), evening=evening)
    if re.search(r"\bweek[- ]?end\b", text):
        saturday = _next_weekday(5, today)
        days = (saturday, saturday + timedelta(days=1)) if today.weekday() != 6 else (today,)
        return DateSpec(dates=days, evening=evening)
    if re.search(r"\bfin de (?:la )?semaine\b", text):
        return DateSpec(dates=(_next_weekday(4, today),), evening=evening)

    if re.search(r"\bapres[- ]demain\b", text):
        return DateSpec(dates=(today + timedelta(days=2),), evening=evening)
    if re.search(r"\bdemain\b", text):
        return DateSpec(dates=(today + timedelta(days=1),), evening=evening)
    if re.search(r"\b(aujourd'hui|ce soir|ce matin|cet apres-midi)\b", text):
        return DateSpec(dates=(today,), evening=evening)

    # « vendredi 6 » : le numéro du jour l'emporte sur le nom
    match = re.search(rf"\b(?:{_WEEKDAY_PATTERN})\s+(\d{{1,2}})\b(?!\s*[h:])", text)
    if match:
        found = _next_with_day(int(match.group(1)), today)
        return DateSpec(dates=(found,), evening=evening) if found else None

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


# Mots qui annoncent une tâche, même sans date
_TASK_WORDS = re.compile(
    r"^(?:il faut|faut|je dois|dois|penser a|pense a|ne pas oublier|oublie pas|n'oublie pas|"
    r"rendre|reviser|revoir|faire|finir|terminer|preparer|envoyer|appeler|contacter|ecrire|"
    r"repondre|acheter|payer|signer|imprimer|lire|relire|reserver|inscrire|m'inscrire|deposer|"
    r"rediger|apprendre|commander|rembourser|prendre rdv|prendre rendez-vous|rappeler|valider|"
    r"remplir|telecharger|installer|ranger|nettoyer|reparer|tp|td|dm|devoir|exam|partiel|projet)\b")
_LEAD_WORDS = re.compile(r"^(?:il faut que je|il faut|faut|je dois|dois|penser a|pense a|"
                         r"ne pas oublier de|ne pas oublier|n'oublie pas de|oublie pas de|n'oublie pas|"
                         r"oublie pas)\s+")
# Expressions de date à retirer du titre, avec la préposition qui les précède
_PREP = r"(?:(?:pour|avant|d'ici|jusqu'au|jusqu'a|au plus tard|a rendre|a faire|des)\s+)?(?:(?:le|ce|cette)\s+)?"
_DATE_WORDS = [
    rf"du\s+\d{{1,2}}(?:/\d{{1,2}})?\s+au\s+\d{{1,2}}(?:/\d{{1,2}}|\s+(?:{_MONTH_PATTERN}))?",
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?",
    rf"(?:(?:{_WEEKDAY_PATTERN})\s+)?\d{{1,2}}\s+(?:{_MONTH_PATTERN})(?:\s+\d{{4}})?",
    rf"dans\s+(?:{_NUMBER_PATTERN})\s+(?:jours?|semaines?)",
    r"(?:la\s+)?semaine prochaine|(?:ce\s+)?week[- ]?end|fin de (?:la )?semaine",
    r"apres[- ]demain(?:\s+(?:matin|soir|apres-midi))?",
    r"demain(?:\s+(?:matin|soir|apres-midi))?",
    r"aujourd'hui|ce soir|ce matin|cet apres-midi|soir(?:ee)?",
    rf"(?:{_WEEKDAY_PATTERN})(?:\s+prochain|\s+\d{{1,2}})?(?:\s+(?:matin|soir|apres-midi))?",
    r"le\s+\d{1,2}(?!\s*[h:\d])",
]
_DATE_IN_TITLE = re.compile(rf"(?<!\w){_PREP}(?:{'|'.join(_DATE_WORDS)})(?!\w)")


def _fold(text: str) -> tuple[str, list[int]]:
    """Comme normalize, mais garde pour chaque caractère sa position dans le texte d'origine."""
    folded, positions = [], []
    for index, char in enumerate(text.replace("’", "'")):
        for part in unicodedata.normalize("NFKD", char):
            if not unicodedata.combining(part):
                folded.append(part.lower())
                positions.append(index)
    return "".join(folded), positions


def task_title(message: str) -> str:
    """Le message sans ses mots de date ni « il faut », en gardant accents et majuscules."""
    text = message.strip()
    folded, positions = _fold(text)
    # De la fin vers le début, pour que les positions restantes restent justes
    for match in reversed(list(_DATE_IN_TITLE.finditer(folded))):
        start, end = positions[match.start()], positions[match.end() - 1] + 1
        text = text[:start] + " " + text[end:]
    text = " ".join(text.split())
    lead = _LEAD_WORDS.match(normalize(text))
    if lead:
        # « il faut » et compagnie : on retire autant de mots que dans la forme normalisée
        text = " ".join(text.split()[len(lead.group(0).split()):])
    text = re.sub(r"(?i)[\s,;:.!-]+(?:pour|avant|le|à|a|et|d'ici)?[\s,;:.!?-]*$", "", text).strip(" ,;:.!-")
    return text[:1].upper() + text[1:] if text else ""


def guess_task(message: str, today: date) -> Intent | None:
    """Tâche probable dans un message libre : une date précise, ou un verbe de tâche."""
    if normalize(message).endswith("?") or len(message) > 200:
        return None
    message = _without_greeting(message)
    text = normalize(message)
    dates = parse_dates(text, today)
    if dates is not None and dates.weekly is not None:
        return None
    if dates is None and not _TASK_WORDS.match(text):
        return None
    title = task_title(message)
    if len(normalize(title)) < 3:
        return None
    return Intent(MAYBE_TASK, text=title, dates=dates)


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


# Commandes d'un seul tenant : la phrase entière doit correspondre (fautes de frappe tolérées)
COMMANDS = {
    WEEK: ("semaine", "voir la semaine", "ma semaine", "planning", "mon planning", "agenda", "mon agenda",
           "emploi du temps", "edt", "programme", "cette semaine"),
    MAILS: ("mail", "mails", "mes mails", "voir mes mails", "email", "emails", "mes emails",
            "voir mes emails", "courrier", "boite mail"),
    HELP: ("aide", "help", "?", "commandes", "les commandes", "tu sais faire quoi", "que sais tu faire",
           "qu'est-ce que tu sais faire", "comment ca marche", "menu"),
    TODO: ("todo", "todo list", "todolist", "to do", "to-do", "ma todo", "taches", "mes taches",
           "liste", "ma liste", "a faire", "voir la todo", "voir mes taches", "j'ai quoi a faire",
           "quoi faire"),
    REVISE: ("reviser", "revisions", "revision", "quoi reviser", "que reviser", "je revise quoi",
             "conseils", "conseils revision", "on revise quoi", "je dois reviser quoi"),
    DONE: ("fait", "c'est fait", "termine", "fini", "j'ai fini", "j'ai fait", "ok fait", "ca y est", "done"),
    POSTPONE: ("reporte", "reporter", "decale", "plus tard", "pas maintenant", "flemme"),
}
_COMMAND_KIND = {phrase: kind for kind, phrases in COMMANDS.items() for phrase in phrases}
# Premiers mots des commandes à paramètres, corrigés s'ils sont mal tapés
_HEADS = ("ajoute", "confiance", "reporte", "objectif", "creneau", "supprime")
_POLITE = r"(?:stp|svp|s'il te pla[iî]t|s'il vous pla[iî]t|merci(?: beaucoup)?|please|pls)"
_GREETING = r"(?:salut|coucou|hello|hey|bonjour|bonsoir|yo|fouettard)"
_ARTICLE = re.compile(r"^(?:(?:les|le|la|mes|mon|ma|cette|ces|ce|du|de la|des)\s+|l')+")
# Mots-clés suffisants dans une question ou un message court : « c'est quoi mon edt ? »
_KEYWORDS = (
    (WEEK, r"\b(?:semaine|planning|agenda|edt|emploi du temps)\b"),
    (MAILS, r"\b(?:e?mails?|courriers?)\b"),
    (TODO, r"\b(?:todo|taches)\b"),
    (REVISE, r"\breviser\b.*\bquoi\b|\bquoi\b.*\breviser\b"),
)
HELP_TOPICS = ("todo", "taches", "dates", "jours", "projets", "club", "revisions", "reviser", "confiance",
               "agenda", "semaine")


def clean(message: str) -> str:
    """Forme normalisée, sans salutations, politesse ni ponctuation finale."""
    text = normalize(message)
    text = re.sub(rf"^{_GREETING}\b[\s,!.]*", "", text)
    text = re.sub(rf"\b{_POLITE}(?!\w)", "", text)
    text = re.sub(r"[\s,;.!?:)(-]+$", "", text)
    text = re.sub(r"^(?:tu peux|peux-tu|peux tu|est-ce que tu peux)\s+", "", text)
    return " ".join(text.split())


def _fix_first_word(text: str) -> str:
    """« ajoue réviser thermo » → « ajoute réviser thermo »."""
    first, _, rest = text.partition(" ")
    if len(first) < 5 or first in _HEADS:
        return text
    found = get_close_matches(first, _HEADS, n=1, cutoff=0.82)
    return f"{found[0]} {rest}".strip() if found else text


def closest_command(message: str) -> str | None:
    """Commande la plus proche d'un message incompris, pour la suggérer."""
    text = clean(message)
    found = get_close_matches(text, list(_COMMAND_KIND), n=1, cutoff=0.6)
    if found:
        return found[0]
    first = text.partition(" ")[0]
    found = get_close_matches(first, _HEADS, n=1, cutoff=0.7) if len(first) >= 4 else []
    return found[0] if found else None


def _without_greeting(message: str) -> str:
    """Texte d'origine (accents, majuscules) sans salutation, politesse ni « tu peux »."""
    text = re.sub(rf"(?i)[\s,]*\b{_POLITE}(?!\w)", "", message.replace("’", "'")).strip()
    text = re.sub(rf"(?i)^{_GREETING}\b[\s,!.]*", "", text)
    return re.sub(r"(?i)^(?:tu peux|peux-tu|peux tu|est-ce que tu peux)\s+", "", text)


def _after_verb(message: str) -> str:
    """Texte d'origine après « ajoute » (ou sa faute de frappe), sans « à la todo »."""
    text = _without_greeting(message)
    text = text.split(None, 1)[1] if " " in text else ""
    todo = r"(?:(?:à|a|dans|sur) (?:la|ma) (?:todo(?: ?list)?|liste))"
    text = re.sub(rf"(?i)^{todo}\s*:?\s*", "", text)
    return re.sub(rf"(?i)\s+{todo}(?=\s|$)", "", text)


def understand(message: str, today: date) -> Intent:
    """Transforme un message privé en intention."""
    if message.strip() == "?":
        return Intent(HELP)
    text = clean(message)
    if not text:
        return Intent(UNKNOWN)
    kind = _COMMAND_KIND.get(text)
    if kind is None and 4 <= len(text) <= 30:
        found = get_close_matches(text, list(_COMMAND_KIND), n=1, cutoff=0.8)
        kind = _COMMAND_KIND[found[0]] if found else None
    if kind is not None:
        return Intent(kind)
    match = re.match(r"(?:aide|help|\?)\s+(?:sur |pour |avec )?(?:les |la |le )?(\w+)", text)
    if match:
        topic = get_close_matches(match.group(1), HELP_TOPICS, n=1, cutoff=0.7)
        return Intent(HELP, text=topic[0] if topic else "")
    text = _fix_first_word(text)

    match = re.match(r"(?:ma )?confiance(?:\s+(?:en|de|du|des|pour))?(?:\s+(.*?))?"
                     r"(?:\s*[:=]?\s*(\d)(?:\s*/\s*5)?)?$", text)
    if match:
        subject = (match.group(1) or "").strip()
        if match.group(2) is None:
            return Intent(CONFIDENCE, text=subject)
        return Intent(CONFIDENCE, text=subject, number=float(match.group(2)))

    if re.search(r"elle (ne )?vient (pas|plus)", text) or re.search(
            r"\b(annule|supprime)\b.*\b(jour|journee)\s+(leger|legere|off)\b", text):
        return Intent(CANCEL_LIGHT, dates=parse_dates(text, today))

    match = re.match(r"(?:j'ai\s+)?(?:fait|c'est fait|termine|fini|done)\b\s*(?:avec\s+)?(.*)", text)
    if match:
        return Intent(DONE, text=_ARTICLE.sub("", match.group(1)))
    match = re.match(r"(?:je\s+)?(?:reporte[rz]?|decale[rz]?|remets?)\b\s*(.*?)(?:\s+a\s+demain)?$", text)
    if match:
        return Intent(POSTPONE, text=_ARTICLE.sub("", match.group(1)))

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

    if re.match(r"(?:r?ajoute[rz]?|note[rz]?|mets)\b", text):
        title = re.split(r"(?i)\s+(?:pour|avant|d'ici)\s+", _after_verb(message))[0].strip(" ,;:.!")
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

    short = len(text.split()) <= 5 and parse_dates(text, today) is None
    if not _TASK_WORDS.match(text) and (message.strip().endswith("?") or short):
        for kind, pattern in _KEYWORDS:
            if re.search(pattern, text):
                return Intent(kind)

    return guess_task(message, today) or Intent(UNKNOWN)
