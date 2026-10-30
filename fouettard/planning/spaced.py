"""Révisions espacées : J+1, J+3, J+7, J+21 après le cours, ajustées par la note.

Variante simplifiée de SM-2 : chaque note fait évoluer un facteur de facilité
(« ease ») qui allonge ou raccourcit les intervalles suivants.
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta

from fouettard.planning.agenda import ALL_SUBJECTS

FACILE = "facile"
MOYEN = "moyen"
DIFFICILE = "difficile"
GRADES = (FACILE, MOYEN, DIFFICILE)

# Écart entre deux révisions : J+1 -> J+3 (2 j), J+3 -> J+7 (4 j), J+7 -> J+21 (14 j)
GAPS = [2, 4, 14]
DEFAULT_EASE = 2.5
MIN_EASE = 1.3
EASY_BONUS = 1.3
# Pendant un partiel proche, une matière peut revenir au bout de ce nombre de jours
BOOST_MIN_GAP = 2


@dataclass(frozen=True)
class Review:
    id: int
    subject: str
    course_date: date
    due_on: date
    interval_days: int = 1
    step: int = 0
    ease: float = DEFAULT_EASE
    last_grade: str | None = None
    last_done_on: date | None = None
    active: bool = True


@dataclass(frozen=True)
class ReviewGroup:
    """Séance de révision d'une matière. Toutes ses révisions dues sont faites ensemble."""

    subject: str
    review_ids: tuple[int, ...]
    boosted: bool = False
    hard: bool = False


def after_grade(review: Review, grade: str, today: date,
                exam_in_days: int | None = None) -> Review:
    """Révision suivante d'après la note. active=False si la révision est terminée."""
    if grade not in GRADES:
        raise ValueError(f"note inconnue : {grade}")

    ease = review.ease
    if grade == FACILE:
        ease += 0.15
    elif grade == DIFFICILE:
        ease = max(MIN_EASE, ease - 0.2)

    step = review.step
    if grade == DIFFICILE:
        # On reste à la même étape et on revoit deux fois plus tôt
        interval = max(1, round(review.interval_days * 0.5))
    else:
        if step >= len(GAPS):
            return replace(review, active=False, ease=ease, last_grade=grade, last_done_on=today)
        factor = ease / DEFAULT_EASE * (EASY_BONUS if grade == FACILE else 1)
        interval = max(1, round(GAPS[step] * factor))
        step += 1

    if exam_in_days is not None and exam_in_days > 0:
        interval = min(interval, max(1, exam_in_days // 2))

    return replace(review, ease=ease, step=step, interval_days=interval,
                   due_on=today + timedelta(days=interval),
                   last_grade=grade, last_done_on=today)


def exam_days_for(subject: str, exam_distances: dict[str, int]) -> int | None:
    """Jours avant le prochain examen concernant cette matière, ou None."""
    values = [exam_distances[name] for name in (subject, ALL_SUBJECTS) if name in exam_distances]
    return min(values) if values else None


def select_due(reviews: list[Review], today: date, exam_distances: dict[str, int],
               limit: int) -> list[ReviewGroup]:
    """Séances du jour : une par matière, au plus « limit ».

    Priorité aux matières dont le partiel approche, puis aux notes « difficile »,
    puis aux plus anciennes. Ce qui dépasse attend, sans compter comme du retard.
    """
    groups: dict[str, list[Review]] = {}
    boosted_subjects = set()
    for review in reviews:
        if not review.active:
            continue
        exam_days = exam_days_for(review.subject, exam_distances)
        boosted = exam_days is not None and (
            review.last_done_on is None
            or (today - review.last_done_on).days >= BOOST_MIN_GAP)
        if review.due_on <= today or boosted:
            groups.setdefault(review.subject, []).append(review)
            if boosted:
                boosted_subjects.add(review.subject)

    def sort_key(subject: str):
        members = groups[subject]
        hard = any(review.last_grade == DIFFICILE for review in members)
        return (subject not in boosted_subjects, not hard,
                min(review.due_on for review in members), subject)

    return [ReviewGroup(subject, tuple(review.id for review in groups[subject]),
                        subject in boosted_subjects,
                        any(review.last_grade == DIFFICILE for review in groups[subject]))
            for subject in sorted(groups, key=sort_key)[:limit]]
