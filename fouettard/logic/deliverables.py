"""Règles de /checklist : quels livrables doivent être présents dans le dépôt du jeu."""

from dataclasses import dataclass


@dataclass
class Deliverable:
    """Un livrable attendu. paths : fichiers acceptés (un seul suffit)."""

    name: str
    paths: list[str]


REQUIRED_DELIVERABLES = [
    Deliverable("README", ["README.md"]),
    Deliverable("Guide d'installation", ["docs/install.md"]),
    Deliverable("Manuel utilisateur", ["docs/user_manual.md"]),
    Deliverable("Architecture", ["docs/architecture.md"]),
    Deliverable("Journal IA", ["docs/ai_journal.md", "docs/ai_journal.xlsx"]),
]

# Statuts possibles d'un livrable
OK = "ok"
ALMOST_EMPTY = "almost_empty"
MISSING = "missing"


@dataclass
class DeliverableStatus:
    deliverable: Deliverable
    status: str             # OK, ALMOST_EMPTY ou MISSING
    path: str | None = None  # fichier trouvé
    size: int | None = None  # taille en octets


def all_paths() -> list[str]:
    """Tous les fichiers à vérifier dans le dépôt."""
    return [path for deliverable in REQUIRED_DELIVERABLES for path in deliverable.paths]


def check_deliverables(sizes: dict[str, int | None], min_size: int) -> list[DeliverableStatus]:
    """Statut de chaque livrable.

    sizes    : taille de chaque fichier (None = fichier absent)
    min_size : en dessous, un fichier texte est "quasi vide"

    Un .xlsx vide pèse déjà plusieurs Ko : on vérifie seulement sa présence.
    """
    results = []
    for deliverable in REQUIRED_DELIVERABLES:
        found = [path for path in deliverable.paths if sizes.get(path) is not None]
        if not found:
            results.append(DeliverableStatus(deliverable, MISSING))
            continue
        # S'il y a plusieurs fichiers possibles, on garde le plus gros
        path = max(found, key=lambda p: sizes[p])
        size = sizes[path]
        too_small = not path.endswith(".xlsx") and size < min_size
        results.append(DeliverableStatus(deliverable, ALMOST_EMPTY if too_small else OK, path, size))
    return results
