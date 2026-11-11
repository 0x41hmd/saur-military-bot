import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database.db import aget_db_connection
from database.db import initialize_database, get_db_connection, ping_database, close_database
from utils.helpers import validate_persistent_menus
from utils.menus import spawn_menus

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = False


REGISTERED_ONLY_COMMANDS = {
    "balance", "dailyreward",
    "casino play",
    "trivia play",
    "shop list", "shop buy", "shop orders",
}


class NotRegistered(app_commands.CheckFailure):
    pass


def _is_registered(discord_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM members WHERE discord_id = ? LIMIT 1", (str(discord_id),)
        ).fetchone()
    return row is not None


class MilitaryTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if command is None or command.qualified_name not in REGISTERED_ONLY_COMMANDS:
            return True
        try:
            registered = await asyncio.to_thread(_is_registered, interaction.user.id)
        except Exception as e:
            logger.error(f"Registration check failed: {e}", exc_info=True)
            raise app_commands.CheckFailure("Something went wrong. Please try again.")
        if not registered:
            raise NotRegistered()
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, (NotRegistered, app_commands.CheckFailure)):
            msg = ("You need to be a registered member to use this command. Ask HQ to add you."
                   if isinstance(error, NotRegistered) else str(error) or "You can't use this command.")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass
            return
        await super().on_error(interaction, error)


class MilitaryBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            tree_cls=MilitaryTree,
        )
        self._startup_done = False

    async def setup_hook(self):
        try:
            initialize_database()
            self.db_heartbeat.start()
            await self._load_cogs()
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.critical(f"Setup failed: {e}", exc_info=True)
            raise

    async def _load_cogs(self):
        cogs = {
            "private": ["logs", "setup", "backup", "member", "points", "promotion", "admin", "analytics", "activity"],
            "public": ["profile", "leaderboard", "reports", "online", "ticket", "events", "recruitment", "casino", "trivia", "shop", "help"]
        }
        for group, names in cogs.items():
            for name in names:
                try:
                    await self.load_extension(f"cogs.{group}.{name}")
                except Exception as e:
                    logger.error(f"Failed to load {name}: {e}", exc_info=True)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} ({self.user.id}) across {len(self.guilds)} guild(s)")

        if not self._startup_done:
            await self._run_startup()
            self._startup_done = True

        logger.info("=" * 60)
        logger.info("Bot ready")
        logger.info("=" * 60)

    async def _run_startup(self):
        jobs = [
            ("Persistent views", self._register_persistent_views()),
            ("Persistent menus", validate_persistent_menus(self)),
            ("Report & ticket menus", spawn_menus(self)),
            ("Ticket views", self._restore_ticket_views()),
            ("Approval views", self._restore_approval_views()),
        ]
        for name, coro in jobs:
            try:
                await coro
            except Exception as e:
                logger.error(f"{name} failed: {e}", exc_info=True)

    async def _register_persistent_views(self):
        from cogs.public.reports import ReportMenuView
        from cogs.public.ticket import TicketSelectView, CloseTicketView

        self.add_view(ReportMenuView(cog=None, bot=self))
        self.add_view(TicketSelectView(timeout=None))

        ticket_cog = self.get_cog("Ticket")
        if ticket_cog:
            self.add_view(CloseTicketView(cog=ticket_cog, ticket_channel_id=0))
        else:
            logger.warning("Ticket cog not loaded, CloseTicketView not registered")

    async def _restore_ticket_views(self):
        from cogs.public.ticket import CloseTicketView

        ticket_cog = self.get_cog("Ticket")
        if not ticket_cog:
            logger.warning("Ticket cog not loaded, skipping view restoration")
            return

        async with aget_db_connection() as conn:
            open_tickets = (await conn.execute(
                "SELECT channel_id, message_id FROM tickets WHERE status = 'open'"
            )).fetchall()

        restored = 0
        for ticket in open_tickets:
            channel_id = int(ticket['channel_id'])
            channel = self.get_channel(channel_id)

            if not channel:
                logger.warning(f"Ticket channel {channel_id} not found")
                continue

            if not ticket['message_id']:
                logger.warning(f"Ticket {channel_id} has no stored message_id")
                continue

            try:
                message = await channel.fetch_message(int(ticket['message_id']))
                await message.edit(view=CloseTicketView(cog=ticket_cog, ticket_channel_id=channel.id))
                restored += 1
            except discord.NotFound:
                logger.warning(f"Ticket message {ticket['message_id']} not found in {channel_id}")
            except discord.Forbidden:
                logger.warning(f"No permission to restore ticket view in {channel_id}")
            except Exception as e:
                logger.error(f"Error restoring ticket view for {channel_id}: {e}")

        if restored:
            logger.info(f"Restored {restored} ticket view(s)")

    async def _restore_approval_views(self):
        reports_cog = self.get_cog("Reports")
        if not reports_cog:
            logger.warning("Reports cog not loaded, skipping approval view restoration")
            return
        await reports_cog.restore_approval_views()

    @tasks.loop(minutes=4)
    async def db_heartbeat(self):
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, ping_database)
        if ok:
            logger.debug("Database heartbeat ok")

    async def close(self):
        logger.info("Shutting down...")
        self.db_heartbeat.cancel()
        await super().close()
        close_database()


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical("DISCORD_TOKEN not set")
        sys.exit(1)

    bot = MilitaryBot()
    try:
        async with bot:
            await bot.start(token)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
