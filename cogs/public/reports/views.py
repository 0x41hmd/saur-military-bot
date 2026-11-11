from discord import Interaction, ui, ButtonStyle
import discord
import asyncio
import logging
from typing import TYPE_CHECKING

from database.db import aget_db_connection
from utils.helpers import plain
from utils.settings import (
    OPERATION_TYPES,
)
from .base import ReportState, SafeView, _has_manage_reports_permission
from .modals import ActivityInputModal, AgainstNameInputModal, ArrestsInputModal, AttemptsInputModal, DeleteIDModal, DurationInputModal, EditIDModal, EnemiesInputModal, NotesInputModal, ScreenshotInputModal
from .workflow import start_report_workflow

if TYPE_CHECKING:
    from .cog import Reports

logger = logging.getLogger(__name__)


class StepView(ui.View):
    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__(timeout=300)
        self.state                = state
        self.cog                  = cog
        self.original_interaction = original_interaction


class DurationView(StepView):
    @ui.button(label="Add Duration", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(DurationInputModal(self.state, self.cog, self.original_interaction))


class ActivityView(StepView):
    @ui.button(label="Add Activity", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(ActivityInputModal(self.state, self.cog, self.original_interaction))


class ArrestsView(StepView):
    @ui.button(label="Add Arrests", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(ArrestsInputModal(self.state, self.cog, self.original_interaction))


class NotesView(StepView):
    @ui.button(label="Add Notes", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(NotesInputModal(self.state, self.cog, self.original_interaction))

    @ui.button(label="Skip", style=ButtonStyle.success)
    async def skip_button(self, inter: Interaction, button: ui.Button):
        try:
            await inter.response.defer(ephemeral=True)
            self.state.step += 1
            await self.cog.show_step(inter, self.state)
        except Exception:
            logger.exception("Error in NotesView skip_button")


class EnemiesView(StepView):
    @ui.button(label="Add Enemies", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(EnemiesInputModal(self.state, self.cog, self.original_interaction))


class AgainstNameView(StepView):
    @ui.button(label="Add Name", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(AgainstNameInputModal(self.state, self.cog, self.original_interaction))


class AttemptsView(StepView):
    @ui.button(label="Add Attempts", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(AttemptsInputModal(self.state, self.cog, self.original_interaction))


class ScreenshotView(StepView):
    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction, submit_fn):
        super().__init__(state, cog, original_interaction)
        self.submit_fn = submit_fn

    @ui.button(label="Add Screenshot", style=ButtonStyle.secondary)
    async def add_button(self, inter: Interaction, button: ui.Button):
        await inter.response.send_modal(ScreenshotInputModal(self.state, self.cog, self.original_interaction))

    @ui.button(label="Submit Report", style=ButtonStyle.success)
    async def submit_button(self, inter: Interaction, button: ui.Button):
        try:
            await inter.response.defer(ephemeral=True)
            await self.submit_fn(inter, self.state)
        except Exception:
            logger.exception("Error in ScreenshotView submit_button")


class ExtraPointsApprovalView(ui.View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id
        self.approve_button.custom_id = f"extra_points_approve_{request_id}"
        self.decline_button.custom_id = f"extra_points_decline_{request_id}"

    async def _handle_approval(self, interaction: Interaction, approved: bool):
        if not (await asyncio.to_thread(_has_manage_reports_permission, interaction)):
            await interaction.response.send_message(**plain("You do not have permission to manage reports."), ephemeral=True)
            return
        cog = interaction.client.get_cog("Reports")
        if cog:
            await cog.handle_extra_points_approval(interaction, self.request_id, approved=approved)
        else:
            await interaction.response.send_message(**plain("Error: Cog not found"), ephemeral=True)

    @ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="extra_points_approve_btn")
    async def approve_button(self, interaction: Interaction, button: ui.Button):
        await self._handle_approval(interaction, approved=True)

    @ui.button(label="Decline", style=discord.ButtonStyle.red, custom_id="extra_points_decline_btn")
    async def decline_button(self, interaction: Interaction, button: ui.Button):
        await self._handle_approval(interaction, approved=False)


class ReportMenuView(SafeView):
    def __init__(self, cog, bot=None, is_af: bool = False):
        super().__init__(timeout=None)
        self._set_refs(cog=cog)
        self.is_af              = is_af
        self.bot                = bot
        self.selected_operation = None
        select = ui.Select(
            placeholder="Select an operation...",
            options=[discord.SelectOption(label=op_name, value=op_key) for op_key, op_name in OPERATION_TYPES.items()],
            custom_id="report_menu_operation_select",
            row=0,
        )
        select.callback = self.on_operation_select
        self.add_item(select)

    async def on_operation_select(self, interaction: discord.Interaction):
        self.selected_operation = interaction.data['values'][0]
        await interaction.response.defer()

    @ui.button(label="Create Report", style=discord.ButtonStyle.green, row=1, custom_id="report_menu_create_btn")
    async def create_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self.selected_operation:
            await interaction.response.send_message(**plain("Please select an operation first"), ephemeral=True)
            return
        if self.selected_operation.startswith("af_"):
            async with aget_db_connection() as conn:
                result = (await conn.execute("SELECT is_af FROM members WHERE discord_id = ?", (interaction.user.id,))).fetchone()
                if not (result and bool(result['is_af'])):
                    await interaction.response.send_message(**plain("You must be Air Force to submit AF operations."), ephemeral=True)
                    return
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            await start_report_workflow(interaction, self.selected_operation)
        except Exception:
            pass

    @ui.button(label="Edit Report", style=discord.ButtonStyle.gray, row=1, custom_id="report_menu_edit_btn")
    async def edit_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.selected_operation == "extra_points":
            await interaction.response.send_message(
                "Extra points requests cannot be edited. Please submit a new request.", ephemeral=True)
            return
        cog = self.get_cog() or interaction.client.get_cog("Reports")
        if not cog:
            await interaction.response.send_message(**plain("Reporting system not loaded"), ephemeral=True)
            return
        if not self.selected_operation:
            await interaction.response.send_message(**plain("Please select an operation first"), ephemeral=True)
            return
        await interaction.response.send_modal(EditIDModal(cog, interaction.user.id, self.selected_operation))

    @ui.button(label="Delete Report", style=discord.ButtonStyle.danger, row=1, custom_id="report_menu_delete_btn")
    async def delete_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.selected_operation == "extra_points":
            await interaction.response.send_message(
                "Extra points requests cannot be deleted via this menu. Please ask HQ to decline the request.", ephemeral=True)
            return
        cog = self.get_cog() or interaction.client.get_cog("Reports")
        if not cog:
            await interaction.response.send_message(**plain("Reporting system not loaded"), ephemeral=True)
            return
        if not self.selected_operation:
            await interaction.response.send_message(**plain("Please select an operation first"), ephemeral=True)
            return
        await interaction.response.send_modal(DeleteIDModal(cog, self.selected_operation))
