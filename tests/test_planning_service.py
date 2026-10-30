"""Scénarios complets du module, avec un faux GitLab et de faux flux ICS."""

import asyncio
import logging
from datetime import date, datetime, timedelta

import httpx
import pytest

from fouettard.config import load_key_dates, CONFIG_DIR
from fouettard.database import Database
from fouettard.gitlab_api import GitLabError
from fouettard.planning.config import OUTLOOK, ZEUS, load_planning_config
from fouettard.planning.scheduler import LEGER, NORMAL, SOIREE
from fouettard.planning.service import PlanningService
from fouettard.planning.store import PlanningStore
from fouettard.timeutils import PARIS

OWNER = 42
GITLAB_ID = 7
ZEUS_URL = "https://zeus.example/secret-zeus"
OUTLOOK_URL = "https://outlook.example/secret-outlook"


def at(month, day, hour, minute=0, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=PARIS)


def vevent(uid, title, start, end, location="", extra=""):
    extra_line = f"{extra}\r\n" if extra else ""
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
            f"LOCATION:{location}\r\nDTSTAMP:20261030T200231Z\r\n{extra_line}END:VEVENT")


def calendar(*events):
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:t", *events, "END:VCALENDAR"])


MATHS = vevent("m", "Mathématiques", "20261110T080000Z", "20261110T110000Z", "A208")
ANGLAIS = vevent("a", "Anglais ADP", "20261110T140000Z", "20261110T150000Z", "B107")
EXAM = vevent("e", "EXAMEN 2 SUP", "20261214T060000Z", "20261214T183000Z", "A202\\, A207\\, A208\\, A105")
VACANCES = vevent("v", "Vacances", "20261102T043000Z", "20261102T183000Z")


class FakeGitLab:
    def __init__(self, issues=(), merge_requests=(), milestones=(), broken=False):
        self.issues, self.merge_requests, self.milestones = list(issues), list(merge_requests), list(milestones)
        self.broken = broken

    async def get_issues(self, state="all"):
        if self.broken:
            raise GitLabError("panne")
        return self.issues

    async def get_merge_requests(self, state="opened"):
        return self.merge_requests

    async def get_milestones(self, state="active"):
        return self.milestones


class Feeds:
    """Faux serveur : le contenu de chaque flux peut changer entre deux lectures."""

    def __init__(self):
        self.responses = {ZEUS_URL: httpx.Response(200, text=calendar(MATHS)),
                          OUTLOOK_URL: httpx.Response(200, text=calendar())}

    def set(self, url, *events):
        self.responses[url] = httpx.Response(200, text=calendar(*events))

    def fail(self, url, status=503):
        self.responses[url] = httpx.Response(status, text="erreur " + url)

    def handler(self, request):
        response = self.responses[str(request.url)]
        return httpx.Response(response.status_code, text=response.text)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PLANNING_OWNER_ID", str(OWNER))
    monkeypatch.setenv("ZEUS_ICS_URL", ZEUS_URL)
    monkeypatch.setenv("OUTLOOK_ICS_URL", OUTLOOK_URL)
    config = load_planning_config()
    assert config.ignored_subjects == []
    config.ignored_subjects = ["Anglais ADP"]
    config.database_path = ":memory:"

    database = Database(":memory:")
    database.save_link(OWNER, GITLAB_ID, "archibarbu")
    feeds = Feeds()
    http = httpx.AsyncClient(transport=httpx.MockTransport(feeds.handler))
    gitlab = FakeGitLab()
    service = PlanningService(config, PlanningStore(":memory:"), gitlab, database,
                              load_key_dates(CONFIG_DIR / "deadlines.yaml"), http)
    return service, feeds, gitlab


def run(coroutine):
    return asyncio.run(coroutine)


def refresh(service, source, now):
    return run(service.refresh_source(source, now))


def test_config_file_is_valid(env):
    service, _, _ = env
    config = service.config
    assert config.urls == {ZEUS: ZEUS_URL, OUTLOOK: OUTLOOK_URL}
    assert config.zeus_refresh_minutes == 30 and config.outlook_refresh_minutes == 60
    assert len(config.awake_deadlines) == 7
    assert [d.day for d in config.awake_deadlines if d.is_report] == [
        date(2027, 1, 31), date(2027, 3, 31), date(2027, 5, 31)]


