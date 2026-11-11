from discord import Interaction, ui, TextStyle
import discord
import asyncio
import logging
from typing import TYPE_CHECKING

from database.db import aget_db_connection
from utils.helpers import get_channel_safe, plain
from utils.settings import (
    OPERATION_TYPES,
    OPERATION_CHANNEL_MAP,
)
from .base import ReportState, SafeModal, _has_manage_reports_permission
from .constants import OPERATION_REQUIREMENTS

if TYPE_CHECKING:
    from .cog import Reports

logger = logging.getLogger(__name__)


class NotesInputModal(SafeModal, title="Add Notes"):
    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)
        self.notes_input = ui.TextInput(
            label="Notes", style=TextStyle.paragraph,
            required=False, max_length=1000, placeholder="Enter notes (optional)",
        )
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        self.state.notes = self.notes_input.value or None
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class ScreenshotInputModal(SafeModal, title="Add Screenshot URL"):
    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)
        self.url_input = ui.TextInput(
            label="Screenshot URL", placeholder="https://...",
            required=False, max_length=500,
        )
        self.add_item(self.url_input)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        self.state.screenshot = self.url_input.value or None
        cog = self.get_cog()
        if self.state.operation_type == "extra_points":
            if cog:
                try:
                    await cog.submit_report_to_hq(interaction, self.state)
                except Exception:
                    logger.exception("Error in submit_report_to_hq from ScreenshotInputModal")
        else:
            self.state.step += 1
            if cog:
                try:
                    await cog.show_step(interaction, self.state)
                except Exception:
                    logger.exception("Error advancing to next step from ScreenshotInputModal")


