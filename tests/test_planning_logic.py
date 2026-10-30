"""Agenda, révisions espacées, compréhension des phrases, construction du planning."""

from datetime import date, datetime, time, timedelta

import pytest

from fouettard.planning import agenda, j3d, nlp, spaced
from fouettard.planning.config import PlanningConfig, WeeklySlot
from fouettard.planning.ics import CalEvent
from fouettard.planning.scheduler import (CLUB_FIXE, DEVOIR, J3D, LEGER, OFF, REVISION, SOIREE,
                                          VOITURE, PlanRequest, ReviewRequest, TaskRequest, build_plan)
from fouettard.timeutils import PARIS


def at(month, day, hour, minute=0, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=PARIS)


def make_event(title, start, end, source="zeus", key=None, location=""):
    return CalEvent(source, key or f"{title}{start}", title, start, end, location)


CONFIG = PlanningConfig(owner_id=1, urls={}, database_path=":memory:",
                        review_subjects=["Mathématiques", "Programmation", "Physique"],
                        subject_aliases={"maths": "Mathématiques"})


# --- Agenda : les titres réels de Zeus --------------------------------------

@pytest.mark.parametrize("title, kind", [
    ("Mathématiques", agenda.COURS), ("CM PROGRAMMATION", agenda.COURS),
    ("EXAMEN 2 SUP", agenda.EXAM), ("Controle de cours de maths", agenda.EXAM),
    ("RATTRAPAGES S1", agenda.RATTRAPAGE), ("Férié", agenda.CONGE), ("Férie", agenda.CONGE),
    ("FERIE", agenda.CONGE), ("Vacances", agenda.CONGE), ("Pas d'examen ce jour là", agenda.INFO),
    ("Forum international 2027 au KB", agenda.COURS),
])
def test_kind_of_real_zeus_titles(title, kind):
    assert agenda.kind_of(make_event(title, at(11, 9, 9), at(11, 9, 10))) == kind


@pytest.mark.parametrize("title, subject", [
    ("CM PROGRAMMATION", "Programmation"), ("Programmation", "Programmation"),
    ("Mathématiques", "Mathématiques"), ("Controle de cours de maths", "Mathématiques"),
    ("Anglais ACF", None), ("EXAMEN 2 SUP", None),
])
def test_subject_of(title, subject):
    assert agenda.subject_of(title, CONFIG) == subject


def test_vacation_does_not_block_but_courses_do():
    assert not agenda.is_blocking(make_event("Vacances", at(11, 2, 5), at(11, 2, 19)), CONFIG)
    assert agenda.is_blocking(make_event("Physique", at(11, 2, 5), at(11, 2, 19)), CONFIG)
    assert not agenda.is_blocking(make_event("RATTRAPAGES S1", at(1, 25, 8, year=2027),
                                             at(1, 25, 20, year=2027)), CONFIG)
    assert not agenda.is_blocking(make_event("Pas d'examen ce jour là", at(12, 18, 7), at(12, 18, 19)), CONFIG)


def test_multi_day_event_keeps_daily_hours_not_nights():
    event = make_event("ASSERTIVITE", at(1, 11, 7, year=2027), at(1, 15, 20, year=2027))
    assert agenda.day_window(event, date(2027, 1, 13)) == (at(1, 13, 7, year=2027), at(1, 13, 20, year=2027))
    assert agenda.day_window(event, date(2027, 1, 16)) is None
    assert agenda.day_window(event, date(2027, 1, 10)) is None


def test_exam_without_subject_concerns_all_subjects():
    events = [make_event("EXAMEN 2 SUP", at(12, 14, 7), at(12, 14, 19)),
              make_event("Controle de cours de maths", at(11, 23, 8), at(11, 23, 13))]
    distances = agenda.exam_distances(events, date(2026, 11, 20), 30, CONFIG)
    assert distances == {agenda.ALL_SUBJECTS: 24, "Mathématiques": 3}


# --- Doublons ---------------------------------------------------------------