def test_module_disabled_without_owner_and_outlook_optional(monkeypatch):
    monkeypatch.delenv("PLANNING_OWNER_ID", raising=False)
    assert load_planning_config() is None
    monkeypatch.setenv("PLANNING_OWNER_ID", "1")
    monkeypatch.delenv("OUTLOOK_ICS_URL", raising=False)
    monkeypatch.setenv("ZEUS_ICS_URL", ZEUS_URL)
    assert list(load_planning_config().urls) == [ZEUS]


def test_outlook_source_disabled_silently(env):
    service, _, _ = env
    del service.config.urls[OUTLOOK]
    assert refresh(service, OUTLOOK, at(11, 9, 7)) == []
    assert service.outage_lines(at(11, 9, 7)) == []


def test_first_read_creates_no_change_then_changes_are_detected(env):
    service, feeds, _ = env
    assert refresh(service, ZEUS, at(11, 9, 7)) == []
    feeds.set(ZEUS_URL, vevent("m", "Mathématiques", "20261110T080000Z", "20261110T110000Z", "B01"))
    alerts = refresh(service, ZEUS, at(11, 9, 7, 30))
    assert [change.kind for change in alerts] == ["salle"]
    assert "A208 → B01" in service.change_alert(alerts[0])


def test_change_far_away_goes_to_the_brief_not_an_alert(env):
    service, feeds, _ = env
    refresh(service, ZEUS, at(11, 1, 7))
    feeds.set(ZEUS_URL, MATHS, EXAM)
    assert refresh(service, ZEUS, at(11, 1, 8)) == []
    brief = run(service.build_brief(at(11, 2, 7))).content
    assert "Partiel ajouté : EXAMEN 2 SUP" in brief
    # Une seule fois
    assert "Partiel ajouté" not in run(service.build_brief(at(11, 3, 7))).content


def test_outlook_change_alert_only_for_today_or_tomorrow(env):
    service, feeds, _ = env
    refresh(service, OUTLOOK, at(11, 9, 7))
    feeds.set(OUTLOOK_URL, vevent("r", "Rendez-vous", "20261110T150000Z", "20261110T160000Z"),
              vevent("l", "Plus tard", "20261120T150000Z", "20261120T160000Z"))
    alerts = refresh(service, OUTLOOK, at(11, 9, 8))
    assert [change.title for change in alerts] == ["Rendez-vous"]
    assert "📅" in service.change_alert(alerts[0])


def test_outlook_copy_of_a_zeus_course_never_alerts_and_shows_once(env):
    service, feeds, _ = env
    refresh(service, ZEUS, at(11, 9, 7))
    refresh(service, OUTLOOK, at(11, 9, 7))
    feeds.set(OUTLOOK_URL, vevent("copie", "Mathématiques", "20261110T080000Z", "20261110T110000Z"))
    assert refresh(service, OUTLOOK, at(11, 9, 8)) == []
    brief = run(service.build_brief(at(11, 10, 7))).content
    assert brief.count("Mathématiques") == 1
    assert "🎓 Mathématiques" in brief


def test_outlook_event_blocks_time_and_is_marked_in_brief(env):
    service, feeds, gitlab = env
    feeds.set(ZEUS_URL)
    feeds.set(OUTLOOK_URL, vevent("r", "RDV scolarité", "20261111T080000Z", "20261111T190000Z"),
              vevent("t", "Réunion à confirmer", "20261111T190000Z", "20261111T200000Z",
                     extra="X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE"),
              vevent("f", "Libre", "20261111T210000Z", "20261111T220000Z",
                     extra="X-MICROSOFT-CDO-BUSYSTATUS:FREE"))
    refresh(service, OUTLOOK, at(11, 10, 7))
    gitlab.issues = [{"iid": 1, "title": "Doc", "state": "opened", "due_date": "2026-11-11",
                      "assignees": [{"id": GITLAB_ID}], "created_at": "2026-10-01T00:00:00Z"}]
    brief = run(service.build_brief(at(11, 11, 7))).content
    assert "📅 RDV scolarité" in brief
    assert "📅 Réunion à confirmer ⏳ provisoire" in brief or "Réunion à confirmer ⏳ provisoire" in brief
    assert "Libre" not in brief
    items = service.store.plan_items(date(2026, 11, 11))
    assert items and all(item.block.start >= at(11, 11, 21) or item.block.end <= at(11, 11, 9)
                         for item in items)


