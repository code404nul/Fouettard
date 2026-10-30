"""Configuration du module planning.

- .env : PLANNING_OWNER_ID, ZEUS_ICS_URL, OUTLOOK_ICS_URL, IMAP_* (secrets) ;
- config/planning.yaml : réglages et dates AWAKE.

Sans PLANNING_OWNER_ID, le module est désactivé et le reste du bot tourne normalement.
"""

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

from fouettard.config import CONFIG_DIR, DATA_DIR, ConfigError, parse_time, read_yaml

ZEUS = "zeus"
OUTLOOK = "outlook"
MAIL = "mail"


@dataclass(frozen=True)
class ImapSettings:
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 993
    folder: str = "INBOX"


@dataclass(frozen=True)
class AwakeDeadline:
    day: date
    name: str
    is_report: bool = False  # à envoyer avant 23 h 59, pénalité si retard


@dataclass(frozen=True)
class WeeklySlot:
    """Créneau qui revient chaque semaine (0 = lundi)."""

    weekday: int
    start: time
    end: time


@dataclass
class PlanningConfig:
    owner_id: int
    urls: dict[str, str] = field(repr=False)  # uniquement les sources configurées
    database_path: Path
    brief_time: time = time(7, 0)
    evening_time: time = time(21, 0)
    day_start: time = time(8, 0)
    day_end: time = time(22, 0)
    evening_start: time = time(18, 0)
    meals: list[tuple[time, time]] = field(default_factory=list)
    zeus_refresh_minutes: int = 30
    outlook_refresh_minutes: int = 60
    min_project_block_minutes: int = 60
    max_project_block_minutes: int = 120
    review_minutes: int = 15
    max_reviews_per_day: int = 4
    exam_boost_days: int = 14
    club_weekly_goal_hours: float = 5
    j3d_weekly_floor_hours: float = 3
    j3d_default_issue_hours: float = 2
    j3d_urgent_hours: int = 48
    j3d_stale_days: int = 5
    review_subjects: list[str] = field(default_factory=list)
    subject_aliases: dict[str, str] = field(default_factory=dict)
    ignored_subjects: list[str] = field(default_factory=list)
    probable_homework_subjects: list[str] = field(default_factory=list)
    rattrapages_block: bool = False
    club_preferred_weekdays: list[int] = field(default_factory=lambda: [3])
    fixed_slots: list[WeeklySlot] = field(default_factory=list)
    protected_slots: list[WeeklySlot] = field(default_factory=list)
    awake_deadlines: list[AwakeDeadline] = field(default_factory=list)
    imap: ImapSettings | None = field(default=None, repr=False)  # None : mails désactivés
    mail_refresh_minutes: int = 15
    mail_lookback_days: int = 7
    mail_school_domains: list[str] = field(default_factory=list)
    mail_prof_senders: list[str] = field(default_factory=list)
    mail_noise_domains: list[str] = field(default_factory=list)
    mail_noise_keywords: list[str] = field(default_factory=list)
    mail_action_keywords: list[str] = field(default_factory=list)
    mail_urgent_keywords: list[str] = field(default_factory=list)


def _naive(text: str) -> time:
    return parse_time(text).replace(tzinfo=None)


def _weekly_slots(entries: list[dict] | None) -> list[WeeklySlot]:
    return [WeeklySlot(int(entry["weekday"]), _naive(entry["start"]), _naive(entry["end"]))
            for entry in entries or []]


def _load_imap() -> ImapSettings | None:
    """Réglages IMAP, ou None s'il manque un identifiant : les mails sont alors désactivés."""
    values = [os.getenv(name, "").strip() for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD")]
    if not all(values):
        return None
    port = os.getenv("IMAP_PORT", "993").strip()
    if not port.isdigit():
        raise ConfigError("IMAP_PORT doit être un nombre.")
    return ImapSettings(*values, port=int(port), folder=os.getenv("IMAP_FOLDER", "INBOX").strip())


def load_planning_config(path: Path | None = None) -> PlanningConfig | None:
    """Renvoie la configuration, ou None si le module n'est pas activé.

    Le .env a déjà été chargé par load_config().
    """
    owner = os.getenv("PLANNING_OWNER_ID", "").strip()
    if not owner:
        return None
    if not owner.isdigit():
        raise ConfigError("PLANNING_OWNER_ID doit être un nombre (ton identifiant Discord).")

    env_names = {ZEUS: "ZEUS_ICS_URL", OUTLOOK: "OUTLOOK_ICS_URL"}
    urls = {source: os.environ[name].strip() for source, name in env_names.items()
            if os.getenv(name, "").strip()}

    content = read_yaml(path or CONFIG_DIR / "planning.yaml")
    config = PlanningConfig(owner_id=int(owner), urls=urls,
                            database_path=DATA_DIR / "planning.db", imap=_load_imap())

    for name in ["brief_time", "evening_time", "day_start", "day_end", "evening_start"]:
        if name in content:
            setattr(config, name, _naive(content.pop(name)))
    config.meals = [(_naive(start), _naive(end)) for start, end in content.pop("meals", [])]
    config.fixed_slots = _weekly_slots(content.pop("fixed_slots", None))
    config.protected_slots = _weekly_slots(content.pop("protected_slots", None))
    config.awake_deadlines = [AwakeDeadline(entry["date"], entry["name"],
                                            bool(entry.get("report")))
                              for entry in content.pop("awake_deadlines", None) or []]

    # Le reste correspond directement aux champs de PlanningConfig
    unknown = [name for name in content if not hasattr(config, name)]
    if unknown:
        raise ConfigError(f"Réglages inconnus dans planning.yaml : {', '.join(sorted(unknown))}")
    for name, value in content.items():
        if value is not None:
            setattr(config, name, value)
    return config
