"""Commandes sur l'état global du projet : /progress, /team, /deadlines."""

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from fouettard import timeutils
from fouettard.formatting import build_deadlines_embed, build_progress_embed, build_team_embed
from fouettard.logic.key_dates import sort_key_dates
from fouettard.logic.progress import (compute_progress, compute_progress_by, domain_name,
                                      epic_name, iteration_name, milestone_name,
                                      should_use_weights, uses_domain_labels)
from fouettard.logic.team import ACTIVITY_LOOKBACK_DAYS, summarize_team


class ProjectCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="progress", description="Avancement du projet, global et par milestone")
    async def progress(self, interaction: discord.Interaction):
        await interaction.response.defer()

        issues = await self.bot.gitlab.get_issues()
        features = await self.bot.get_features()
        project = await self.bot.get_project()

        weighted = should_use_weights(issues, features.weights)
        sections = {"Par milestone": compute_progress_by(issues, milestone_name, weighted)}
        # Sections supplémentaires si la licence GitLab le permet
        if features.iterations:
            sections["Par itération"] = compute_progress_by(issues, iteration_name, weighted)
        if features.epics:
            sections["Par epic"] = compute_progress_by(issues, epic_name, weighted)
        elif uses_domain_labels(issues):
            # Pas de vrais epics (projet hors groupe) : on regroupe par label "domaine::..."
            sections["Par domaine"] = compute_progress_by(issues, domain_name, weighted)

        embed = build_progress_embed(project["name"], compute_progress(issues, weighted),
                                     sections, weighted, features)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="team", description="Vue d'ensemble : issues et activité de chaque membre")
    async def team(self, interaction: discord.Interaction):
        await interaction.response.defer()

        today = timeutils.today()
        members = await self.bot.gitlab.get_members()
        open_issues = await self.bot.gitlab.get_issues(state="opened")
        events = await self.bot.gitlab.get_events(after=today - timedelta(days=ACTIVITY_LOOKBACK_DAYS))
        features = await self.bot.get_features()

        summaries = summarize_team(members, open_issues, events, today)
        discord_ids = {link.gitlab_id: link.discord_id for link in self.bot.db.get_all_links()}
        embed = build_team_embed(summaries, discord_ids, today, ACTIVITY_LOOKBACK_DAYS,
                                 show_health=features.health_status)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="deadlines", description="Compte à rebours des échéances de la SAE")
    async def deadlines(self, interaction: discord.Interaction):
        key_dates = sort_key_dates(self.bot.config.key_dates)
        await interaction.response.send_message(
            embed=build_deadlines_embed(key_dates, timeutils.today()))


async def setup(bot):
    await bot.add_cog(ProjectCog(bot))