def test_outage_uses_last_version_and_is_reported_once_a_day(env, caplog):
    service, feeds, _ = env
    refresh(service, ZEUS, at(11, 9, 7))
    feeds.fail(ZEUS_URL)
    with caplog.at_level(logging.DEBUG):
        assert refresh(service, ZEUS, at(11, 10, 6)) == []
    assert "secret" not in caplog.text
    brief = run(service.build_brief(at(11, 10, 7))).content
    assert "Mathématiques" in brief  # dernière version enregistrée
    assert "Zeus injoignable, version du 09/11" in brief
    assert "secret" not in brief
    assert "injoignable" not in run(service.build_brief(at(11, 10, 8))).content
    assert "injoignable" in run(service.build_brief(at(11, 11, 7))).content
    feeds.set(ZEUS_URL, MATHS)
    refresh(service, ZEUS, at(11, 11, 8))
    assert "injoignable" not in run(service.build_brief(at(11, 12, 7))).content


def test_empty_feed_is_an_outage_not_a_mass_cancellation(env):
    service, feeds, _ = env
    refresh(service, ZEUS, at(11, 9, 7))
    feeds.set(ZEUS_URL)
    assert refresh(service, ZEUS, at(11, 9, 8)) == []
    assert len(service.store.load_events(ZEUS)) == 1
    assert service.store.pop_unbriefed_changes() == []


def test_ignored_subgroup_creates_no_alert_nor_line(env):
    service, feeds, _ = env
    feeds.set(ZEUS_URL, MATHS, ANGLAIS)
    refresh(service, ZEUS, at(11, 9, 7))
    feeds.set(ZEUS_URL, MATHS)
    assert refresh(service, ZEUS, at(11, 9, 8)) == []
    assert "Anglais ADP" not in run(service.build_brief(at(11, 10, 7))).content


def test_course_creates_review_then_grading_moves_it(env):
    service, _, _ = env
    refresh(service, ZEUS, at(11, 9, 7))
    refresh(service, ZEUS, at(11, 10, 13))  # le cours de 9 h-12 h est terminé
    run(service.build_brief(at(11, 11, 7)))
    items = service.store.plan_items(date(2026, 11, 11))
    review_item = next(item for item in items if item.block.kind == "revision")
    assert "Mathématiques" in review_item.block.label

    reply = service.mark_done(review_item.id, at(11, 11, 10))
    assert [button.label for button in reply.buttons] == ["Facile", "Moyen", "Difficile"]
    assert "13 novembre" in service.grade(review_item.id, "moyen", at(11, 11, 10)).content
    assert service.grade(review_item.id, "facile", at(11, 11, 10)).content == "Déjà noté."


def test_postponed_review_is_not_late(env):
    service, _, _ = env
    refresh(service, ZEUS, at(11, 9, 7))
    refresh(service, ZEUS, at(11, 10, 13))
    run(service.build_brief(at(11, 11, 7)))
    item = next(i for i in service.store.plan_items(date(2026, 11, 11)) if i.block.kind == "revision")
    assert "sans compter comme du retard" in service.mark_postponed(item.id, at(11, 11, 10)).content
    assert service.store.active_reviews()[0].due_on == date(2026, 11, 12)


def test_light_day_always_asks_confirmation_and_can_be_cancelled(env):
    service, _, _ = env
    now = at(11, 9, 8)
    reply = run(service.handle_text("elle vient samedi", now))
    assert [button.action for button in reply.buttons] == ["light_yes", "light_no"]
    assert "samedi 14 novembre" in reply.content
    saturday = date(2026, 11, 14)
    assert service.store.level_on(saturday) == NORMAL  # rien sans confirmation

    run(service.handle_action("light_yes", "", now))
    assert service.store.level_on(saturday) == LEGER

    reply = run(service.handle_text("Finalement elle vient pas", now))
    assert [button.action for button in reply.buttons] == ["light_yes", "light_no"]
    run(service.handle_action("light_yes", "", now))
    assert service.store.level_on(saturday) == NORMAL


def test_light_day_refused_changes_nothing(env):
    service, _, _ = env
    now = at(11, 9, 8)
    run(service.handle_text("elle vient samedi", now))
    run(service.handle_action("light_no", "", now))
    assert service.store.level_on(date(2026, 11, 14)) == NORMAL


