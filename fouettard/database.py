"""Stockage persistant du bot dans une base SQLite.

Deux tables :
- links       : quel compte Discord correspond à quel compte GitLab ;
- sent_alerts : quelles alertes ont déjà été envoyées et quand
                (pour ne pas spammer le salon avec la même alerte).
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    discord_id      INTEGER PRIMARY KEY,
    gitlab_id       INTEGER NOT NULL UNIQUE,
    gitlab_username TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    alert_key TEXT PRIMARY KEY,   -- ex : "overdue:issue:42"
    sent_at   TEXT NOT NULL       -- date et heure ISO de l'envoi
);
"""


@dataclass
class Link:
    """Liaison entre un compte Discord et un compte GitLab."""

    discord_id: int
    gitlab_id: int
    gitlab_username: str


class Database:
    """Petite surcouche autour de sqlite3."""

    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row  # accès aux colonnes par leur nom
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- Liaisons Discord <-> GitLab ---------------------------------------

    def save_link(self, discord_id: int, gitlab_id: int, gitlab_username: str) -> None:
        """Enregistre (ou remplace) la liaison d'un compte Discord."""
        with self._conn:  # "with" valide la transaction automatiquement
            self._conn.execute(
                """
                INSERT INTO links (discord_id, gitlab_id, gitlab_username)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE
                    SET gitlab_id = excluded.gitlab_id,
                        gitlab_username = excluded.gitlab_username
                """,
                (discord_id, gitlab_id, gitlab_username),
            )

    def get_link_by_discord(self, discord_id: int) -> Link | None:
        row = self._conn.execute(
            "SELECT * FROM links WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return Link(**row) if row else None

    def get_link_by_gitlab_id(self, gitlab_id: int) -> Link | None:
        row = self._conn.execute(
            "SELECT * FROM links WHERE gitlab_id = ?", (gitlab_id,)
        ).fetchone()
        return Link(**row) if row else None

    def get_all_links(self) -> list[Link]:
        rows = self._conn.execute("SELECT * FROM links").fetchall()
        return [Link(**row) for row in rows]

    # --- Anti-spam des alertes ---------------------------------------------

    def was_alert_sent_recently(self, key: str, now: datetime,
                                repeat_after: timedelta | None) -> bool:
        """Vrai si l'alerte "key" a déjà été envoyée et ne doit pas être renvoyée.

        repeat_after : délai au bout duquel on peut renvoyer la même alerte.
                       None = ne jamais la renvoyer.
        """
        row = self._conn.execute(
            "SELECT sent_at FROM sent_alerts WHERE alert_key = ?", (key,)
        ).fetchone()
        if row is None:
            return False  # jamais envoyée
        if repeat_after is None:
            return True  # alerte unique, déjà envoyée
        sent_at = datetime.fromisoformat(row["sent_at"])
        return now - sent_at < repeat_after

    def mark_alert_sent(self, key: str, now: datetime) -> None:
        """Note que l'alerte "key" vient d'être envoyée."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sent_alerts (alert_key, sent_at) VALUES (?, ?)",
                (key, now.isoformat()),
            )