@pytest.mark.parametrize("title, start, duplicate", [
    ("Mathématiques", at(11, 10, 9), True),
    ("Cours de Mathématiques", at(11, 10, 9), True),
    ("mathematique", at(11, 10, 9, 10), True),
    ("Réunion de projet", at(11, 10, 9), False),
    ("Mathématiques", at(11, 10, 14), False),
])
def test_is_duplicate(title, start, duplicate):
    zeus = [make_event("Mathématiques", at(11, 10, 9), at(11, 10, 12))]
    outlook = make_event(title, start, start + timedelta(hours=3), "outlook")
    assert agenda.is_duplicate(outlook, zeus) is duplicate


def test_merge_keeps_zeus_entry_only():
    zeus = make_event("Physique", at(11, 12, 14), at(11, 12, 16))
    copy = make_event("Physique", at(11, 12, 14), at(11, 12, 16), "outlook")
    meeting = make_event("Rendez-vous scolarité", at(11, 12, 17), at(11, 12, 18), "outlook")
    merged = agenda.merge_sources({"zeus": [zeus], "outlook": [copy, meeting]}, CONFIG)
    assert merged == [zeus, meeting]


def test_ignored_subgroup_is_hidden_and_not_shown_through_outlook():
    config = PlanningConfig(owner_id=1, urls={}, database_path=":memory:", ignored_subjects=["Anglais ADP"])
    zeus = make_event("Anglais ADP", at(11, 10, 15), at(11, 10, 16))
    copy = make_event("Anglais ADP", at(11, 10, 15), at(11, 10, 16), "outlook")
    assert agenda.merge_sources({"zeus": [zeus], "outlook": [copy]}, config) == []


# --- Changements -------------------------------------------------------------

NOW = at(11, 9, 8)


def test_diff_detects_add_move_room_cancel():
    old = [make_event("A", at(11, 10, 9), at(11, 10, 10), key="a", location="A1"),
           make_event("B", at(11, 10, 11), at(11, 10, 12), key="b"),
           make_event("C", at(11, 10, 14), at(11, 10, 15), key="c", location="A1")]
    new = [make_event("A", at(11, 10, 9), at(11, 10, 10), key="a", location="B2"),
           make_event("C", at(11, 11, 14), at(11, 11, 15), key="c", location="A1"),
           make_event("EXAMEN", at(11, 20, 8), at(11, 20, 12), key="d")]
    kinds = {change.title: change.kind for change in agenda.diff_events(old, new, NOW)}
    assert kinds == {"A": agenda.SALLE, "B": agenda.ANNULATION, "C": agenda.DEPLACEMENT,
                     "EXAMEN": agenda.AJOUT}
    assert next(c for c in agenda.diff_events(old, new, NOW) if c.title == "EXAMEN").is_exam


def test_diff_ignores_past_events_and_regenerated_uids():
    past = make_event("Passé", at(11, 1, 9), at(11, 1, 10), key="p")
    same = make_event("Stable", at(11, 10, 9), at(11, 10, 10), key="ancien")
    regenerated = make_event("Stable", at(11, 10, 9), at(11, 10, 10), key="nouveau")
    assert agenda.diff_events([past, same], [regenerated], NOW) == []


def test_alert_only_for_today_and_tomorrow():
    today = date(2026, 11, 9)
    for offset, expected in [(-1, False), (0, True), (1, True), (2, False)]:
        change = agenda.Change(agenda.AJOUT, "outlook", "x", at(11, 9, 12) + timedelta(days=offset))
        assert agenda.is_alert_worthy(change, today) is expected


# --- Révisions espacées ------------------------------------------------------

DAY = date(2026, 11, 10)


def review(**values):
    defaults = dict(id=1, subject="Physique", course_date=DAY, due_on=DAY)
    return spaced.Review(**{**defaults, **values})


def test_medium_grades_follow_the_ladder_from_course_date():
    current, day = review(), DAY + timedelta(days=1)  # J+1
    offsets = []
    for _ in range(3):
        current = spaced.after_grade(current, spaced.MOYEN, day)
        day = current.due_on
        offsets.append((day - DAY).days)
    assert offsets == [3, 7, 21]
    assert not spaced.after_grade(current, spaced.MOYEN, day).active


def test_easy_lengthens_and_hard_shortens():
    easy = spaced.after_grade(review(step=2, interval_days=4), spaced.FACILE, DAY)
    medium = spaced.after_grade(review(step=2, interval_days=4), spaced.MOYEN, DAY)
    hard = spaced.after_grade(review(step=2, interval_days=4), spaced.DIFFICILE, DAY)
    assert easy.interval_days > medium.interval_days > hard.interval_days
    assert hard.step == 2 and hard.interval_days == 2 and hard.ease < medium.ease