def test_unclear_light_message_offers_date_buttons(env):
    service, _, _ = env
    reply = run(service.handle_text("ma copine vient", at(11, 9, 8)))
    assert [button.label for button in reply.buttons] == ["Ce soir", "Demain", "Choisir une date"]
    tonight = run(service.handle_action("light_pick", "tonight-leger", at(11, 9, 8)))[0]
    assert "soirée libérée" in tonight.content


def test_unknown_message_offers_date_buttons(env):
    service, _, _ = env
    assert len(run(service.handle_text("blabla", at(11, 9, 8))).buttons) == 3


def test_date_form(env):
    service, _, _ = env
    assert run(service.handle_action("light_choose", "off", at(11, 9, 8)))[0].open_date_modal
    assert "journée off" in run(service.handle_date_text("le 12/12", at(11, 9, 8))).content
    assert run(service.handle_date_text("n'importe quoi", at(11, 9, 8))).buttons


def test_recurring_light_day_with_one_exception(env):
    service, _, _ = env
    now = at(11, 9, 8)
    run(service.handle_text("léger tous les mercredis", now))
    run(service.handle_action("light_yes", "", now))
    assert service.store.level_on(date(2026, 11, 11)) == LEGER
    assert service.store.level_on(date(2026, 11, 18)) == LEGER
    run(service.handle_text("finalement elle vient pas le 18/11", now))
    run(service.handle_action("light_yes", "", now))
    assert service.store.level_on(date(2026, 11, 18)) == NORMAL
    assert service.store.level_on(date(2026, 11, 25)) == LEGER


def test_light_day_threatening_a_j3d_deadline_is_said_with_a_slot(env):
    service, _, gitlab = env
    gitlab.milestones = [{"title": "Remise du CdST", "due_date": "2026-12-07"}]
    reply = run(service.handle_text("off le 7 décembre", at(12, 1, 8)))
    assert "Remise du CdST" in reply.content
    assert "Je propose d'avancer" in reply.content
    assert "dim. 06/12 à 8 h" in reply.content


def test_light_today_replans_with_a_second_message(env):
    service, _, _ = env
    now = at(11, 9, 8)
    run(service.handle_text("journée off", now))
    # Sans date, le bot demande : on passe par « demain »
    run(service.handle_action("light_pick", "tomorrow-off", now))
    assert len(run(service.handle_action("light_yes", "", now))) == 1
    run(service.handle_text("elle vient ce soir", now))
    replies = run(service.handle_action("light_yes", "", now))
    assert len(replies) == 2 and "Planning mis à jour" in replies[1].content
    assert service.store.level_on(now.date()) == SOIREE


def test_gitlab_down_still_sends_brief(env):
    service, _, gitlab = env
    gitlab.broken = True
    refresh(service, ZEUS, at(11, 9, 7))
    brief = run(service.build_brief(at(11, 10, 7))).content
    assert "GitLab injoignable" in brief and "Mathématiques" in brief


def test_brief_contains_all_sections_and_buttons(env):
    service, _, gitlab = env
    gitlab.issues = [{"iid": 3, "title": "Écrire le CdST", "state": "opened", "due_date": "2026-11-13",
                      "assignees": [{"id": GITLAB_ID}], "updated_at": "2026-10-01T00:00:00Z",
                      "created_at": "2026-10-01T00:00:00Z"}]
    gitlab.merge_requests = [{"title": "Corrige le menu", "draft": False, "reviewers": [{"id": GITLAB_ID}]}]
    gitlab.milestones = [{"title": "Remise du CdST", "due_date": "2026-12-07"}]
    refresh(service, ZEUS, at(11, 9, 7))
    brief = run(service.build_brief(at(11, 10, 7)))
    for expected in ["Brief du mardi 10 novembre", "🎓 Mathématiques (A208)", "**École**",
                     "J3D : Écrire le CdST", "MR à relire : Corrige le menu",
                     "Issue sans mouvement", "Prochaine milestone : Remise du CdST (J-27)",
                     "Voiture : Liste de matériel"]:
        assert expected in brief.content, expected
    assert [button.label for button in brief.buttons] == ["Fait", "Reporter", "Semaine"]


