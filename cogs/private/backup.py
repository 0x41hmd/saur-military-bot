import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.db import (
    auto_backup_database, export_backup_bytes, get_channel_ids, get_config_value, set_config_value,
)
from utils.helpers import plain
from utils.settings import DEVELOPER_ID

logger = logging.getLogger(__name__)

BACKUP_CHANNEL_KEY = "channel_backup"
BACKUP_INTERVAL_KEY = "backup_interval_hours"
LAST_BACKUP_KEY = "last_backup_at"
DEFAULT_INTERVAL_HOURS = 168
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24 * 90


def parse_interval_hours(value, default: float = DEFAULT_INTERVAL_HOURS) -> float:
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return default
    return hours if MIN_INTERVAL_HOURS <= hours <= MAX_INTERVAL_HOURS else default


def backup_is_due(last_iso: Optional[str], interval_hours: float, now: Optional[datetime] = None) -> bool:
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() / 3600 >= interval_hours


def describe_interval(hours: float) -> str:
    if hours % 24 == 0:
        days = int(hours // 24)
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{hours:g} hour{'s' if hours != 1 else ''}"


class Backup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.scheduled_backup.start()

    async def cog_unload(self) -> None:
        self.scheduled_backup.cancel()

    async def _backup_channel(self) -> Optional[discord.abc.Messageable]:
        ids = await asyncio.to_thread(get_channel_ids, BACKUP_CHANNEL_KEY)
        if not ids:
            return None
        channel = self.bot.get_channel(ids[0])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(ids[0])
            except discord.HTTPException:
                return None
        return channel

    async def _post_backup(self, channel: discord.abc.Messageable, title: str) -> str:
        name, payload = await asyncio.to_thread(export_backup_bytes)

        limit = getattr(getattr(channel, "guild", None), "filesize_limit", 25 * 1024 * 1024)
        if len(payload) > limit:
            raise RuntimeError(
                f"The backup is {len(payload) / 1024 / 1024:.1f} MB, over this server's "
                f"{limit / 1024 / 1024:.0f} MB upload limit.")

        await channel.send(
            f"**{title}** ({len(payload) / 1024:.1f} KB)\nKeep this file somewhere safe.",
            file=discord.File(io.BytesIO(payload), filename=name),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.to_thread(set_config_value, LAST_BACKUP_KEY, datetime.now(timezone.utc).isoformat())
        return f"{name} ({len(payload) / 1024:.1f} KB)"

    @tasks.loop(hours=1)
    async def scheduled_backup(self) -> None:
        try:
            interval = parse_interval_hours(await asyncio.to_thread(get_config_value, BACKUP_INTERVAL_KEY, None))
            last = await asyncio.to_thread(get_config_value, LAST_BACKUP_KEY, None)
            if not backup_is_due(last, interval):
                return

            channel = await self._backup_channel()
            if channel is None:
                logger.warning("Scheduled backup is due but no backup channel is set. Run /setup to choose one.")
                return

            async with self._lock:
                await asyncio.to_thread(auto_backup_database, 4)
                result = await self._post_backup(channel, "Scheduled database backup")
            logger.info(f"Scheduled backup posted: {result}")
        except Exception as e:
            logger.error(f"Scheduled backup error: {e}", exc_info=True)

    @scheduled_backup.before_loop
    async def _before_scheduled_backup(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="backup", description="Back up the whole database and post it in the backup channel (developer only)")
    async def backup(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != DEVELOPER_ID:
            await interaction.response.send_message(**plain("Only the developer can run this command!"), ephemeral=True)
            logger.warning(f"Unauthorized backup attempt by {interaction.user.id}")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = await self._backup_channel()
        if channel is None:
            await interaction.followup.send(
                **plain("No backup channel is set. Run `/setup` and pick one in the Backup Channel step."))
            return

        try:
            async with self._lock:
                result = await self._post_backup(channel, f"Manual database backup by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                **plain(f"I can't post in <#{channel.id}>. I need to see it, send messages and attach files there."))
            return
        except Exception as e:
            logger.error(f"Manual backup failed: {e}", exc_info=True)
            await interaction.followup.send(**plain(f"Backup failed: {e}"))
            return

        await interaction.followup.send(**plain(f"Backup posted in <#{channel.id}>: {result}"))

    @app_commands.command(name="backup_schedule", description="Set how often the automatic backup runs (developer only)")
    @app_commands.describe(days="Days between automatic backups", hours="Extra hours between automatic backups")
    async def backup_schedule(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 90] = 0,
        hours: app_commands.Range[int, 0, 23] = 0,
    ) -> None:
        if interaction.user.id != DEVELOPER_ID:
            await interaction.response.send_message(**plain("Only the developer can run this command!"), ephemeral=True)
            return

        total = days * 24 + hours
        if total < MIN_INTERVAL_HOURS:
            current = parse_interval_hours(await asyncio.to_thread(get_config_value, BACKUP_INTERVAL_KEY, None))
            await interaction.response.send_message(
                **plain(f"Pass `days` and/or `hours` (at least 1 hour, at most 90 days). "
                        f"Current schedule: every {describe_interval(current)}."),
                ephemeral=True)
            return

        if not await asyncio.to_thread(set_config_value, BACKUP_INTERVAL_KEY, str(total)):
            await interaction.response.send_message(**plain("Could not save the schedule."), ephemeral=True)
            return
        await interaction.response.send_message(
            **plain(f"Automatic backups will now run every {describe_interval(total)}."), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backup(bot))
