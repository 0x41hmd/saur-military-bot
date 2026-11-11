import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import discord
from discord.ext import commands

from utils.helpers import NO_PING, get_channel_safe
from utils.settings import EMBED_FOOTER_TEXT, LOG_STYLE

logger = logging.getLogger(__name__)

_CATEGORIES = (
    (("removed", "deleted", "declined"),            0xE74C3C, "Removal"),
    (("added", "created", "approved", "submitted"), 0x2ECC71, "Addition"),
    (("updated", "edited", "adjusted", "changed"),  0xF1C40F, "Change"),
)
_DEFAULT_CATEGORY = (0x3498DB, "Info")


def _categorize(action: str):
    lowered = action.lower()
    for keywords, color, label in _CATEGORIES:
        if any(k in lowered for k in keywords):
            return color, label
    return _DEFAULT_CATEGORY


class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def get_audit_channel(self) -> Optional[discord.TextChannel]:
        try:
            return await get_channel_safe(self.bot, 'audit_log_channel')
        except Exception as e:
            logger.error(f"Failed to get audit channel: {e}", exc_info=True)
            return None

    @staticmethod
    def _build_embed(action, description, user, target_user, details, now) -> discord.Embed:
        color, label = _categorize(action)
        embed = discord.Embed(
            title=action,
            description=description or None,
            color=color,
            timestamp=now,
        )
        if user:
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

        if target_user and (not user or target_user.id != user.id):
            embed.add_field(name="Target", value=target_user.mention, inline=True)
        for key, value in (details or {}).items():
            embed.add_field(name=key, value=f"({value})" if key == "Reason" else f"`{value}`", inline=True)

        embed.set_footer(text=f"{EMBED_FOOTER_TEXT}  \u2022  {label}")
        return embed

    @staticmethod
    def _build_text(action, description, user, target_user, details, now) -> str:
        lines = [f"**{action}**  \u00b7  <t:{int(now.timestamp())}:T>"]
        if description:
            lines.append(description)

        meta = []
        if user:
            who = user.mention
            if target_user and target_user.id != user.id:
                who += f" to {target_user.mention}"
            meta.append(who)
        meta.extend(f"{k}: ({v})" if k == "Reason" else f"{k}: `{v}`" for k, v in (details or {}).items())
        if meta:
            lines.append("-# " + "  \u00b7  ".join(meta))
        return "\n".join(lines)

    async def log_action(
        self,
        action: str,
        description: str = "",
        user: Optional[discord.User] = None,
        target_user: Optional[discord.User] = None,
        details: Optional[Dict[str, str]] = None,
    ) -> bool:
        audit_channel = await self.get_audit_channel()
        if not audit_channel:
            logger.warning(f"Audit channel not found for action: {action}")
            return False

        try:
            now = datetime.now(timezone.utc)
            if LOG_STYLE == "text":
                text = self._build_text(action, description, user, target_user, details, now)
                await audit_channel.send(text, allowed_mentions=NO_PING)
            else:
                embed = self._build_embed(action, description, user, target_user, details, now)
                await audit_channel.send(embed=embed, allowed_mentions=NO_PING)
            logger.debug(f"Logged action: {action} by {user.id if user else 'Unknown'}")
            return True

        except discord.DiscordException as e:
            logger.error(f"Discord error sending log message: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Unexpected error in log_action: {e}", exc_info=True)
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(Logs(bot))
