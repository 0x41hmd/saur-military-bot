import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import errors as db_errors
from database.db import aget_db_connection, get_channel_ids
from utils.helpers import create_embed, plain
from utils.menus import spawn_menus
from utils.settings import DEVELOPER_ID, SHOP_CONFIG

logger = logging.getLogger(__name__)

SETUP_TIMEOUT = 600
MAX_CHANNELS_PER_STEP = 10
MAX_FIELD_CHARS = 1000

CHANNEL_PROMPTS = [
    ("reporting",         "Enter Reporting Channel"),
    ("operations",        "Enter Operations Channel"),
    ("counter_terrorism", "Enter Counter Terrorism Channel"),
    ("warzone",           "Enter Warzone Channel"),
    ("doomsday",          "Enter Doomsday Channel"),
    ("call_of_duty",      "Enter Call of Duty Channel"),
    ("training",          "Enter Training Channel"),
    ("patrol",            "Enter Patrol Channel"),
    ("ammo_transport",    "Enter Ammo Transport Channel"),
    ("extra_points",      "Enter Extra Points Channel"),
    ("af_warzone",        "Enter AF Warzone Channel"),
    ("af_training",       "Enter AF Training Channel"),
    ("af_counter",        "Enter AF Counter Rented Channel"),
    ("promotion",         "Enter Promotion Channel"),
    ("open_ticket",       "Enter Support Ticket Channel"),
    ("ticket_transcript", "Enter Ticket Transcripts Channel (HQ only, closed tickets are saved here)"),
    ("audit_log",         "Enter Audit Log Channel"),
    ("recruitment",       "Enter Recruitment Channel (public, Apply / Re-instatement buttons)"),
    ("recruitment_logs",  "Enter Recruitment Logs Channel (HQ only, applications & votes)"),
    ("welcome",           "Enter Welcome Channel (new members are greeted here)"),
    ("shop_orders",       "Enter Shop Orders Channel (HQ only, new orders get Confirm / Reject buttons)"),
    ("backup",            "Enter Backup Channel (HQ only, database backups are posted here)"),
]

CHANNEL_DB_MAP = {
    "reporting":         "channel_reporting",
    "operations":        "channel_operations",
    "counter_terrorism": "channel_counter_terrorism",
    "warzone":           "channel_warzone",
    "doomsday":          "channel_doomsday",
    "call_of_duty":      "channel_call_of_duty",
    "training":          "channel_training",
    "patrol":            "channel_patrol",
    "ammo_transport":    "channel_ammo_transport",
    "extra_points":      "channel_extra_points",
    "af_warzone":        "channel_af_warzone",
    "af_counter":        "channel_af_counter",
    "af_training":       "channel_af_training",
    "promotion":         "promotion_channel",
    "open_ticket":       "open_ticket_channel",
    "ticket_transcript": "ticket_transcript_channel",
    "audit_log":         "audit_log_channel",
    "recruitment":       "channel_recruitment",
    "recruitment_logs":  "channel_recruitment_logs",
    "welcome":           "channel_welcome",
    "shop_orders":       SHOP_CONFIG["order_channel_key"],
    "backup":            "channel_backup",
}


@dataclass
class SetupState:
    prompts: List[tuple] = field(default_factory=lambda: list(CHANNEL_PROMPTS))
    channels: Dict[str, List[int]] = field(default_factory=dict)
    existing: Dict[str, List[int]] = field(default_factory=dict)
    index: int = 0

    @property
    def total(self) -> int:
        return len(self.prompts)

    @property
    def done(self) -> bool:
        return self.index >= self.total

    @property
    def current_key(self) -> Optional[str]:
        return None if self.done else self.prompts[self.index][0]

    @property
    def current_prompt(self) -> Optional[str]:
        return None if self.done else self.prompts[self.index][1]

    def can_skip(self) -> bool:
        return not self.done and bool(self.existing.get(self.current_key))

    def can_go_back(self) -> bool:
        return self.index > 0

    def select(self, channel_ids: List[int]) -> None:
        if self.done:
            raise ValueError("setup is already complete")
        unique = list(dict.fromkeys(int(c) for c in channel_ids))
        if not unique:
            raise ValueError("pick at least one channel")
        self.channels[self.current_key] = unique
        self.index += 1

    def skip(self) -> None:
        if not self.can_skip():
            raise ValueError("nothing saved for this step, so it can't be skipped")
        self.channels[self.current_key] = list(self.existing[self.current_key])
        self.index += 1

    def back(self) -> None:
        if not self.can_go_back():
            raise ValueError("already at the first step")
        self.index -= 1

    def serialized(self) -> Dict[str, str]:
        return {CHANNEL_DB_MAP[key]: ",".join(str(c) for c in ids)
                for key, ids in self.channels.items() if key in CHANNEL_DB_MAP}


