"""Construction du planning d'une journée (logique pure, sans Discord ni réseau).

Ordre de remplissage des créneaux libres :
créneaux fixes -> J3D -> devoirs -> révisions -> voiture -> domotique.
L'école passe toujours avant le club, et tout manque est signalé dans « warnings ».
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from fouettard.planning.config import WeeklySlot
from fouettard.timeutils import PARIS

# Niveaux d'une journée
NORMAL = "normal"
SOIREE = "soiree"
LEGER = "leger"
OFF = "off"

# Types de blocs
J3D = "j3d"
DEVOIR = "devoir"
REVISION = "revision"
VOITURE = "voiture"
DOMOTIQUE = "domotique"
CLUB_FIXE = "club"
CLUB_KINDS = (VOITURE, DOMOTIQUE, CLUB_FIXE)

MIN_SCHOOL_BLOCK = 30  # minutes : en dessous, pas de bloc J3D ni de devoir
LIGHT_DAY_MAX_REVIEWS = 1

Slot = tuple[datetime, datetime]


@dataclass(frozen=True)
class TaskRequest:
    id: int
    title: str
    due_on: date | None
    minutes: int = 60


@dataclass(frozen=True)
class ReviewRequest:
    label: str
    ref: str  # identifiants à retrouver quand la révision est faite


@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime
    kind: str
    label: str
    ref: str = ""


@dataclass
class PlanRequest:
    day: date
    level: str = NORMAL
    busy: list[Slot] = field(default_factory=list)
    meals: list[tuple[time, time]] = field(default_factory=list)
    fixed_slots: list[WeeklySlot] = field(default_factory=list)
    protected_slots: list[WeeklySlot] = field(default_factory=list)
    day_start: time = time(8, 0)
    day_end: time = time(22, 0)
    evening_start: time = time(18, 0)
    not_before: datetime | None = None
    j3d_minutes: int = 0
    j3d_label: str = "J3D"
    j3d_urgent: bool = False
    tasks: list[TaskRequest] = field(default_factory=list)
    reviews: list[ReviewRequest] = field(default_factory=list)
    review_minutes: int = 15
    voiture_minutes: int = 0
    voiture_min_block: int = 60
    voiture_label: str = "Voiture AWAKE"
    club_all_day: bool = False  # vacances : le club n'attend pas le soir
    domotique_minutes: int = 0
    min_project_block: int = 60
    max_project_block: int = 120


@dataclass
class DayPlan:
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    j3d_placed_minutes: int = 0


def _at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=PARIS)


def _minutes(slot: Slot) -> int:
    return int((slot[1] - slot[0]).total_seconds() // 60)


def subtract(slots: list[Slot], removed: list[Slot]) -> list[Slot]:
    """Retire des plages d'une liste de créneaux."""
    result = list(slots)
    for gone_start, gone_end in removed:
        next_result = []
        for start, end in result:
            if gone_end <= start or end <= gone_start:
                next_result.append((start, end))
                continue
            if start < gone_start:
                next_result.append((start, gone_start))
            if gone_end < end:
                next_result.append((gone_end, end))
        result = next_result
    return sorted(result)


def intersect(slots: list[Slot], windows: list[Slot]) -> list[Slot]:
    result = []
    for start, end in slots:
        for window_start, window_end in windows:
            low, high = max(start, window_start), min(end, window_end)
            if low < high:
                result.append((low, high))
    return sorted(result)


def _weekly_windows(slots: list[WeeklySlot], day: date) -> list[Slot]:
    return [(_at(day, slot.start), _at(day, slot.end))
            for slot in slots if slot.weekday == day.weekday()]


def _format_minutes(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest:02d}" if rest else f"{hours} h"


def _take(slots: list[Slot], index: int, minutes: int) -> Slot:
    """Prend « minutes » au début du créneau et met à jour la liste."""
    start, end = slots[index]
    taken_end = start + timedelta(minutes=minutes)
    if taken_end >= end:
        slots.pop(index)
        return start, end
    slots[index] = (taken_end, end)
    return start, taken_end


def _first_at_least(slots: list[Slot], minutes: int) -> int | None:
    for index, slot in enumerate(slots):
        if _minutes(slot) >= minutes:
            return index
    return None


def _place_project(slots: list[Slot], minutes: int, min_block: int, max_block: int,
                   kind: str, label: str) -> Block | None:
    """Place un projet du club dans le premier bloc assez long."""
    wanted = min(minutes, max_block)
    if wanted < min_block:
        return None
    index = _first_at_least(slots, min_block)
    if index is None:
        return None
    start, end = _take(slots, index, min(wanted, _minutes(slots[index])))
    return Block(start, end, kind, label)


