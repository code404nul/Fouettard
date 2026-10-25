"""Mise en forme des messages Discord (textes et embeds)."""

from datetime import date, datetime

import discord

from fouettard.gitlab_api import (Features, GitLabAuthError, GitLabError,
                                  GitLabNotFound, GitLabUnreachable)
from fouettard.logic.alerts import Alert
from fouettard.logic.deliverables import ALMOST_EMPTY, MISSING, OK, DeliverableStatus
from fouettard.logic.key_dates import KeyDate, days_until, is_past, is_this_week
from fouettard.logic.progress import Progress, progress_bar
from fouettard.logic.recap import MemberRecap
from fouettard.logic.tasks import (HEALTH_ICONS, Suggestions, due_date, issue_link,
                                   markdown_link, priority_label)
from fouettard.logic.team import MemberSummary

# Limites imposées par Discord
FIELD_MAX_LENGTH = 1024
DESCRIPTION_MAX_LENGTH = 4096

COLOR = discord.Color.orange()

WEEKDAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


# --- Petits outils ---------------------------------------------------------

def truncate(text: str, max_length: int = FIELD_MAX_LENGTH) -> str:
    """Coupe un texte trop long pour Discord."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 2] + " …"


def format_date(day: date) -> str:
    """Exemple : "lun. 07/12/2026"."""
    return f"{WEEKDAYS[day.weekday()]} {day:%d/%m/%Y}"


def relative_days(days: int) -> str:
    """Nombre de jours (positif = futur) en texte : "demain", "il y a 3 jours"..."""
    if days == 0:
        return "aujourd'hui"
    if days == 1:
        return "demain"
    if days == -1:
        return "hier"
    if days > 0:
        return f"dans {days} jours"
    return f"il y a {-days} jours"


def format_due(issue: dict, today: date) -> str:
    """Échéance d'une issue : "📅 sam. 25/10/2026 (demain)" ou "⚠️ en retard (...)"."""
    due = due_date(issue)
    if due is None:
        return "pas d'échéance"
    days = (due - today).days
    icon = "⚠️ en retard," if days < 0 else "📅"
    return f"{icon} {format_date(due)} ({relative_days(days)})"


def gitlab_error_message(error: GitLabError) -> str:
    """Message affiché à l'utilisateur quand GitLab pose problème."""
    if isinstance(error, GitLabUnreachable):
        return "⚠️ GitLab est injoignable pour le moment. Réessaie dans quelques minutes."
    if isinstance(error, GitLabAuthError):
        return "⚠️ Le token GitLab du bot est refusé. Préviens la personne qui héberge le bot."
    if isinstance(error, GitLabNotFound):
        return "⚠️ Élément introuvable sur GitLab (vérifie GITLAB_PROJECT_ID)."
    return "⚠️ Erreur inattendue avec GitLab. Les détails sont dans les logs du bot."


# --- /progress -------------------------------------------------------------

def format_progress_line(progress: Progress, weighted: bool) -> str:
    """Une ligne du type : `██████░░░░` **60 %** · 6/10 issues"""
    unit = "points de poids" if weighted else "issues"
    return (f"`{progress_bar(progress.percent)}` **{progress.percent:.0f} %**"
            f" · {progress.done}/{progress.total} {unit}")


def build_progress_embed(project_name: str, overall: Progress,
                         sections: dict[str, dict[str, Progress]],
                         weighted: bool, features: Features) -> discord.Embed:
    """Embed de la commande /progress.

    sections : {"Par milestone": {"Sprint 1": Progress, ...}, ...}
    """
    embed = discord.Embed(
        title=f"📊 Avancement de {project_name}",
        description="**Global**\n" + format_progress_line(overall, weighted),
        color=COLOR,
    )
    for section_title, groups in sections.items():
        if not groups:
            continue
        lines = [f"**{name}**\n{format_progress_line(progress, weighted)}"
                 for name, progress in groups.items()]
        embed.add_field(name=section_title, value=truncate("\n".join(lines)), inline=False)

    footer = "Pondéré par le poids des issues" if weighted else "Chaque issue compte pour 1"
    enabled = features.enabled_names()
    footer += " · Options GitLab : " + (", ".join(enabled) if enabled else "aucune détectée")
    embed.set_footer(text=footer)
    return embed


# --- /todo -----------------------------------------------------------------

