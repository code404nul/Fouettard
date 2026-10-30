"""Mémoire du module planning, dans une base SQLite séparée (data/planning.db)."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from fouettard.planning.agenda import Change
from fouettard.planning.ics import CalEvent
from fouettard.planning.scheduler import NORMAL, Block
from fouettard.planning.spaced import Review

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    source TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL,
    start TEXT NOT NULL, end TEXT NOT NULL, location TEXT NOT NULL,
    tentative INTEGER NOT NULL, all_day INTEGER NOT NULL,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS calendar_status (
    source TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    event_start TEXT NOT NULL, detail TEXT NOT NULL, is_exam INTEGER NOT NULL,
    briefed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, subject TEXT NOT NULL, course_date TEXT NOT NULL,
    due_on TEXT NOT NULL, interval_days INTEGER NOT NULL, step INTEGER NOT NULL,
    ease REAL NOT NULL, last_grade TEXT, last_done_on TEXT, active INTEGER NOT NULL,
    UNIQUE (subject, course_date)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_on TEXT, scheduled_on TEXT,
    minutes INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    kind TEXT NOT NULL, label TEXT NOT NULL, ref TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned'
);
CREATE TABLE IF NOT EXISTS light_days (
    id INTEGER PRIMARY KEY, day TEXT, weekday INTEGER, level TEXT NOT NULL,
    UNIQUE (day, weekday)
);
CREATE TABLE IF NOT EXISTS custom_events (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_state (
    project TEXT PRIMARY KEY, next_step TEXT, last_touched TEXT
);
CREATE TABLE IF NOT EXISTS project_log (
    id INTEGER PRIMARY KEY, project TEXT NOT NULL, day TEXT NOT NULL, minutes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mails (
    id INTEGER PRIMARY KEY, uid_key TEXT NOT NULL UNIQUE, sender TEXT NOT NULL,
    subject TEXT NOT NULL, received TEXT NOT NULL, category TEXT NOT NULL,
    due_on TEXT, state TEXT NOT NULL DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sent_keys (key TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
"""

PLANNED = "planned"
DONE = "done"
POSTPONED = "postponed"


@dataclass(frozen=True)
class StoredMail:
    id: int
    sender: str
    subject: str
    received: datetime
    category: str
    due_on: date | None
    state: str


@dataclass(frozen=True)
class PlanItem:
    id: int
    day: date
    block: Block
    state: str