def _mentions(ids: List[int]) -> str:
    return ", ".join(f"<#{i}>" for i in ids)


class SetupView(discord.ui.View):
    def __init__(self, flow: "ChannelSetup") -> None:
        super().__init__(timeout=SETUP_TIMEOUT)
        self.flow = flow
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        state = self.flow.state
        if state.done:
            return

        select = discord.ui.ChannelSelect(
            placeholder="Pick the channel(s) for this step",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=MAX_CHANNELS_PER_STEP,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary,
                                 disabled=not state.can_go_back(), row=1)
        back.callback = self._on_back
        self.add_item(back)

        skip = discord.ui.Button(label="Keep current", style=discord.ButtonStyle.primary,
                                 disabled=not state.can_skip(), row=1)
        skip.callback = self._on_skip
        self.add_item(skip)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != DEVELOPER_ID:
            await interaction.response.send_message(**plain("Only the developer can use this setup."), ephemeral=True)
            return False
        return True

    async def _advance(self, interaction: discord.Interaction) -> None:
        self.flow.last_interaction = interaction
        if self.flow.state.done:
            await self.flow.finish(interaction)
            return
        self._rebuild()
        await interaction.response.edit_message(embed=self.flow.status_embed(), view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        select = next(c for c in self.children if isinstance(c, discord.ui.ChannelSelect))
        self.flow.state.select([c.id for c in select.values])
        await self._advance(interaction)

    async def _on_skip(self, interaction: discord.Interaction) -> None:
        self.flow.state.skip()
        await self._advance(interaction)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.flow.state.back()
        self.flow.last_interaction = interaction
        self._rebuild()
        await interaction.response.edit_message(embed=self.flow.status_embed(), view=self)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=create_embed(title="Setup cancelled", description="Nothing was saved."), view=None)

    async def on_timeout(self) -> None:
        interaction = self.flow.last_interaction
        if not interaction:
            return
        try:
            await interaction.edit_original_response(
                embed=create_embed(title="Setup timed out",
                                   description="Nothing was saved. Run `/setup` to start over."),
                view=None)
        except Exception:
            pass