def test_exam_shortens_interval():
    result = spaced.after_grade(review(step=2, interval_days=4), spaced.MOYEN, DAY, exam_in_days=6)
    assert result.interval_days == 3


def test_due_reviews_are_grouped_by_subject_and_capped():
    reviews = [review(id=index, subject=subject, due_on=DAY - timedelta(days=index))
               for index, subject in enumerate(["A", "A", "B", "C", "D", "E"])]
    assert next(group for group in spaced.select_due(reviews, DAY, {}, limit=9)
                if group.subject == "A").review_ids == (0, 1)
    groups = spaced.select_due(reviews, DAY, {}, limit=4)
    assert [group.subject for group in groups] == ["E", "D", "C", "B"]  # les plus anciennes


def test_nearby_exam_comes_first_and_brings_back_a_subject_not_due():
    reviews = [review(id=1, subject="Maths", due_on=DAY + timedelta(days=9), last_done_on=DAY - timedelta(days=3)),
               review(id=2, subject="Physique", due_on=DAY - timedelta(days=5)),
               review(id=3, subject="Algo", due_on=DAY + timedelta(days=9))]
    groups = spaced.select_due(reviews, DAY, {"Maths": 4}, limit=4)
    assert [group.subject for group in groups] == ["Maths", "Physique"]
    assert groups[0].boosted


# --- Dates et phrases --------------------------------------------------------

TODAY = date(2026, 10, 30)  # un vendredi


@pytest.mark.parametrize("text, expected", [
    ("demain", [date(2026, 10, 31)]),
    ("après-demain", [date(2026, 11, 1)]),
    ("elle vient samedi", [date(2026, 10, 31)]),
    ("vendredi prochain", [date(2026, 11, 6)]),
    ("vendredi", [date(2026, 10, 30)]),
    ("du 6 au 8", [date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)]),
    ("du 30 au 2", [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)]),
    ("du 6 au 8 décembre", [date(2026, 12, 6), date(2026, 12, 7), date(2026, 12, 8)]),
    ("le 12/01", [date(2027, 1, 12)]),
    ("le 12/01/2027", [date(2027, 1, 12)]),
    ("le 6 décembre", [date(2026, 12, 6)]),
    ("le 2", [date(2026, 11, 2)]),
    ("ce soir", [date(2026, 10, 30)]),
])
def test_parse_dates(text, expected):
    assert list(nlp.parse_dates(text, TODAY).dates) == expected


def test_parse_weekly_and_evening_and_nothing():
    assert nlp.parse_dates("tous les mercredis", TODAY).weekly == 2
    assert nlp.parse_dates("elle vient ce soir", TODAY).evening
    assert nlp.parse_dates("bonjour", TODAY) is None
    assert nlp.parse_dates("le 31/02", TODAY) is None


@pytest.mark.parametrize("text, kind, level", [
    ("elle vient ce soir", nlp.LIGHT, SOIREE),
    ("elle vient samedi", nlp.LIGHT, LEGER),
    ("ma copine vient du 6 au 8", nlp.LIGHT, LEGER),
    ("journée off", nlp.LIGHT, OFF),
    ("off demain", nlp.LIGHT, OFF),
    ("léger tous les mercredis", nlp.LIGHT, LEGER),
])
def test_understand_light_days(text, kind, level):
    intent = nlp.understand(text, TODAY)
    assert (intent.kind, intent.level) == (kind, level)


@pytest.mark.parametrize("text, kind", [
    ("Finalement elle vient pas", nlp.CANCEL_LIGHT), ("elle ne vient plus samedi", nlp.CANCEL_LIGHT),
    ("fait", nlp.DONE), ("Fait maths", nlp.DONE), ("reporte", nlp.POSTPONE),
    ("j'en suis à monter le châssis", nlp.PROGRESS), ("J’en suis au câblage voiture", nlp.PROGRESS),
    ("ajoute réviser thermo pour lundi", nlp.ADD_TASK),
    ("point de suivi AWAKE le 12/01 à 18h", nlp.ADD_EVENT),
    ("semaine", nlp.WEEK), ("objectif club 6h", nlp.SET_GOAL),
    ("créneau club jeudi 18h-20h", nlp.ADD_CLUB_SLOT), ("supprime créneau club jeudi", nlp.REMOVE_CLUB_SLOT),
    ("bloc minimum 45", nlp.SET_MIN_BLOCK), ("il fait beau", nlp.UNKNOWN),
])
def test_understand_commands(text, kind):
    assert nlp.understand(text, TODAY).kind == kind