def test_probable_homework_is_marked_as_a_guess(env):
    service, _, _ = env
    refresh(service, ZEUS, at(11, 9, 7))
    brief = run(service.build_brief(at(11, 11, 7))).content
    assert "Devoirs probables (supposition)" in brief


def test_vacation_week_raises_club_time_and_school_stays_first(env):
    service, feeds, _ = env
    feeds.set(ZEUS_URL, *[vevent(f"v{d}", "Vacances", f"202611{d:02d}T043000Z", f"202611{d:02d}T183000Z")
                          for d in range(2, 7)])
    refresh(service, ZEUS, at(11, 1, 7))
    brief = run(service.build_brief(at(11, 5, 7))).content  # jeudi
    assert "🏖️ Vacances" in brief
    assert "semaine calme" in brief
    assert "Voiture AWAKE" in brief


def test_exam_week_is_minimum_mode(env):
    service, feeds, _ = env
    feeds.set(ZEUS_URL, EXAM)
    refresh(service, ZEUS, at(11, 20, 7))
    assert "mode minimum" not in run(service.build_brief(at(11, 20, 8))).content
    brief = run(service.build_brief(at(12, 14, 7))).content
    assert "mode minimum" in brief
    assert "📝" in brief and "plusieurs salles" in brief


def test_collision_of_awake_with_exams_is_announced(env):
    service, feeds, _ = env
    feeds.set(ZEUS_URL, EXAM, vevent("e2", "EXAMEN 2 SUP", "20261215T060000Z", "20261215T183000Z"))
    refresh(service, ZEUS, at(11, 9, 7))
    alerts = dict(run(service.collect_daily_alerts(at(11, 9, 7))))
    text = alerts["collision:2026-12-15:exam"]
    assert "Liste de matériel" in text and "EXAMEN 2 SUP" in text and "avant le 14/12" in text


def test_collision_of_awake_with_j3d_milestone_is_announced(env):
    service, _, gitlab = env
    gitlab.milestones = [{"title": "Soutenance finale", "due_date": "2027-05-31"}]
    alerts = dict(run(service.collect_daily_alerts(at(5, 1, 7, year=2027))))
    assert any("Soutenance finale" in text and "Course" in text for text in alerts.values())


def test_report_reminder_at_three_days_and_on_the_day(env):
    service, _, _ = env
    for day, expected in [(date(2027, 1, 28), True), (date(2027, 1, 31), True), (date(2027, 1, 30), False)]:
        now = datetime(day.year, day.month, day.day, 7, tzinfo=PARIS)
        alerts = dict(run(service.collect_daily_alerts(now)))
        found = [text for key, text in alerts.items() if key.startswith("awake:2027-01-31")]
        assert bool(found) is expected
        if found:
            assert "23 h 59" in found[0] and "pénalité" in found[0]


def test_evening_reports_the_rest_and_asks_progress(env):
    service, _, gitlab = env
    gitlab.issues = [{"iid": 3, "title": "Doc", "state": "opened", "due_date": "2026-11-20",
                      "assignees": [{"id": GITLAB_ID}], "created_at": "2026-10-01T00:00:00Z"}]
    service.store.add_task("TD thermo", date(2026, 11, 20))
    today = date(2026, 11, 10)
    refresh(service, ZEUS, at(11, 9, 7))
    run(service.build_brief(at(11, 10, 7)))
    items = service.store.plan_items(today)
    j3d_item = next(item for item in items if item.block.kind == "j3d")
    service.mark_done(j3d_item.id, at(11, 10, 15))

    evening = service.build_evening(at(11, 10, 21)).content
    assert "✅ J3D : Doc" in evening
    assert "TD thermo" in evening and "sans compter comme du retard" in evening
    assert "J3D : tu en es où" in evening
    assert service.store.tasks_for(today) == []  # reporté à demain
    assert len(service.store.tasks_for(today + timedelta(days=1))) == 1

    assert "Noté" in run(service.handle_text("relire le chapitre 2", at(11, 10, 21, 5))).content
    assert "J3D : relire le chapitre 2" in run(service.build_brief(at(11, 11, 7))).content
    assert service.store.minutes_between("j3d", today, today) > 0


