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

HELP_TEXT = """**Ce que tu peux m'écrire** (fautes de frappe, accents et « stp » ne me gênent pas)
📅 **Voir** : « semaine » (ou « planning », « edt »), « mails », « todo »
✅ **Aujourd'hui** : « fait », « fait maths », « reporte », « reporte la révision »
📝 **Tâches** : « ajoute réviser thermo pour lundi », ou juste « rendre le TP jeudi » (je te demande avant d'ajouter)
📚 **Révisions** : « réviser », « confiance », « confiance maths 2 »
🔧 **Projets** : « j'en suis à monter le châssis (voiture) », « point de suivi AWAKE le 12/01 à 18h »
🌙 **Jours légers** : « elle vient samedi », « journée off demain », « finalement elle vient pas »
⚙️ **Réglages club** : « objectif club 6h », « créneau club jeudi 18h-20h »

Détails : « aide todo », « aide dates », « aide révisions », « aide projets », « aide jours », « aide club »"""

HELP_TOPICS = {
    "todo": """**📝 Todo list**
- « ajoute réviser thermo pour lundi » : ajouté tout de suite, avec l'échéance
- « ajoute appeler la banque » : ajouté sans échéance
- Une phrase qui commence par un verbe (« rendre », « payer », « envoyer », « il faut », « je dois »…) ou qui contient une date : je propose de l'ajouter, tu confirmes avec un bouton
- « todo » : voir la liste
- « fait » / « reporte » : boutons des éléments du jour ; « fait maths » vise directement l'élément qui contient « maths »
- « reporter » décale à demain, sans compter comme du retard
Pour les dates acceptées : « aide dates ».""",
    "dates": """**📆 Dates que je comprends**
- « aujourd'hui », « ce soir », « demain », « après-demain »
- « lundi » (le prochain, ou aujourd'hui si on y est), « lundi prochain », « vendredi 6 »
- « le 12 », « le 12/01 », « le 12/01/2027 », « le 6 décembre »
- « dans 3 jours », « dans deux semaines », « la semaine prochaine » (le lundi)
- « ce week-end », « fin de semaine » (le vendredi)
- « du 6 au 8 », « du 30 au 2 », « du 6 au 8 décembre »
- « tous les mercredis » (seulement pour les jours légers)
Heures : « 18h », « 18 h 30 », « 18:30 ».""",
    "revisions": """**📚 Révisions**
- « réviser » : quoi revoir avant les prochains contrôles et partiels, selon la courbe de l'oubli
- « confiance » : ta confiance par matière (1 à 5)
- « confiance maths 2 » ou « confiance en physique 4/5 » : la changer
- Après une révision « fait », je te demande Facile / Moyen / Difficile pour espacer la suivante""",
    "projets": """**🔧 Projets** (J3D, Voiture AWAKE, Domotique)
- « j'en suis à câbler la voiture » : note la prochaine étape (je devine le projet avec « voiture », « awake », « domotique », « local », « j3d », sinon je demande)
- Le soir, je te demande où tu en es : réponds librement, ou « passer »
- « point de suivi AWAKE le 12/01 à 18h » : ajoute le rendez-vous""",
    "jours": """**🌙 Jours légers**
- « elle vient samedi », « ma copine vient du 6 au 8 » : journée légère (pas de club, 15 min de révision au plus)
- « elle vient ce soir » : seulement la soirée libérée
- « journée off demain », « off ce week-end » : rien, sauf urgence J3D
- « léger tous les mercredis » : chaque semaine
- « finalement elle vient pas », « annule journée légère samedi » : retour au planning normal""",
    "club": """**⚙️ Réglages du club**
- « objectif club 6h » : heures visées par semaine
- « bloc minimum 45 » : durée minimale d'un bloc projet, en minutes
- « créneau club jeudi 18h-20h » : créneau fixe chaque semaine
- « supprime créneau club jeudi »""",
}
HELP_TOPICS.update({"taches": HELP_TOPICS["todo"], "reviser": HELP_TOPICS["revisions"],
                    "confiance": HELP_TOPICS["revisions"], "agenda": HELP_TOPICS["dates"],
                    "semaine": HELP_TOPICS["dates"]})


def help_text(topic: str = "") -> str:
    return HELP_TOPICS.get(topic, HELP_TEXT)


# Ce que « Tu voulais dire… » propose pour une commande proche
SUGGESTION_EXAMPLES = {
    "ajoute": "ajoute réviser thermo pour lundi", "confiance": "confiance maths 2",
    "reporte": "reporte", "objectif": "objectif club 6h", "creneau": "créneau club jeudi 18h-20h",
    "supprime": "supprime créneau club jeudi", "reviser": "réviser", "revisions": "réviser",
    "revision": "réviser", "taches": "todo", "mes taches": "todo", "decale": "reporte",
}


def unknown_text(suggestion: str | None) -> str:
    if suggestion:
        example = SUGGESTION_EXAMPLES.get(suggestion, suggestion)
        return (f"Je n'ai pas compris. Tu voulais dire « {example} » ?\n"
                "Sinon, c'est pour un jour léger ? Écris « aide » pour tout voir.")
    return "Je n'ai pas compris. C'est pour un jour léger ? Sinon, écris « aide »."


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
