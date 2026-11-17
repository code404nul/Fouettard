"""Courbe de l'oubli : quoi réviser avant un contrôle ou un partiel.

Rétention estimée R = P + (1 - P) × exp(-t / S) (Ebbinghaus, avec un plancher) :
- t : jours depuis le dernier contact avec la matière (dernier cours ou dernière révision) ;
- S : stabilité du souvenir, en jours. Elle part de ta confiance (1 à 5) et grandit
  à chaque révision notée « facile » ou « moyen » ;
- P : ce qui reste à long terme d'un cours compris, plus haut si tu es confiant.

Priorité d'une matière pour un examen dans d jours :
    poids (volume horaire ou coef) × oubli prévu le jour J × urgence
où l'oubli prévu est 1 - R(t + d) et l'urgence 1 / (1 + d / 7).
"""

import math
from dataclasses import dataclass
from datetime import date

from fouettard.planning import agenda
from fouettard.planning.config import Course, PlanningConfig
from fouettard.planning.ics import CalEvent
from fouettard.planning.spaced import DIFFICILE, FACILE, MOYEN

# Stabilité de départ, en jours, selon la confiance
BASE_STABILITY = {1: 3.0, 2: 5.0, 3: 8.0, 4: 12.0, 5: 20.0}
# Part qui ne s'oublie pas, selon la confiance
FLOOR = {1: 0.10, 2: 0.25, 3: 0.40, 4: 0.55, 5: 0.70}
GROWTH = {FACILE: 1.8, MOYEN: 1.4, DIFFICILE: 1.0}
MAX_STABILITY = 90.0
DEFAULT_CONFIDENCE = 3
UNKNOWN_DAYS = 14          # aucun cours ni révision connus : on suppose que c'est loin
DEFAULT_WEIGHT = 0.5       # matière sans module dans planning.yaml
URGENCY_DAYS = 7
MAX_SUBJECTS_PER_EXAM = 3
MAX_COURSES_SHOWN = 2


@dataclass(frozen=True)
class SubjectState:
    subject: str
    confidence: int
    last_contact: date | None
    grades: tuple[str, ...] = ()  # notes des révisions depuis le dernier cours


@dataclass(frozen=True)
class Advice:
    subject: str
    score: float
    retention_now: float
    retention_exam: float
    confidence: int
    courses: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpcomingExam:
    title: str
    day: date
    subjects: tuple[str, ...] | None  # None : toutes les matières


def stability(confidence: int, grades: tuple[str, ...] = ()) -> float:
    value = BASE_STABILITY[max(1, min(5, confidence))]
    for grade in grades:
        value *= GROWTH.get(grade, 1.0)
    return min(value, MAX_STABILITY)


def retention(days: float, stab: float, floor: float = 0.0) -> float:
    return floor + (1 - floor) * math.exp(-max(0.0, days) / stab)


def memory(confidence: int, grades: tuple[str, ...], days: float) -> float:
    """Part du cours encore en tête après « days » jours sans le revoir."""
    level = max(1, min(5, confidence))
    return retention(days, stability(level, grades), FLOOR[level])


def subject_weights(courses: list[Course]) -> dict[str, float]:
    """Poids de chaque matière entre 0 et 1.

    Somme des coef (ou des heures) de ses modules, puis racine carrée : sans elle,
    les 445 h de maths écraseraient tout le reste.
    """
    totals: dict[str, float] = {}
    for course in courses:
        totals[course.subject] = totals.get(course.subject, 0.0) + course.weight
    roots = {subject: math.sqrt(total) for subject, total in totals.items() if total > 0}
    top = max(roots.values(), default=0)
    return {subject: value / top for subject, value in roots.items()} if top else {}


def advise(state: SubjectState, exam_day: date, today: date, weight: float) -> Advice:
    days = (today - state.last_contact).days if state.last_contact else UNKNOWN_DAYS
    until_exam = max(0, (exam_day - today).days)
    now = memory(state.confidence, state.grades, days)
    at_exam = memory(state.confidence, state.grades, days + until_exam)
    urgency = 1 / (1 + until_exam / URGENCY_DAYS)
    return Advice(state.subject, weight * (1 - at_exam) * urgency, now, at_exam, state.confidence)


def courses_to_review(courses: list[Course], subject: str, today: date) -> tuple[str, ...]:
    """Modules déjà commencés de la matière, les plus gros d'abord."""
    started = [course for course in courses if course.subject == subject
               and (course.start is None or course.start <= today)]
    started.sort(key=lambda course: -course.weight)
    return tuple(course.name for course in started[:MAX_COURSES_SHOWN])


def upcoming_exams(events: list[CalEvent], today: date, horizon_days: int,
                   config: PlanningConfig) -> list[UpcomingExam]:
    """Contrôles et partiels à venir, un par titre (un partiel de 3 jours compte une fois)."""
    seen: dict[str, UpcomingExam] = {}
    for event in sorted(events, key=lambda item: item.start):
        if agenda.kind_of(event) != agenda.EXAM:
            continue
        day = event.start.date()
        if not 0 < (day - today).days <= horizon_days or event.title in seen:
            continue
        subjects = config.exam_subjects.get(event.title)
        if subjects is None:
            subject = agenda.subject_of(event.title, config)
            subjects = [subject] if subject else None
        seen[event.title] = UpcomingExam(event.title, day, tuple(subjects) if subjects else None)
    return list(seen.values())


def exam_advice(exam: UpcomingExam, states: dict[str, SubjectState], today: date,
                weights: dict[str, float], courses: list[Course]) -> list[Advice]:
    """Matières à réviser pour cet examen, la plus urgente d'abord."""
    subjects = exam.subjects or tuple(states)
    # Matière sans module connu : pas plus lourde que la plus légère des autres
    default = min(weights.values(), default=DEFAULT_WEIGHT)
    found = []
    for subject in subjects:
        state = states.get(subject) or SubjectState(subject, DEFAULT_CONFIDENCE, None)
        advice = advise(state, exam.day, today, weights.get(subject, default))
        found.append(Advice(**{**advice.__dict__,
                               "courses": courses_to_review(courses, subject, today)}))
    found.sort(key=lambda advice: -advice.score)
    return found if exam.subjects else found[:MAX_SUBJECTS_PER_EXAM]