def build_plan(request: PlanRequest) -> DayPlan:
    day = request.day
    plan = DayPlan()
    level = request.level

    start, end = _at(day, request.day_start), _at(day, request.day_end)
    if request.not_before and request.not_before > start:
        start = request.not_before
    if level == SOIREE:
        end = min(end, _at(day, request.evening_start))
    if start >= end:
        return plan

    # 1. Cours, repas et créneaux fixes du club
    fixed_windows = [] if level in (LEGER, OFF) else _weekly_windows(request.fixed_slots, day)
    for fixed_start, fixed_end in subtract(fixed_windows, request.busy):
        if fixed_end > start and fixed_start < end:
            plan.blocks.append(Block(fixed_start, fixed_end, CLUB_FIXE, "Club (créneau fixe)"))
    meals = [(_at(day, meal_start), _at(day, meal_end)) for meal_start, meal_end in request.meals]
    free = subtract([(start, end)], request.busy + meals + fixed_windows)

    # Les créneaux protégés ne servent qu'au club
    protected_windows = [] if level in (LEGER, OFF) else _weekly_windows(request.protected_slots, day)
    protected = intersect(free, protected_windows)
    general = subtract(free, protected_windows)

    # 2. J3D (tant que c'est possible, même si ça retire la place du club)
    j3d_allowed = level in (NORMAL, SOIREE) or request.j3d_urgent
    remaining = request.j3d_minutes if j3d_allowed else 0
    while remaining >= MIN_SCHOOL_BLOCK:
        index = _first_at_least(general, 60)
        if index is None:
            index = _first_at_least(general, MIN_SCHOOL_BLOCK)
        if index is None:
            break
        length = min(remaining, request.max_project_block, _minutes(general[index]))
        block_start, block_end = _take(general, index, length)
        plan.blocks.append(Block(block_start, block_end, J3D, request.j3d_label))
        plan.j3d_placed_minutes += length
        remaining -= length
    if remaining >= MIN_SCHOOL_BLOCK:
        plan.warnings.append(
            f"J3D : il manque {_format_minutes(remaining)} aujourd'hui, la journée est pleine. "
            "Le club est retiré ; répartis ce travail dans l'équipe ou réserve un créneau demain.")

    # 3. Devoirs par échéance
    tasks_allowed = level in (NORMAL, SOIREE)
    for task in sorted(request.tasks, key=lambda item: (item.due_on is None,
                                                        item.due_on or date.max, item.id)):
        urgent = task.due_on is not None and task.due_on <= day + timedelta(days=1)
        if level == OFF or not (tasks_allowed or urgent):
            continue
        index = _first_at_least(general, min(task.minutes, MIN_SCHOOL_BLOCK))
        if index is None:
            if urgent:
                plan.warnings.append(
                    f"« {task.title} » est à rendre bientôt et ne rentre pas aujourd'hui : "
                    "trouve un créneau ou reporte autre chose.")
            continue
        length = min(task.minutes, _minutes(general[index]))
        block_start, block_end = _take(general, index, length)
        plan.blocks.append(Block(block_start, block_end, DEVOIR, task.title, f"task:{task.id}"))

    # 4. Révisions : d'abord dans les petits trous
    review_cap = {OFF: 0, LEGER: LIGHT_DAY_MAX_REVIEWS}.get(level, len(request.reviews))
    for review in request.reviews[:review_cap]:
        candidates = [(_minutes(slot), slot_index) for slot_index, slot in enumerate(general)
                      if _minutes(slot) >= request.review_minutes]
        if not candidates:
            break
        _, index = min(candidates)
        block_start, block_end = _take(general, index, request.review_minutes)
        plan.blocks.append(Block(block_start, block_end, REVISION, review.label, review.ref))

    # 5 et 6. Club : soir ou week-end, jamais un jour léger
    if level not in (LEGER, OFF):
        if day.weekday() >= 5 or request.club_all_day:
            club_slots = protected + general
        else:
            cut = _at(day, request.evening_start)
            club_slots = protected + intersect(general, [(cut, _at(day, request.day_end))])
        block = _place_project(club_slots, request.voiture_minutes, request.voiture_min_block,
                               request.max_project_block, VOITURE, request.voiture_label)
        if block:
            plan.blocks.append(block)
        block = _place_project(club_slots, request.domotique_minutes, request.min_project_block,
                               request.max_project_block, DOMOTIQUE, "Domotique du local")
        if block:
            plan.blocks.append(block)

    plan.blocks.sort(key=lambda item: item.start)
    return plan
