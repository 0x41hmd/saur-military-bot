import discord
import time
import asyncio
import logging

from database.db import aget_db_connection
from utils.helpers import member_cache, plain
from .base import ReportState

logger = logging.getLogger(__name__)


async def start_report_workflow(interaction: discord.Interaction, operation_type: str):
    try:
        cog = interaction.client.get_cog("Reports")
        if not cog:
            try:
                await interaction.followup.send(**plain("Reporting system not loaded"), ephemeral=True)
            except discord.NotFound:
                pass
            return
        cache_key   = f"all_members_{int(time.time() / 300)}"
        all_members = member_cache.get(cache_key)
        if all_members is None:
            try:
                async with aget_db_connection() as conn:
                    rows = (await conn.execute(
                        "SELECT discord_id, discord_name, ig_name, rank, is_af, af_rank FROM members ORDER BY discord_name"
                    )).fetchall()
                    all_members = [dict(row) for row in rows]
                member_cache.set(cache_key, all_members)
            except Exception as e:
                logger.error(f"Error fetching members: {e}", exc_info=True)
                try:
                    await interaction.followup.send(**plain("Error loading members list"), ephemeral=True)
                except discord.NotFound:
                    pass
                return
        state             = ReportState(operation_type=operation_type, reporter_id=str(interaction.user.id))
        state.all_members = all_members
        cog._set_state(interaction.user.id, state)
        await cog.show_step(interaction, state)
    except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
        pass
