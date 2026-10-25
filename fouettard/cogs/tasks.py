"""Commandes sur les tâches : /todo (mes issues) et /suggest (tâches libres)."""

import discord
from discord import app_commands
from discord.ext import commands

from fouettard import timeutils
from fouettard.formatting import build_suggest_embed, build_todo_embed
from fouettard.logic.tasks import (UNAVAILABLE_LABELS, build_priority_order, is_assigned_to,
                                   sort_by_due_date, suggest_issues)

# Choix proposés pour l'option "inclure" de /suggest
INCLUDE_ALL = "toutes"
INCLUDE_CHOICES = ([app_commands.Choice(name=label, value=label) for label in UNAVAILABLE_LABELS]
                   + [app_commands.Choice(name=INCLUDE_ALL, value=INCLUDE_ALL)])


class TasksCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="todo", description="Tes issues ouvertes, triées par échéance")
    async def todo(self, interaction: discord.Interaction):
        link = self.bot.db.get_link_by_discord(interaction.user.id)
        if link is None:
            await interaction.response.send_message(
                "Relie d'abord ton compte GitLab avec `/link <username_gitlab>`.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        open_issues = await self.bot.gitlab.get_issues(state="opened")
        features = await self.bot.get_features()

        mine = [issue for issue in open_issues if is_assigned_to(issue, link.gitlab_id)]
        embed = build_todo_embed(link.gitlab_username, sort_by_due_date(mine),
                                 timeutils.today(), show_health=features.health_status)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="suggest", description="Propose des tâches libres à prendre")
    @app_commands.describe(inclure="Afficher aussi les issues avec ce label (masquées par défaut)")
    @app_commands.choices(inclure=INCLUDE_CHOICES)
    async def suggest(self, interaction: discord.Interaction,
                      inclure: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        open_issues = await self.bot.gitlab.get_issues(state="opened")
        labels = await self.bot.gitlab.get_labels()

        # Labels à ne PAS masquer, selon l'option choisie
        if inclure is None:
            included = set()
        elif inclure.value == INCLUDE_ALL:
            included = set(UNAVAILABLE_LABELS)
        else:
            included = {inclure.value}

        priority_order = build_priority_order(labels)
        suggestions = suggest_issues(open_issues, priority_order, included)
        await interaction.followup.send(
            embed=build_suggest_embed(suggestions, priority_order, timeutils.today()))


async def setup(bot):
    await bot.add_cog(TasksCog(bot))