class Setup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _is_developer(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == DEVELOPER_ID

    @app_commands.command(name="setup", description="Configure bot channels (developer only)")
    async def setup(self, interaction: discord.Interaction) -> None:
        if not self._is_developer(interaction):
            await interaction.response.send_message(**plain("Only the developer can run setup commands!"), ephemeral=True)
            logger.warning(f"Unauthorized setup attempt by {interaction.user.id}")
            return

        logger.info(f"Setup started by {interaction.user.id}")
        try:
            await ChannelSetup(interaction, self.bot).start()
        except Exception as e:
            logger.error(f"Setup failed: {e}", exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(**plain(f"Setup failed: {e}"), ephemeral=True)
                else:
                    await interaction.response.send_message(**plain(f"Setup failed: {e}"), ephemeral=True)
            except Exception:
                pass


class ChannelSetup:
    def __init__(self, interaction: discord.Interaction, bot: commands.Bot) -> None:
        self.interaction = interaction
        self.last_interaction = interaction
        self.bot = bot
        self.state = SetupState()

    async def start(self) -> None:
        guild = self.interaction.guild
        for key, _ in CHANNEL_PROMPTS:
            saved = await asyncio.to_thread(get_channel_ids, CHANNEL_DB_MAP[key]) or []
            alive = [cid for cid in saved if guild.get_channel(cid)]
            if alive:
                self.state.existing[key] = alive

        view = SetupView(self)
        await self.interaction.response.send_message(embed=self.status_embed(), view=view, ephemeral=True)

    def status_embed(self) -> discord.Embed:
        state = self.state
        embed = create_embed(
            title="Channel Configuration",
            description=f"Progress: {state.index}/{state.total}\n"
                        "*(Pick one or more channels from the dropdown below.)*",
        )

        done_lines = []
        for key, prompt in state.prompts[:state.index]:
            if key in state.channels:
                label = prompt.replace("Enter ", "").split(" (")[0]
                done_lines.append(f"{label}: {_mentions(state.channels[key])}")
        if done_lines:
            text = "\n".join(done_lines)
            if len(text) > MAX_FIELD_CHARS:
                text = "...\n" + text[-(MAX_FIELD_CHARS - 4):]
            embed.add_field(name="Completed", value=text, inline=False)

        if not state.done:
            value = state.current_prompt.replace("Enter ", "Select the ", 1)
            current = state.existing.get(state.current_key)
            if current:
                value += f"\nCurrently set to: {_mentions(current)}"
            embed.add_field(name=f"Step {state.index + 1}/{state.total}", value=value[:MAX_FIELD_CHARS], inline=False)
        return embed

    async def finish(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=create_embed(title="Saving...", description="Saving channels and posting the menus."), view=None)
        try:
            await self.save_channels()
            menus = await spawn_menus(self.bot)
            await interaction.edit_original_response(embed=self._completion_embed(menus), view=None)
        except db_errors.Error as e:
            logger.error(f"Database error saving channels: {e}", exc_info=True)
            await self._report_error(interaction, f"Database error: {e}")
        except Exception as e:
            logger.error(f"Error saving channels: {e}", exc_info=True)
            await self._report_error(interaction, str(e))

    async def _report_error(self, interaction: discord.Interaction, error: str) -> None:
        try:
            await interaction.edit_original_response(
                embed=create_embed(title="Setup error", description=error), view=None)
        except Exception:
            pass

    async def save_channels(self) -> None:
        rows = self.state.serialized()
        async with aget_db_connection() as conn:
            for db_key, value in rows.items():
                await conn.execute(
                    "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                    (db_key, value)
                )
            await conn.commit()
        logger.info(f"Saved {len(rows)} channel configurations")

    def _completion_embed(self, menus: dict) -> discord.Embed:
        channels = self.state.channels
        embed = create_embed(
            title="Setup Complete",
            description=f"Successfully configured {len(channels)} channel(s)"
        )
        guild = self.interaction.guild

        for key, field_name in (("reporting", "Report System"), ("open_ticket", "Ticket System"),
                                ("recruitment", "Recruitment")):
            if key not in channels:
                continue
            channel = guild.get_channel(channels[key][0])
            if not channel:
                continue
            if menus.get(key):
                embed.add_field(name=field_name, value=f"Enabled in {channel.mention}", inline=False)
            else:
                embed.add_field(
                    name=field_name,
                    value=f"Saved for {channel.mention}, but the menu could not be posted. Check the bot's permissions there.",
                    inline=False
                )

        if "shop_orders" in channels:
            channel = guild.get_channel(channels["shop_orders"][0])
            if channel:
                me = channel.permissions_for(guild.me)
                if me.view_channel and me.send_messages and me.embed_links:
                    value = f"New orders will be posted in {channel.mention}"
                else:
                    value = (f"Saved for {channel.mention}, but I need to see it, send messages "
                             f"and embed links there.")
                embed.add_field(name="Shop Orders", value=value, inline=False)

        if "backup" in channels:
            channel = guild.get_channel(channels["backup"][0])
            if channel:
                me = channel.permissions_for(guild.me)
                if me.view_channel and me.send_messages and me.attach_files:
                    value = f"Backups will be posted in {channel.mention}. Use `/backup` for one now."
                else:
                    value = (f"Saved for {channel.mention}, but I need to see it, send messages "
                             f"and attach files there.")
                embed.add_field(name="Backups", value=value, inline=False)
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Setup(bot))
