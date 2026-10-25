"""Point d'entrée du bot. Lancement : python -m fouettard"""

import logging
import sys

import discord

from fouettard.bot import FouettardBot
from fouettard.config import ConfigError, load_config

log = logging.getLogger("fouettard")


def setup_logging() -> None:
    """Affiche les logs dans la console, avec la date et l'heure."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # httpx écrit une ligne par requête : trop bavard
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()

    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Configuration invalide : %s", exc)
        sys.exit(1)

    bot = FouettardBot(config)
    try:
        # log_handler=None : discord.py utilise notre configuration des logs
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error("Token Discord refusé : vérifie DISCORD_TOKEN dans le fichier .env.")
        sys.exit(1)


if __name__ == "__main__":
    main()