class PlanningStore:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- Calendriers -------------------------------------------------------

    def load_events(self, source: str) -> list[CalEvent]:
        rows = self._conn.execute(
            "SELECT * FROM calendar_events WHERE source = ? ORDER BY start", (source,)).fetchall()
        return [CalEvent(row["source"], row["key"], row["title"],
                         datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"]),
                         row["location"], bool(row["tentative"]), bool(row["all_day"]))
                for row in rows]

    def save_events(self, source: str, events: list[CalEvent]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM calendar_events WHERE source = ?", (source,))
            self._conn.executemany(
                "INSERT INTO calendar_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(source, event.key, event.title, event.start.isoformat(), event.end.isoformat(),
                  event.location, int(event.tentative), int(event.all_day)) for event in events])

    def mark_source_ok(self, source: str, now: datetime) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO calendar_status (source, last_ok, last_error) VALUES (?, ?, NULL) "
                "ON CONFLICT(source) DO UPDATE SET last_ok = excluded.last_ok, last_error = NULL",
                (source, now.isoformat()))

    def mark_source_error(self, source: str, now: datetime) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO calendar_status (source, last_error) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_error = excluded.last_error",
                (source, now.isoformat()))

    def source_status(self, source: str) -> tuple[datetime | None, datetime | None]:
        """(dernière lecture réussie, dernière erreur non résolue)."""
        row = self._conn.execute(
            "SELECT * FROM calendar_status WHERE source = ?", (source,)).fetchone()
        if row is None:
            return None, None
        return (datetime.fromisoformat(row["last_ok"]) if row["last_ok"] else None,
                datetime.fromisoformat(row["last_error"]) if row["last_error"] else None)

    def add_changes(self, changes: list[Change]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO changes (source, kind, title, event_start, detail, is_exam) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(change.source, change.kind, change.title, change.when.isoformat(),
                  change.detail, int(change.is_exam)) for change in changes])

    def pop_unbriefed_changes(self) -> list[Change]:
        """Changements pas encore annoncés dans un brief (et les marque comme annoncés)."""
        rows = self._conn.execute("SELECT * FROM changes WHERE briefed = 0 ORDER BY event_start").fetchall()
        with self._conn:
            self._conn.execute("UPDATE changes SET briefed = 1 WHERE briefed = 0")
        return [Change(row["kind"], row["source"], row["title"],
                       datetime.fromisoformat(row["event_start"]), row["detail"],
                       bool(row["is_exam"])) for row in rows]

    # --- Révisions ---------------------------------------------------------

    def add_review(self, subject: str, course_date: date) -> None:
        """Crée la révision d'un cours (sans effet si elle existe déjà)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO reviews (subject, course_date, due_on, interval_days, step, ease, active) "
                "VALUES (?, ?, ?, 1, 0, 2.5, 1)",
                (subject, course_date.isoformat(), (course_date + timedelta(days=1)).isoformat()))

    @staticmethod
    def _review(row) -> Review:
        return Review(row["id"], row["subject"], date.fromisoformat(row["course_date"]),
                      date.fromisoformat(row["due_on"]), row["interval_days"], row["step"],
                      row["ease"], row["last_grade"],
                      date.fromisoformat(row["last_done_on"]) if row["last_done_on"] else None,
                      bool(row["active"]))

    def active_reviews(self) -> list[Review]:
        return [self._review(row) for row in
                self._conn.execute("SELECT * FROM reviews WHERE active = 1").fetchall()]

    def get_review(self, review_id: int) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return self._review(row) if row else None

    def update_review(self, review: Review) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reviews SET due_on = ?, interval_days = ?, step = ?, ease = ?, "
                "last_grade = ?, last_done_on = ?, active = ? WHERE id = ?",
                (review.due_on.isoformat(), review.interval_days, review.step, review.ease,
                 review.last_grade,
                 review.last_done_on.isoformat() if review.last_done_on else None,
                 int(review.active), review.id))

    # --- Tâches et devoirs -------------------------------------------------

    def add_task(self, title: str, due_on: date | None, minutes: int = 60) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO tasks (title, due_on, minutes) VALUES (?, ?, ?)",
                (title, due_on.isoformat() if due_on else None, minutes))
        return cursor.lastrowid

    def tasks_for(self, day: date) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE done = 0 AND (scheduled_on IS NULL OR scheduled_on <= ?)",
            (day.isoformat(),)).fetchall()
        return [dict(row) for row in rows]

    def finish_task(self, task_id: int) -> None:
        with self._conn:
            self._conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))

    def reschedule_task(self, task_id: int, day: date) -> None:
        with self._conn:
            self._conn.execute("UPDATE tasks SET scheduled_on = ? WHERE id = ?",
                               (day.isoformat(), task_id))

    # --- Planning du jour --------------------------------------------------

    def replace_planned(self, day: date, blocks: list[Block]) -> None:
        """Remplace le plan du jour. Ce qui est déjà fait ou reporté est conservé."""
        with self._conn:
            self._conn.execute("DELETE FROM plan_items WHERE day = ? AND state = ?",
                               (day.isoformat(), PLANNED))
            self._conn.executemany(
                "INSERT INTO plan_items (day, start, end, kind, label, ref) VALUES (?, ?, ?, ?, ?, ?)",
                [(day.isoformat(), block.start.isoformat(), block.end.isoformat(),
                  block.kind, block.label, block.ref) for block in blocks])

    def plan_items(self, day: date, state: str | None = None) -> list[PlanItem]:
        query = "SELECT * FROM plan_items WHERE day = ?"
        params = [day.isoformat()]
        if state:
            query += " AND state = ?"
            params.append(state)
        rows = self._conn.execute(query + " ORDER BY start", params).fetchall()
        return [self._plan_item(row) for row in rows]

    @staticmethod
    def _plan_item(row) -> PlanItem:
        return PlanItem(row["id"], date.fromisoformat(row["day"]),
                        Block(datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"]),
                              row["kind"], row["label"], row["ref"]), row["state"])

    def get_plan_item(self, item_id: int) -> PlanItem | None:
        row = self._conn.execute("SELECT * FROM plan_items WHERE id = ?", (item_id,)).fetchone()
        return self._plan_item(row) if row else None

    def set_item_state(self, item_id: int, state: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE plan_items SET state = ? WHERE id = ?", (state, item_id))

    # --- Jours légers ------------------------------------------------------

    def set_light_day(self, day: date, level: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM light_days WHERE day = ?", (day.isoformat(),))
            self._conn.execute("INSERT INTO light_days (day, level) VALUES (?, ?)",
                               (day.isoformat(), level))

    def set_weekly_light(self, weekday: int, level: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM light_days WHERE weekday = ?", (weekday,))
            self._conn.execute("INSERT INTO light_days (weekday, level) VALUES (?, ?)",
                               (weekday, level))

    def remove_weekly_light(self, weekday: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM light_days WHERE weekday = ?", (weekday,))

    def level_on(self, day: date) -> str:
        """Niveau d'un jour. Une date précise l'emporte sur une habitude récurrente."""
        row = self._conn.execute("SELECT level FROM light_days WHERE day = ?",
                                 (day.isoformat(),)).fetchone()
        if row:
            return row["level"]
        row = self._conn.execute("SELECT level FROM light_days WHERE weekday = ?",
                                 (day.weekday(),)).fetchone()
        return row["level"] if row else NORMAL

    def weekly_light(self, weekday: int) -> str | None:
        row = self._conn.execute("SELECT level FROM light_days WHERE weekday = ?",
                                 (weekday,)).fetchone()
        return row["level"] if row else None

    def next_light_days(self, today: date, limit: int = 5) -> list[date]:
        """Prochaines dates précises marquées comme légères."""
        rows = self._conn.execute(
            "SELECT day FROM light_days WHERE day >= ? AND level != ? ORDER BY day LIMIT ?",
            (today.isoformat(), NORMAL, limit)).fetchall()
        return [date.fromisoformat(row["day"]) for row in rows]

    # --- Rendez-vous ajoutés à la main --------------------------------------

    def add_custom_event(self, title: str, start: datetime, end: datetime) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO custom_events (title, start, end) VALUES (?, ?, ?)",
                               (title, start.isoformat(), end.isoformat()))

    def custom_events_on(self, day: date) -> list[tuple[str, datetime, datetime]]:
        rows = self._conn.execute(
            "SELECT * FROM custom_events WHERE start LIKE ? ORDER BY start",
            (f"{day.isoformat()}%",)).fetchall()
        return [(row["title"], datetime.fromisoformat(row["start"]),
                 datetime.fromisoformat(row["end"])) for row in rows]

    # --- Projets -----------------------------------------------------------

    def set_next_step(self, project: str, step: str, today: date) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO project_state (project, next_step, last_touched) VALUES (?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET next_step = excluded.next_step, "
                "last_touched = excluded.last_touched",
                (project, step, today.isoformat()))

    def next_step(self, project: str) -> str | None:
        row = self._conn.execute("SELECT next_step FROM project_state WHERE project = ?",
                                 (project,)).fetchone()
        return row["next_step"] if row else None

    def log_time(self, project: str, day: date, minutes: int) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO project_log (project, day, minutes) VALUES (?, ?, ?)",
                               (project, day.isoformat(), minutes))

    def minutes_between(self, project: str, first: date, last: date) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(minutes), 0) AS total FROM project_log "
            "WHERE project = ? AND day BETWEEN ? AND ?",
            (project, first.isoformat(), last.isoformat())).fetchone()
        return row["total"]

    # --- Mails (jamais le corps du message) ----------------------------------

    def known_mail_keys(self) -> set[str]:
        return {row["uid_key"] for row in self._conn.execute("SELECT uid_key FROM mails").fetchall()}

    def add_mail(self, uid_key: str, sender: str, subject: str, received: datetime, category: str,
                 due_on: date | None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO mails (uid_key, sender, subject, received, category, due_on) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid_key, sender, subject, received.isoformat(), category,
                 due_on.isoformat() if due_on else None))

    @staticmethod
    def _mail(row) -> StoredMail:
        return StoredMail(row["id"], row["sender"], row["subject"],
                          datetime.fromisoformat(row["received"]), row["category"],
                          date.fromisoformat(row["due_on"]) if row["due_on"] else None, row["state"])

    def mails_to_handle(self, category: str, since: datetime) -> list[StoredMail]:
        rows = self._conn.execute(
            "SELECT * FROM mails WHERE category = ? AND state = 'new' AND received >= ? "
            "ORDER BY received DESC", (category, since.isoformat())).fetchall()
        return [self._mail(row) for row in rows]

    def get_mail(self, mail_id: int) -> StoredMail | None:
        row = self._conn.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone()
        return self._mail(row) if row else None

    def close_mail(self, mail_id: int) -> None:
        with self._conn:
            self._conn.execute("UPDATE mails SET state = 'done' WHERE id = ?", (mail_id,))

    def forget_old_mails(self, before: datetime) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM mails WHERE received < ?", (before.isoformat(),))

    # --- Réglages et anti-doublons -----------------------------------------

    def get_value(self, key: str, default=None):
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_value(self, key: str, value) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                               (key, json.dumps(value)))

    def delete_value(self, key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    def was_sent(self, key: str) -> bool:
        return self._conn.execute("SELECT 1 FROM sent_keys WHERE key = ?", (key,)).fetchone() is not None

    def mark_sent(self, key: str, now: datetime) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO sent_keys (key, sent_at) VALUES (?, ?)",
                               (key, now.isoformat()))