def build_todo_embed(username: str, issues: list[dict], today: date,
                     show_health: bool) -> discord.Embed:
    """Embed de /todo : les issues ouvertes d'une personne, déjà triées."""
    embed = discord.Embed(title=f"📝 Issues ouvertes de {username}", color=COLOR)
    if not issues:
        embed.description = "Rien à faire pour l'instant 🎉"
        return embed

    lines = []
    for issue in issues:
        health = HEALTH_ICONS.get(issue.get("health_status"), "") if show_health else ""
        lines.append(f"{health} {issue_link(issue)}\n  {format_due(issue, today)}".strip())
    embed.description = truncate("\n".join(lines), DESCRIPTION_MAX_LENGTH)
    embed.set_footer(text=f"{len(issues)} issue(s) · triées par échéance")
    return embed


# --- /suggest --------------------------------------------------------------

def build_suggest_embed(suggestions: Suggestions, priority_order: list[str],
                        today: date) -> discord.Embed:
    """Embed de /suggest : les tâches libres à prendre."""
    embed = discord.Embed(title="💡 Tâches libres à prendre", color=COLOR)
    if suggestions.issues:
        lines = []
        for issue in suggestions.issues:
            priority = priority_label(issue, priority_order)
            details = [priority.split("::", 1)[1] if priority else "sans priorité",
                       format_due(issue, today)]
            lines.append(f"{issue_link(issue)}\n  {' · '.join(details)}")
        embed.description = truncate("\n".join(lines), DESCRIPTION_MAX_LENGTH)
    else:
        embed.description = "Aucune issue libre pour le moment."

    if suggestions.hidden:
        counts = ", ".join(f"{count} {label}" for label, count in suggestions.hidden.items())
        text = f"Masquées : {counts}. Option « inclure » pour les voir."
        if suggestions.hidden.get("Doing") or suggestions.hidden.get("Review"):
            text += "\n⚠️ Des issues en Doing/Review n'ont personne d'assigné : un oubli ?"
        embed.set_footer(text=text)
    return embed


# --- /team -----------------------------------------------------------------

def format_last_activity(last_activity: datetime | None, today: date,
                         lookback_days: int) -> str:
    if last_activity is None:
        return f"aucune depuis {lookback_days} jours"
    return relative_days((last_activity.date() - today).days)


def build_team_embed(summaries: list[MemberSummary], discord_ids: dict[int, int],
                     today: date, lookback_days: int, show_health: bool) -> discord.Embed:
    """Embed de /team. discord_ids : id GitLab -> id Discord (membres liés)."""
    embed = discord.Embed(title="👥 Vue d'ensemble de l'équipe", color=COLOR)
    for summary in summaries[:25]:  # Discord : 25 champs maximum
        lines = [f"Issues ouvertes : **{summary.open_issues}**",
                 f"En retard : **{summary.overdue}**" + (" ⚠️" if summary.overdue else "")]
        if show_health:
            lines.append(f"À risque : **{summary.at_risk}**")
        lines.append("Dernière activité : "
                     + format_last_activity(summary.last_activity, today, lookback_days))
        discord_id = discord_ids.get(summary.gitlab_id)
        lines.append(f"Discord : <@{discord_id}>" if discord_id else "Discord : pas lié (/link)")
        embed.add_field(name=f"{summary.name} (@{summary.username})",
                        value="\n".join(lines), inline=True)
    if not summaries:
        embed.description = "Aucun membre trouvé dans le projet GitLab."
    return embed


# --- /deadlines ------------------------------------------------------------

def format_key_date(key_date: KeyDate, today: date) -> str:
    """Une ligne de /deadlines."""
    week = f"semaine du {format_date(key_date.week_start)}"
    if is_past(key_date, today):
        return f"~~**{key_date.name}**~~ · {week} · ✅ passée"
    if is_this_week(key_date, today):
        return f"📍 **{key_date.name}** · {week} · **c'est cette semaine !**"
    return f"⏳ **{key_date.name}** · {week} · {relative_days(days_until(key_date, today))}"


def build_deadlines_embed(key_dates: list[KeyDate], today: date) -> discord.Embed:
    lines = [format_key_date(key_date, today) for key_date in key_dates]
    return discord.Embed(title="🗓️ Échéances de la SAE",
                         description="\n".join(lines) or "Aucune date dans config/deadlines.yaml.",
                         color=COLOR)


# --- Alertes automatiques --------------------------------------------------

def render_alert(alert: Alert, discord_ids: dict[int, int]) -> str:
    """Texte final d'une alerte : "{mentions}" devient @Discord (si /link) ou le nom GitLab."""
    names = []
    for person in alert.people:
        discord_id = discord_ids.get(person["id"])
        names.append(f"<@{discord_id}>" if discord_id else f"**{person['name']}**")
    mentions = ", ".join(names) if names else "*personne n'est assigné*"
    # replace plutôt que format : un titre d'issue avec des { } ne pose pas de problème
    return alert.text.replace("{mentions}", mentions)


