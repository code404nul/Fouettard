"""Tests de la lecture de config/settings.yaml."""

from datetime import time

import pytest

from fouettard.config import CONFIG_DIR, ConfigError, Settings, load_settings
from fouettard.timeutils import PARIS


def test_real_settings_file_matches_defaults():
    # Le fichier livré doit correspondre aux valeurs par défaut
    assert load_settings(CONFIG_DIR / "settings.yaml") == Settings()


def test_settings_values(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text('check_times: ["08:30"]\ninactivity_days: 3\n', encoding="utf-8")
    settings = load_settings(path)
    assert settings.check_times == [time(8, 30, tzinfo=PARIS)]
    assert settings.inactivity_days == 3
    assert settings.mr_review_days == 2  # valeur par défaut conservée


def test_unknown_setting_is_rejected(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("inactivity_day: 3\n", encoding="utf-8")  # faute de frappe
    with pytest.raises(ConfigError):
        load_settings(path)


def test_invalid_time_is_rejected(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text('weekly_recap_time: "20h"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)
