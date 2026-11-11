import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from utils.helpers import get_channel_safe
from database.db import aget_db_connection

if TYPE_CHECKING:
    from cogs.private.logs import Logs

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Africa/Cairo")

EVENT_SCHEDULE = {
    0: "warzone",
    1: "doomsday",
    2: "warzone",
    3: "doomsday",
    4: "warzone",
    5: "doomsday",
    6: "warzone",
}

EVENT_NAMES = {"doomsday": "DOOMSDAY", "warzone": "WARZONE"}
EVENT_DURATION_MINUTES = {"doomsday": 30, "warzone": 100}
CYCLE_STARTS = {"am": (9, 0), "pm": (21, 0)}
BANNER_LEAD_MINUTES = 60

DM_RATE_LIMIT_DELAY = 0.5
SPAM_INTERVAL_MINUTES = 30


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._event_created: Dict[str, bool] = {}
        self._spam_last_sent: Dict[str, int] = {}
        self._event_dms: Dict[str, Dict[int, int]] = {}
        self.event_scheduler.start()

    def _get_logs_cog(self) -> Optional["Logs"]:
        return self.bot.get_cog("Logs")

    def _get_guild(self) -> Optional[discord.Guild]:
        return self.bot.guilds[0] if self.bot.guilds else None

    def get_today_event(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(LOCAL_TZ)
        return EVENT_SCHEDULE[(now.weekday() + 1) % 7]

    async def get_event_channel(self) -> Optional[discord.TextChannel]:
        return await get_channel_safe(self.bot, "channel_call_of_duty")

    def _find_event_channel(self, event_name: str) -> Optional[discord.abc.GuildChannel]:
        guild = self._get_guild()
        if not guild:
            return None
        name = EVENT_NAMES[event_name]
        return discord.utils.find(
            lambda c: c.name == name and isinstance(c, (discord.StageChannel, discord.VoiceChannel)),
            guild.channels,
        )

    async def create_event_channel(self, event_name: str) -> Optional[discord.abc.GuildChannel]:
        guild = self._get_guild()
        if not guild:
            return None

        existing = self._find_event_channel(event_name)
        if existing:
            return existing

        name = EVENT_NAMES[event_name]
        try:
            cod_channel = await self.get_event_channel()
            category = cod_channel.category if cod_channel else None
            channel = None

            if "COMMUNITY" in guild.features:
                try:
                    channel = await guild.create_stage_channel(
                        name, category=category, reason="Event starting soon")
                    try:
                        await channel.create_instance(
                            topic=f"{name} is starting soon", reason="Event starting soon")
                    except discord.HTTPException:
                        pass
                except discord.Forbidden:
                    raise
                except Exception as e:
                    logger.warning(f"Stage channel failed ({e}), falling back to a voice channel.")

            if channel is None:
                channel = await guild.create_voice_channel(
                    name, category=category, reason="Event starting soon")

            logs_cog = self._get_logs_cog()
            if logs_cog:
                await logs_cog.log_action(
                    action="Event Channel Created", description=f"{channel.mention} created")
            return channel
        except discord.Forbidden:
            logger.error("Event channel: the bot needs Manage Channels in that category/server")
        except Exception as e:
            logger.error(f"create_event_channel error: {e}", exc_info=True)
        return None

    async def delete_event_channel(self, event_name: str) -> bool:
        channel = self._find_event_channel(event_name)
        if not channel:
            return True
        try:
            await channel.delete(reason="Event ended")
            return True
        except discord.NotFound:
            return True
        except Exception as e:
            logger.error(f"delete_event_channel error: {e}")
            return False

    async def send_starting_soon_message(self, event_name: str) -> None:
        channel = await self.get_event_channel()
        if not channel:
            logger.error("COD channel not found.")
            return
        try:
            await channel.send(f"@everyone **{EVENT_NAMES[event_name]}** is starting soon...")
        except Exception as e:
            logger.error(f"send_starting_soon_message error: {e}")

    async def send_banner_dm(self, event_name: str, event_url: str) -> None:
        name = EVENT_NAMES[event_name]

        try:
            async with aget_db_connection() as conn:
                rows = (await conn.execute(
                    "SELECT discord_id FROM members WHERE status = 'Active'"
                )).fetchall()
                member_ids = [int(r["discord_id"]) for r in rows]
        except Exception as e:
            logger.error(f"Database query error: {e}")
            return

        if not member_ids:
            return

        dm_text = f"**{name}** is starting soon. Get ready to join the event."

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Connect to the voice channel", url=event_url, style=discord.ButtonStyle.link))

        sent = self._event_dms.setdefault(event_name, {})
        for discord_id in member_ids:
            try:
                user = await self.bot.fetch_user(discord_id)
                msg = await user.send(dm_text, view=view)
                sent[discord_id] = msg.id
                await asyncio.sleep(DM_RATE_LIMIT_DELAY)
            except Exception:
                continue

    async def delete_event_dms(self, event_name: str) -> None:
        dms = self._event_dms.pop(event_name, None)
        if not dms:
            return

        for discord_id, message_id in dms.items():
            try:
                user = await self.bot.fetch_user(discord_id)
                channel = await user.create_dm()
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except Exception:
                continue

    async def _cleanup_all(self) -> None:
        for event_name in EVENT_NAMES:
            if self._find_event_channel(event_name) is not None:
                await self.delete_event_channel(event_name)
            if event_name in self._event_dms:
                await self.delete_event_dms(event_name)
        self._event_created.clear()
        self._spam_last_sent.clear()

    @tasks.loop(minutes=1)
    async def event_scheduler(self):
        try:
            now = datetime.now(LOCAL_TZ)
            current_min = now.hour * 60 + now.minute
            event_name = self.get_today_event(now)
            duration = EVENT_DURATION_MINUTES[event_name]
            in_any_window = False

            for cycle_name, (start_hour, start_minute) in CYCLE_STARTS.items():
                event_key = f"{event_name}_{cycle_name}_{now.date()}"
                start_min = start_hour * 60 + start_minute
                banner_min = start_min - BANNER_LEAD_MINUTES

                if banner_min <= current_min < start_min + duration:
                    in_any_window = True

                if not (banner_min <= current_min < start_min):
                    continue

                if event_key not in self._event_created:
                    already_exists = self._find_event_channel(event_name) is not None
                    channel = await self.create_event_channel(event_name)
                    if channel and not already_exists:
                        await self.send_banner_dm(event_name, channel.jump_url)
                    self._event_created[event_key] = True

                last = self._spam_last_sent.get(event_key)
                if last is None or (current_min - last) >= SPAM_INTERVAL_MINUTES:
                    await self.send_starting_soon_message(event_name)
                    self._spam_last_sent[event_key] = current_min

            if not in_any_window:
                await self._cleanup_all()

        except Exception as e:
            logger.error(f"event_scheduler error: {e}", exc_info=True)

    @event_scheduler.before_loop
    async def before_event_scheduler(self):
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        self.event_scheduler.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
