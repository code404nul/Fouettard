"""Commande /link : relier son compte Discord à son compte GitLab."""

import discord
from discord import app_commands
from discord.ext import commands


class LinkCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="link", description="Relie ton compte Discord à ton compte GitLab")
    @app_commands.describe(username_gitlab="Ton nom d'utilisateur GitLab (sans le @)")
    async def link(self, interaction: discord.Interaction, username_gitlab: str):
        # defer = "je réfléchis..." : on a jusqu'à 15 min pour répondre au lieu de 3 s.
        # ephemeral = seule la personne qui tape la commande voit la réponse.
        await interaction.response.defer(ephemeral=True)

        username = username_gitlab.strip().lstrip("@")
        user = await self.bot.gitlab.find_user(username)
        if user is None:
            await interaction.followup.send(
                f"❌ Aucun utilisateur GitLab nommé **{username}**. Vérifie l'orthographe "
                "(c'est le @pseudo affiché sur ton profil GitLab).", ephemeral=True)
            return

        # Un compte GitLab ne peut être relié qu'à une seule personne sur Discord
        existing = self.bot.db.get_link_by_gitlab_id(user["id"])
        if existing and existing.discord_id != interaction.user.id:
            await interaction.followup.send(
                f"❌ Le compte GitLab **{user['username']}** est déjà relié à <@{existing.discord_id}>.",
                ephemeral=True)
            return

        self.bot.db.save_link(interaction.user.id, user["id"], user["username"])
        message = f"✅ Ton compte Discord est maintenant relié à **{user['username']}** sur GitLab."

        # Simple avertissement si la personne n'est pas membre du projet
        members = await self.bot.gitlab.get_members()
        if all(member["id"] != user["id"] for member in members):
            message += "\n⚠️ Attention : ce compte n'est pas (encore) membre du projet GitLab."

        await interaction.followup.send(message, ephemeral=True)


async def setup(bot):
    """Appelé par bot.load_extension() pour ajouter ce cog au bot."""
    await bot.add_cog(LinkCog(bot))
