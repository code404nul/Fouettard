"""Logique autour des issues : échéances, tri, priorités et suggestions."""

from dataclasses import dataclass, field
from datetime import date

# Labels qui rendent une issue indisponible pour /suggest :
# bloquée, déjà en cours, ou en attente de relecture.
UNAVAILABLE_LABELS = ["bloque", "Doing", "Review"]

PRIORITY_LABEL_PREFIX = "prio::"

# Icônes du "health status" GitLab (option payante)
HEALTH_ICONS = {"on_track": "🟢", "needs_attention": "🟡", "at_risk": "🔴"}


# --- Liens -----------------------------------------------------------------

def markdown_link(title: str, url: str) -> str:
    """Lien cliquable [titre](url) pour Discord."""
    # Les crochets dans le titre casseraient le lien Markdown
    title = title.replace("[", "(").replace("]", ")")
    return f"[{title}]({url})"


def issue_link(issue: dict) -> str:
    """Lien cliquable vers une issue : [#12 Titre](url)."""
    return markdown_link(f"#{issue['iid']} {issue['title']}", issue["web_url"])


# --- Échéances -------------------------------------------------------------

def due_date(issue: dict) -> date | None:
    """Échéance d'une issue, ou None si elle n'en a pas."""
    text = issue.get("due_date")
    return date.fromisoformat(text) if text else None


def is_overdue(issue: dict, today: date) -> bool:
    """Vrai si l'issue est ouverte et que son échéance est passée."""
    due = due_date(issue)
    return issue["state"] == "opened" and due is not None and due < today


def sort_by_due_date(issues: list[dict]) -> list[dict]:
    """Tri par échéance (les plus proches d'abord, sans échéance à la fin),
    puis par ancienneté."""
    def sort_key(issue):
        due = due_date(issue)
        return (due is None, due or date.max, issue["created_at"])
    return sorted(issues, key=sort_key)


# --- Assignés --------------------------------------------------------------

def assignee_ids(issue: dict) -> list[int]:
    """Identifiants GitLab des personnes assignées (0, 1 ou plusieurs)."""
    return [assignee["id"] for assignee in issue.get("assignees") or []]


def is_assigned_to(issue: dict, gitlab_id: int) -> bool:
    return gitlab_id in assignee_ids(issue)


# --- Priorités -------------------------------------------------------------

def build_priority_order(labels: list[dict]) -> list[str]:
    """Liste des labels "prio::..." du plus au moins prioritaire.

    Si des priorités sont réglées dans GitLab (étoile sur le label), on suit
    cet ordre. Sinon, on trie par nom : "prio::P1-..." avant "prio::P2-...".
    """
    priority_labels = [label for label in labels
                       if label["name"].startswith(PRIORITY_LABEL_PREFIX)]
    priority_labels.sort(key=lambda label: (label.get("priority") is None,
                                            label.get("priority") or 0,
                                            label["name"].lower()))
    return [label["name"] for label in priority_labels]


def priority_label(issue: dict, priority_order: list[str]) -> str | None:
    """Le label de priorité le plus fort de l'issue, ou None."""
    labels = [label for label in priority_order if label in (issue.get("labels") or [])]
    return labels[0] if labels else None


def priority_rank(issue: dict, priority_order: list[str]) -> int:
    """Rang de priorité : 0 = la plus haute. Sans priorité : après toutes les autres."""
    label = priority_label(issue, priority_order)
    return priority_order.index(label) if label else len(priority_order)


# --- Suggestions -----------------------------------------------------------

@dataclass
class Suggestions:
    """Résultat de /suggest."""

    issues: list[dict]                                  # les issues proposées
    hidden: dict[str, int] = field(default_factory=dict)  # label -> nb d'issues masquées


def suggest_issues(open_issues: list[dict], priority_order: list[str],
                   included_labels: set[str] = frozenset(), limit: int = 5) -> Suggestions:
    """Issues ouvertes et non assignées, triées par priorité, échéance puis ancienneté.

    Les issues avec un label de UNAVAILABLE_LABELS sont masquées, sauf si
    ce label fait partie de included_labels.
    """
    hidden_labels = [label for label in UNAVAILABLE_LABELS if label not in included_labels]
    candidates = []
    hidden: dict[str, int] = {}

    for issue in open_issues:
        if assignee_ids(issue):
            continue  # déjà prise par quelqu'un
        blocking = [label for label in hidden_labels if label in (issue.get("labels") or [])]
        if blocking:
            hidden[blocking[0]] = hidden.get(blocking[0], 0) + 1
            continue
        candidates.append(issue)

    def sort_key(issue):
        due = due_date(issue)
        return (priority_rank(issue, priority_order), due or date.max, issue["created_at"])

    candidates.sort(key=sort_key)
    return Suggestions(issues=candidates[:limit], hidden=hidden)
