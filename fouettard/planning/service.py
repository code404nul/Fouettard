"""Cerveau du module : assemble les sources et répond aux messages.

Ce fichier ne dépend pas de Discord : il renvoie des textes et des boutons (Reply),
ce qui permet de le tester entièrement avec un faux GitLab.
"""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

import httpx

from fouettard.gitlab_api import GitLabClient, GitLabError
from fouettard.logic.key_dates import KeyDate
from fouettard.planning import agenda, j3d, mail, messages as msg, nlp, spaced
from fouettard.planning.config import AwakeDeadline, MAIL, OUTLOOK, ZEUS, PlanningConfig, WeeklySlot
from fouettard.planning.ics import CalEvent, IcsError, fetch_ics, parse_ics
from fouettard.planning.scheduler import (CLUB_FIXE, DOMOTIQUE, J3D, LEGER, NORMAL, OFF,
                                          REVISION, SOIREE, VOITURE, DayPlan, PlanRequest,
                                          ReviewRequest, TaskRequest, build_plan, subtract)
from fouettard.planning.store import DONE, PLANNED, POSTPONED, PlanItem, PlanningStore
from fouettard.timeutils import PARIS

log = logging.getLogger(__name__)

HISTORY_DAYS = 2      # on garde les cours récents pour créer leurs révisions
HORIZON_DAYS = 300    # les partiels et les dates AWAKE sont loin
COLLISION_HORIZON_DAYS = 45
MAIL_RETENTION_DAYS = 60  # au-delà, le mail est effacé de la base
MAX_MAILS_IN_BRIEF = 5
MAX_MAILS_WITH_BUTTONS = 10
DEFAULT_EVENT_MINUTES = 30
PROJECTS_WITH_TIME = {J3D, VOITURE, DOMOTIQUE, CLUB_FIXE}

CHARGE = "chargee"
CALME = "calme"
REGULIERE = "normale"


@dataclass
class ButtonSpec:
    label: str
    action: str
    arg: str = ""
    style: str = "secondary"


@dataclass
class Reply:
    content: str
    buttons: list[ButtonSpec] = field(default_factory=list)
    open_date_modal: bool = False


@dataclass
class GitLabView:
    """Photo du GitLab J3D à un instant donné."""

    ok: bool = True
    gitlab_id: int | None = None
    linked: bool = False
    mine: list[dict] = field(default_factory=list)
    merge_requests: list[dict] = field(default_factory=list)
    milestones: list[dict] = field(default_factory=list)


BRIEF_BUTTONS = [ButtonSpec("Fait", "list_done", style="success"),
                 ButtonSpec("Reporter", "list_postpone"),
                 ButtonSpec("Semaine", "week", style="primary")]


def mail_rules(config: PlanningConfig) -> mail.MailRules:
    return mail.MailRules(config.mail_school_domains, config.mail_prof_senders,
                          config.mail_noise_domains, config.mail_noise_keywords,
                          config.mail_action_keywords, config.mail_urgent_keywords)


