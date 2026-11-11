from discord import Interaction, Embed
import discord
from datetime import datetime, timezone
import time
import asyncio
import logging

from database.db import aget_db_connection
from database.db import get_next_operation_report_id
from utils.helpers import get_channel_safe, member_cache, convert_ids_to_ints, create_embed, plain
from utils.settings import (
    EMBED_FOOTER_TEXT,
    RANK_TAG_MAP, RANK_ORDER, OPERATION_TYPES, OPERATION_CHANNEL_MAP,
)
from .base import ReportState
from .constants import AF_OPS, OPERATION_REQUIREMENTS, _RANK_INDEX, calculate_points, escape_markdown
from .views import ExtraPointsApprovalView

logger = logging.getLogger(__name__)


class SubmitMixin:
    def calculate_points_for_member(self, uid: int, state: ReportState, member_data: dict) -> tuple:
        if not member_data:
            return (0, 0, 0)
        return calculate_points(
            state.operation_type,
            member_data.get('is_af') or 0,
            uid in state.leaders,
            uid in state.airforce,
        )

    async def _validate_submission(self, interaction: Interaction, state: ReportState,
                                    leaders, participants, airforce, all_participants, af_pilot):
        ids_to_check = list({str(state.reporter_id)} | {str(uid) for uid in all_participants})
        async with aget_db_connection() as conn:
            placeholders = ",".join("?" * len(ids_to_check))
            rows         = (await conn.execute(
                f"SELECT discord_id, rank, is_af, ig_name FROM members WHERE discord_id IN ({placeholders})",
                ids_to_check,
            )).fetchall()
        member_lookup = {int(r['discord_id']): {**dict(r), 'discord_id': int(r['discord_id'])} for r in rows}
        existing_ids  = {str(uid) for uid in member_lookup}

        async def _send_error(msg: str):
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                pass

        if str(state.reporter_id) not in existing_ids:
            await _send_error("**Error:** You are not registered in the database.")
            return None
        if all_participants:
            missing = [str(uid) for uid in all_participants if str(uid) not in existing_ids]
            if missing:
                await _send_error(f"**Error:** The following members are not registered in the database:\n`{', '.join(missing)}`")
                return None
        participants_in_restricted = set(participants) & set(leaders + airforce)
        if participants_in_restricted:
            await _send_error(f"**Error:** Members in Participants cannot also be in Leaders or Air Force: "
                              f"{', '.join(str(uid) for uid in participants_in_restricted)}")
            return None
        op_config = OPERATION_REQUIREMENTS.get(state.operation_type, {})
        if state.operation_type == "af_counter":
            for condition, message in [
                (not af_pilot,           "**Air Force Counter Rented** requires an **Air Force Pilot**"),
                (not state.against_name, "**Air Force Counter Rented** requires an enemy name (**Against**)"),
            ]:
                if condition:
                    await _send_error(message)
                    return None
        else:
            min_leaders      = op_config.get('min_leaders', 0)
            min_participants = op_config.get('min_participants', 0)
            min_total        = op_config.get('min_total_attendance', 1)
            op_label         = OPERATION_TYPES[state.operation_type]
            for condition, message in [
                (len(leaders)          < min_leaders,      f"**{op_label}** requires at least **{min_leaders} Leader{'s' if min_leaders != 1 else ''}** (got {len(leaders)})"),
                (len(participants)     < min_participants,  f"**{op_label}** requires at least **{min_participants} Participant{'s' if min_participants != 1 else ''}** (got {len(participants)})"),
                (len(all_participants) < min_total,        f"**{op_label}** requires at least **{min_total} total participant{'s' if min_total != 1 else ''}** (got {len(all_participants)})"),
            ]:
                if condition:
                    await _send_error(message)
                    return None
        return member_lookup

    def _build_report_embed(self, state: ReportState, operation_report_id, reporter_user,
                             reporter_ig_name, leaders, participants, airforce, af_pilot,
                             member_dict, interaction: Interaction) -> Embed:
        op_name = OPERATION_TYPES[state.operation_type].replace(' Operation', '').replace(' Ops', '').replace('Air Force ', '')
        embed   = create_embed(title=f"{op_name} #{operation_report_id}", thumbnail=True)
        embed.timestamp = datetime.now(timezone.utc)
        embed.set_author(name=reporter_ig_name, icon_url=reporter_user.display_avatar.url)
        if state.edit_mode:
            editor_member  = member_dict.get(interaction.user.id)
            editor_ig_name = editor_member.get('ig_name', 'Unknown') if editor_member else 'Unknown'
            embed.set_footer(text=f"Edited by {editor_ig_name}  \u2022  {EMBED_FOOTER_TEXT}", icon_url=interaction.user.display_avatar.url)
        is_af_op    = state.operation_type in AF_OPS
        requires_af = OPERATION_REQUIREMENTS.get(state.operation_type, {}).get('requires_airforce', False)

        if state.operation_type != "af_counter":
            leader_names, leader_count = self._format_member_display(leaders, member_dict, is_af_op)
            embed.add_field(
                name=f"Leaders ({leader_count})",
                value="\n".join(f"• {n}" for n in leader_names) if leader_names else "None",
                inline=False)

        if requires_af and state.operation_type not in AF_OPS and state.operation_type != "af_counter":
            try:
                if airforce:
                    airforce_names, af_count = self._format_member_display(airforce, member_dict, True)
                    embed.add_field(
                        name=f"Air Force ({af_count})",
                        value="\n".join(f"• {n}" for n in airforce_names) if airforce_names else "None",
                        inline=False)
                else:
                    embed.add_field(name="Air Force (0)", value="None", inline=False)
            except Exception as e:
                logger.warning(f"Error building airforce field: {e}")
                embed.add_field(name="Air Force (0)", value="None", inline=False)

        if state.operation_type != "af_counter":
            try:
                if participants:
                    participant_names, total = self._format_member_display(participants, member_dict, is_af_op)
                    lines = [f"• {n}" for n in participant_names]
                    value = "\n".join(lines)
                    if len(value) > 1024:
                        truncated, char_count, shown = [], 0, 0
                        for line in lines:
                            if char_count + len(line) + 1 + 20 > 1024:
                                break
                            truncated.append(line)
                            char_count += len(line) + 1
                            shown      += 1
                        value     = "\n".join(truncated)
                        remaining = total - shown
                        if remaining > 0:
                            value += f"\n... +{remaining} more"
                    embed.add_field(
                        name=f"Participants ({total})",
                        value=value if participant_names else "None",
                        inline=False)
                else:
                    embed.add_field(name="Participants (0)", value="None", inline=False)
            except Exception as e:
                logger.warning(f"Error building participants field: {e}")
                embed.add_field(name="Participants (0)", value="None", inline=False)

        if state.operation_type == "af_counter":
            try:
                pilot_name = "None"
                if af_pilot:
                    pilot_uid_int = af_pilot if isinstance(af_pilot, int) else int(af_pilot)
                    pilot_member  = member_dict.get(pilot_uid_int)
                    if pilot_member:
                        tag, _       = self._get_rank_info(pilot_member, True)
                        if tag:
                            tag = tag.replace('*', '\\*')
                        pilot_name = f"{tag}-{escape_markdown(pilot_member['ig_name'])}" if tag else escape_markdown(pilot_member['ig_name'])
                embed.add_field(name="Pilot", value=pilot_name, inline=False)
                if state.spawn_type:   embed.add_field(name="Spawn Type", value=state.spawn_type,    inline=True)
                if state.against_name: embed.add_field(name="Against",    value=state.against_name,  inline=True)
                if state.attempts:     embed.add_field(name="Attempts",   value=str(state.attempts), inline=True)
            except Exception as e:
                logger.warning(f"Error building pilot field: {e}")

        if state.operation_type == "af_warzone":
            embed.add_field(
                name="Enemies",
                value=", ".join(state.enemies) if state.enemies else "*No enemies specified*",
                inline=False)
        if state.operation_type == "patrol":
            embed.add_field(
                name="Arrests",
                value=str(state.arrests) if state.arrests is not None else "*No arrests recorded*",
                inline=True)
        if state.operation_type in ("training", "af_training"):
            embed.add_field(
                name="Activity",
                value=state.activity if state.activity else "*No activity specified*",
                inline=False)

        if state.date_time: embed.add_field(name="Date & Time", value=state.date_time,             inline=True)
        if state.result:    embed.add_field(name="Result",      value=state.result,                inline=True)
        if state.at_level:  embed.add_field(name="AT Level",    value=f"Level {state.at_level}",  inline=True)
        if state.duration:  embed.add_field(name="Duration",    value=state.duration,              inline=True)
        if state.reason:    embed.add_field(name="Reason",      value=state.reason,                inline=False)
        embed.add_field(name="Notes", value=state.notes if state.notes else "*No notes provided*", inline=False)
        if state.screenshot:
            embed.add_field(name="Screenshot", value=f"[Link]({state.screenshot})", inline=False)
            if state.screenshot.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                embed.set_image(url=state.screenshot)
        else:
            embed.add_field(name="Screenshot", value="*No screenshot provided*", inline=False)
        return embed

    async def _post_embed_to_channel(self, state: ReportState, embed: Embed, report_id: int):
        channel_key = OPERATION_CHANNEL_MAP.get(state.operation_type, "operation_reports")
        try:
            channel = await get_channel_safe(self.bot, channel_key)
            if not channel:
                logger.warning(f"Channel '{channel_key}' not found. Report saved but not posted.")
                return
            if state.edit_mode:
                async with aget_db_connection() as conn:
                    old_msg_row = (await conn.execute("SELECT message_id FROM reports WHERE id = ?", (report_id,))).fetchone()
                old_msg_id = old_msg_row['message_id'] if old_msg_row else None
                if old_msg_id:
                    try:
                        await (await channel.fetch_message(int(old_msg_id))).edit(embed=embed)
                        return
                    except Exception:
                        pass
                new_msg = await channel.send(embed=embed)
                async with aget_db_connection() as conn:
                    (await conn.execute("UPDATE reports SET message_id = ? WHERE id = ?", (str(new_msg.id), report_id)))
                    (await conn.commit())
            else:
                msg = await channel.send(embed=embed)
                async with aget_db_connection() as conn:
                    (await conn.execute("UPDATE reports SET message_id = ? WHERE id = ?", (str(msg.id), report_id)))
                    (await conn.commit())
        except Exception as e:
            logger.error(f"Failed to fetch/send report: {e}")

    async def submit_report(self, interaction: Interaction, state: ReportState):
        try:
            user_id      = str(interaction.user.id)
            current_time = time.time()

            if not state.all_members:
                async with aget_db_connection() as conn:
                    members = (await conn.execute(
                        "SELECT discord_id, discord_name, ig_name, rank, is_af, af_rank FROM members ORDER BY discord_name"
                    )).fetchall()
                    state.all_members = [{**dict(m), 'discord_id': int(dict(m)['discord_id'])} for m in members]

            async with self._cooldown_lock:
                if user_id in self.report_cooldowns:
                    time_since = current_time - self.report_cooldowns[user_id]
                    if time_since < self._report_submit_cooldown:
                        try:
                            await interaction.followup.send(
                                f"Please wait {self._report_submit_cooldown - time_since:.1f}s before **submitting** another report.",
                                ephemeral=True)
                        except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                            pass
                        return
                self.report_cooldowns[user_id] = current_time

            af_pilot = state.af_pilot or (state.old_af_pilot if state.edit_mode else None)
            af_counter_participants = [af_pilot] if state.operation_type == "af_counter" and af_pilot else []

            leaders      = convert_ids_to_ints(state.leaders)
            participants = convert_ids_to_ints(state.participants)
            airforce     = convert_ids_to_ints(state.airforce)
            state.leaders      = leaders
            state.participants = participants
            state.airforce     = airforce
            all_participants = list(set(leaders + participants + airforce + convert_ids_to_ints(af_counter_participants)))

            member_lookup = await self._validate_submission(
                interaction, state, leaders, participants, airforce, all_participants, af_pilot)
            if not member_lookup:
                return

            async with aget_db_connection() as conn:
                if state.edit_mode:
                    async with self._cooldown_lock:
                        if user_id in self.edit_cooldowns:
                            time_since = current_time - self.edit_cooldowns[user_id]
                            if time_since < self._report_edit_cooldown:
                                try:
                                    await interaction.followup.send(
                                        f"Please wait {self._report_edit_cooldown - time_since:.1f}s before **editing** another report.",
                                        ephemeral=True)
                                except (discord.NotFound, discord.HTTPException, asyncio.TimeoutError):
                                    pass
                                return
                    report_id           = state.original_report_id
                    operation_report_id = state.operation_report_id
                    (await conn.execute(
                        "UPDATE reports SET result = ?, at_level = ?, reason = ?, notes = ?, screenshot = ?, "
                        "arrests = ?, duration = ?, activity = ?, enemies = ? WHERE id = ?",
                        (state.result, state.at_level, state.reason, state.notes, state.screenshot, state.arrests,
                         state.duration,
                         state.activity if state.operation_type in ("training", "af_training") else None,
                         ",".join(state.enemies) if state.enemies and state.operation_type == "af_warzone" else None,
                         report_id),
                    ))
                    old_all_participants = set(state.old_leaders + state.old_participants + state.old_airforce)
                    completely_removed   = old_all_participants - set(all_participants)
                    if completely_removed:
                        removed_rows = (await conn.execute(
                            "SELECT discord_id, promo_points_gained, af_points_gained, leadership_points_gained, participant_rank "
                            "FROM operation_participants WHERE report_id = ? AND discord_id IN ({})".format(
                                ",".join("?" * len(completely_removed))),
                            [report_id] + [str(uid) for uid in completely_removed],
                        )).fetchall()
                        valid_op_columns = set(OPERATION_REQUIREMENTS.keys())
                        op_col = state.operation_type if state.operation_type in valid_op_columns else None
                        for row in removed_rows:
                            if op_col:
                                (await conn.execute(
                                    f"UPDATE rank_stats SET promo_points = MAX(0, promo_points - ?), "
                                    f"af_points = MAX(0, af_points - ?), leadership_points = MAX(0, leadership_points - ?), "
                                    f"{op_col} = MAX(0, {op_col} - 1) WHERE discord_id = ? AND rank = ?",
                                    (row['promo_points_gained'] or 0, row['af_points_gained'] or 0,
                                     row['leadership_points_gained'] or 0, row['discord_id'], row['participant_rank']),
                                ))
                            else:
                                (await conn.execute(
                                    "UPDATE rank_stats SET promo_points = MAX(0, promo_points - ?), "
                                    "af_points = MAX(0, af_points - ?), leadership_points = MAX(0, leadership_points - ?) "
                                    "WHERE discord_id = ? AND rank = ?",
                                    (row['promo_points_gained'] or 0, row['af_points_gained'] or 0,
                                     row['leadership_points_gained'] or 0, row['discord_id'], row['participant_rank']),
                                ))
                        (await conn.execute(
                            "DELETE FROM operation_participants WHERE report_id = ? AND discord_id IN ({})".format(
                                ",".join("?" * len(completely_removed))),
                            [report_id] + [str(uid) for uid in completely_removed],
                        ))
                else:
                    operation_report_id = (await asyncio.to_thread(get_next_operation_report_id, state.operation_type))
                    cursor = (await conn.execute(
                        "INSERT INTO reports (operation_report_id, report_type, reporter_id, channel_id, date_time, "
                        "duration, result, at_level, activity, reason, notes, screenshot, af_pilot, spawn_type, "
                        "against_name, attempts, arrests, enemies) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (operation_report_id, state.operation_type, state.reporter_id, str(interaction.channel_id),
                         state.date_time, state.duration, state.result, state.at_level,
                         state.activity if state.operation_type in ("training", "af_training") else None,
                         state.reason, state.notes, state.screenshot,
                         state.af_pilot       if state.operation_type == "af_counter" else None,
                         state.spawn_type     if state.operation_type == "af_counter" else None,
                         state.against_name   if state.operation_type == "af_counter" else None,
                         state.attempts       if state.operation_type == "af_counter" else None,
                         state.arrests        if state.operation_type == "patrol"     else None,
                         ",".join(state.enemies) if state.enemies and state.operation_type == "af_warzone" else None),
                    ))
                    report_id = cursor.lastrowid

                existing_participants = {}
                if state.edit_mode and all_participants:
                    placeholders = ",".join("?" * len(all_participants))
                    existing_participants = {
                        int(row['discord_id']): row['id']
                        for row in (await conn.execute(
                            f"SELECT discord_id, id FROM operation_participants WHERE report_id = ? AND discord_id IN ({placeholders})",
                            [report_id] + [str(pid) for pid in all_participants],
                        )).fetchall()
                    }

                leaders_set  = set(leaders)
                airforce_set = set(airforce)
                points_for_member = {}
                for uid in all_participants:
                    member_data = member_lookup.get(uid)
                    if member_data:
                        promo_points, af_points, leadership_points = self.calculate_points_for_member(uid, state, member_data)
                        participant_rank = member_data.get('rank')
                    else:
                        promo_points = af_points = leadership_points = 0
                        participant_rank = None
                    points_for_member[uid] = (promo_points, af_points, leadership_points)
                    was_leader   = int(uid in leaders_set)
                    was_airforce = int(uid in airforce_set)
                    if state.edit_mode and uid in existing_participants:
                        (await conn.execute(
                            "UPDATE operation_participants SET was_leader = ?, was_airforce = ? WHERE report_id = ? AND discord_id = ?",
                            (was_leader, was_airforce, report_id, uid),
                        ))
                    else:
                        (await conn.execute(
                            "INSERT INTO operation_participants (discord_id, report_id, was_leader, was_airforce, "
                            "promo_points_gained, af_points_gained, leadership_points_gained, participant_rank) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (uid, report_id, was_leader, was_airforce,
                             promo_points, af_points, leadership_points, participant_rank),
                        ))

                if state.edit_mode:
                    old_set          = set(state.old_leaders + state.old_participants + state.old_airforce)
                    members_to_award = set(all_participants) - old_set
                    members_with_role_changes = set(all_participants) & old_set
                    role_change_map  = {}
                    if members_with_role_changes:
                        placeholders = ",".join("?" * len(members_with_role_changes))
                        role_change_map = {
                            int(row['discord_id']): row
                            for row in (await conn.execute(
                                f"SELECT discord_id, promo_points_gained, af_points_gained, leadership_points_gained, "
                                f"participant_rank FROM operation_participants WHERE report_id = ? AND discord_id IN ({placeholders})",
                                [report_id] + list(members_with_role_changes),
                            )).fetchall()
                        }
                    for uid in members_with_role_changes:
                        old_p = role_change_map.get(uid)
                        if not old_p:
                            continue
                        old_promo, old_af, old_lead = old_p['promo_points_gained'] or 0, old_p['af_points_gained'] or 0, old_p['leadership_points_gained'] or 0
                        new_promo, new_af, new_lead = points_for_member[uid]
                        delta_promo = new_promo - old_promo
                        delta_af    = new_af   - old_af
                        delta_lead  = new_lead - old_lead
                        if delta_promo != 0 or delta_af != 0 or delta_lead != 0:
                            rank = old_p['participant_rank'] or member_lookup.get(uid, {}).get('rank')
                            if rank:
                                try:
                                    rs = (await conn.execute(
                                        "SELECT promo_points, leadership_points, af_points, warzone, doomsday, "
                                        "ammo_transport, bank_ops, casino_ops, securicar_ops, andromada_ops, "
                                        "andromada_terrors_ops, assault_ops, training, patrol, extra_points "
                                        "FROM rank_stats WHERE discord_id = ? AND rank = ?",
                                        (uid, rank),
                                    )).fetchone()
                                except Exception:
                                    rs = None
                                if rs:
                                    total_existing = sum(rs[k] or 0 for k in rs.keys())
                                    current_rank   = member_lookup.get(uid, {}).get('rank')
                                    if total_existing == 0 and current_rank != rank:
                                        delta_promo = min(delta_promo, 0)
                                        delta_af    = min(delta_af,    0)
                                        delta_lead  = min(delta_lead,  0)
                                (await conn.execute(
                                    "UPDATE rank_stats SET promo_points = MAX(0, promo_points + ?), "
                                    "af_points = MAX(0, af_points + ?), leadership_points = MAX(0, leadership_points + ?) "
                                    "WHERE discord_id = ? AND rank = ?",
                                    (delta_promo, delta_af, delta_lead, uid, rank),
                                ))
                                (await conn.execute(
                                    "UPDATE operation_participants SET promo_points_gained = ?, af_points_gained = ?, "
                                    "leadership_points_gained = ? WHERE report_id = ? AND discord_id = ?",
                                    (new_promo, new_af, new_lead, report_id, uid),
                                ))
                else:
                    members_to_award = set(all_participants)

                for uid in members_to_award:
                    promo_points, af_points, leadership_points = points_for_member[uid]
                    member_data = member_lookup.get(uid)
                    if not member_data:
                        continue
                    rank = member_data['rank']
                    (await conn.execute(
                        "INSERT OR IGNORE INTO rank_stats (discord_id, rank, warzone, doomsday, ammo_transport, "
                        "bank_ops, casino_ops, securicar_ops, andromada_ops, andromada_terrors_ops, assault_ops, "
                        "training, patrol, extra_points, promo_points, af_points, leadership_points, "
                        "af_warzone, af_counter, af_training) VALUES (?, ?, 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0)",
                        (str(uid), rank),
                    ))
                    (await conn.execute(
                        "UPDATE rank_stats SET promo_points = promo_points + ?, af_points = af_points + ?, "
                        "leadership_points = leadership_points + ?, {0} = {0} + 1 WHERE discord_id = ? AND rank = ?".format(state.operation_type),
                        (promo_points, af_points, leadership_points, str(uid), rank),
                    ))
                    (await conn.execute(
                        "UPDATE operation_participants SET promo_points_gained = ?, af_points_gained = ?, "
                        "leadership_points_gained = ?, participant_rank = ? WHERE report_id = ? AND discord_id = ?",
                        (promo_points, af_points, leadership_points, rank, report_id, uid),
                    ))
                (await conn.commit())

            try:
                all_members_in_op = set(all_participants) | set(state.old_leaders) | set(state.old_participants) | set(state.old_airforce)
                for uid in all_members_in_op:
                    member_cache.invalidate(f"profile:{uid}")
                member_cache.invalidate(f"all_members_{int(time.time() / 300)}")
            except Exception:
                logger.debug("Cache invalidation error", exc_info=True)

            reporter_user    = await self.bot.fetch_user(state.reporter_id)
            reporter_ig_name = "Unknown"
            try:
                reporter_data = member_lookup.get(int(state.reporter_id))
                if reporter_data:
                    reporter_ig_name = reporter_data.get('ig_name', 'Unknown')
            except Exception as e:
                logger.warning(f"Error fetching reporter ig_name: {e}")

            member_dict = self._get_or_build_member_dict(state)
            embed       = self._build_report_embed(state, operation_report_id, reporter_user, reporter_ig_name,
                                                    leaders, participants, airforce, af_pilot, member_dict, interaction)
            await self._post_embed_to_channel(state, embed, report_id)

            op_name = OPERATION_TYPES[state.operation_type].replace(' Operation', '').replace(' Ops', '').replace('Air Force ', '')
            try:
                if state.edit_mode:
                    complete_text = f"Report #{state.operation_report_id} has been updated."
                    logs_cog = self.bot.get_cog("Logs")
                    if logs_cog:
                        await logs_cog.log_action(
                            action="Report Edited",
                            description=f"{op_name} operation report (#{state.operation_report_id}) has been edited",
                            user=interaction.user)
                    async with self._cooldown_lock:
                        self.edit_cooldowns[user_id] = time.time()
                else:
                    complete_text = f"Report #{operation_report_id} has been submitted."
                    logs_cog = self.bot.get_cog("Logs")
                    if logs_cog:
                        await logs_cog.log_action(
                            action="Report Created",
                            description=f"{op_name} operation report (#{operation_report_id}) has been created with {len(all_participants)} participant(s)",
                            user=interaction.user)
                await state.message.edit(content=complete_text, embed=None, view=None)
            except Exception as e:
                logger.warning(f"Error updating original response: {e}")
            self._states.pop(interaction.user.id, None)

        except Exception as e:
            logger.error(f"ERROR submitting report: {e}", exc_info=True)
            try:
                await interaction.followup.send(**plain(f"Error: {str(e)}"), ephemeral=True)
            except (discord.DiscordException, discord.NotFound, asyncio.TimeoutError):
                pass

    async def submit_report_to_hq(self, interaction: Interaction, state: ReportState):
        try:
            member_map       = {}
            reporter_ig_name = "Unknown"
            async with aget_db_connection() as conn:
                cursor = (await conn.execute(
                    "INSERT INTO extra_points_requests (report_type, submitted_by, date_time, participants, "
                    "reason, action, screenshot, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (state.operation_type, str(interaction.user.id), state.date_time,
                     ",".join(str(p) for p in state.participants) if state.participants else "",
                     "Extra Points", state.activity, state.screenshot, "pending"),
                ))
                report_id    = cursor.lastrowid
                all_ids      = list({str(state.reporter_id)} | ({str(p) for p in state.participants} if state.participants else set()))
                placeholders = ",".join("?" * len(all_ids))
                member_map   = {
                    int(row['discord_id']): dict(row)
                    for row in (await conn.execute(
                        f"SELECT discord_id, ig_name, rank FROM members WHERE discord_id IN ({placeholders})",
                        all_ids,
                    )).fetchall()
                }
                reporter_from_map = member_map.get(int(state.reporter_id))
                if reporter_from_map:
                    reporter_ig_name = reporter_from_map.get('ig_name', reporter_ig_name)
                (await conn.commit())

            embed        = create_embed(title=f"Extra Points #{report_id}", thumbnail=True, footer=True)
            embed.timestamp = datetime.now(timezone.utc)
            reporter_user = await self.bot.fetch_user(state.reporter_id)
            embed.set_author(name=reporter_ig_name, icon_url=reporter_user.display_avatar.url)

            if state.participants:
                participant_entries, unknown_entries = [], []
                for pid in state.participants:
                    pid_int = int(pid) if isinstance(pid, str) else pid
                    member  = member_map.get(pid_int)
                    if member:
                        escaped_tag = RANK_TAG_MAP.get(member['rank'], member['rank']).replace('*', '\\*')
                        sort_key    = _RANK_INDEX.get(member['rank'], len(RANK_ORDER))
                        participant_entries.append((sort_key, f"\u2022 {escaped_tag}-{escape_markdown(member['ig_name'])}"))
                    else:
                        unknown_entries.append("\u2022 Unknown Member")
                participant_entries.sort(key=lambda x: x[0])
                embed.add_field(
                    name="Participants",
                    value="\n".join(line for _, line in participant_entries) + ("\n" + "\n".join(unknown_entries) if unknown_entries else "") or "None",
                    inline=False,
                )
            if state.reason:   embed.add_field(name="Reason",     value=state.reason,   inline=False)
            if state.activity: embed.add_field(name="Action",     value=state.activity, inline=False)
            if state.screenshot:
                embed.add_field(name="Screenshot", value=f"[Link]({state.screenshot})", inline=False)
                if state.screenshot.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    embed.set_image(url=state.screenshot)
            else:
                embed.add_field(name="Screenshot", value="*No screenshot provided*", inline=False)

            try:
                channel = await get_channel_safe(self.bot, "channel_extra_points")
                if not channel:
                    raise RuntimeError("HQ channel not available")
                msg = await channel.send(embed=embed, view=ExtraPointsApprovalView(report_id))
                async with aget_db_connection() as conn:
                    (await conn.execute(
                        "UPDATE extra_points_requests SET message_id = ?, channel_id = ? WHERE id = ?",
                        (str(msg.id), str(channel.id), report_id),
                    ))
                    (await conn.commit())
            except Exception as e:
                logger.error(f"Failed to send to HQ: {e}")
                await interaction.followup.send(**plain(f"Error sending to HQ: {str(e)}"), ephemeral=True)
                return

            await state.message.edit(
                content="Extra Points request has been sent to HQ for approval.",
                embed=None, view=None)
            logs_cog = self.bot.get_cog("Logs")
            if logs_cog:
                await logs_cog.log_action(
                    action="Extra Points Request",
                    description=f"Extra Points request (#{report_id}) has been submitted with "
                                f"{len(state.participants) if state.participants else 0} participant(s)",
                    user=interaction.user)
            self._states.pop(interaction.user.id, None)

        except Exception as e:
            logger.error(f"ERROR in submit_report_to_hq: {e}", exc_info=True)
            await interaction.followup.send(**plain(f"Error: {str(e)}"), ephemeral=True)
