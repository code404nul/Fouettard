"""Chargement de la configuration du bot.

Trois sources :
- le fichier .env : secrets et identifiants ;
- config/settings.yaml : réglages (seuils, horaires des alertes...) ;
- config/deadlines.yaml : les dates clés de la SAE.
"""

import os
from dataclasses import dataclass, field, fields
from datetime import date, time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from fouettard.logic.key_dates import KeyDate
from fouettard.timeutils import PARIS

# Dossier racine du projet (celui qui contient .env, config/, data/...)
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"

REQUIRED_ENV_VARS = [
    "DISCORD_TOKEN",
    "DISCORD_CHANNEL_ID",
    "GITLAB_URL",
    "GITLAB_TOKEN",
    "GITLAB_PROJECT_ID",
]


class ConfigError(Exception):
    """Erreur de configuration (variable manquante ou invalide)."""


@dataclass
class Settings:
    """Réglages de config/settings.yaml. Les valeurs ici sont celles par défaut."""

    check_times: list[time] = field(default_factory=lambda: [time(9, 0, tzinfo=PARIS),
                                                             time(18, 0, tzinfo=PARIS)])
    inactivity_days: int = 5
    overdue_repeat_days: int = 2
    mr_review_days: int = 2
    key_date_reminders: list[int] = field(default_factory=lambda: [14, 7, 2])
    weekly_recap_time: time = time(20, 0, tzinfo=PARIS)
    checklist_min_size: int = 200


@dataclass
class Config:
    """Toutes les valeurs de configuration dont le bot a besoin."""

    discord_token: str
    discord_channel_id: int
    gitlab_url: str
    gitlab_token: str
    gitlab_project_id: str
    database_path: Path
    settings: Settings
    key_dates: list[KeyDate]


def read_yaml(path: Path) -> dict:
    """Lit un fichier YAML et renvoie son contenu (dictionnaire)."""
    try:
        with open(path, encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        raise ConfigError(f"Fichier introuvable : {path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"Fichier {path.name} mal écrit : {exc}")


def parse_time(text: str) -> time:
    """Convertit "18:30" en heure de Paris."""
    try:
        hours, minutes = str(text).split(":")
        return time(int(hours), int(minutes), tzinfo=PARIS)
    except ValueError:
        raise ConfigError(f"Heure invalide : {text!r} (format attendu HH:MM, ex : \"09:00\")")


def load_settings(path: Path) -> Settings:
    """Lit config/settings.yaml. Une valeur absente garde sa valeur par défaut."""
    content = read_yaml(path)
    settings = Settings()

    # Une clé inconnue est sûrement une faute de frappe : on le signale
    known = {setting.name for setting in fields(Settings)}
    unknown = set(content) - known
    if unknown:
        raise ConfigError(f"Réglages inconnus dans {path.name} : {', '.join(sorted(unknown))}")

    if "check_times" in content:
        settings.check_times = [parse_time(text) for text in content["check_times"]]
    if "weekly_recap_time" in content:
        settings.weekly_recap_time = parse_time(content["weekly_recap_time"])
    for name in ["inactivity_days", "overdue_repeat_days", "mr_review_days", "checklist_min_size"]:
        if name in content:
            if not isinstance(content[name], int) or content[name] < 1:
                raise ConfigError(f"{name} doit être un nombre entier positif.")
            setattr(settings, name, content[name])
    if "key_date_reminders" in content:
        settings.key_date_reminders = [int(days) for days in content["key_date_reminders"]]
    return settings


def load_key_dates(path: Path) -> list[KeyDate]:
    """Lit les dates clés de la SAE dans un fichier YAML."""
    content = read_yaml(path)
    key_dates = []
    for entry in content.get("deadlines") or []:
        # YAML transforme directement "2026-12-07" en objet date
        if not isinstance(entry.get("week_start"), date) or not entry.get("name"):
            raise ConfigError(f"Entrée invalide dans {path.name} : {entry} "
                              "(il faut un 'name' et un 'week_start' au format AAAA-MM-JJ)")
        key_dates.append(KeyDate(name=entry["name"], week_start=entry["week_start"]))
    return key_dates


def load_config() -> Config:
    """Lit le .env et les fichiers YAML, et renvoie un objet Config.

    Lève ConfigError avec un message clair si quelque chose manque.
    """
    load_dotenv(ROOT_DIR / ".env")

    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise ConfigError(
            "Variables manquantes dans le fichier .env : " + ", ".join(missing)
            + " (voir .env.example)"
        )

    channel_id = os.environ["DISCORD_CHANNEL_ID"].strip()
    if not channel_id.isdigit():
        raise ConfigError("DISCORD_CHANNEL_ID doit être un nombre (identifiant du salon).")

    return Config(
        discord_token=os.environ["DISCORD_TOKEN"].strip(),
        discord_channel_id=int(channel_id),
        gitlab_url=os.environ["GITLAB_URL"].strip(),
        gitlab_token=os.environ["GITLAB_TOKEN"].strip(),
        gitlab_project_id=os.environ["GITLAB_PROJECT_ID"].strip(),
        database_path=DATA_DIR / "fouettard.db",
        settings=load_settings(CONFIG_DIR / "settings.yaml"),
        key_dates=load_key_dates(CONFIG_DIR / "deadlines.yaml"),
    )
