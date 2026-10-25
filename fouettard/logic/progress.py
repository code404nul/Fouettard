"""Calcul de l'avancement du projet à partir des issues GitLab.

Avancement = issues fermées / total des issues.
Si les poids sont disponibles, chaque issue compte pour son poids
au lieu de compter pour 1.
"""

from collections.abc import Callable
from dataclasses import dataclass

# Poids utilisé pour une issue qui n'a pas de poids renseigné
DEFAULT_WEIGHT = 1

# Les vrais epics GitLab n'existent que dans un groupe. Dans notre projet
# personnel, les labels "domaine::..." jouent ce rôle : "domaine::son" -> "son".
DOMAIN_LABEL_PREFIX = "domaine::"


@dataclass
class Progress:
    """Avancement d'un ensemble d'issues."""

    done: int = 0   # issues fermées (ou somme de leurs poids)
    total: int = 0  # toutes les issues (ou somme de leurs poids)

    @property
    def percent(self) -> float:
        if self.total == 0:
            return 0.0
        return 100 * self.done / self.total


def should_use_weights(issues: list[dict], weights_available: bool) -> bool:
    """On pondère seulement si GitLab gère les poids ET qu'au moins une issue en a un."""
    return weights_available and any(issue.get("weight") is not None for issue in issues)


def issue_value(issue: dict, use_weights: bool) -> int:
    """Ce que "vaut" une issue dans le calcul."""
    if not use_weights:
        return 1
    weight = issue.get("weight")
    return DEFAULT_WEIGHT if weight is None else weight


def compute_progress(issues: list[dict], use_weights: bool = False) -> Progress:
    """Avancement d'une liste d'issues."""
    progress = Progress()
    for issue in issues:
        value = issue_value(issue, use_weights)
        progress.total += value
        if issue["state"] == "closed":
            progress.done += value
    return progress


def compute_progress_by(issues: list[dict], get_group: Callable[[dict], str],
                        use_weights: bool = False) -> dict[str, Progress]:
    """Avancement par groupe (milestone, itération, epic...).

    get_group : fonction qui donne le nom du groupe d'une issue.
    Les groupes sont triés par nom, ceux commençant par "Sans" à la fin.
    """
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        groups.setdefault(get_group(issue), []).append(issue)

    ordered_names = sorted(groups, key=lambda name: (name.startswith("Sans "), name.lower()))
    return {name: compute_progress(groups[name], use_weights) for name in ordered_names}


# --- Fonctions qui donnent le groupe d'une issue ---------------------------

def milestone_name(issue: dict) -> str:
    milestone = issue.get("milestone") or {}
    return milestone.get("title") or "Sans milestone"


def iteration_name(issue: dict) -> str:
    iteration = issue.get("iteration") or {}
    if not iteration:
        return "Sans itération"
    # Les itérations automatiques n'ont pas toujours de titre : on affiche leurs dates
    return iteration.get("title") or f"{iteration.get('start_date')} → {iteration.get('due_date')}"


def epic_name(issue: dict) -> str:
    epic = issue.get("epic") or {}
    return epic.get("title") or "Sans epic"


def domain_name(issue: dict) -> str:
    """Domaine d'une issue d'après son label "domaine::...", ou "Sans domaine"."""
    for label in issue.get("labels") or []:
        if label.startswith(DOMAIN_LABEL_PREFIX):
            return label[len(DOMAIN_LABEL_PREFIX):]
    return "Sans domaine"


def uses_domain_labels(issues: list[dict]) -> bool:
    """Vrai si au moins une issue a un label "domaine::..."."""
    return any(domain_name(issue) != "Sans domaine" for issue in issues)


# --- Affichage -------------------------------------------------------------

def progress_bar(percent: float, width: int = 20) -> str:
    """Barre de progression en texte, par exemple : ██████░░░░░░░░░░░░░░"""
    filled = round(percent / 100 * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)
