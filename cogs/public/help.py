import logging

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from utils import guide
from utils.helpers import create_embed

logger = logging.getLogger(__name__)


def build_help_embed() -> discord.Embed:
    embed = create_embed(
        title="Help",
        description="Everything you can use as a member. Commands start with `/`; "
                    "reports and tickets are started from the menus in their channels.",
    )
    embed.add_field(name="`/profile`",       value=guide.PROFILE,       inline=False)
    embed.add_field(name="`/leaderboard`",   value=guide.LEADERBOARD,   inline=False)
    embed.add_field(name="`/online`",        value=guide.ONLINE,        inline=False)
    embed.add_field(name="`Casino`",         value=guide.CASINO,        inline=False)
    embed.add_field(name="`Trivia`",         value=guide.TRIVIA,        inline=False)
    embed.add_field(name="`Shop`",           value=guide.SHOP,          inline=False)
    embed.add_field(name="`Tickets`",        value=guide.tickets(),     inline=False)
    embed.add_field(name="`Create a report`", value=guide.REPORT_CREATE, inline=False)
    embed.add_field(name="`Edit a report`",   value=guide.REPORT_EDIT,   inline=False)
    embed.add_field(name="`Delete a report`", value=guide.REPORT_DELETE, inline=False)
    return embed


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show everything you can do as a member")
    async def help_command(self, interaction: Interaction):
        try:
            await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)
        except discord.HTTPException as e:
            logger.error(f"Failed to send help message: {e}", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
