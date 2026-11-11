from discord import Interaction, ui, ButtonStyle, SelectOption
import discord
import asyncio
from typing import List
import logging
from typing import TYPE_CHECKING

from utils.settings import (
    RANK_ORDER,
    AF_RANK_ORDER,
)
from .base import ReportState, SafeView
from .constants import AF_OPS, _AF_RANK_INDEX, _RANK_INDEX

if TYPE_CHECKING:
    from .cog import Reports

logger = logging.getLogger(__name__)


class PaginatedSelect(SafeView):
    def __init__(self, members: List[dict], min_vals: int, max_vals: int, title: str,
                 state: ReportState, cog: 'Reports', interaction: Interaction,
                 pre_selected: List[int] = None, is_airforce_field: bool = False):
        super().__init__(timeout=600)
        self.min_vals          = min_vals
        self.max_vals          = max_vals
        self.title             = title
        self.current_page      = 0
        self.page_size         = 25
        self.total_pages       = max(1, (len(members) + self.page_size - 1) // self.page_size)
        self.selected:         List[int] = []
        if pre_selected and not state.edit_mode:
            for item in pre_selected:
                try:
                    self.selected.append(int(item) if isinstance(item, str) else item)
                except (ValueError, TypeError):
                    pass
        self._selection_made   = False
        self.state             = state
        self.is_airforce_field = is_airforce_field
        self._set_refs(cog=cog, original_interaction=interaction)
        self._sorted_members   = self._sort_members(members)
        self.update_select()

    def _sort_members(self, members: List[dict]) -> List[dict]:
        is_af_op = self.state.operation_type in AF_OPS
        def key(m):
            if is_af_op:
                af_rank = m.get('af_rank', 'None')
                return _AF_RANK_INDEX.get(af_rank, len(AF_RANK_ORDER)) if af_rank and af_rank != 'None' else len(AF_RANK_ORDER)
            return _RANK_INDEX.get(m.get('rank', 'Cadet'), len(RANK_ORDER))
        return sorted(members, key=key)

    def _get_rank_display(self, member, is_af_op: bool) -> str:
        if self.is_airforce_field or is_af_op:
            af_rank = member.get('af_rank', 'None')
            if member.get('is_af', 0) and af_rank and af_rank != 'None':
                return af_rank
        return member.get('rank', 'Cadet')

    def update_select(self):
        for item in list(self.children):
            if isinstance(item, ui.Select):
                self.remove_item(item)

        start        = self.current_page * self.page_size
        page_members = self._sorted_members[start:start + self.page_size]
        is_af_op     = self.state.operation_type in AF_OPS

        selected_ids = {str(s) for s in self.selected}
        options = [
            SelectOption(
                label=m.get('ig_name', 'Unknown'),
                value=str(int(m['discord_id']) if isinstance(m['discord_id'], str) else m['discord_id']),
                description=self._get_rank_display(m, is_af_op),
                default=str(m['discord_id']) in selected_ids,
            )
            for m in page_members
        ]

        page_info = f"Page {self.current_page + 1}/{self.total_pages}" if self.total_pages > 1 else ""
        if self.selected and self.total_pages > 1:
            page_info = f"{page_info} ({len(self.selected)} selected)"
        max_vals  = max(1, min(self.max_vals, len(options))) if options else 1
        select    = ui.Select(
            placeholder=f"{self.title} {page_info}".strip(),
            min_values=min(self.min_vals, max_vals),
            max_values=max_vals,
            options=options if options else [SelectOption(label="No members available", value="none")],
        )
        select.callback = self.on_select
        self.add_item(select)
        self.update_buttons()

    async def on_select(self, interaction: Interaction):
        start            = self.current_page * self.page_size
        page_members     = self._sorted_members[start:start + self.page_size]
        current_page_ids = {str(m['discord_id']) for m in page_members}
        if self.max_vals == 1:
            kept = []
        else:
            kept = [s for s in self.selected if str(s) not in current_page_ids]
        for val in interaction.data['values']:
            if val != "none":
                try:
                    kept.append(int(val))
                except (ValueError, TypeError):
                    pass
        self.selected        = kept
        self._selection_made = True
        try:
            await interaction.response.defer()
        except discord.errors.InteractionResponded:
            pass

    @ui.button(label="Previous", style=ButtonStyle.gray, row=1)
    async def prev(self, interaction: Interaction, button: ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            (await asyncio.to_thread(self.update_select))
            try:
                await interaction.response.edit_message(view=self)
            except discord.errors.InteractionResponded:
                await interaction.followup.send(view=self, ephemeral=True)
            except Exception:
                pass

    @ui.button(label="Next", style=ButtonStyle.gray, row=1)
    async def next(self, interaction: Interaction, button: ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            (await asyncio.to_thread(self.update_select))
            try:
                await interaction.response.edit_message(view=self)
            except discord.errors.InteractionResponded:
                await interaction.followup.send(view=self, ephemeral=True)
            except Exception:
                pass

    @ui.button(label="Done", style=ButtonStyle.success, row=2)
    async def submit(self, interaction: Interaction, button: ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        filtered = [] if (self.state.edit_mode and not self._selection_made) \
                   else [s for s in self.selected if s != "none"]

        if   self.title == "Leaders":      self.state.leaders      = filtered
        elif self.title == "Pilot":        self.state.af_pilot     = filtered[0] if filtered else None
        elif self.title == "Air Force":    self.state.airforce     = filtered
        elif self.title == "Participants": self.state.participants  = filtered

        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")

    def update_buttons(self):
        self.prev.disabled = self.current_page == 0
        self.next.disabled = self.current_page >= self.total_pages - 1


class ResultSelect(ui.View):
    def __init__(self, options: List[str], default_value: str = None):
        super().__init__(timeout=300)
        self.callback       = None
        self.selected_value = default_value
        select = ui.Select(
            placeholder="Select result...",
            options=[SelectOption(label=v, value=v, default=(default_value == v)) for v in options],
        )
        select.callback = self.on_select
        self.add_item(select)
        done = ui.Button(label="Done", style=discord.ButtonStyle.green)
        done.callback = self.on_done
        self.add_item(done)

    async def on_select(self, interaction: Interaction):
        self.selected_value = interaction.data['values'][0]
        await interaction.response.defer()

    async def on_done(self, interaction: Interaction):
        try:
            await interaction.response.defer()
            if self.callback and self.selected_value:
                await self.callback(self.selected_value)
        except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError, discord.errors.InteractionResponded):
            pass


class ATLevelSelect(ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.callback       = None
        self.selected_value = None
        select = ui.Select(
            placeholder="Select AT Level...",
            options=[SelectOption(label=f"Level {i}", value=str(i)) for i in range(1, 4)],
        )
        select.callback = self.on_select
        self.add_item(select)
        done = ui.Button(label="Done", style=discord.ButtonStyle.green)
        done.callback = self.on_done
        self.add_item(done)

    async def on_select(self, interaction: Interaction):
        self.selected_value = interaction.data['values'][0]
        await interaction.response.defer()

    async def on_done(self, interaction: Interaction):
        try:
            await interaction.response.defer()
            if self.callback and self.selected_value:
                await self.callback(self.selected_value)
        except Exception:
            pass