# --- Récap hebdomadaire ----------------------------------------------------

def build_weekly_embed(overall: Progress, weighted: bool, closed_this_week: list[dict],
                       overdue: list[dict], today: date) -> discord.Embed:
    embed = discord.Embed(title="📬 Récap de la semaine",
                          description="**Avancement global**\n" + format_progress_line(overall, weighted),
                          color=COLOR)

    if closed_this_week:
        lines = []
        for issue in closed_this_week:
            people = ", ".join(a["name"] for a in issue.get("assignees") or []) or "non assignée"
            lines.append(f"✅ {issue_link(issue)} ({people})")
        value = "\n".join(lines)
    else:
        value = "Aucune issue fermée cette semaine."
    embed.add_field(name=f"Terminées cette semaine ({len(closed_this_week)})",
                    value=truncate(value), inline=False)

    if overdue:
        value = "\n".join(f"⚠️ {issue_link(issue)} · {format_due(issue, today)}" for issue in overdue)
    else:
        value = "Aucun retard 👏"
    embed.add_field(name=f"En retard ({len(overdue)})", value=truncate(value), inline=False)
    return embed


# --- /checklist ------------------------------------------------------------

def format_size(size: int) -> str:
    return f"{size} octets" if size < 1024 else f"{size / 1024:.1f} Ko"


def build_checklist_embed(results: list[DeliverableStatus], branch: str,
                          min_size: int) -> discord.Embed:
    lines = []
    for result in results:
        name = result.deliverable.name
        if result.status == MISSING:
            expected = " ou ".join(f"`{path}`" for path in result.deliverable.paths)
            lines.append(f"❌ **{name}** : manquant ({expected})")
        elif result.status == ALMOST_EMPTY:
            lines.append(f"⚠️ **{name}** : `{result.path}` quasi vide ({format_size(result.size)})")
        else:
            note = " · contenu non vérifié" if result.path.endswith(".xlsx") else ""
            lines.append(f"✅ **{name}** : `{result.path}` ({format_size(result.size)}{note})")

    problems = sum(1 for result in results if result.status != OK)
    embed = discord.Embed(title="📋 Vérification des livrables", description="\n".join(lines),
                          color=discord.Color.green() if problems == 0 else COLOR)
    embed.set_footer(text=f"Branche {branch} · « quasi vide » = moins de {min_size} octets")
    return embed


# --- /recap ----------------------------------------------------------------

def build_recap_embed(recaps: list[MemberRecap], unattributed: list[dict],
                      start: date, end: date) -> discord.Embed:
    """Résumé court (le détail est dans le fichier joint)."""
    lines = [f"**{recap.name}** : {len(recap.commits)} commit(s), {len(recap.issues)} issue(s) fermée(s)"
             for recap in recaps]
    if unattributed:
        lines.append(f"*Non attribués : {len(unattributed)} commit(s)*")
    embed = discord.Embed(title=f"🧾 Contributions du {start:%d/%m/%Y} au {end:%d/%m/%Y}",
                          description="\n".join(lines) or "Aucun membre.", color=COLOR)
    embed.set_footer(text="Détail complet dans le fichier joint · commits de merge exclus")
    return embed


def build_recap_markdown(recaps: list[MemberRecap], unattributed: list[dict],
                         start: date, end: date, project_name: str) -> str:
    """Récap détaillé au format Markdown, prêt à copier dans le dossier de preuves."""
    lines = [f"# Contributions individuelles — {project_name}",
             f"Période : du {start:%d/%m/%Y} au {end:%d/%m/%Y}", ""]

    def commit_line(commit):
        day = commit["created_at"][:10]
        return f"- {day} — {markdown_link(commit['title'], commit['web_url'])}"

    for recap in recaps:
        lines += [f"## {recap.name} (@{recap.username})", "",
                  f"### Commits ({len(recap.commits)})"]
        lines += [commit_line(commit) for commit in recap.commits] or ["- aucun"]
        lines += ["", f"### Issues fermées ({len(recap.issues)})"]
        lines += [f"- {issue_link(issue)}" for issue in recap.issues] or ["- aucune"]
        lines.append("")

    if unattributed:
        lines += ["## Commits non attribués", "",
                  "Auteur git non reconnu (vérifier `git config user.email`).", ""]
        lines += [commit_line(commit) + f" — {commit.get('author_name')}" for commit in unattributed]
        lines.append("")
    return "\n".join(lines)
