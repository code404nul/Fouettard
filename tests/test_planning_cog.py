"""Branchement dans le bot : filtre du propriétaire, boutons, panne de chargement."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fouettard import bot as bot_module
from fouettard.config import Config, Settings
from fouettard.bot import FouettardBot
from fouettard.planning.cog import PlanningButton, PlanningCog, build_view, split_message
from fouettard.planning.service import ButtonSpec, Reply

OWNER = 42


def make_bot(tmp_path):
    config = Config(discord_token="x", discord_channel_id=1, gitlab_url="https://gitlab.example",
                    gitlab_token="x", gitlab_project_id="1", database_path=tmp_path / "bot.db",
                    settings=Settings(), key_dates=[])
    return FouettardBot(config)


async def load_only(bot, extension):
    await bot.load_extension(extension)
    return bot.get_cog("PlanningCog")


def test_all_buttons_keep_a_valid_custom_id():
    view = build_view([ButtonSpec("Fait", "list_done"), ButtonSpec("Note", "grade", "12-facile"),
                       ButtonSpec("Soir", "light_pick", "tonight-leger")])
    for item in view.children:
        assert PlanningButton.__discord_ui_compiled_template__.fullmatch(item.custom_id)
        assert len(item.custom_id) <= 100


def test_split_message_respects_the_limit_and_keeps_lines():
    text = "\n".join(f"ligne {index} " + "x" * 80 for index in range(100))
    chunks = split_message(text)
    assert len(chunks) > 1 and all(len(chunk) <= 1900 for chunk in chunks)
    assert "\n".join(chunks) == text


def test_module_disabled_without_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("PLANNING_OWNER_ID", raising=False)

    async def scenario():
        bot = make_bot(tmp_path)
        assert await load_only(bot, "fouettard.planning.cog") is None
        await bot.close()

    asyncio.run(scenario())


def test_cog_ignores_everyone_but_the_owner_and_guild_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANNING_OWNER_ID", str(OWNER))
    monkeypatch.delenv("ZEUS_ICS_URL", raising=False)
    monkeypatch.delenv("OUTLOOK_ICS_URL", raising=False)
    monkeypatch.setattr("fouettard.planning.config.DATA_DIR", tmp_path)

    async def scenario():
        bot = make_bot(tmp_path)
        cog = await load_only(bot, "fouettard.planning.cog")
        assert type(cog).__name__ == PlanningCog.__name__  # load_extension réimporte le module
        cog.send_reply = AsyncMock()
        cog.service.handle_text = AsyncMock(return_value=Reply("ok"))

        def message(author_id, guild=None, is_bot=False):
            return SimpleNamespace(guild=guild, content="semaine",
                                   author=SimpleNamespace(id=author_id, bot=is_bot),
                                   channel=SimpleNamespace(send=AsyncMock()))

        await cog.on_message(message(999))
        await cog.on_message(message(OWNER, guild=object()))
        cog.service.handle_text.assert_not_awaited()
        await cog.on_message(message(OWNER))
        cog.service.handle_text.assert_awaited_once()
        cog.send_reply.assert_awaited_once()

        button = PlanningButton("week", "", "Semaine")
        other = SimpleNamespace(client=bot, user=SimpleNamespace(id=999))
        mine = SimpleNamespace(client=bot, user=SimpleNamespace(id=OWNER))
        assert not await button.interaction_check(other)
        assert await button.interaction_check(mine)
        await bot.remove_cog("PlanningCog")
        await bot.close()

    asyncio.run(scenario())


def test_broken_module_does_not_stop_the_bot(tmp_path, monkeypatch):
    """Une erreur de chargement du planning laisse les commandes GitLab en place."""
    monkeypatch.setattr(bot_module, "OPTIONAL_EXTENSIONS", ["fouettard.planning.nexiste_pas"])

    async def scenario():
        bot = make_bot(tmp_path)
        bot.gitlab.get_project = AsyncMock(return_value={"name_with_namespace": "x", "web_url": "y"})
        bot.gitlab.detect_features = AsyncMock(return_value=SimpleNamespace(
            confirmed=True, enabled_names=lambda: []))
        await bot.setup_hook()
        names = {command.name for command in bot.tree.get_commands()}
        assert {"link", "todo", "suggest", "progress", "recap", "deadlines"} <= names
        await bot.close()

    asyncio.run(scenario())
