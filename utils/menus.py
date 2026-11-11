import logging
from typing import Dict, Optional

import discord
from discord.ext import commands

from utils.helpers import config_cache, create_embed, ensure_persistent_menu, get_channel_safe
from utils.settings import OPERATION_TYPES

logger = logging.getLogger(__name__)


def _build_report_embed() -> discord.Embed:
    embed = create_embed(
        title="Submit Operation Report",
        description="Select an operation type below to begin the report workflow."
    )
    global_ops = [f"• {v}" for k, v in OPERATION_TYPES.items() if not k.startswith("af_")]
    af_ops = [f"• {v}" for k, v in OPERATION_TYPES.items() if k.startswith("af_")]

    if global_ops:
        embed.add_field(name="Global Operations", value="\n".join(global_ops), inline=False)
    if af_ops:
        embed.add_field(name="Air Force Operations", value="\n".join(af_ops), inline=False)
    return embed


def _build_ticket_embed() -> discord.Embed:
    return create_embed(
        title="Support Tickets",
        description="Select a ticket type below to create a support ticket.\n\nOur HQ team will assist you shortly."
    )


async def ensure_report_menu(bot: commands.Bot) -> Optional[discord.Message]:
    from cogs.public.reports import ReportMenuView

    channel = await get_channel_safe(bot, 'channel_reporting')
    if not channel:
        logger.warning("Reporting channel not configured")
        return None

    return await ensure_persistent_menu(
        channel,
        lambda b: ReportMenuView(None, b),
        _build_report_embed,
        "placeholder_message_id",
        bot
    )


async def ensure_ticket_menu(bot: commands.Bot) -> Optional[discord.Message]:
    from cogs.public.ticket import TicketSelectView

    channel = await get_channel_safe(bot, 'open_ticket_channel')
    if not channel:
        logger.warning("Ticket channel not configured")
        return None

    return await ensure_persistent_menu(
        channel,
        lambda b: TicketSelectView(timeout=None),
        _build_ticket_embed,
        "ticket_menu_message_id",
        bot
    )


async def ensure_recruitment_menu(bot: commands.Bot) -> Optional[discord.Message]:
    from cogs.public.recruitment import RecruitmentDashboardView, build_dashboard_embed, parse_wave_number

    channel = await get_channel_safe(bot, 'channel_recruitment')
    if not channel:
        logger.warning("Recruitment channel not configured")
        return None

    wave_number = None
    cog = bot.get_cog("Recruitment")
    if cog:
        wave = await cog.get_active_wave()
        wave_number = parse_wave_number(wave.name) if wave else None
    embed = build_dashboard_embed(wave_number)

    return await ensure_persistent_menu(
        channel,
        lambda b: RecruitmentDashboardView(),
        lambda: embed,
        "recruitment_menu_message_id",
        bot
    )


async def spawn_menus(bot: commands.Bot) -> Dict[str, Optional[discord.Message]]:
    config_cache.invalidate()
    return {
        "reporting": await ensure_report_menu(bot),
        "open_ticket": await ensure_ticket_menu(bot),
        "recruitment": await ensure_recruitment_menu(bot),
    }
