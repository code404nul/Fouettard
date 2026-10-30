"""Partie Discord du module planning : boucles, messages privés et boutons.

Toute la logique est dans service.py. Seul le propriétaire (PLANNING_OWNER_ID)
peut parler au module : les autres messages et clics sont ignorés.
"""

import logging
from datetime import datetime, time

import discord
import httpx
from discord.ext import commands, tasks

from fouettard import timeutils
from fouettard.planning.config import OUTLOOK, ZEUS, PlanningConfig, load_planning_config
from fouettard.planning.service import ButtonSpec, PlanningService, Reply
from fouettard.planning.store import PlanningStore
from fouettard.timeutils import PARIS

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 1900
BUTTONS_PER_MESSAGE = 25
# Si le bot redémarre après l'heure du brief, il l'envoie quand même jusqu'à midi
BRIEF_CATCH_UP_UNTIL = time(12, 0)
NO_MENTIONS = discord.AllowedMentions.none()
BUTTON_STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
                 "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}


def split_message(text: str) -> list[str]:
    """Découpe un long message en morceaux acceptés par Discord, entre deux lignes."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > MESSAGE_LIMIT:
            line = line[MESSAGE_LIMIT:]
        if len(current) + len(line) + 1 > MESSAGE_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class PlanningButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"planning:(?P<action>[a-z_]+):(?P<arg>[^:]*)"):
    """Bouton qui fonctionne encore après un redémarrage du bot."""

    def __init__(self, action: str, arg: str = "", label: str | None = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary):
        self.action = action
        self.arg = arg
        super().__init__(discord.ui.Button(label=label, style=style,
                                           custom_id=f"planning:{action}:{arg}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["arg"], item.label, item.style)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cog = interaction.client.get_cog("PlanningCog")
        return cog is not None and interaction.user.id == cog.config.owner_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("PlanningCog").on_button(interaction, self.action, self.arg)


class LightDateModal(discord.ui.Modal, title="Choisir une date"):
    answer = discord.ui.TextInput(label="Quel jour ?", placeholder="samedi, 12/01, du 6 au 8...",
                                  max_length=60)

    def __init__(self, cog: "PlanningCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        reply = await self.cog.service.handle_date_text(str(self.answer), timeutils.now())
        await self.cog.send_reply(reply)


def build_view(buttons: list[ButtonSpec]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for spec in buttons[:BUTTONS_PER_MESSAGE]:
        view.add_item(PlanningButton(spec.action, spec.arg, spec.label, BUTTON_STYLES[spec.style]))
    return view


class PlanningCog(commands.Cog, name="PlanningCog"):
    def __init__(self, bot, config: PlanningConfig):
        self.bot = bot
        self.config = config
        self.store = PlanningStore(config.database_path)
        self.http = httpx.AsyncClient()
        self.service = PlanningService(config, self.store, bot.gitlab, bot.db,
                                       bot.config.key_dates, self.http)
        self.zeus_refresh.change_interval(minutes=config.zeus_refresh_minutes)
        self.outlook_refresh.change_interval(minutes=config.outlook_refresh_minutes)
        self.mail_refresh.change_interval(minutes=config.mail_refresh_minutes)
        self.morning.change_interval(time=config.brief_time.replace(tzinfo=PARIS))
        self.evening.change_interval(time=config.evening_time.replace(tzinfo=PARIS))

    async def cog_load(self):
        self.bot.add_dynamic_items(PlanningButton)
        if ZEUS in self.config.urls:
            self.zeus_refresh.start()
        if OUTLOOK in self.config.urls:
            self.outlook_refresh.start()
        if self.config.imap:
            self.mail_refresh.start()
        self.morning.start()
        self.evening.start()

    async def cog_unload(self):
        for loop in (self.zeus_refresh, self.outlook_refresh, self.mail_refresh, self.morning, self.evening):
            loop.cancel()
        self.bot.remove_dynamic_items(PlanningButton)
        await self.http.aclose()
        self.store.close()

    # --- Envoi -------------------------------------------------------------

    async def send_text(self, text: str, view: discord.ui.View | None = None) -> None:
        owner = await self.bot.fetch_user(self.config.owner_id)
        chunks = split_message(text) or [" "]
        for chunk in chunks[:-1]:
            await owner.send(chunk, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
        kwargs = {"view": view} if view else {}
        await owner.send(chunks[-1], allowed_mentions=NO_MENTIONS, suppress_embeds=True, **kwargs)

    async def send_reply(self, reply: Reply) -> None:
        if reply.content:
            await self.send_text(reply.content, build_view(reply.buttons) if reply.buttons else None)

    # --- Réception ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is not None or message.author.id != self.config.owner_id:
            return
        if message.author.bot or not message.content.strip():
            return
        try:
            await self.send_reply(await self.service.handle_text(message.content, timeutils.now()))
        except Exception:
            log.exception("Erreur en traitant un message privé")
            await message.channel.send("⚠️ Une erreur s'est produite. Les détails sont dans les logs du bot.")

    async def on_button(self, interaction: discord.Interaction, action: str, arg: str) -> None:
        try:
            # Discord exige une réponse en 3 s : on accuse réception avant d'appeler GitLab
            if action != "light_choose":
                await interaction.response.defer()
            replies = await self.service.handle_action(action, arg, timeutils.now())
            if replies and replies[0].open_date_modal:
                await interaction.response.send_modal(LightDateModal(self))
                return
            for reply in replies:
                await self.send_reply(reply)
        except Exception:
            log.exception("Erreur sur le bouton %s", action)
            await self.send_text("⚠️ Une erreur s'est produite. Les détails sont dans les logs du bot.")

    # --- Boucles -----------------------------------------------------------

    async def _refresh(self, source: str) -> None:
        # Une boucle « tasks » s'arrête définitivement sur une erreur non attrapée
        try:
            for change in await self.service.refresh_source(source, timeutils.now()):
                await self.send_text(self.service.change_alert(change))
        except Exception:
            log.exception("Erreur inattendue en lisant le calendrier %s", source)

    @tasks.loop(minutes=30)  # remplacé dans __init__ par planning.yaml
    async def zeus_refresh(self):
        await self._refresh(ZEUS)

    @tasks.loop(minutes=60)  # remplacé dans __init__ par planning.yaml
    async def outlook_refresh(self):
        await self._refresh(OUTLOOK)

    async def _refresh_mail(self) -> None:
        try:
            for alert in await self.service.refresh_mail(timeutils.now()):
                await self.send_text(alert)
        except Exception:
            log.exception("Erreur inattendue en lisant les mails")

    @tasks.loop(minutes=15)  # remplacé dans __init__ par planning.yaml
    async def mail_refresh(self):
        await self._refresh_mail()

    @zeus_refresh.before_loop
    @outlook_refresh.before_loop
    @mail_refresh.before_loop
    async def wait_ready(self):
        await self.bot.wait_until_ready()

    async def send_brief_once(self) -> None:
        now = timeutils.now()
        key = f"brief:{now.date().isoformat()}"
        if self.store.was_sent(key):
            return
        try:
            await self._refresh(ZEUS)
            await self._refresh(OUTLOOK)
            await self._refresh_mail()
            await self.send_reply(await self.service.build_brief(now))
            self.store.mark_sent(key, now)
            for alert_key, text in await self.service.collect_daily_alerts(now):
                if not self.store.was_sent(alert_key):
                    await self.send_text(text)
                    self.store.mark_sent(alert_key, now)
        except Exception:
            log.exception("Erreur inattendue dans le brief du matin")

    @tasks.loop(hours=24)  # remplacé dans __init__ par l'heure de planning.yaml
    async def morning(self):
        await self.send_brief_once()

    @morning.before_loop
    async def before_morning(self):
        await self.bot.wait_until_ready()
        now = timeutils.now()
        brief_at = datetime.combine(now.date(), self.config.brief_time, tzinfo=PARIS)
        catch_up_until = datetime.combine(now.date(), BRIEF_CATCH_UP_UNTIL, tzinfo=PARIS)
        if brief_at <= now < catch_up_until:
            await self.send_brief_once()

    @tasks.loop(hours=24)  # remplacé dans __init__ par l'heure de planning.yaml
    async def evening(self):
        now = timeutils.now()
        key = f"evening:{now.date().isoformat()}"
        if self.store.was_sent(key):
            return
        try:
            await self.send_reply(self.service.build_evening(now))
            self.store.mark_sent(key, now)
        except Exception:
            log.exception("Erreur inattendue dans le bilan du soir")

    @evening.before_loop
    async def before_evening(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    config = load_planning_config()
    if config is None:
        log.info("Module planning désactivé : PLANNING_OWNER_ID absent du .env.")
        return
    await bot.add_cog(PlanningCog(bot, config))
    sources = list(config.urls) + (["mail"] if config.imap else [])
    log.info("Module planning activé (sources : %s).", ", ".join(sources) or "aucune")
