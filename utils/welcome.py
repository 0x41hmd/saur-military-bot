import logging
from typing import List, Optional, Tuple

import discord
from discord.ext import commands

from utils import guide
from utils.helpers import create_embed, ensure_persistent_menu, format_rank_for_display, get_channel_safe
from utils.settings import WELCOME_GIF_URL, WELCOME_RULES

logger = logging.getLogger(__name__)

_WELCOME_CHANNELS: Tuple[Tuple[str, str, str], ...] = (
    ("Reports",    "channel_reporting",  "submit your operation reports"),
    ("Tickets",    "open_ticket_channel", "contact HQ, ask for a promotion, request leave"),
)


def _bullets(lines: List[str]) -> str:
    return "\n".join(f"\u2022 {line}" for line in lines)


async def _channel_lines(bot: commands.Bot) -> str:
    lines = []
    for _, key, purpose in _WELCOME_CHANNELS:
        channel = await get_channel_safe(bot, key)
        if channel:
            lines.append(f"\u2022 {channel.mention}: {purpose}")
    return "\n".join(lines)


async def _find_welcome_channel(
    bot: commands.Bot,
    user: discord.abc.User,
    fallback_channel: Optional[discord.abc.Messageable],
) -> Optional[discord.abc.Messageable]:
    channel = await get_channel_safe(bot, "channel_welcome")
    if channel:
        return channel

    guild = getattr(user, "guild", None) or getattr(fallback_channel, "guild", None)
    if guild:
        for candidate in guild.text_channels:
            if "welcome" in candidate.name.lower() and candidate.permissions_for(guild.me).send_messages:
                return candidate

    return fallback_channel


async def build_welcome_embed(
    bot: commands.Bot,
    title: str,
    description: str,
    user: Optional[discord.abc.User] = None,
) -> discord.Embed:
    embed = create_embed(title=title, description=description, user=user)
    if WELCOME_GIF_URL:
        embed.set_image(url=WELCOME_GIF_URL)
    embed.add_field(name="`Rules`", value=_bullets(WELCOME_RULES), inline=False)
    embed.add_field(name="`/profile`", value=guide.PROFILE, inline=False)
    embed.add_field(name="`/leaderboard`", value=guide.LEADERBOARD, inline=False)
    embed.add_field(name="`/online`", value=guide.ONLINE, inline=False)
    embed.add_field(name="`Reports`", value=(
        f"**Create:** {guide.REPORT_CREATE}\n\n**Edit:** {guide.REPORT_EDIT}\n\n**Delete:** {guide.REPORT_DELETE}"
    ), inline=False)
    embed.add_field(name="`Tickets`", value=guide.TICKETS_BRIEF, inline=False)

    channel_lines = await _channel_lines(bot)
    if channel_lines:
        embed.add_field(name="`Where to find things`", value=channel_lines, inline=False)
    return embed


async def ensure_welcome_message(bot: commands.Bot) -> Optional[discord.Message]:
    channel = await get_channel_safe(bot, "channel_welcome")
    if not channel:
        logger.warning("Welcome channel not configured")
        return None

    embed = await build_welcome_embed(
        bot,
        title="Welcome",
        description="Glad to have you here. Here is everything you need to get started. "
                    "You can run `/help` anytime to see it again.",
    )
    return await ensure_persistent_menu(
        channel,
        lambda *_: None,
        lambda: embed,
        "welcome_message_id",
        bot,
    )


async def send_welcome(
    bot: commands.Bot,
    user: discord.abc.User,
    rank: str,
    ig_name: str,
    fallback_channel: Optional[discord.abc.Messageable] = None,
) -> bool:
    try:
        channel = await _find_welcome_channel(bot, user, fallback_channel)
        if not channel:
            logger.warning("No channel available for the welcome message")
            return False

        embed = await build_welcome_embed(
            bot,
            title=f"Welcome to the ranks, {ig_name}!",
            description=(
                f"{user.mention}, you have joined as **{format_rank_for_display(rank)}**.\n"
                "We are proud to have you with us. Here is everything you need to get started, "
                "and you can run `/help` anytime to see it again."
            ),
            user=user,
        )

        await channel.send(
            content=f"Welcome aboard, {user.mention}!",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=[user], roles=False, everyone=False),
        )
        return True

    except Exception as e:
        logger.error(f"Failed to send welcome message for {getattr(user, 'id', '?')}: {e}", exc_info=True)
        return False