class PlanningService:
    def __init__(self, config: PlanningConfig, store: PlanningStore, gitlab: GitLabClient,
                 database, key_dates: list[KeyDate], http: httpx.AsyncClient):
        self.config = config
        self.store = store
        self.gitlab = gitlab
        self.database = database
        self.key_dates = key_dates
        self.http = http

    # --- Calendriers -------------------------------------------------------

    async def refresh_source(self, source: str, now: datetime) -> list[agenda.Change]:
        """Relit un flux. Renvoie les changements qui méritent une alerte immédiate.

        Si le flux est en panne, la dernière version enregistrée reste utilisée.
        """
        url = self.config.urls.get(source)
        if not url:
            return []
        today = now.date()
        try:
            text = await fetch_ics(self.http, url)
            events = await asyncio.to_thread(parse_ics, text, source,
                                             today - timedelta(days=HISTORY_DAYS),
                                             today + timedelta(days=HORIZON_DAYS))
            old = self.store.load_events(source)
            # Un calendrier soudain vide est une panne, pas l'annulation de tous les cours
            if not events and any(event.end > now for event in old):
                raise IcsError("calendrier vide")
        except IcsError as exc:
            log.warning("Lecture %s impossible : %s", msg.SOURCE_NAMES[source], exc)
            self.store.mark_source_error(source, now)
            return []

        # À la toute première lecture, tout serait « nouveau » : rien à signaler
        first_read = self.store.source_status(source)[0] is None
        changes = [] if first_read else agenda.diff_events(old, events, now)
        changes = self._relevant_changes(source, changes, old + events)
        self.store.save_events(source, events)
        self.store.mark_source_ok(source, now)
        self.store.add_changes(changes)
        if source == ZEUS:
            self._create_reviews(now)
        return [change for change in changes if agenda.is_alert_worthy(change, today)]

    def _relevant_changes(self, source: str, changes: list[agenda.Change],
                          involved: list[CalEvent]) -> list[agenda.Change]:
        """Écarte les changements des cours ignorés et des doublons de Zeus."""
        titles = {(event.title, event.start): event for event in involved}
        zeus = self.store.load_events(ZEUS) if source == OUTLOOK else []
        kept = []
        for change in changes:
            event = titles.get((change.title, change.when))
            if event is None:
                event = next((item for item in involved if item.title == change.title), None)
            if event is not None and agenda.is_ignored(event, self.config):
                continue
            if source == OUTLOOK and event is not None and agenda.is_duplicate(event, zeus):
                continue
            kept.append(change)
        return kept

    def _create_reviews(self, now: datetime) -> None:
        """Crée les révisions des cours terminés. Sans effet si elles existent déjà."""
        for event in self.store.load_events(ZEUS):
            if event.end > now or agenda.kind_of(event) != agenda.COURS:
                continue
            if agenda.is_ignored(event, self.config):
                continue
            subject = agenda.subject_of(event.title, self.config)
            if subject:
                self.store.add_review(subject, event.start.date())

    def all_events(self) -> list[CalEvent]:
        by_source = {source: self.store.load_events(source) for source in (ZEUS, OUTLOOK)}
        return agenda.merge_sources(by_source, self.config)

    def outage_lines(self, now: datetime) -> list[str]:
        """Signale discrètement un flux en panne, au plus une fois par jour."""
        lines = []
        sources = list(self.config.urls) + ([MAIL] if self.config.imap else [])
        for source in sources:
            last_ok, last_error = self.store.source_status(source)
            if last_error is None:
                continue
            key = f"outage:{source}:{now.date().isoformat()}"
            if self.store.was_sent(key):
                continue
            self.store.mark_sent(key, now)
            since = f"version du {last_ok.strftime('%d/%m à %H:%M')}" if last_ok else "aucune version enregistrée"
            lines.append(f"ℹ️ {msg.SOURCE_NAMES[source]} injoignable, {since}.")
        return lines

    # --- Mails --------------------------------------------------------------

    async def refresh_mail(self, now: datetime) -> list[str]:
        """Lit les nouveaux mails, les trie et renvoie les alertes (mails urgents de l'école)."""
        settings = self.config.imap
        if settings is None:
            return []
        today = now.date()
        since = mail.lookback_start(today, self.config.mail_lookback_days)
        try:
            messages = await asyncio.to_thread(mail.fetch_new_messages, settings, since,
                                               self.store.known_mail_keys(), now)
        except mail.MailError as exc:
            log.warning("Lecture des mails impossible : %s", exc)
            self.store.mark_source_error(MAIL, now)
            return []

        # À la toute première lecture, les anciens mails ne déclenchent pas d'alerte
        first_read = self.store.source_status(MAIL)[0] is None
        rules = mail_rules(self.config)
        alerts = []
        for message in messages:
            due = mail.find_due_date(f"{message.subject}. {message.text}", message.received.date())
            category = mail.classify(message, rules, due)
            # Pour le bruit, on ne garde que l'identifiant qui évite de le relire
            keep = category != mail.BRUIT
            self.store.add_mail(message.uid_key, message.sender if keep else "",
                                message.subject if keep else "", message.received, category, due)
            recent = now - message.received <= timedelta(days=1)
            if not first_read and recent and mail.is_urgent(message, rules, due):
                alerts.append(f"📧 Mail urgent de {msg.safe_text(message.sender_name or message.sender, 40)} : "
                              f"{msg.safe_text(message.subject)}" + self._due_text(due))
        self.store.mark_source_ok(MAIL, now)
        self.store.forget_old_mails(now - timedelta(days=MAIL_RETENTION_DAYS))
        return alerts

    @staticmethod
    def _due_text(due: date | None) -> str:
        return f" (date repérée : {msg.short_day(due)})" if due else ""

    def _mail_line(self, stored) -> str:
        return (f"{msg.safe_text(stored.sender, 40)} : {msg.safe_text(stored.subject)}"
                + self._due_text(stored.due_on))

    def _mails_to_handle(self, now: datetime):
        since = now - timedelta(days=self.config.mail_lookback_days)
        return self.store.mails_to_handle(mail.A_TRAITER, since)

    def _mail_brief_lines(self, now: datetime) -> list[str]:
        """Section du brief. Les mails « à savoir » ne sont montrés qu'une fois."""
        if self.config.imap is None:
            return []
        to_handle = self._mails_to_handle(now)
        lines = []
        if to_handle:
            lines += ["", "**Mails à traiter**"]
            lines += [f"- {self._mail_line(stored)}" for stored in to_handle[:MAX_MAILS_IN_BRIEF]]
            if len(to_handle) > MAX_MAILS_IN_BRIEF:
                lines.append(f"- et {len(to_handle) - MAX_MAILS_IN_BRIEF} autres (écris « mails »)")
        to_know = self._mails_to_know(now)
        lines += self._to_know_lines(to_know)
        for stored in to_know:
            self.store.close_mail(stored.id)
        return lines

    def _mails_to_know(self, now: datetime):
        return self.store.mails_to_handle(mail.A_SAVOIR, now - timedelta(days=self.config.mail_lookback_days))

    def _to_know_lines(self, to_know) -> list[str]:
        if not to_know:
            return []
        lines = ["", "**Mails à savoir**"]
        lines += [f"- {self._mail_line(stored)}" for stored in to_know[:MAX_MAILS_IN_BRIEF]]
        if len(to_know) > MAX_MAILS_IN_BRIEF:
            lines.append(f"- et {len(to_know) - MAX_MAILS_IN_BRIEF} autres")
        return lines

    def list_mails(self, now: datetime) -> Reply:
        """Les mails « à savoir » sont montrés sans être clos : le brief les affichera encore."""
        if self.config.imap is None:
            return Reply("La lecture des mails n'est pas configurée.")
        last_ok, last_error = self.store.source_status(MAIL)
        if last_ok is None:
            return Reply("⚠️ Je n'ai pas encore réussi à lire ta boîte mail. Réessaie dans un quart d'heure.")
        lines = []
        if last_error is not None and last_error > last_ok:
            lines += ["⚠️ Boîte mail injoignable pour l'instant : cette liste peut être incomplète.", ""]
        to_handle = self._mails_to_handle(now)
        to_know_lines = self._to_know_lines(self._mails_to_know(now))
        if not to_handle:
            lines.append("Aucun mail à traiter.")
            return Reply("\n".join(lines + to_know_lines))
        shown = to_handle[:MAX_MAILS_WITH_BUTTONS]
        lines.append("📧 **Mails à traiter**")
        lines += [f"{number}. {self._mail_line(stored)}" for number, stored in enumerate(shown, 1)]
        lines += to_know_lines
        buttons = []
        for number, stored in enumerate(shown, 1):
            buttons.append(ButtonSpec(f"{number} traité", "mail_done", str(stored.id), "success"))
            if stored.due_on:
                buttons.append(ButtonSpec(f"{number} au planning", "mail_add", str(stored.id), "primary"))
        return Reply("\n".join(lines), buttons)

    def close_mail(self, mail_id: int, add_to_plan: bool) -> Reply:
        stored = self.store.get_mail(mail_id)
        if stored is None or stored.state != "new":
            return Reply("Ce mail est déjà traité.")
        self.store.close_mail(mail_id)
        if add_to_plan and stored.due_on:
            title = msg.safe_text(stored.subject, 60)
            self.store.add_task(f"Mail : {title}", stored.due_on)
            return Reply(f"📝 Ajouté au planning : « {title} », pour {msg.day_name(stored.due_on)}.")
        return Reply("✅ Mail traité.")

    # --- GitLab ------------------------------------------------------------

    async def read_gitlab(self) -> GitLabView:
        link = self.database.get_link_by_discord(self.config.owner_id)
        view = GitLabView(linked=link is not None, gitlab_id=link.gitlab_id if link else None)
        try:
            issues = await self.gitlab.get_issues(state="opened")
            view.mine = j3d.my_issues(issues, view.gitlab_id)
            view.merge_requests = j3d.merge_requests_to_review(
                await self.gitlab.get_merge_requests(state="opened"), view.gitlab_id)
            view.milestones = await self.gitlab.get_milestones()
        except GitLabError as exc:
            log.warning("GitLab indisponible pour le planning : %s", exc)
            view.ok = False
        return view

    def _j3d_dates(self, view: GitLabView) -> list[tuple[date, str]]:
        """Échéances J3D : les milestones GitLab, ou deadlines.yaml si GitLab ne répond pas."""
        found = [(j3d.milestone_date(milestone), milestone["title"]) for milestone in view.milestones
                 if j3d.milestone_date(milestone)]
        if found:
            return sorted(found)
        return sorted((key_date.week_start, key_date.name) for key_date in self.key_dates)

    # --- Réglages modifiables en message privé -----------------------------

    def _club_goal_minutes(self) -> int:
        return int(self.store.get_value("club_goal_hours", self.config.club_weekly_goal_hours) * 60)

    def _min_block(self) -> int:
        return int(self.store.get_value("min_block", self.config.min_project_block_minutes))

    def _fixed_slots(self) -> list[WeeklySlot]:
        stored = self.store.get_value("club_slots")
        if stored is None:
            return self.config.fixed_slots
        return [WeeklySlot(slot["weekday"], time.fromisoformat(slot["start"]),
                           time.fromisoformat(slot["end"])) for slot in stored]

    # --- Construction du planning ------------------------------------------

    def week_mode(self, day: date, events: list[CalEvent], view: GitLabView) -> str:
        monday, sunday = msg.week_of(day)
        week_days = [monday + timedelta(days=offset) for offset in range(7)]
        if any(agenda.kind_of(event) == agenda.EXAM
               for week_day in week_days for event in agenda.events_on(events, week_day)):
            return CHARGE
        if any(monday <= due <= sunday or 0 <= (due - day).days <= 7
               for due, _ in self._j3d_dates(view)):
            return CHARGE
        if sum(agenda.is_vacation(events, week_day) for week_day in week_days) >= 3:
            return CALME
        return REGULIERE

    def _next_awake(self, today: date) -> AwakeDeadline | None:
        upcoming = [deadline for deadline in self.config.awake_deadlines if deadline.day >= today]
        return min(upcoming, key=lambda deadline: deadline.day) if upcoming else None

    def _voiture_minutes(self, day: date, mode: str) -> tuple[int, int]:
        """(temps à placer, taille minimale du bloc) pour la voiture ce jour-là."""
        if mode == CHARGE:
            # Mode minimum : seules les échéances AWAKE incontournables comptent
            deadline = self._next_awake(day)
            if deadline and (deadline.day - day).days <= 7:
                return 30, 30
            return 0, 30
        goal = self._club_goal_minutes()
        if mode == CALME:
            goal = int(goal * 1.5)
        monday, sunday = msg.week_of(day)
        remaining = goal - self.store.minutes_between(VOITURE, monday, sunday)
        if remaining <= 0:
            return 0, self._min_block()
        preferred = set(self.config.club_preferred_weekdays) | {5, 6}
        if day.weekday() not in preferred:
            # Un autre soir de semaine ne sert que si les créneaux préférés ne suffisent pas
            days_left = sum(1 for offset in range(1, (sunday - day).days + 1)
                            if (day + timedelta(days=offset)).weekday() in preferred)
            if remaining <= days_left * self.config.max_project_block_minutes:
                return 0, self._min_block()
        return remaining, self._min_block()

    def plan_day(self, day: date, view: GitLabView, events: list[CalEvent],
                 now: datetime | None = None) -> tuple[DayPlan, str, str, j3d.J3dNeed]:
        """Calcule le plan d'un jour. Renvoie (plan, niveau, mode de la semaine, besoin J3D)."""
        config = self.config
        level = self.store.level_on(day)
        mode = self.week_mode(day, events, view)

        busy = [agenda.day_window(event, day) for event in agenda.events_on(events, day)
                if agenda.is_blocking(event, config)]
        busy += [(start, end) for _, start, end in self.store.custom_events_on(day)]

        def is_workday(candidate: date) -> bool:
            return self.store.level_on(candidate) in (NORMAL, SOIREE)

        need = j3d.compute_need(view.mine, day, is_workday, config.j3d_default_issue_hours,
                                config.j3d_urgent_hours)
        j3d_minutes = max(0, need.minutes - self.store.minutes_between(J3D, day, day))
        j3d_label = "J3D"
        if need.deadline:
            status = "en retard depuis le" if need.deadline < day else "échéance"
            j3d_label = f"J3D : {need.deadline_name} ({status} {need.deadline.strftime('%d/%m')})"
        else:
            # Milestones sans issue attribuée : on garde quand même un temps minimal
            milestone = j3d.active_milestone(view.milestones, day)
            monday, sunday = msg.week_of(day)
            logged = self.store.minutes_between(J3D, monday, sunday)
            if milestone and logged < config.j3d_weekly_floor_hours * 60:
                j3d_minutes = 60
                j3d_label = f"J3D : {milestone['title']}"

        distances = agenda.exam_distances(events, day, config.exam_boost_days, config)
        groups = spaced.select_due(self.store.active_reviews(), day, distances,
                                   config.max_reviews_per_day)
        reviews = [ReviewRequest(
            f"Révision {group.subject}" + (" (partiel proche)" if group.boosted else "")
            + (" (notée difficile)" if group.hard else ""),
            "review:" + ",".join(str(review_id) for review_id in group.review_ids))
            for group in groups]

        tasks = [TaskRequest(task["id"], task["title"],
                             date.fromisoformat(task["due_on"]) if task["due_on"] else None,
                             task["minutes"]) for task in self.store.tasks_for(day)]

        voiture, voiture_min = self._voiture_minutes(day, mode)
        domotique = 0
        monday, sunday = msg.week_of(day)
        goal_met = self.store.minutes_between(VOITURE, monday, sunday) >= self._club_goal_minutes()
        if mode != CHARGE and (mode == CALME or goal_met):
            domotique = self._min_block()

        plan = build_plan(PlanRequest(
            day=day, level=level, busy=busy, meals=config.meals,
            fixed_slots=self._fixed_slots(), protected_slots=config.protected_slots,
            day_start=config.day_start, day_end=config.day_end,
            evening_start=config.evening_start, not_before=now,
            j3d_minutes=j3d_minutes, j3d_label=j3d_label, j3d_urgent=need.urgent,
            tasks=tasks, reviews=reviews, review_minutes=config.review_minutes,
            voiture_minutes=voiture, voiture_min_block=voiture_min,
            club_all_day=agenda.is_vacation(events, day),
            domotique_minutes=domotique, min_project_block=self._min_block(),
            max_project_block=config.max_project_block_minutes))
        if need.impossible and level in (LEGER, OFF):
            plan.warnings.append(f"J3D : « {need.deadline_name} » tombe sur un jour allégé, "
                                 "il faudra réserver un créneau.")
        return plan, level, mode, need

    # --- Brief du matin ----------------------------------------------------

    async def build_brief(self, now: datetime) -> Reply:
        today = now.date()
        self._create_reviews(now)
        view = await self.read_gitlab()
        events = self.all_events()
        plan, level, mode, need = self.plan_day(today, view, events)
        self.store.replace_planned(today, plan.blocks)

        lines = [f"☀️ **Brief du {msg.day_name(today)}**"]
        if level != NORMAL:
            lines.append(f"*{msg.LEVEL_NAMES[level].capitalize()} : {msg.LEVEL_EFFECTS[level]}*")

        changes = self.store.pop_unbriefed_changes()
        if changes:
            lines += ["", "**Changements**"] + [f"- {msg.change_line(change)}" for change in changes]

        day_lines = [msg.event_line(event, today) for event in agenda.events_on(events, today)]
        day_lines = [line for line in day_lines if line]
        lines += ["", "**Cours et rendez-vous**"]
        lines += [f"- {line}" for line in day_lines] or ["- Rien au calendrier."]

        lines += self._mail_brief_lines(now)

        school = [block for block in plan.blocks if not msg.is_club(block)]
        club = [block for block in plan.blocks if msg.is_club(block)]
        lines += ["", "**École**"] + ([f"- {msg.block_line(block)}" for block in school]
                                      or ["- Rien à placer."])
        lines += self._j3d_attention(view, now)

        title = {CHARGE: "Club (semaine chargée, mode minimum)",
                 CALME: "Club (semaine calme, objectif augmenté)"}.get(mode, "Club")
        lines += ["", f"**{title}**"]
        lines += [f"- {msg.block_line(block)}" for block in club] or ["- Rien de prévu aujourd'hui."]
        lines += self._awake_lines(today)

        probable = self._probable_homework(today, events)
        if probable:
            lines += ["", "**Devoirs probables (supposition)**"] + [f"- {line}" for line in probable]

        progress = self._progress_lines()
        if progress:
            lines += ["", "**Avancement**"] + [f"- {line}" for line in progress]

        notes = [f"⚠️ {warning}" for warning in plan.warnings] + self.outage_lines(now)
        if not view.ok:
            notes.append("ℹ️ GitLab injoignable : J3D n'est pas à jour.")
        elif not view.linked:
            notes.append("ℹ️ Relie ton compte GitLab avec `/link` pour voir tes issues.")
        if notes:
            lines += [""] + notes
        buttons = list(BRIEF_BUTTONS)
        if self.config.imap and self._mails_to_handle(now):
            buttons.append(ButtonSpec("Mails", "mails", style="primary"))
        return Reply("\n".join(lines), buttons)

    def _j3d_attention(self, view: GitLabView, now: datetime) -> list[str]:
        lines = [f"- MR à relire : {mr['title']}" for mr in view.merge_requests]
        for milestone in j3d.slipping_milestones(view.milestones, view.mine, now.date()):
            lines.append(f"- ⚠️ Milestone dépassée avec des issues ouvertes : {milestone['title']}")
        for issue in j3d.stale_issues(view.mine, now, self.config.j3d_stale_days):
            lines.append(f"- Issue sans mouvement : #{issue['iid']} {issue['title']}")
        upcoming = j3d.active_milestone(view.milestones, now.date())
        if upcoming:
            lines.append(f"- Prochaine milestone : {upcoming['title']} "
                         f"({msg.countdown(j3d.milestone_date(upcoming), now.date())})")
        return lines

    def _awake_lines(self, today: date) -> list[str]:
        deadline = self._next_awake(today)
        if not deadline:
            return []
        return [f"- Voiture : {deadline.name} ({msg.countdown(deadline.day, today)})"]

    def _probable_homework(self, today: date, events: list[CalEvent]) -> list[str]:
        """Suppositions à partir des cours d'hier. Toujours présentées comme telles."""
        yesterday = today - timedelta(days=1)
        subjects = []
        for event in agenda.events_on(events, yesterday):
            subject = agenda.subject_of(event.title, self.config)
            if (subject in self.config.probable_homework_subjects and subject not in subjects
                    and agenda.kind_of(event) == agenda.COURS
                    and not event.title.lower().startswith("cm ")):
                subjects.append(subject)
        return [f"{subject} : exercices à finir après le cours d'hier ?" for subject in subjects]

    def _progress_lines(self) -> list[str]:
        lines = []
        for project in (J3D, VOITURE, DOMOTIQUE):
            step = self.store.next_step(project)
            if step:
                lines.append(f"{msg.project_name(project)} : {step}")
        return lines

    # --- Alertes -----------------------------------------------------------

    def change_alert(self, change: agenda.Change) -> str:
        return f"🔔 {msg.change_line(change)}"

    async def collect_daily_alerts(self, now: datetime) -> list[tuple[str, str]]:
        """Alertes (clé anti-doublon, texte) : échéances proches et conflits à venir."""
        today = now.date()
        alerts = []
        for deadline in self.config.awake_deadlines:
            days = (deadline.day - today).days
            if days == 3 or days == 0:
                detail = " À envoyer avant 23 h 59, sinon 1 point de pénalité par jour." if deadline.is_report else ""
                alerts.append((f"awake:{deadline.day}:{days}",
                               f"🚗 AWAKE : {deadline.name} {msg.countdown(deadline.day, today)}.{detail}"))
        view = await self.read_gitlab()
        for issue in view.mine:
            due = j3d.issue_deadline(issue)
            if due and 0 <= (due - today).days <= 1:
                alerts.append((f"j3d:{issue['iid']}:{due}",
                               f"🎓 J3D : #{issue['iid']} {issue['title']} à rendre "
                               f"{msg.countdown(due, today)}."))
        alerts += self._collision_alerts(today, view)
        return alerts

    def _collision_alerts(self, today: date, view: GitLabView) -> list[tuple[str, str]]:
        """Échéances AWAKE qui tombent pendant des examens ou une échéance J3D."""
        events = self.all_events()
        alerts = []
        for deadline in self.config.awake_deadlines:
            if not 0 <= (deadline.day - today).days <= COLLISION_HORIZON_DAYS:
                continue
            exams = [event for event in agenda.events_on(events, deadline.day)
                     if agenda.kind_of(event) == agenda.EXAM]
            if exams:
                first_free = deadline.day
                while any(agenda.kind_of(event) == agenda.EXAM
                          for event in agenda.events_on(events, first_free - timedelta(days=1))):
                    first_free -= timedelta(days=1)
                alerts.append((f"collision:{deadline.day}:exam",
                               f"⚠️ {deadline.name} ({deadline.day.strftime('%d/%m')}) tombe pendant "
                               f"{exams[0].title}. L'école passe d'abord : prévois de finir avant le "
                               f"{first_free.strftime('%d/%m')}, ou confie-le à l'équipe."))
            monday, sunday = msg.week_of(deadline.day)
            for due, name in self._j3d_dates(view):
                if monday <= due <= sunday:
                    alerts.append((f"collision:{deadline.day}:{name}",
                                   f"⚠️ {deadline.name} ({deadline.day.strftime('%d/%m')}) est dans la "
                                   f"même semaine que {name} (J3D, {due.strftime('%d/%m')}). L'école "
                                   "passe d'abord : avance le travail du club ou répartis-le."))
        return alerts

    # --- Bilan du soir -----------------------------------------------------

    def build_evening(self, now: datetime) -> Reply:
        today = now.date()
        items = self.store.plan_items(today)
        done = [item for item in items if item.state == DONE]
        pending = [item for item in items if item.state == PLANNED]

        lines = [f"🌙 **Bilan du {msg.day_name(today)}**"]
        if not items:
            lines.append("Rien n'était prévu aujourd'hui.")
        if done:
            lines += ["", "**Fait**"] + [f"- ✅ {item.block.label}" for item in done]
        for item in pending:
            self._postpone_item(item, today)
        if pending:
            lines += ["", "**Reporté à demain (sans compter comme du retard)**"]
            lines += [f"- {item.block.label}" for item in pending]

        worked = []
        for item in done:
            project = VOITURE if item.block.kind == CLUB_FIXE else item.block.kind
            if project in (J3D, VOITURE, DOMOTIQUE) and project not in worked:
                worked.append(project)
        self.store.set_value("progress_queue", worked)
        if worked:
            lines += ["", f"**{msg.project_name(worked[0])} : tu en es où ?** Réponds en une ligne "
                          "(ou « passer ») : ce sera la prochaine étape affichée demain."]
        return Reply("\n".join(lines))

    # --- Vue de la semaine ---------------------------------------------------

    async def build_week(self, now: datetime) -> Reply:
        view = await self.read_gitlab()
        events = self.all_events()
        lines = ["📅 **Les 7 prochains jours**"]
        for offset in range(7):
            day = now.date() + timedelta(days=offset)
            plan, level, _, _ = self.plan_day(day, view, events)
            header = f"**{msg.short_day(day)}**"
            if level != NORMAL:
                header += f" ({msg.LEVEL_NAMES[level]})"
            lines += ["", header]
            calendar_lines = [msg.event_line(event, day) for event in agenda.events_on(events, day)]
            lines += [f"- {line}" for line in calendar_lines if line]
            lines += [f"- → {msg.block_line(block)}" for block in plan.blocks]
            if not plan.blocks and not any(calendar_lines):
                lines.append("- Libre.")
        return Reply("\n".join(lines))

    # --- Messages reçus ------------------------------------------------------

    async def handle_text(self, text: str, now: datetime) -> Reply:
        today = now.date()
        intent = nlp.understand(text, today)
        queue = self.store.get_value("progress_queue", [])

        if intent.kind == nlp.UNKNOWN and queue:
            return self._answer_progress_queue(text.strip(), queue, today)
        if intent.kind == nlp.WEEK:
            return await self.build_week(now)
        if intent.kind == nlp.MAILS:
            return self.list_mails(now)
        if intent.kind == nlp.HELP:
            return Reply(msg.HELP_TEXT)
        if intent.kind == nlp.DONE:
            return self._mark_by_text(intent.text, today, now, done=True)
        if intent.kind == nlp.POSTPONE:
            return self._mark_by_text(intent.text, today, now, done=False)
        if intent.kind == nlp.PROGRESS:
            return self._record_progress(intent, queue, today)
        if intent.kind == nlp.ADD_TASK:
            return self._add_task(intent)
        if intent.kind == nlp.ADD_EVENT:
            return self._add_event(intent)
        if intent.kind == nlp.LIGHT:
            return await self._ask_light(intent.dates, intent.level, now)
        if intent.kind == nlp.CANCEL_LIGHT:
            return await self._ask_cancel_light(intent.dates, now)
        if intent.kind == nlp.SET_GOAL:
            self.store.set_value("club_goal_hours", intent.number)
            return Reply(f"👍 Objectif hebdo du club : {intent.number:g} h.")
        if intent.kind == nlp.SET_MIN_BLOCK:
            self.store.set_value("min_block", int(intent.number))
            return Reply(f"👍 Bloc projet minimum : {int(intent.number)} min.")
        if intent.kind == nlp.ADD_CLUB_SLOT:
            return self._add_club_slot(intent)
        if intent.kind == nlp.REMOVE_CLUB_SLOT:
            return self._remove_club_slot(int(intent.number))
        return Reply("Je n'ai pas compris. C'est pour un jour léger ? Sinon, écris « aide ».",
                     self._light_date_buttons(LEGER))

    def _answer_progress_queue(self, text: str, queue: list[str], today: date) -> Reply:
        project = queue[0]
        if nlp.normalize(text) != "passer":
            self.store.set_next_step(project, text, today)
        queue = queue[1:]
        self.store.set_value("progress_queue", queue)
        if queue:
            return Reply(f"Noté. **{msg.project_name(queue[0])} : tu en es où ?**")
        return Reply("Noté, bonne soirée.")

    def _record_progress(self, intent: nlp.Intent, queue: list[str], today: date) -> Reply:
        project = intent.project or (queue[0] if queue else None)
        if project is None:
            self.store.set_value("pending_step", intent.text)
            return Reply("C'est pour quel projet ?",
                         [ButtonSpec(msg.project_name(name), "progress", name)
                          for name in (J3D, VOITURE, DOMOTIQUE)])
        self.store.set_next_step(project, intent.text, today)
        if project in queue:
            self.store.set_value("progress_queue", [name for name in queue if name != project])
        return Reply(f"📌 {msg.project_name(project)} : prochaine étape « {intent.text} ».")

    def _add_task(self, intent: nlp.Intent) -> Reply:
        if not intent.text:
            return Reply("Que veux-tu ajouter ? Exemple : « ajoute réviser thermo pour lundi ».")
        due = intent.dates.dates[0] if intent.dates and intent.dates.dates else None
        self.store.add_task(intent.text, due)
        when = f" pour {msg.day_name(due)}" if due else " (sans échéance)"
        return Reply(f"📝 Ajouté : « {intent.text} »{when}.")

    def _add_event(self, intent: nlp.Intent) -> Reply:
        if not (intent.dates and intent.dates.dates and intent.moment):
            return Reply("Il me manque la date ou l'heure. Exemple : « point de suivi AWAKE le 12/01 à 18h ».")
        start = datetime.combine(intent.dates.dates[0], intent.moment, tzinfo=PARIS)
        self.store.add_custom_event(intent.text, start, start + timedelta(minutes=DEFAULT_EVENT_MINUTES))
        return Reply(f"📌 Ajouté : {intent.text}, {msg.day_name(start.date())} à {msg.hour(start)}.")

    def _add_club_slot(self, intent: nlp.Intent) -> Reply:
        weekday = int(intent.number)
        slots = [slot for slot in self._fixed_slots() if slot.weekday != weekday]
        slots.append(WeeklySlot(weekday, intent.moment, intent.end_moment))
        self._save_club_slots(slots)
        return Reply(f"👍 Créneau club : chaque {msg.WEEKDAY_NAMES[weekday]}, "
                     f"{msg.hour(intent.moment)}-{msg.hour(intent.end_moment)}.")

    def _remove_club_slot(self, weekday: int) -> Reply:
        self._save_club_slots([slot for slot in self._fixed_slots() if slot.weekday != weekday])
        return Reply(f"👍 Plus de créneau club le {msg.WEEKDAY_NAMES[weekday]}.")

    def _save_club_slots(self, slots: list[WeeklySlot]) -> None:
        self.store.set_value("club_slots", [
            {"weekday": slot.weekday, "start": slot.start.isoformat(), "end": slot.end.isoformat()}
            for slot in slots])

    # --- Fait / Reporter -----------------------------------------------------

    def _pending(self, today: date) -> list[PlanItem]:
        return self.store.plan_items(today, PLANNED)

    def list_items(self, today: date, done: bool) -> Reply:
        pending = self._pending(today)
        if not pending:
            return Reply("Plus rien de prévu aujourd'hui.")
        action, question = (("done", "Qu'as-tu terminé ?") if done
                            else ("postpone", "Que veux-tu reporter ?"))
        return Reply(question, [ButtonSpec(item.block.label[:80], action, str(item.id),
                                           "success" if done else "secondary")
                                for item in pending[:20]])

    def _mark_by_text(self, text: str, today: date, now: datetime, done: bool) -> Reply:
        pending = self._pending(today)
        if text:
            wanted = nlp.normalize(text)
            matches = [item for item in pending if wanted in nlp.normalize(item.block.label)]
            if len(matches) == 1:
                return self.mark_done(matches[0].id, now) if done else self.mark_postponed(matches[0].id, now)
        return self.list_items(today, done)

    def mark_done(self, item_id: int, now: datetime) -> Reply:
        item = self.store.get_plan_item(item_id)
        if item is None or item.state != PLANNED:
            return Reply("Cet élément n'est plus à faire.")
        self.store.set_item_state(item.id, DONE)
        block = item.block
        if block.kind in PROJECTS_WITH_TIME:
            minutes = int((block.end - block.start).total_seconds() // 60)
            project = VOITURE if block.kind == CLUB_FIXE else block.kind
            self.store.log_time(project, now.date(), minutes)
        if block.ref.startswith("task:"):
            self.store.finish_task(int(block.ref.split(":")[1]))
        if block.kind == REVISION:
            return Reply(f"✅ {block.label}. Comment ça s'est passé ?",
                         [ButtonSpec("Facile", "grade", f"{item.id}-{spaced.FACILE}", "success"),
                          ButtonSpec("Moyen", "grade", f"{item.id}-{spaced.MOYEN}", "primary"),
                          ButtonSpec("Difficile", "grade", f"{item.id}-{spaced.DIFFICILE}", "danger")])
        return Reply(f"✅ {block.label}")

    def _postpone_item(self, item: PlanItem, today: date) -> None:
        tomorrow = today + timedelta(days=1)
        self.store.set_item_state(item.id, POSTPONED)
        if item.block.ref.startswith("task:"):
            self.store.reschedule_task(int(item.block.ref.split(":")[1]), tomorrow)
        if item.block.kind == REVISION:
            for review_id in self._review_ids(item):
                review = self.store.get_review(review_id)
                if review:
                    self.store.update_review(replace(review, due_on=tomorrow))

    def mark_postponed(self, item_id: int, now: datetime) -> Reply:
        item = self.store.get_plan_item(item_id)
        if item is None or item.state != PLANNED:
            return Reply("Cet élément n'est plus à reporter.")
        self._postpone_item(item, now.date())
        return Reply(f"↪️ {item.block.label} : reporté à demain, sans compter comme du retard.")

    @staticmethod
    def _review_ids(item: PlanItem) -> list[int]:
        return [int(value) for value in item.block.ref.removeprefix("review:").split(",") if value]

    def grade(self, item_id: int, grade: str, now: datetime) -> Reply:
        key = f"graded:{item_id}"
        if self.store.was_sent(key):
            return Reply("Déjà noté.")
        item = self.store.get_plan_item(item_id)
        if item is None or item.block.kind != REVISION:
            return Reply("Cette révision n'existe plus.")
        today = now.date()
        distances = agenda.exam_distances(self.all_events(), today, self.config.exam_boost_days,
                                          self.config)
        next_due = None
        for review_id in self._review_ids(item):
            review = self.store.get_review(review_id)
            if review is None:
                continue
            updated = spaced.after_grade(review, grade, today,
                                         spaced.exam_days_for(review.subject, distances))
            self.store.update_review(updated)
            if updated.active:
                next_due = min(next_due or updated.due_on, updated.due_on)
        self.store.mark_sent(key, now)
        follow = f" Prochaine révision : {msg.day_name(next_due)}." if next_due else " Cette matière est à jour."
        return Reply(f"📈 Noté « {grade} ».{follow}")

    # --- Jours légers --------------------------------------------------------

    def _light_date_buttons(self, level: str) -> list[ButtonSpec]:
        return [ButtonSpec("Ce soir", "light_pick", f"tonight-{level}"),
                ButtonSpec("Demain", "light_pick", f"tomorrow-{level}"),
                ButtonSpec("Choisir une date", "light_choose", level)]

    def _describe(self, dates: list[date], weekly: int | None) -> str:
        if weekly is not None:
            return f"tous les {msg.WEEKDAY_NAMES[weekly]}s"
        return ", ".join(msg.day_name(day) for day in dates)

    async def _ask_light(self, spec: nlp.DateSpec | None, level: str, now: datetime) -> Reply:
        """Résume ce qui a été compris et demande toujours confirmation."""
        if spec is None or (not spec.dates and spec.weekly is None):
            return Reply("Pour quel jour ?", self._light_date_buttons(level))
        self.store.set_value("pending_light", {
            "dates": [day.isoformat() for day in spec.dates], "weekly": spec.weekly, "level": level})
        text = (f"Compris : **{msg.LEVEL_NAMES[level]}**, {self._describe(list(spec.dates), spec.weekly)}. "
                f"{msg.LEVEL_EFFECTS[level]}")
        if level in (LEGER, OFF):
            conflicts = await self._light_conflicts(list(spec.dates), now)
            if conflicts:
                text += "\n" + "\n".join(conflicts)
        return Reply(text + "\nC'est bien ça ?", [ButtonSpec("Oui", "light_yes", style="success"),
                                                  ButtonSpec("Non", "light_no", style="danger")])

    async def _ask_cancel_light(self, spec: nlp.DateSpec | None, now: datetime) -> Reply:
        today = now.date()
        if spec and (spec.dates or spec.weekly is not None):
            return await self._ask_light(spec, NORMAL, now)
        if self.store.level_on(today) != NORMAL:
            target = today
        else:
            upcoming = self.store.next_light_days(today, 1)
            if not upcoming:
                return Reply("Aucun jour léger n'est prévu.")
            target = upcoming[0]
        return await self._ask_light(nlp.DateSpec(dates=(target,)), NORMAL, now)

    async def _light_conflicts(self, days: list[date], now: datetime) -> list[str]:
        """Échéances J3D menacées par ces jours légers, avec un créneau proposé."""
        if not days:
            return []
        view = await self.read_gitlab()
        events = self.all_events()
        deadlines = [(j3d.issue_deadline(issue), issue["title"]) for issue in view.mine]
        deadlines += [(j3d.milestone_date(milestone), milestone["title"]) for milestone in view.milestones]
        lines = []
        for due, name in deadlines:
            if due and any(0 <= (due - day).days <= 1 for day in days):
                proposal = self._propose_slot(min(days) - timedelta(days=1), events, now)
                lines.append(f"⚠️ J3D : « {name} » est dû le {due.strftime('%d/%m')}. "
                             f"Je propose d'avancer le travail : {proposal}.")
        return lines

    def _propose_slot(self, day: date, events: list[CalEvent], now: datetime) -> str:
        """Premier créneau libre d'au moins 1 h, en remontant jusqu'à aujourd'hui."""
        candidate = day
        while candidate >= now.date():
            busy = [agenda.day_window(event, candidate) for event in agenda.events_on(events, candidate)
                    if agenda.is_blocking(event, self.config)]
            begin = datetime.combine(candidate, self.config.day_start, tzinfo=PARIS)
            end = datetime.combine(candidate, self.config.day_end, tzinfo=PARIS)
            if candidate == now.date():
                begin = max(begin, now)
            for start, stop in subtract([(begin, end)], busy):
                if stop - start >= timedelta(hours=1):
                    return f"{msg.short_day(candidate)} à {msg.hour(start)}"
            candidate -= timedelta(days=1)
        return "aucun créneau libre d'ici là, parles-en à l'équipe"

    def apply_light(self, now: datetime) -> tuple[Reply, bool]:
        """Applique le jour léger confirmé. Renvoie (réponse, faut-il replanifier aujourd'hui)."""
        pending = self.store.get_value("pending_light")
        if not pending:
            return Reply("Plus rien à confirmer."), False
        self.store.delete_value("pending_light")
        level = pending["level"]
        days = [date.fromisoformat(text) for text in pending["dates"]]
        if pending["weekly"] is not None:
            if level == NORMAL:
                self.store.remove_weekly_light(pending["weekly"])
            else:
                self.store.set_weekly_light(pending["weekly"], level)
        for day in days:
            self.store.set_light_day(day, level)
        text = (f"👍 C'est noté : {msg.LEVEL_NAMES[level]}, "
                f"{self._describe(days, pending['weekly'])}. Ces jours ne comptent pas comme du retard.")
        return Reply(text), now.date() in days or pending["weekly"] == now.weekday()

    async def replan_today(self, now: datetime) -> Reply:
        """Recalcule ce qui reste de la journée après un changement."""
        view = await self.read_gitlab()
        plan, _, _, _ = self.plan_day(now.date(), view, self.all_events(), now=now)
        self.store.replace_planned(now.date(), plan.blocks)
        lines = ["🔄 **Planning mis à jour**"]
        lines += [f"- {msg.block_line(block)}" for block in plan.blocks] or ["- Plus rien de prévu."]
        lines += [f"⚠️ {warning}" for warning in plan.warnings]
        return Reply("\n".join(lines))

    # --- Boutons -------------------------------------------------------------

    async def handle_action(self, action: str, arg: str, now: datetime) -> list[Reply]:
        """Réponses à un clic sur un bouton."""
        today = now.date()
        if action == "list_done":
            return [self.list_items(today, done=True)]
        if action == "list_postpone":
            return [self.list_items(today, done=False)]
        if action == "week":
            return [await self.build_week(now)]
        if action == "mails":
            return [self.list_mails(now)]
        if action == "mail_done":
            return [self.close_mail(int(arg), add_to_plan=False)]
        if action == "mail_add":
            return [self.close_mail(int(arg), add_to_plan=True)]
        if action == "done":
            return [self.mark_done(int(arg), now)]
        if action == "postpone":
            return [self.mark_postponed(int(arg), now)]
        if action == "grade":
            item_id, grade = arg.split("-", 1)
            return [self.grade(int(item_id), grade, now)]
        if action == "progress":
            step = self.store.get_value("pending_step")
            self.store.delete_value("pending_step")
            if not step:
                return [Reply("Plus rien à noter.")]
            self.store.set_next_step(arg, step, today)
            return [Reply(f"📌 {msg.project_name(arg)} : prochaine étape « {step} ».")]
        if action == "light_yes":
            reply, replan = self.apply_light(now)
            return [reply, await self.replan_today(now)] if replan else [reply]
        if action == "light_no":
            self.store.delete_value("pending_light")
            return [Reply("D'accord, je ne change rien.")]
        if action == "light_pick":
            choice, level = arg.split("-", 1)
            if choice == "tonight":
                spec = nlp.DateSpec(dates=(today,), evening=True)
                level = SOIREE if level == LEGER else level
            else:
                spec = nlp.DateSpec(dates=(today + timedelta(days=1),))
            return [await self._ask_light(spec, level, now)]
        if action == "light_choose":
            self.store.set_value("pending_level", arg)
            return [Reply("", open_date_modal=True)]
        return [Reply("Ce bouton n'est plus valable.")]

    async def handle_date_text(self, text: str, now: datetime) -> Reply:
        """Réponse au formulaire « Choisir une date »."""
        level = self.store.get_value("pending_level", LEGER)
        spec = nlp.parse_dates(text, now.date())
        if spec is None:
            return Reply("Je n'ai pas reconnu cette date. Exemples : « samedi », « 12/01 », « du 6 au 8 ».",
                         self._light_date_buttons(level))
        return await self._ask_light(spec, level, now)
