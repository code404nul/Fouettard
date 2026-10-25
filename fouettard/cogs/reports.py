"""Commandes de bilan : /checklist (livrables) et /recap (contributions)."""

import io
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from fouettard import timeutils
from fouettard.formatting import build_checklist_embed, build_recap_embed, build_recap_markdown
from fouettard.logic.deliverables import all_paths, check_deliverables
from fouettard.logic.recap import build_recap, closed_in_period

log = logging.getLogger(__name__)

# Période maximale de /recap (au-delà, trop de requêtes et un fichier énorme)
MAX_RECAP_DAYS = 366


class ReportsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="checklist", description="Vérifie les livrables dans le dépôt du jeu")
    async def checklist(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # On relit le projet : la branche par défaut a pu changer
        project = await self.bot.gitlab.get_project()
        branch = project.get("default_branch")
        if not branch:
            await interaction.followup.send("Le dépôt est vide : aucun fichier à vérifier.")
            return

        sizes = {path: await self.bot.gitlab.get_file_size(path, branch) for path in all_paths()}
        min_size = self.bot.config.settings.checklist_min_size
        results = check_deliverables(sizes, min_size)
        await interaction.followup.send(embed=build_checklist_embed(results, branch, min_size))

    @app_commands.command(name="recap", description="Commits et issues fermées par membre sur une période")
    @app_commands.describe(date_debut="Début de la période (JJ/MM/AAAA)",
                           date_fin="Fin de la période, incluse (JJ/MM/AAAA)")
    async def recap(self, interaction: discord.Interaction, date_debut: str, date_fin: str):
        try:
            start = timeutils.parse_user_date(date_debut)
            end = timeutils.parse_user_date(date_fin)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if start > end:
            await interaction.response.send_message("❌ La date de début est après la date de fin.",
                                                    ephemeral=True)
            return
        if (end - start).days > MAX_RECAP_DAYS:
            await interaction.response.send_message(
                f"❌ Période trop longue (maximum {MAX_RECAP_DAYS} jours).", ephemeral=True)
            return

        await interaction.response.defer()
        gitlab = self.bot.gitlab
        members = await gitlab.get_members()
        commits = await gitlab.get_commits(timeutils.start_of_day(start), timeutils.end_of_day(end))
        # Les push servent à reconnaître les auteurs : on prend aussi ceux faits
        # après la période (un commit peut être poussé quelques jours plus tard)
        push_events = await gitlab.get_events(after=start - timedelta(days=1),
                                              before=timeutils.today() + timedelta(days=1),
                                              action="pushed")
        closed = closed_in_period(await gitlab.get_issues(state="closed"), start, end)
        project = await self.bot.get_project()

        recaps, unattributed = build_recap(members, commits, push_events, closed)
        embed = build_recap_embed(recaps, unattributed, start, end)
        markdown = build_recap_markdown(recaps, unattributed, start, end, project["name"])
        file = discord.File(io.BytesIO(markdown.encode("utf-8")),
                            filename=f"recap_{start.isoformat()}_{end.isoformat()}.md")
        try:
            await interaction.followup.send(embed=embed, file=file)
        except discord.Forbidden:
            log.error("/recap : le bot n'a pas la permission « Joindre des fichiers »")
            await interaction.followup.send(
                embed=embed, content="⚠️ Je n'ai pas la permission d'envoyer le fichier détaillé "
                                     "(permission « Joindre des fichiers » à donner au bot).")


async def setup(bot):
    await bot.add_cog(ReportsCog(bot))
