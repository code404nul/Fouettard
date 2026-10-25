"""Dates clés de la SAE (remise du CdST, soutenances) et compte à rebours."""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class KeyDate:
    """Une date clé : son nom et le lundi de la semaine concernée."""

    name: str
    week_start: date

    @property
    def week_end(self) -> date:
        """Dimanche de la semaine concernée."""
        return self.week_start + timedelta(days=6)


def days_until(key_date: KeyDate, today: date) -> int:
    """Nombre de jours avant le début de la semaine (négatif si commencée)."""
    return (key_date.week_start - today).days


def is_this_week(key_date: KeyDate, today: date) -> bool:
    """Vrai si on est pendant la semaine de la date clé."""
    return key_date.week_start <= today <= key_date.week_end


def is_past(key_date: KeyDate, today: date) -> bool:
    """Vrai si la semaine de la date clé est terminée."""
    return today > key_date.week_end


def sort_key_dates(key_dates: list[KeyDate]) -> list[KeyDate]:
    """Dates clés triées de la plus proche à la plus lointaine."""
    return sorted(key_dates, key=lambda key_date: key_date.week_start)