def test_task_keeps_original_title_and_date():
    intent = nlp.understand("ajoute Réviser thermo pour lundi", TODAY)
    assert intent.text == "Réviser thermo"
    assert intent.dates.dates == (date(2026, 11, 2),)


def test_event_date_and_time():
    intent = nlp.understand("point de suivi AWAKE le 12/01 à 18h30", TODAY)
    assert intent.dates.dates == (date(2027, 1, 12),)
    assert intent.moment == time(18, 30)
    assert intent.project == "voiture"


def test_progress_keeps_accents_and_finds_project():
    intent = nlp.understand("j'en suis à câbler la voiture", TODAY)
    assert intent.text == "câbler la voiture"
    assert intent.project == "voiture"


# --- Construction du planning ------------------------------------------------

MONDAY = date(2026, 11, 9)
THURSDAY = date(2026, 11, 12)
SATURDAY = date(2026, 11, 14)
MEALS = [(time(12, 30), time(13, 30)), (time(19, 30), time(20, 15))]


def kinds_of(plan):
    return [block.kind for block in plan.blocks]


def test_nothing_overlaps_courses_or_meals():
    busy = [(at(11, 9, 9), at(11, 9, 12)), (at(11, 9, 14), at(11, 9, 18))]
    plan = build_plan(PlanRequest(
        day=MONDAY, busy=busy, meals=MEALS, j3d_minutes=120,
        tasks=[TaskRequest(1, "TD", MONDAY + timedelta(days=3))],
        reviews=[ReviewRequest(f"R{i}", f"r{i}") for i in range(3)],
        voiture_minutes=60, domotique_minutes=60))
    occupied = busy + [(at(11, 9, 12, 30), at(11, 9, 13, 30)), (at(11, 9, 19, 30), at(11, 9, 20, 15))]
    for block in plan.blocks:
        assert all(block.end <= start or end <= block.start for start, end in occupied), block
    for first, second in zip(plan.blocks, plan.blocks[1:]):
        assert first.end <= second.start


def test_school_comes_before_club_when_time_is_short():
    busy = [(at(11, 9, 8), at(11, 9, 20))]
    plan = build_plan(PlanRequest(day=MONDAY, busy=busy, j3d_minutes=90, voiture_minutes=60))
    assert kinds_of(plan) == [J3D]
    assert plan.j3d_placed_minutes == 90
    # 20 h - 22 h : J3D prend 1 h 30, il reste 30 min, pas assez pour la voiture


def test_missing_j3d_time_is_said_clearly():
    plan = build_plan(PlanRequest(day=MONDAY, busy=[(at(11, 9, 8), at(11, 9, 21))], j3d_minutes=180))
    assert plan.j3d_placed_minutes == 60
    assert "J3D : il manque 2 h" in plan.warnings[0]


def test_reviews_go_in_small_gaps_first():
    busy = [(at(11, 9, 8), at(11, 9, 10)), (at(11, 9, 10, 20), at(11, 9, 18))]
    plan = build_plan(PlanRequest(day=MONDAY, busy=busy, reviews=[ReviewRequest("R", "r")]))
    assert plan.blocks[0].start == at(11, 9, 10)


def test_car_in_the_evening_on_weekdays_any_time_on_weekends():
    weekday = build_plan(PlanRequest(day=MONDAY, voiture_minutes=60))
    assert weekday.blocks[0].start == at(11, 9, 18)
    weekend = build_plan(PlanRequest(day=SATURDAY, voiture_minutes=60))
    assert weekend.blocks[0].start == at(11, 14, 8)


def test_project_never_in_a_small_gap():
    busy = [(at(11, 14, 8), at(11, 14, 12)), (at(11, 14, 12, 20), at(11, 14, 22))]
    assert build_plan(PlanRequest(day=SATURDAY, busy=busy, voiture_minutes=60)).blocks == []