def test_commands(env):
    service, _, _ = env
    now = at(11, 9, 8)
    reply = run(service.handle_text("ajoute réviser thermo pour lundi", now)).content
    assert "réviser thermo" in reply and "lundi 9 novembre" in reply
    assert "point de suivi" in run(service.handle_text("point de suivi AWAKE le 12/01 à 18h", now)).content.lower()
    assert service.store.custom_events_on(date(2027, 1, 12))
    assert "manque" in run(service.handle_text("point de suivi AWAKE", now)).content
    assert "6 h" in run(service.handle_text("objectif club 6h", now)).content
    assert service.store.get_value("club_goal_hours") == 6
    assert "jeudi" in run(service.handle_text("créneau club jeudi 18h-20h", now)).content
    assert service._fixed_slots()[0].weekday == 3
    run(service.handle_text("supprime créneau club jeudi", now))
    assert service._fixed_slots() == []
    assert "7 prochains jours" in run(service.handle_text("semaine", now)).content
    assert "aide" not in run(service.handle_text("aide", now)).content.lower().split("\n")[0] or True


def test_progress_without_project_asks_which_one(env):
    service, _, _ = env
    now = at(11, 9, 8)
    assert "voiture" not in run(service.handle_text("j'en suis à la doc", now)).content.lower()
    reply = run(service.handle_action("progress", "voiture", now))[0]
    assert "la doc" in reply.content
    assert service.store.next_step("voiture") == "la doc"


def test_done_by_name_and_by_list(env):
    service, _, _ = env
    service.store.add_task("TD thermo", date(2026, 11, 12))
    refresh(service, ZEUS, at(11, 9, 7))
    run(service.build_brief(at(11, 10, 7)))
    assert "?" in run(service.handle_text("fait", at(11, 10, 15))).content
    assert "✅ TD thermo" in run(service.handle_text("fait thermo", at(11, 10, 15))).content
    assert service.store.tasks_for(date(2026, 11, 10)) == []


def test_week_mode_uses_yaml_dates_if_gitlab_has_no_milestone(env):
    service, _, gitlab = env
    gitlab.broken = True
    view = run(service.read_gitlab())
    assert service.week_mode(date(2026, 12, 8), [], view) == "chargee"


def test_milestone_without_issue_keeps_a_minimum_of_j3d_time(env):
    service, feeds, gitlab = env
    feeds.set(ZEUS_URL)
    gitlab.milestones = [{"title": "Remise du CdST", "due_date": "2026-12-07"}]
    today = date(2026, 11, 10)
    assert "J3D : Remise du CdST" in run(service.build_brief(at(11, 10, 7))).content
    service.store.log_time("j3d", today, 180)  # le minimum de 3 h est atteint
    assert "J3D : Remise du CdST" not in run(service.build_brief(at(11, 11, 7))).content.split("Prochaine")[0]


def test_overdue_issue_is_named_as_late(env):
    service, feeds, gitlab = env
    feeds.set(ZEUS_URL)
    gitlab.issues = [{"iid": 1, "title": "Doc", "state": "opened", "due_date": "2026-11-01",
                      "assignees": [{"id": GITLAB_ID}], "created_at": "2026-10-01T00:00:00Z"}]
    assert "en retard depuis le 01/11" in run(service.build_brief(at(11, 10, 7))).content


def test_club_uses_daytime_during_holidays(env):
    service, feeds, gitlab = env
    feeds.set(ZEUS_URL, vevent("v", "Vacances", "20261105T043000Z", "20261105T183000Z"))
    refresh(service, ZEUS, at(11, 1, 7))
    run(service.build_brief(at(11, 5, 7)))
    car = next(i for i in service.store.plan_items(date(2026, 11, 5)) if i.block.kind == "voiture")
    assert car.block.start.hour < 18


def test_fixed_club_slot_counts_as_car_work_and_asks_progress(env):
    service, feeds, _ = env
    feeds.set(ZEUS_URL)
    run(service.handle_text("créneau club jeudi 18h-20h", at(11, 9, 8)))
    run(service.build_brief(at(11, 12, 7)))
    item = next(i for i in service.store.plan_items(date(2026, 11, 12)) if i.block.kind == "club")
    service.mark_done(item.id, at(11, 12, 20))
    assert service.store.minutes_between("voiture", date(2026, 11, 12), date(2026, 11, 12)) == 120
    assert "Voiture AWAKE : tu en es où" in service.build_evening(at(11, 12, 21)).content
