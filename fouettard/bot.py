"""Le bot Discord lui-même : connexion, chargement des commandes, erreurs."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from fouettard.config import Config
from fouettard.database import Database
from fouettard.formatting import gitlab_error_message
from fouettard.gitlab_api import Features, GitLabClient, GitLabError

log = logging.getLogger(__name__)

# Fichiers de commandes (dossier fouettard/cogs) chargés au démarrage
EXTENSIONS = [
    "fouettard.cogs.link",
    "fouettard.cogs.project",
    "fouettard.cogs.tasks",
    "fouettard.cogs.reports",
    "fouettard.cogs.alerts",
]
OPTIONAL_EXTENSIONS = ["fouettard.planning.cog"]


class FouettardBot(commands.Bot):
    def __init__(self, config: Config):
        # Les intents par défaut suffisent : on n'utilise que des slash commands.
        # when_mentioned : pas de commandes "!xxx", donc pas besoin de lire les messages.
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.config = config
        self.db = Database(config.database_path)
        self.gitlab = GitLabClient(config.gitlab_url, config.gitlab_token,
                                   config.gitlab_project_id)
        self._features: Features | None = None
        self._project: dict | None = None
        self._commands_synced = False

    async def setup_hook(self) -> None:
        """Appelé une fois au démarrage, avant la connexion à Discord."""
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        # Une panne d'un module facultatif ne doit jamais empêcher le suivi GitLab
        for extension in OPTIONAL_EXTENSIONS:
            try:
                await self.load_extension(extension)
            except Exception:
                log.exception("Module facultatif %s non chargé", extension)
        # Toutes les erreurs des slash commands passent par on_command_error
        self.tree.on_error = self.on_command_error

        # Premier contact avec GitLab : si ça échoue, le bot démarre quand même
        # et réessaiera à la prochaine commande.
        try:
            project = await self.get_project()
            log.info("Projet GitLab : %s (%s)", project["name_with_namespace"], project["web_url"])
            features = await self.get_features()
            if not features.confirmed:
                log.info("Aucune issue dans le projet : détection des options GitLab "
                         "reportée à la création de la première issue.")
        except GitLabError as exc:
            log.error("Impossible de joindre GitLab au démarrage : %s", exc)

    async def on_ready(self) -> None:
        log.info("Connecté à Discord en tant que %s", self.user)
        if self._commands_synced:
            return  # on_ready peut être appelé plusieurs fois (reconnexions)
        self._commands_synced = True

        channel = self.get_channel(self.config.discord_channel_id)
        if channel is None:
            log.error("Salon %s introuvable : vérifie DISCORD_CHANNEL_ID et que le bot "
                      "a bien été invité sur le serveur.", self.config.discord_channel_id)
            return

        # On enregistre les commandes sur le serveur du salon : elles
        # apparaissent immédiatement (une synchro globale prend jusqu'à 1 h).
        guild = channel.guild
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("%d commandes slash synchronisées sur le serveur « %s »", len(synced), guild.name)
        except discord.HTTPException as exc:
            log.error("Échec de la synchronisation des commandes : %s", exc)

    async def close(self) -> None:
        """Arrêt propre : on ferme GitLab et la base avant de quitter."""
        await self.gitlab.close()
        self.db.close()
        await super().close()

    # --- Informations GitLab mémorisées ------------------------------------

    async def get_project(self) -> dict:
        """Infos du projet, récupérées une seule fois puis mémorisées."""
        if self._project is None:
            self._project = await self.gitlab.get_project()
        return self._project

    async def get_features(self) -> Features:
        """Options GitLab disponibles, détectées puis mémorisées.

        Tant que le projet n'a aucune issue, la détection est incomplète :
        on la refait à chaque appel jusqu'à ce qu'une issue existe.
        """
        if self._features is None or not self._features.confirmed:
            self._features = await self.gitlab.detect_features()
            if self._features.confirmed:
                log.info("Options GitLab détectées : %s",
                         ", ".join(self._features.enabled_names()) or "aucune (version gratuite)")
        return self._features

    # --- Gestion des erreurs -----------------------------------------------

    async def on_command_error(self, interaction: discord.Interaction,
                               error: app_commands.AppCommandError) -> None:
        """Appelé quand une slash command plante : on prévient l'utilisateur
        au lieu de laisser Discord afficher "L'application ne répond plus"."""
        original = getattr(error, "original", error)
        command_name = interaction.command.name if interaction.command else "?"
        if isinstance(original, GitLabError):
            log.error("Erreur GitLab dans /%s : %s", command_name, original)
            message = gitlab_error_message(original)
        else:
            log.exception("Erreur inattendue dans une commande", exc_info=original)
            message = "⚠️ Une erreur inattendue s'est produite. Les détails sont dans les logs du bot."

        # Selon le moment de l'erreur, on a déjà répondu (defer) ou pas encore
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
