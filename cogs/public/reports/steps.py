from discord import Interaction, Embed, ui, ButtonStyle
import discord
import asyncio
import logging

from utils.helpers import create_embed
from utils.settings import (
    AF_RANK_TAG_MAP,
    AF_RANK_ORDER, OPERATION_TYPES,
)
from .base import ReportState
from .constants import AF_OPS, AF_WZ_RESULTS, STANDARD_RESULTS, WARZONE_RESULTS, _AF_RANK_INDEX, _sort_members_by_rank, escape_markdown
from .selects import ATLevelSelect, PaginatedSelect, ResultSelect
from .views import ActivityView, AgainstNameView, ArrestsView, AttemptsView, DurationView, EnemiesView, NotesView, ScreenshotView

logger = logging.getLogger(__name__)


def _bullet_list(names, limit: int = 1024) -> str:
    names = list(names)
    if not names:
        return "None"
    lines, size = [], 0
    for i, name in enumerate(names):
        line = f"• {name}"
        remaining = len(names) - i
        if size + len(line) + 1 > limit - 20 and remaining > 1:
            lines.append(f"... +{remaining} more")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


class StepsMixin:
    async def show_step(self, interaction: Interaction, state: ReportState):
        workflow = self._OP_WORKFLOWS.get(state.operation_type, [])
        if not workflow or state.step > len(workflow):
            await self.submit_report(interaction, state)
            return
        await workflow[state.step - 1](interaction, state)

    async def _update_or_send_step(self, interaction: Interaction, state: ReportState, embed: Embed, view: ui.View):
        if state.message is None:
            try:
                state.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            except Exception as e:
                logger.error(f"Failed to send first message: {e}", exc_info=True)
        else:
            try:
                await state.message.edit(embed=embed, view=view)
            except Exception as e:
                logger.error(f"Failed to edit message: {e}", exc_info=True)

    def _make_step_embed(self, state: ReportState, description: str) -> Embed:
        title = f"{OPERATION_TYPES[state.operation_type]} - Step {state.step}"
        if state.edit_mode:
            title = f"**Editing:** {title}"
        return create_embed(title=title, description=description, thumbnail=False)

    async def step_select_leaders(self, interaction: Interaction, state: ReportState):
        embed       = self._make_step_embed(state, "Select **Leaders**")
        member_dict = None
        if state.leaders or (state.edit_mode and state.old_leaders):
            member_dict = self._get_or_build_member_dict(state)
        if state.leaders and member_dict:
            is_af_op = state.operation_type in AF_OPS
            names    = _sort_members_by_rank(state.leaders, member_dict, use_af_rank=is_af_op)
            embed.add_field(name="Selected Leaders", value=_bullet_list(names), inline=False)
        if state.edit_mode and state.old_leaders and member_dict:
            names = _sort_members_by_rank(state.old_leaders, member_dict)
            embed.add_field(name=f"Previous Leaders ({len(state.old_leaders)})", value=_bullet_list(names), inline=False)
        pre_selected     = state.old_leaders if state.edit_mode else []
        filtered_members = [m for m in state.all_members if m.get('is_af', 0) == 1] \
                           if state.operation_type in AF_OPS else state.all_members
        view = PaginatedSelect(filtered_members, 0, 25, "Leaders", state, self, interaction, pre_selected)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_airforce(self, interaction: Interaction, state: ReportState):
        embed       = self._make_step_embed(state, "Select **Air Force Members**")
        member_dict = None
        if state.airforce or (state.edit_mode and state.old_airforce):
            member_dict = self._get_or_build_member_dict(state)
        if state.leaders:
            embed.add_field(name="Leaders", value=f"{len(state.leaders)} selected", inline=True)
        if state.airforce and member_dict:
            af_entries = []
            for aid in state.airforce:
                member = member_dict.get(aid)
                if not member:
                    continue
                af_rank  = member.get('af_rank', 'None')
                sort_key = _AF_RANK_INDEX.get(af_rank, len(AF_RANK_ORDER)) if af_rank and af_rank != 'None' else len(AF_RANK_ORDER)
                if af_rank and af_rank != 'None':
                    tag = AF_RANK_TAG_MAP.get(af_rank, af_rank).replace('*', '\\*')
                    af_entries.append((sort_key, f"{tag}-{escape_markdown(member.get('ig_name'))}"))
                else:
                    af_entries.append((sort_key, escape_markdown(member.get('ig_name'))))
            af_entries.sort(key=lambda x: x[0])
            embed.add_field(name="Selected Air Force", value=_bullet_list(n for _, n in af_entries), inline=False)
        if state.edit_mode and state.old_airforce and member_dict:
            names = _sort_members_by_rank(state.old_airforce, member_dict, use_af_rank=True)
            embed.add_field(name=f"Previous Air Force ({len(state.old_airforce)})", value=_bullet_list(names), inline=False)
        pre_selected     = list(set((state.old_airforce or []) + (state.old_leaders or []))) if state.edit_mode else []
        filtered_members = [m for m in state.all_members if m.get('is_af', 0) == 1]
        view = PaginatedSelect(filtered_members, 0, 25, "Air Force", state, self, interaction, pre_selected, is_airforce_field=True)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_participants(self, interaction: Interaction, state: ReportState):
        embed        = self._make_step_embed(state, "Select **Participants**")
        pre_selected = state.old_participants if state.edit_mode else []
        member_dict  = None
        if state.edit_mode and state.old_participants:
            member_dict = self._get_or_build_member_dict(state)
            names = _sort_members_by_rank(state.old_participants, member_dict)
            embed.add_field(name=f"Previous Participants ({len(state.old_participants)})", value=_bullet_list(names), inline=False)
        elif state.edit_mode and state.participants:
            member_dict = self._get_or_build_member_dict(state)
            names = _sort_members_by_rank(state.participants, member_dict)
            embed.add_field(name="Selected Participants", value=_bullet_list(names), inline=False)
        if state.leaders:
            embed.add_field(name="Leaders",   value=f"{len(state.leaders)} selected",   inline=True)
        if state.airforce:
            embed.add_field(name="Air Force", value=f"{len(state.airforce)} selected",  inline=True)
        excluded_ids     = {str(x) for x in state.leaders} | {str(x) for x in state.airforce}
        filtered_members = [m for m in state.all_members if str(m['discord_id']) not in excluded_ids]
        if state.operation_type in AF_OPS:
            filtered_members = [m for m in filtered_members if m.get('is_af', 0) == 1]
        view = PaginatedSelect(filtered_members, 0, 25, "Participants", state, self, interaction, pre_selected)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_participants_extra(self, interaction: Interaction, state: ReportState):
        embed       = self._make_step_embed(state, "Select **Participants**")
        member_dict = None
        if state.participants or (state.edit_mode and state.old_participants):
            member_dict = self._get_or_build_member_dict(state)
        if state.participants and member_dict:
            names = _sort_members_by_rank(state.participants, member_dict)
            embed.add_field(name=f"Current Participants ({len(state.participants)})", value=_bullet_list(names), inline=False)
        if state.edit_mode and state.old_participants and member_dict:
            names = _sort_members_by_rank(state.old_participants, member_dict)
            embed.add_field(name=f"Previous Participants ({len(state.old_participants)})", value=_bullet_list(names), inline=False)
        pre_selected = state.old_participants if state.edit_mode else []
        view = PaginatedSelect(state.all_members, 0, 25, "Participants", state, self, interaction, pre_selected)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_af_pilot(self, interaction: Interaction, state: ReportState):
        embed       = self._make_step_embed(state, "Select **Air Force Pilot** (Required)")
        member_dict = self._get_or_build_member_dict(state)
        if state.af_pilot:
            pilot_uid = int(state.af_pilot) if not isinstance(state.af_pilot, int) else state.af_pilot
            embed.add_field(name="Selected Pilot", value=member_dict.get(pilot_uid, {}).get('ig_name', 'Unknown'), inline=False)
        if state.edit_mode and state.old_af_pilot:
            pilot_uid = int(state.old_af_pilot) if not isinstance(state.old_af_pilot, int) else state.old_af_pilot
            embed.add_field(name="Previous Pilot", value=member_dict.get(pilot_uid, {}).get('ig_name', 'Unknown'), inline=False)
            pre_selected = [state.old_af_pilot]
        elif state.af_pilot:
            pre_selected = [state.af_pilot]
        else:
            pre_selected = []
        af_only_members = [m for m in state.all_members if m.get('is_af', 0) == 1]
        view = PaginatedSelect(af_only_members, 1, 1, "Pilot", state, self, interaction, pre_selected)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_result(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Select **Result**")
        if state.result:
            embed.add_field(
                name="Previous Result" if state.edit_mode else "Selected Result",
                value=state.result, inline=False)
        options = WARZONE_RESULTS if state.operation_type == "warzone" else STANDARD_RESULTS
        view    = ResultSelect(options, default_value=state.result)
        view.callback = lambda selected: self.on_result_selected(interaction, state, selected)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_result_af_warzone(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Select **Result**")
        if state.result:
            embed.add_field(
                name="Previous Result" if state.edit_mode else "Selected Result",
                value=state.result, inline=False)
        view  = ResultSelect(AF_WZ_RESULTS, default_value=state.result)
        view.callback = lambda result: self.on_result_selected(interaction, state, result)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_select_at_level(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Select **AT Level**")
        if state.at_level:
            embed.add_field(
                name="Previous AT Level" if state.edit_mode else "Selected AT Level",
                value=f"Level {state.at_level}", inline=False)
        view  = ATLevelSelect()
        view.callback = lambda level: self.on_at_level_selected(interaction, state, level)
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_add_duration(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "How long did the operation take?")
        if state.duration:
            embed.add_field(name="Duration", value=state.duration, inline=False)
        await self._update_or_send_step(interaction, state, embed, DurationView(state, self, interaction))

    async def step_select_activity(self, interaction: Interaction, state: ReportState):
        if state.operation_type in ("training", "af_training"):
            description, field_name = "What did you do in the training?", "Activity"
        else:
            description, field_name = "What action was taken?", "Action"
        embed = self._make_step_embed(state, description)
        if state.activity:
            embed.add_field(name=field_name, value=state.activity, inline=False)
        await self._update_or_send_step(interaction, state, embed, ActivityView(state, self, interaction))

    async def step_add_arrests(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "How many arrests were made?")
        if state.arrests:
            embed.add_field(name="Arrests", value=str(state.arrests), inline=False)
        await self._update_or_send_step(interaction, state, embed, ArrestsView(state, self, interaction))

    async def step_add_notes(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Add **Notes**? (Optional)")
        if state.notes:
            embed.add_field(name="Notes", value=state.notes[:100] + ("..." if len(state.notes) > 100 else ""), inline=False)
        await self._update_or_send_step(interaction, state, embed, NotesView(state, self, interaction))

    async def step_add_enemies(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Who were your **Enemies**?")
        if state.enemies:
            embed.add_field(name="Enemies", value=", ".join(state.enemies), inline=False)
        await self._update_or_send_step(interaction, state, embed, EnemiesView(state, self, interaction))

    async def step_select_spawn_type(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "What did you **Spawn**?")
        if state.spawn_type:
            embed.add_field(name="Selected Spawn", value=state.spawn_type, inline=False)
        view = ui.View(timeout=300)
        for spawn_name in ["Hydra", "Hunter"]:
            async def spawn_callback(inter: Interaction, name=spawn_name):
                await inter.response.defer()
                state.spawn_type  = name
                state.step       += 1
                await self.show_step(inter, state)
            btn          = ui.Button(label=spawn_name, style=ButtonStyle.secondary)
            btn.callback = spawn_callback
            (await asyncio.to_thread(view.add_item, btn))
        await self._update_or_send_step(interaction, state, embed, view)

    async def step_add_against_name(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Who did you fight **Against**?")
        if state.against_name:
            embed.add_field(name="Against", value=state.against_name, inline=False)
        await self._update_or_send_step(interaction, state, embed, AgainstNameView(state, self, interaction))

    async def step_add_attempts(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "How many **Attempts** to take down?")
        if state.attempts:
            embed.add_field(name="Attempts", value=str(state.attempts), inline=False)
        await self._update_or_send_step(interaction, state, embed, AttemptsView(state, self, interaction))

    async def step_add_screenshot(self, interaction: Interaction, state: ReportState):
        embed = self._make_step_embed(state, "Add **Screenshot URL**? (Optional)")
        if state.screenshot:
            embed.add_field(
                name="Screenshot URL",
                value=state.screenshot[:100] + ("..." if len(state.screenshot) > 100 else ""),
                inline=False)
            if state.screenshot.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                embed.set_image(url=state.screenshot)
        submit_fn = self.submit_report_to_hq if state.operation_type == "extra_points" else self.submit_report
        await self._update_or_send_step(interaction, state, embed,
                                        ScreenshotView(state, self, interaction, submit_fn=submit_fn))

    async def on_result_selected(self, interaction: Interaction, state: ReportState, result: str):
        if state.operation_type == "warzone" and result not in WARZONE_RESULTS:
            try:
                await interaction.followup.send(
                    f"Invalid warzone result. Valid options are: {', '.join(WARZONE_RESULTS)}", ephemeral=True)
            except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                pass
            return
        state.result  = result
        state.step   += 1
        try:
            await self.show_step(interaction, state)
        except Exception:
            logger.exception("Error in on_result_selected show_step")

    async def on_at_level_selected(self, interaction: Interaction, state: ReportState, level: str):
        state.at_level  = int(level)
        state.step     += 1
        try:
            await self.show_step(interaction, state)
        except Exception:
            logger.exception("Error in on_at_level_selected show_step")
