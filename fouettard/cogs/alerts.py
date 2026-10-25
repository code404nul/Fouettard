"""Alertes automatiques et récap hebdomadaire, envoyés dans le salon du bot.

Deux boucles (discord.ext.tasks) :
- daily_check   : aux heures de settings.yaml (et au démarrage), vérifie
                  retards, échéances, inactivité, MR en attente et dates clés ;
- weekly_recap  : le dimanche soir, publie le récap de la semaine.

L'anti-spam (table sent_alerts) garantit qu'une même alerte n'est pas
renvoyée avant son délai, même si le bot redémarre.
"""

import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from fouettard import timeutils
from fouettard.formatting import build_weekly_embed, render_alert
from fouettard.gitlab_api import GitLabError
from fouettard.logic.alerts import (Alert, due_tomorrow_alerts, inactivity_alerts,
                                    key_date_alerts, overdue_alerts,
                                    waiting_merge_request_alerts)
from fouettard.logic.progress import compute_progress, should_use_weights
from fouettard.logic.recap import closed_in_period
from fouettard.logic.tasks import is_overdue, sort_by_due_date
from fouettard.logic.team import ACTIVITY_LOOKBACK_DAYS

log = logging.getLogger(__name__)

SUNDAY = 6  # date.weekday() : lundi = 0 ... dimanche = 6

# Les alertes peuvent mentionner des personnes, mais jamais @everyone ni des rôles
ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)


class AlertsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.config.settings
        # Les heures viennent de settings.yaml : on les applique aux boucles
        self.daily_check.change_interval(time=self.settings.check_times)
        self.weekly_recap.change_interval(time=self.settings.weekly_recap_time)

    async def cog_load(self):
        """Démarre les boucles quand le cog est chargé."""
        self.daily_check.start()
        self.weekly_recap.start()

    async def cog_unload(self):
        self.daily_check.cancel()
        self.weekly_recap.cancel()

    # --- Envoi ----------------------------------------------------------------

    def get_channel(self) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(self.bot.config.discord_channel_id)
        if channel is None:
            log.error("Salon des alertes introuvable (DISCORD_CHANNEL_ID).")
        return channel

    async def send_alerts(self, alerts: list[Alert]) -> None:
        """Envoie les alertes qui ne sont pas bloquées par l'anti-spam."""
        channel = self.get_channel()
        if channel is None:
            return
        discord_ids = {link.gitlab_id: link.discord_id for link in self.bot.db.get_all_links()}
        now = timeutils.now()

        for alert in alerts:
            if self.bot.db.was_alert_sent_recently(alert.key, now, alert.repeat_after):
                continue
            try:
                await channel.send(render_alert(alert, discord_ids),
                                   allowed_mentions=ALLOWED_MENTIONS, suppress_embeds=True)
            except discord.HTTPException as exc:
                log.error("Impossible d'envoyer l'alerte %s : %s", alert.key, exc)
                continue  # pas marquée comme envoyée : on réessaiera
            self.bot.db.mark_alert_sent(alert.key, now)
            log.info("Alerte envoyée : %s", alert.key)

    # --- Vérifications --------------------------------------------------------

    async def check_issue_deadlines(self) -> list[Alert]:
        today = timeutils.today()
        open_issues = await self.bot.gitlab.get_issues(state="opened")
        return (overdue_alerts(open_issues, today, self.settings.overdue_repeat_days)
                + due_tomorrow_alerts(open_issues, today))

    async def check_inactivity(self) -> list[Alert]:
        members = await self.bot.gitlab.get_members()
        events = await self.bot.gitlab.get_events(
            after=timeutils.today() - timedelta(days=ACTIVITY_LOOKBACK_DAYS))
        return inactivity_alerts(members, events, timeutils.now(),
                                 self.settings.inactivity_days, ACTIVITY_LOOKBACK_DAYS)

    async def check_merge_requests(self) -> list[Alert]:
        open_mrs = await self.bot.gitlab.get_merge_requests(state="opened")
        approved = set()
        for mr in open_mrs:
            if not mr.get("draft") and await self.bot.gitlab.is_merge_request_approved(mr["iid"]):
                approved.add(mr["iid"])
        return waiting_merge_request_alerts(open_mrs, approved, timeutils.now(),
                                            self.settings.mr_review_days)

    async def check_key_dates(self) -> list[Alert]:
        return key_date_alerts(self.bot.config.key_dates, timeutils.today(),
                               self.settings.key_date_reminders)

    async def run_all_checks(self) -> None:
        """Lance toutes les vérifications. Une erreur dans l'une n'empêche pas les autres."""
        checks = [self.check_issue_deadlines, self.check_merge_requests,
                  self.check_inactivity, self.check_key_dates]
        for check in checks:
            try:
                await self.send_alerts(await check())
            except GitLabError as exc:
                log.error("Vérification %s impossible : %s", check.__name__, exc)
            except Exception:
                # Une boucle "tasks" s'arrête définitivement sur une erreur non attrapée
                log.exception("Erreur inattendue dans %s", check.__name__)

    # --- Boucles --------------------------------------------------------------

    @tasks.loop(hours=24)  # remplacé dans __init__ par les heures de settings.yaml
    async def daily_check(self):
        await self.run_all_checks()

    @daily_check.before_loop
    async def before_daily_check(self):
        # On attend d'être connecté à Discord, puis on vérifie tout de suite
        # (pour ne rien rater si le bot a été éteint à l'heure prévue)
        await self.bot.wait_until_ready()
        await self.run_all_checks()

    @tasks.loop(hours=24)  # remplacé dans __init__ par l'heure de settings.yaml
    async def weekly_recap(self):
        today = timeutils.today()
        if today.weekday() != SUNDAY:
            return
        # Une seule fois par semaine, même si le bot redémarre
        year, week, _ = today.isocalendar()
        key = f"weekly_recap:{year}-W{week}"
        if self.bot.db.was_alert_sent_recently(key, timeutils.now(), None):
            return
        try:
            embed = await self.build_weekly_recap(today)
        except GitLabError as exc:
            log.error("Récap hebdo impossible : %s", exc)
            return
        except Exception:
            log.exception("Erreur inattendue dans le récap hebdo")
            return

        channel = self.get_channel()
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.error("Impossible d'envoyer le récap hebdo : %s", exc)
            return
        self.bot.db.mark_alert_sent(key, timeutils.now())
        log.info("Récap hebdo envoyé")

    @weekly_recap.before_loop
    async def before_weekly_recap(self):
        await self.bot.wait_until_ready()

    async def build_weekly_recap(self, today) -> discord.Embed:
        issues = await self.bot.gitlab.get_issues()
        features = await self.bot.get_features()
        weighted = should_use_weights(issues, features.weights)
        # Semaine = les 7 derniers jours, du lundi au dimanche
        closed_this_week = closed_in_period(issues, today - timedelta(days=6), today)
        overdue = sort_by_due_date([issue for issue in issues if is_overdue(issue, today)])
        return build_weekly_embed(compute_progress(issues, weighted), weighted,
                                  closed_this_week, overdue, today)


async def setup(bot):
    await bot.add_cog(AlertsCog(bot))
