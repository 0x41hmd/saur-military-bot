from discord.ext import commands, tasks
from discord import Interaction
import discord
import time
import asyncio
import logging

from database.db import aget_db_connection
from database.db import get_db_connection, get_channel_ids
from utils.helpers import plain
from utils.settings import (
    RANK_TAG_MAP,
    AF_RANK_TAG_MAP, RANK_ORDER, AF_RANK_ORDER,
)
from .base import ReportState
from .constants import EXTRA_POINTS_VALUE, _AF_RANK_INDEX, _RANK_INDEX, escape_markdown
from .steps import StepsMixin
from .submit import SubmitMixin
from .views import ExtraPointsApprovalView

logger = logging.getLogger(__name__)


class Reports(StepsMixin, SubmitMixin, commands.Cog):
    def __init__(self, bot):
        self.bot    = bot
        self._states: dict = {}
        self.STATE_TTL               = 1800
        self.report_cooldowns        = {}
        self.edit_cooldowns          = {}
        self._cooldown_lock          = asyncio.Lock()
        self._report_submit_cooldown = 60
        self._report_edit_cooldown   = 300
        try:
            channel_ids          = get_channel_ids('channel_reporting')
            self.allowed_channel = channel_ids[0] if channel_ids else None
        except Exception as e:
            logger.warning(f"Failed to get reporting channel: {e}")
            self.allowed_channel = None
        self.cleanup_states.start()
        try:
            self._validate_schema()
        except Exception as e:
            logger.warning(f"Schema validation warning: {e}")
        self._OP_WORKFLOWS = {
            "bank_ops":              [self.step_select_leaders, self.step_select_participants, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "casino_ops":            [self.step_select_leaders, self.step_select_participants, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "securicar_ops":         [self.step_select_leaders, self.step_select_participants, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "assault_ops":           [self.step_select_leaders, self.step_select_participants, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "doomsday":              [self.step_select_leaders, self.step_select_participants, self.step_select_result, self.step_add_duration, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "andromada_ops":         [self.step_select_leaders, self.step_select_airforce, self.step_select_participants, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "andromada_terrors_ops": [self.step_select_leaders, self.step_select_airforce, self.step_select_participants, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "warzone":               [self.step_select_leaders, self.step_select_airforce, self.step_select_participants, self.step_add_duration, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "patrol":                [self.step_select_leaders, self.step_select_participants, self.step_add_duration, self.step_add_arrests, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "ammo_transport":        [self.step_select_leaders, self.step_select_airforce, self.step_select_participants, self.step_select_at_level, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "training":              [self.step_select_leaders, self.step_select_activity, self.step_select_participants, self.step_add_duration, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "af_warzone":            [self.step_select_leaders, self.step_select_participants, self.step_add_enemies, self.step_add_duration, self.step_select_result_af_warzone, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "af_training":           [self.step_select_leaders, self.step_select_activity, self.step_select_participants, self.step_add_duration, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "af_counter":            [self.step_select_af_pilot, self.step_select_spawn_type, self.step_add_against_name, self.step_add_attempts, self.step_select_result, self.step_add_notes, self.step_add_screenshot, self.submit_report],
            "extra_points":          [self.step_select_participants_extra, self.step_select_activity, self.step_add_screenshot],
        }

    @tasks.loop(minutes=5)
    async def cleanup_states(self):
        try:
            now     = time.time()
            expired = [uid for uid, (_, ts) in list(self._states.items()) if now - ts > self.STATE_TTL]
            for uid in expired:
                self._states.pop(uid, None)
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired report states")
        except Exception as e:
            logger.error(f"Error in cleanup_states: {e}", exc_info=True)

    @cleanup_states.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        self.cleanup_states.cancel()
        self._states.clear()

    async def restore_approval_views(self):
        try:
            async with aget_db_connection() as conn:
                pending = (await conn.execute(
                    "SELECT id FROM extra_points_requests WHERE status = 'pending'"
                )).fetchall()
            for row in pending:
                self.bot.add_view(ExtraPointsApprovalView(row['id']))
            if pending:
                logger.info(f"Restored {len(pending)} pending ExtraPointsApprovalView(s)")
        except Exception as e:
            logger.error(f"Failed to restore approval views: {e}", exc_info=True)

    def _set_state(self, user_id: int, state: ReportState):
        self._states[user_id] = (state, time.time())

    def _validate_schema(self):
        with get_db_connection() as conn:
            tables  = [r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()").fetchall()]
            missing = [t for t in ['reports', 'operation_participants', 'members', 'rank_stats'] if t not in tables]
            if missing:
                raise RuntimeError(f"Missing required tables: {missing}")

    @staticmethod
    def _build_member_dict(members_list):
        return {
            int(m['discord_id']) if isinstance(m['discord_id'], str) else m['discord_id']: m
            for m in (members_list or [])
        }

    @staticmethod
    def _get_or_build_member_dict(state: 'ReportState') -> dict:
        if state._member_dict is None:
            state._member_dict = Reports._build_member_dict(state.all_members)
        return state._member_dict

    @staticmethod
    def _get_rank_info(member, is_af_op):
        if is_af_op:
            af_rank = member.get('af_rank', 'None')
            if af_rank and af_rank != 'None':
                return AF_RANK_TAG_MAP.get(af_rank, af_rank), _AF_RANK_INDEX.get(af_rank, len(AF_RANK_ORDER))
            return None, len(AF_RANK_ORDER)
        rank = member.get('rank', 'Cadet')
        return RANK_TAG_MAP.get(rank, rank), _RANK_INDEX.get(rank, len(RANK_ORDER))

    @staticmethod
    def _format_member_display(member_ids, member_dict, is_af_op, limit=None):
        member_data = []
        for uid in (member_ids[:limit] if limit else member_ids):
            uid_int = uid if isinstance(uid, int) else int(uid)
            member  = member_dict.get(uid_int)
            if member:
                tag, rank_order = Reports._get_rank_info(member, is_af_op)
                if tag:
                    tag = tag.replace('*', '\\*')
                member_data.append((rank_order, tag, escape_markdown(member['ig_name'])))
            else:
                logger.warning(f"[EMBED] Member {uid_int} not found in member_dict")
        member_data.sort(key=lambda x: x[0])
        formatted_names = [f"{tag}-{name}" if tag else name for _, tag, name in member_data]
        return formatted_names, len(member_ids)

    async def handle_extra_points_approval(self, interaction: Interaction, request_id: int, approved: bool):
        try:
            await interaction.response.defer()
            async with aget_db_connection() as conn:
                request = (await conn.execute("SELECT * FROM extra_points_requests WHERE id = ?", (request_id,))).fetchone()
                if not request:
                    await interaction.followup.send(**plain("Request not found."), ephemeral=True)
                    return
                if request['status'] != 'pending':
                    await interaction.followup.send(**plain(f"This request has already been {request['status']}."), ephemeral=True)
                    return

                if approved:
                    participants = [int(pid.strip()) for pid in request['participants'].split(',') if pid.strip()]
                    if participants:
                        try:
                            placeholders = ",".join("?" * len(participants))
                            ranks_map    = {
                                int(row['discord_id']): row['rank']
                                for row in (await conn.execute(
                                    f"SELECT discord_id, rank FROM members WHERE discord_id IN ({placeholders})",
                                    participants,
                                )).fetchall()
                            }
                            for pid in participants:
                                if pid not in ranks_map:
                                    logger.warning(f"Participant {pid} not found in members table")
                            valid = [(pid, ranks_map[pid]) for pid in participants if pid in ranks_map]
                            if valid:
                                case_promo   = " ".join(f"WHEN discord_id = '{pid}' AND rank = '{rank}' THEN promo_points + {EXTRA_POINTS_VALUE}" for pid, rank in valid)
                                case_extra   = " ".join(f"WHEN discord_id = '{pid}' AND rank = '{rank}' THEN extra_points + 1"                     for pid, rank in valid)
                                where_clause = " OR ".join(f"(discord_id = '{pid}' AND rank = '{rank}')"                                           for pid, rank in valid)
                                (await conn.execute(
                                    f"UPDATE rank_stats SET "
                                    f"promo_points = CASE {case_promo} ELSE promo_points END, "
                                    f"extra_points = CASE {case_extra} ELSE extra_points END "
                                    f"WHERE {where_clause}",
                                ))
                        except Exception as e:
                            logger.error(f"Error awarding points: {e}", exc_info=True)
                    (await conn.execute(
                        "UPDATE extra_points_requests SET status = 'approved', approved_by = ?, approved_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (str(interaction.user.id), request_id),
                    ))
                    (await conn.commit())
                    if request['message_id']:
                        try:
                            channel = await self.bot.fetch_channel(int(request['channel_id']))
                            message = await channel.fetch_message(int(request['message_id']))
                            await message.edit(view=None)
                        except discord.NotFound:
                            logger.warning(f"Channel or message not found for request {request_id}")
                        except Exception as e:
                            logger.error(f"Failed to update message: {e}", exc_info=True)
                    await interaction.followup.send(**plain(f"Request #{request_id} approved."), ephemeral=True)
                    logs_cog = self.bot.get_cog("Logs")
                    if logs_cog:
                        await logs_cog.log_action(
                            action="Extra Points Request Approved",
                            description=f"Request #{request_id} with {len(participants)} participant(s) has been approved",
                            user=interaction.user)
                    try:
                        reporter = await self.bot.fetch_user(int(request['submitted_by']))
                        await reporter.send(f"Your extra points request #{request_id} was approved by {interaction.user.mention}.")
                    except Exception:
                        pass
                else:
                    (await conn.execute("DELETE FROM extra_points_requests WHERE id = ?", (request_id,)))
                    (await conn.commit())
                    if request['message_id']:
                        try:
                            channel = await self.bot.fetch_channel(int(request['channel_id']))
                            message = await channel.fetch_message(int(request['message_id']))
                            await message.delete()
                        except discord.NotFound:
                            pass
                        except Exception as e:
                            logger.error(f"Failed to delete message: {e}")
                    await interaction.followup.send(**plain(f"Request #{request_id} declined."), ephemeral=True)
                    logs_cog = self.bot.get_cog("Logs")
                    if logs_cog:
                        await logs_cog.log_action(
                            action="Extra Points Request Declined",
                            description=f"Request #{request_id} has been declined",
                            user=interaction.user)
                    try:
                        reporter = await self.bot.fetch_user(int(request['submitted_by']))
                        await reporter.send(f"Your extra points request #{request_id} was declined by {interaction.user.mention}.")
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"ERROR in handle_extra_points_approval: {e}")
            await interaction.followup.send(**plain(f"Error: {str(e)}"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Reports(bot))