class DurationInputModal(SafeModal, title="Add Duration"):
    duration_input = ui.TextInput(
        label="Duration", placeholder="e.g., 30 minutes, 1 hour 15 min",
        required=False, max_length=100,
    )

    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        self.state.duration = self.duration_input.value or None
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class ActivityInputModal(SafeModal, title="Add Activity"):
    activity_input = ui.TextInput(
        label="Activity", placeholder="What did you do?",
        style=TextStyle.paragraph, required=False, max_length=2000,
    )

    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        self.state.activity = self.activity_input.value or None
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class EnemiesInputModal(SafeModal, title="Add Enemies"):
    enemies_input = ui.TextInput(
        label="Enemies", placeholder="Enter enemy names separated by commas",
        style=TextStyle.paragraph, required=False, max_length=500,
    )

    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        self.state.enemies = (
            [e.strip() for e in self.enemies_input.value.split(",") if e.strip()]
            if self.enemies_input.value else []
        )
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class AgainstNameInputModal(SafeModal, title="Add Enemy Name"):
    against_input = ui.TextInput(
        label="Enemy Name", placeholder="Enter the name of the enemy",
        style=TextStyle.short, required=False, max_length=100,
    )

    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        self.state.against_name = self.against_input.value or None
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class AttemptsInputModal(SafeModal, title="Add Attempts"):
    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)
        self.attempts_input = ui.TextInput(
            label="Number of Attempts", placeholder="Enter number of attempts (e.g. 3)",
            style=TextStyle.short, required=False, max_length=2,
        )
        self.add_item(self.attempts_input)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        if self.attempts_input.value:
            try:
                attempts = int(self.attempts_input.value)
                self.state.attempts = str(attempts) if 0 <= attempts <= 99 else None
            except ValueError:
                self.state.attempts = None
        else:
            self.state.attempts = None
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class ArrestsInputModal(SafeModal, title="Add Arrests"):
    arrests_input = ui.TextInput(
        label="Number of Arrests", placeholder="Enter number of arrests (e.g. 5)",
        style=TextStyle.short, required=False, max_length=10,
    )

    def __init__(self, state: ReportState, cog: 'Reports', original_interaction: Interaction):
        super().__init__()
        self.state = state
        self._set_refs(cog=cog, original_interaction=original_interaction)

    async def on_submit(self, interaction: Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        if self.arrests_input.value:
            try:
                self.state.arrests = str(int(self.arrests_input.value))
            except ValueError:
                self.state.arrests = None
        else:
            self.state.arrests = None
        self.state.step += 1
        cog = self.get_cog()
        if cog:
            try:
                await cog.show_step(interaction, self.state)
            except Exception:
                logger.exception("Error advancing to next step")


class EditIDModal(SafeModal, title="Edit Report"):
    report_id_input = ui.TextInput(
        label="Operation Report ID", placeholder="Enter the operation ID",
        required=True, max_length=10,
    )

    def __init__(self, cog, submitter_id: int, selected_operation: str):
        super().__init__()
        self._set_refs(cog=cog)
        self.submitter_id       = submitter_id
        self.selected_operation = selected_operation

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            op_id = int(self.report_id_input.value)
            async with aget_db_connection() as conn:
                report = (await conn.execute(
                    """SELECT r.*,
                              COALESCE(GROUP_CONCAT(DISTINCT CASE WHEN op.was_leader = 1 THEN op.discord_id END), '') as leaders,
                              COALESCE(GROUP_CONCAT(DISTINCT CASE WHEN op.was_leader = 0 AND op.was_airforce = 0 THEN op.discord_id END), '') as participants,
                              COALESCE(GROUP_CONCAT(DISTINCT CASE WHEN op.was_airforce = 1 THEN op.discord_id END), '') as airforce
                       FROM reports r
                       LEFT JOIN operation_participants op ON r.id = op.report_id
                       WHERE r.operation_report_id = ? AND r.report_type = ?
                       GROUP BY r.id""",
                    (op_id, self.selected_operation),
                )).fetchone()
                if not report:
                    await interaction.followup.send(**plain("Report not found."), ephemeral=True)
                    return
                report_dict = dict(report)
                is_reporter = str(interaction.user.id) == str(report_dict['reporter_id'])
                if not (is_reporter or (await asyncio.to_thread(_has_manage_reports_permission, interaction))):
                    await interaction.followup.send(
                        "You can only edit your own reports or if you have manage_reports permission.", ephemeral=True)
                    return
                state                     = ReportState(self.selected_operation, report_dict['reporter_id'])
                state.date_time           = report_dict['date_time']
                state.duration            = report_dict.get('duration')
                state.result              = report_dict['result']
                state.notes               = report_dict['notes']
                state.screenshot          = report_dict['screenshot']
                state.at_level            = report_dict['at_level']
                state.activity            = report_dict.get('activity')
                state.reason              = report_dict['reason']
                state.arrests             = report_dict.get('arrests')
                state.enemies             = report_dict.get('enemies').split(',') if report_dict.get('enemies') else []
                state.operation_report_id = op_id
                state.edit_mode           = True
                state.original_report_id  = report_dict['id']
                state.old_leaders         = [int(u.strip()) for u in report_dict['leaders'].split(',')      if u.strip()] if report_dict['leaders']      else []
                state.old_participants    = [int(u.strip()) for u in report_dict['participants'].split(',')  if u.strip()] if report_dict['participants']  else []
                state.old_airforce        = [int(u.strip()) for u in report_dict['airforce'].split(',')     if u.strip()] if report_dict['airforce']      else []
                state.old_af_pilot        = int(report_dict['af_pilot']) if report_dict['af_pilot'] else None
                members = (await conn.execute(
                    "SELECT discord_id, discord_name, ig_name, rank, is_af, af_rank FROM members ORDER BY discord_name"
                )).fetchall()
                state.all_members = [{**dict(m), 'discord_id': int(dict(m)['discord_id'])} for m in members]
            cog = self.get_cog()
            if cog:
                cog._set_state(self.submitter_id, state)
                await cog.show_step(interaction, state)
        except ValueError:
            await interaction.followup.send(**plain("Please enter a valid number."), ephemeral=True)
        except Exception as e:
            logger.error(f"Error fetching report for edit: {e}")
            await interaction.followup.send(**plain(f"Error: {str(e)}"), ephemeral=True)


class DeleteIDModal(SafeModal, title="Delete Report"):
    report_id_input = ui.TextInput(
        label="Operation Report ID", placeholder="Enter the operation ID to delete",
        required=True, max_length=10,
    )

    def __init__(self, cog, selected_operation: str):
        super().__init__()
        self._set_refs(cog=cog)
        self.selected_operation = selected_operation

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            op_id = int(self.report_id_input.value)
            async with aget_db_connection() as conn:
                report = (await conn.execute(
                    "SELECT id, reporter_id, message_id FROM reports WHERE operation_report_id = ? AND report_type = ?",
                    (op_id, self.selected_operation),
                )).fetchone()
                if not report:
                    await interaction.followup.send(**plain("Report not found."), ephemeral=True)
                    return
                is_reporter = str(interaction.user.id) == str(report['reporter_id'])
                if not (is_reporter or (await asyncio.to_thread(_has_manage_reports_permission, interaction))):
                    await interaction.followup.send(
                        "You can only delete your own reports or if you have manage_reports permission.", ephemeral=True)
                    return
                logs_cog = interaction.client.get_cog("Logs")
                if logs_cog:
                    await logs_cog.log_action(
                        action="Report Deleted",
                        description=f"{self.selected_operation.replace('_', ' ').title()} operation report (#{op_id}) has been deleted",
                        user=interaction.user,
                    )
                db_id        = report['id']
                message_id   = report['message_id']
                participants = (await conn.execute(
                    "SELECT discord_id, promo_points_gained, af_points_gained, leadership_points_gained, participant_rank "
                    "FROM operation_participants WHERE report_id = ?",
                    (db_id,),
                )).fetchall()
                valid_op_columns = set(OPERATION_REQUIREMENTS.keys())
                op_col = self.selected_operation if self.selected_operation in valid_op_columns else None
                for p in participants:
                    if op_col:
                        (await conn.execute(
                            f"UPDATE rank_stats SET promo_points = MAX(0, promo_points - ?), af_points = MAX(0, af_points - ?), "
                            f"leadership_points = MAX(0, leadership_points - ?), {op_col} = MAX(0, {op_col} - 1) "
                            f"WHERE discord_id = ? AND rank = ?",
                            (p['promo_points_gained'] or 0, p['af_points_gained'] or 0,
                             p['leadership_points_gained'] or 0, p['discord_id'], p['participant_rank']),
                        ))
                    else:
                        (await conn.execute(
                            "UPDATE rank_stats SET promo_points = MAX(0, promo_points - ?), af_points = MAX(0, af_points - ?), "
                            "leadership_points = MAX(0, leadership_points - ?) WHERE discord_id = ? AND rank = ?",
                            (p['promo_points_gained'] or 0, p['af_points_gained'] or 0,
                             p['leadership_points_gained'] or 0, p['discord_id'], p['participant_rank']),
                        ))
                (await conn.execute("DELETE FROM operation_participants WHERE report_id = ?", (db_id,)))
                (await conn.execute("DELETE FROM reports WHERE id = ?",                       (db_id,)))
                (await conn.commit())
                if message_id:
                    try:
                        channel_key = OPERATION_CHANNEL_MAP.get(self.selected_operation, "operation_reports")
                        channel     = await get_channel_safe(interaction.client, channel_key)
                        if channel:
                            await (await channel.fetch_message(int(message_id))).delete()
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"Could not delete embed message: {e}")
                await interaction.followup.send(
                    f"Report Deleted\nOperation Report ID: **{op_id}**\n"
                    f"Type: **{OPERATION_TYPES[self.selected_operation]}**\n"
                    f"Points reversed for **{len(participants)} participants**",
                    ephemeral=True,
                )
        except ValueError:
            await interaction.followup.send(**plain("Please enter a valid number."), ephemeral=True)
        except Exception as e:
            logger.error(f"Error deleting report: {e}")
            await interaction.followup.send(**plain(f"Error deleting report: {str(e)}"), ephemeral=True)