def test_protected_slot_is_only_for_the_club():
    slot = WeeklySlot(5, time(8, 0), time(12, 0))
    plan = build_plan(PlanRequest(day=SATURDAY, protected_slots=[slot], j3d_minutes=60, voiture_minutes=60))
    j3d_block = next(block for block in plan.blocks if block.kind == J3D)
    car = next(block for block in plan.blocks if block.kind == VOITURE)
    assert j3d_block.start >= at(11, 14, 12)
    assert car.start == at(11, 14, 8)


def test_fixed_club_slot_is_kept():
    slot = WeeklySlot(3, time(18, 0), time(20, 0))
    plan = build_plan(PlanRequest(day=THURSDAY, fixed_slots=[slot], j3d_minutes=60))
    assert (CLUB_FIXE, at(11, 12, 18)) in [(block.kind, block.start) for block in plan.blocks]
    assert all(block.start != at(11, 12, 18) for block in plan.blocks if block.kind == J3D)


def rich_request(**values):
    defaults = dict(day=SATURDAY, j3d_minutes=120, voiture_minutes=60, domotique_minutes=60,
                    tasks=[TaskRequest(1, "TD", SATURDAY + timedelta(days=5))],
                    reviews=[ReviewRequest("R1", "r1"), ReviewRequest("R2", "r2")])
    return PlanRequest(**{**defaults, **values})


def test_evening_level_frees_the_evening_only():
    plan = build_plan(rich_request(level=SOIREE))
    assert plan.blocks and all(block.end <= at(11, 14, 18) for block in plan.blocks)
    assert J3D in kinds_of(plan)


def test_light_day_no_club_one_review_no_j3d():
    plan = build_plan(rich_request(level=LEGER))
    assert kinds_of(plan) == [REVISION]
    assert plan.blocks[0].end - plan.blocks[0].start == timedelta(minutes=15)


def test_off_day_is_empty_except_j3d_emergency():
    assert build_plan(rich_request(level=OFF)).blocks == []
    assert kinds_of(build_plan(rich_request(level=OFF, j3d_urgent=True))) == [J3D]


def test_light_day_can_only_keep_urgent_homework():
    plan = build_plan(rich_request(level=LEGER, reviews=[],
                                   tasks=[TaskRequest(1, "Urgent", SATURDAY + timedelta(days=1))]))
    assert kinds_of(plan) == [DEVOIR]


def test_replanning_never_goes_back_in_time():
    plan = build_plan(rich_request(not_before=at(11, 14, 15)))
    assert all(block.start >= at(11, 14, 15) for block in plan.blocks)


# --- Besoin J3D --------------------------------------------------------------

def issue(due, estimate=0, spent=0, title="Tâche"):
    return {"iid": 1, "title": title, "due_date": due.isoformat(), "state": "opened",
            "time_stats": {"time_estimate": estimate, "total_time_spent": spent}}


def test_need_spreads_work_until_the_deadline():
    need = j3d.compute_need([issue(MONDAY + timedelta(days=3), estimate=4 * 3600)],
                            MONDAY, lambda day: True, 2, 48)
    assert need.minutes == 60  # 4 h sur 4 jours
    assert not need.urgent


def test_light_days_move_work_to_previous_days():
    deadline = MONDAY + timedelta(days=3)
    light = {MONDAY + timedelta(days=1), MONDAY + timedelta(days=2)}
    need = j3d.compute_need([issue(deadline, estimate=4 * 3600)], MONDAY,
                            lambda day: day not in light, 2, 48)
    assert need.minutes == 120  # 4 h sur 2 jours de travail


def test_deadline_on_only_light_days_is_impossible():
    need = j3d.compute_need([issue(MONDAY, estimate=3600)], MONDAY, lambda day: False, 2, 48)
    assert need.impossible and need.urgent


def test_default_estimate_and_time_spent():
    assert j3d.remaining_hours(issue(MONDAY), 2) == 2
    assert j3d.remaining_hours(issue(MONDAY, 3 * 3600, 3600), 2) == 2
    assert j3d.remaining_hours(issue(MONDAY, 3600, 7200), 2) == 0


def test_milestone_deadline_is_used_when_issue_has_none():
    assert j3d.issue_deadline({"milestone": {"due_date": "2026-12-07"}}) == date(2026, 12, 7)
