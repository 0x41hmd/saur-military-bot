from __future__ import annotations
import asyncio
import dataclasses
import io
import logging
import random
from database import errors as db_errors
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from matplotlib.figure import Figure

from database.db import aget_db_connection
from database.db import get_db_connection
from cogs.public.recruitment import MSG_NOT_SET_UP_STAFF
from utils.helpers import create_embed, require_permissions, send_private_error, plain, format_playtime
from utils.settings import (
    OPERATION_TYPES, ACTIVITY_THRESHOLDS,
    CHART_DPI, CHART_BACKGROUND, CHART_BORDER_COLOR, CHART_BORDER_WIDTH,
    CHART_GRID_COLOR, CHART_GRID_ALPHA,
)

logger = logging.getLogger(__name__)


WEEKLY_REPORT = {"enabled": True}

FEEDBACK_MESSAGES = {
    "Exceptional": [
        "You're crushing it! Keep maintaining this excellent pace.",
        "Outstanding performance this week! Keep it up!",
        "Fantastic activity level! You're leading by example.",
    ],
    "High": [
        "Solid week! You're making great progress.",
        "Good activity level! Keep pushing forward.",
        "Nice work! You're on the right track.",
    ],
    "Moderate": [
        "You're doing well! Keep up the momentum.",
        "Good effort this week. Stay active!",
        "Moving in the right direction. Keep it going!",
    ],
    "Low": [
        "You've earned some points. Try to increase participation next week.",
        "Some activity is better than none. Let's see more next week!",
        "Getting there! Aim higher next week.",
    ],
    "Inactive": [
        "No activity this week. We'd love to see you participate!",
        "You were inactive this week. Get involved and earn those points!",
        "Let's get back in the action next week!",
    ],
}


RECRUITMENT_COLORS = {"apps": "#3B82F6", "length": "#F87171", "per_day": "#D97706"}
RECRUITMENT_TABLE_LIMIT = 3300


@dataclasses.dataclass
class WaveRow:
    number: int
    apps: int
    start: datetime
    end: datetime
    is_open: bool
    accepted: int
    denied: int
    gap_days: Optional[int] = None

    @property
    def length_days(self) -> int:
        return max((self.end.date() - self.start.date()).days, 0)

    @property
    def per_day(self) -> float:
        return self.apps / max(self.length_days, 1)


def _build_wave_rows(waves) -> List[WaveRow]:
    now = datetime.now(timezone.utc)
    rows: List[WaveRow] = []
    for w in waves:
        row = WaveRow(
            number=w.number, apps=len(w.applications), start=w.opened_at, end=w.closed_at or now,
            is_open=w.closed_at is None, accepted=w.accepted, denied=w.denied,
        )
        if rows and not rows[-1].is_open:
            row.gap_days = max((row.start.date() - rows[-1].end.date()).days, 0)
        rows.append(row)
    return rows


def _recruitment_table(rows: List[WaveRow]) -> str:
    blocks = []
    for r in rows:
        blocks.append("\n".join([
            f"**Wave {r.number}{' (open)' if r.is_open else ''}**",
            f"Apps: `{r.apps}`",
            f"Start: `{r.start:%d/%m/%Y}`",
            f"End: `{'open' if r.is_open else f'{r.end:%d/%m/%Y}'}`",
            f"Length: `{r.length_days}` days",
            f"Avg/day: `{r.per_day:.2f}`",
            f"Since last wave: {f'`{r.gap_days}` days' if r.gap_days is not None else '-'}",
        ]))
    kept = len(blocks)
    while kept > 1 and sum(len(b) + 2 for b in blocks[-kept:]) > RECRUITMENT_TABLE_LIMIT:
        kept -= 1
    note = f"\n\n*(showing the latest {kept} of {len(blocks)} waves)*" if kept < len(blocks) else ""
    return "\n\n".join(blocks[-kept:]) + note


def _generate_recruitment_chart(rows: List[WaveRow]) -> Optional[io.BytesIO]:
    if not rows:
        return None
    try:
        labels   = [f"{r.number}{'*' if r.is_open else ''}" for r in rows]
        x        = list(range(len(rows)))
        apps     = [r.apps for r in rows]
        lengths  = [r.length_days for r in rows]
        per_day  = [r.per_day for r in rows]
        dates    = [f"{r.start:%d/%m/%y}" for r in rows]

        fig = Figure(figsize=(14, 11), dpi=CHART_DPI, facecolor=CHART_BACKGROUND)
        fig.patch.set_edgecolor(CHART_BORDER_COLOR)
        fig.patch.set_linewidth(CHART_BORDER_WIDTH)
        grid = fig.add_gridspec(2, 2, height_ratios=[1, 1.15], hspace=0.38, wspace=0.18)

        def style(ax, title, ylabel):
            ax.set_title(title, fontsize=14, fontweight='bold', loc='left', pad=12)
            ax.set_ylabel(ylabel, fontsize=11)
            ax.set_xticks(x)
            ax.grid(axis='y', color=CHART_GRID_COLOR, alpha=CHART_GRID_ALPHA)
            ax.set_axisbelow(True)
            for side in ('top', 'right'):
                ax.spines[side].set_visible(False)

        def line_panel(ax, values, color, title, ylabel, fmt):
            ax.plot(x, values, color=color, marker='o', linewidth=2.2, markersize=6)
            for xi, v in zip(x, values):
                ax.annotate(fmt.format(v), (xi, v), textcoords='offset points', xytext=(0, 9),
                            ha='center', fontsize=9, color=color, fontweight='bold',
                            bbox=dict(boxstyle='round,pad=0.15', fc='white', ec='none', alpha=0.85))
            style(ax, title, ylabel)
            ax.set_xticklabels(labels, fontsize=9)
            ax.set_xlabel('Wave', fontsize=10)
            ax.set_ylim(0, (max(values) if values else 1) * 1.2 or 1)

        line_panel(fig.add_subplot(grid[0, 0]), apps, RECRUITMENT_COLORS['apps'], 'No. of apps per wave', 'Applications', '{:.0f}')
        line_panel(fig.add_subplot(grid[0, 1]), per_day, RECRUITMENT_COLORS['per_day'], 'Avg. apps per day', 'Applications / day', '{:.2f}')

        ax = fig.add_subplot(grid[1, :])
        width = 0.27
        series = (
            ('No. of apps',      apps,    RECRUITMENT_COLORS['apps'],    -width),
            ('Wave length (d)',  lengths, RECRUITMENT_COLORS['length'],  0),
            ('Avg. apps p. day', per_day, RECRUITMENT_COLORS['per_day'], width),
        )
        for name, values, color, offset in series:
            bars = ax.bar([xi + offset for xi in x], values, width=width, color=color, label=name)
            if len(rows) <= 20:
                ax.bar_label(bars, labels=[f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}" for v in values], fontsize=8, padding=2)
        style(ax, 'Applications statistics', 'Value')
        ax.set_xticklabels([f"W{l}\n{d}" for l, d in zip(labels, dates)], fontsize=8)
        ax.legend(loc='upper right', frameon=False, ncol=3)

        fig.text(0.5, 0.945, 'RECRUITMENT WAVES', ha='center', fontsize=20, fontweight='bold')
        if any(r.is_open for r in rows):
            fig.text(0.5, 0.01, '* wave still open: end date is today', ha='center', fontsize=9, color='#555555')

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=CHART_DPI, facecolor=CHART_BACKGROUND, bbox_inches='tight', pad_inches=0.25)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Recruitment chart generation failed: {e}", exc_info=True)
        return None


class Analytics(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _period_bounds(self, days: Optional[int]) -> Tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=days)) if days is not None else datetime(1900, 1, 1, tzinfo=timezone.utc)
        return start.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')


    def _fmt_last_online(self, iso: Optional[str], is_online: bool = False) -> str:
        if is_online:
            return "🟢 Online now"
        if not iso:
            return "🔴 Never"
        try:
            dt = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(timezone.utc)
        except Exception:
            return "🔴 Unknown"
        delta        = datetime.now(timezone.utc) - dt
        total_secs   = int(delta.total_seconds())
        if total_secs < 60:
            return "🟢 Just now"
        if total_secs < 3600:
            return f"🔴 {total_secs // 60}m ago"
        if total_secs < 86400:
            h = total_secs // 3600
            m = (total_secs % 3600) // 60
            return f"🔴 {h}h {m}m ago"
        return f"🔴 {delta.days}d ago ({dt.strftime('%Y-%m-%d')})"

    async def _fetch_playtime_in_window(self, conn, discord_id: str, start_iso: str) -> int:
        start_date = start_iso[:10]
        row = (await conn.execute(
            "SELECT COALESCE(SUM(minutes), 0) as total FROM playtime_log WHERE discord_id = ? AND log_date >= ?",
            (discord_id, start_date)
        )).fetchone()
        return row['total'] if row else 0

    async def _fetch_total_playtime_in_window(self, conn, start_iso: str) -> int:
        start_date = start_iso[:10]
        row = (await conn.execute(
            "SELECT COALESCE(SUM(minutes), 0) as total FROM playtime_log WHERE log_date >= ?",
            (start_date,)
        )).fetchone()
        return row['total'] if row else 0

    def _success_clause(self) -> str:
        return "(r.result = 'Success' OR r.result IN ('4-0','3-1'))"

    def _evaluate_activity(self, current_pts: int, previous_pts: int, thresholds: dict) -> dict:
        trend = "Up" if current_pts > previous_pts else ("Down" if current_pts < previous_pts else "Steady")
        if current_pts >= thresholds['Exceptional']:
            level = "Exceptional"
        elif current_pts >= thresholds['High']:
            level = "High"
        elif current_pts >= thresholds['Moderate']:
            level = "Moderate"
        elif current_pts >= thresholds['Low']:
            level = "Low"
        else:
            level = "Inactive (Recovering)" if current_pts > previous_pts else "Inactive"
        return {'level': level, 'trend': trend, 'is_increasing': current_pts > previous_pts}

    def _format_date(self, iso: Optional[str]) -> str:
        if not iso:
            return "Unknown"
        try:
            return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(timezone.utc).strftime('%b %d, %Y')
        except Exception:
            return iso

    async def operation_type_autocomplete(self, interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=OPERATION_TYPES.get(k, k), value=k)
            for k in OPERATION_TYPES
            if k != "extra_points" and (
                not current or
                k.lower().startswith(current.lower()) or
                OPERATION_TYPES.get(k, "").lower().startswith(current.lower())
            )
        ][:25]

    stats = app_commands.Group(name="analyze", description="Squad analytics")

    async def _fetch_name_map(self, conn, ids: set) -> dict:
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = (await conn.execute(
            f"SELECT discord_id, ig_name FROM members WHERE discord_id IN ({placeholders})",
            list(ids)
        )).fetchall()
        return {r['discord_id']: r['ig_name'] or str(r['discord_id']) for r in rows}

    @stats.command(name="overview", description="Squad overview stats")
    @require_permissions(admin=True, acl="view_analytics")
    @app_commands.describe(days="Days to include (default ALL TIME)")
    async def overview(self, interaction: Interaction, days: Optional[int] = None):
        await interaction.response.defer(ephemeral=False)
        if days is not None and days <= 0:
            await interaction.followup.send(**plain("Days must be at least 1."), ephemeral=True)
            return
        try:
            start_iso, _ = self._period_bounds(days)
            async with aget_db_connection() as conn:
                total_ops  = (await conn.execute("SELECT COUNT(*) as cnt FROM reports WHERE created_at >= ?", (start_iso,))).fetchone()['cnt'] or 0
                succ_ops   = (await conn.execute(f"SELECT COUNT(*) as succ FROM reports r WHERE r.created_at >= ? AND ({self._success_clause()})", (start_iso,))).fetchone()['succ'] or 0
                fail_ops   = total_ops - succ_ops
                total_parts = (await conn.execute("SELECT COUNT(*) as cnt FROM operation_participants WHERE report_id IN (SELECT id FROM reports WHERE created_at >= ?)", (start_iso,))).fetchone()['cnt'] or 0

                types = (await conn.execute("SELECT report_type, COUNT(*) as cnt FROM reports WHERE created_at >= ? GROUP BY report_type ORDER BY cnt DESC", (start_iso,))).fetchall()
                by_type_lines, others = [], 0
                for i, r in enumerate(types):
                    name = OPERATION_TYPES.get(r['report_type'], r['report_type'])
                    if i < 4:
                        by_type_lines.append(f"{name}: {r['cnt']}")
                    else:
                        others += r['cnt']
                if len(types) > 4:
                    by_type_lines.append(f"Others: {others}")

                total_members  = (await conn.execute("SELECT COUNT(*) as cnt FROM members")).fetchone()['cnt'] or 0
                active_members = (await conn.execute("SELECT COUNT(*) as cnt FROM members WHERE status = 'Active'")).fetchone()['cnt'] or 0

                most_row  = (await conn.execute("SELECT op.discord_id, COUNT(DISTINCT op.report_id) as ops FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE r.created_at >= ? GROUP BY op.discord_id ORDER BY ops DESC LIMIT 1", (start_iso,))).fetchone()
                most_name, most_ops = "None", 0
                if most_row:
                    most_ops = most_row['ops'] or 0
                    m = (await conn.execute("SELECT ig_name FROM members WHERE discord_id = ?", (most_row['discord_id'],))).fetchone()
                    most_name = m['ig_name'] if m and m['ig_name'] else str(most_row['discord_id'])

                avg_per_member = total_ops / total_members if total_members else 0

                total_playtime = (await self._fetch_total_playtime_in_window(conn, "1900-01-01 00:00:00"))

                pts_row = (await conn.execute("""
                    SELECT SUM(promo) as promo, SUM(af) as af, SUM(lead) as lead FROM (
                        SELECT promo_points_gained as promo, af_points_gained as af, leadership_points_gained as lead
                        FROM operation_participants WHERE report_id IN (SELECT id FROM reports WHERE created_at >= ?)
                        UNION ALL
                        SELECT
                            CASE WHEN point_type='promo' THEN amount ELSE 0 END,
                            CASE WHEN point_type='af' THEN amount ELSE 0 END,
                            CASE WHEN point_type='leadership' THEN amount ELSE 0 END
                        FROM manual_points_awards WHERE created_at >= ?
                    )""", (start_iso, start_iso))).fetchone()
                promo_pts = pts_row['promo'] or 0
                af_pts    = pts_row['af'] or 0
                lead_pts  = pts_row['lead'] or 0

                extra_pts_requests = (await conn.execute("SELECT participants FROM extra_points_requests WHERE status = 'approved' AND approved_at IS NOT NULL AND approved_at >= ?", (start_iso,))).fetchall()
                promo_pts += sum(len([p.strip() for p in req['participants'].split(',')]) for req in extra_pts_requests if req['participants'])

                leader_ops     = (await conn.execute("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.created_at >= ?", (start_iso,))).fetchone()['cnt'] or 0
                leader_success = (await conn.execute(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.created_at >= ? AND ({self._success_clause()})", (start_iso,))).fetchone()['succ'] or 0
                leader_win_rate = (leader_success / leader_ops * 100) if leader_ops else 0
                leader_pts      = (await conn.execute("SELECT SUM(op.leadership_points_gained) as pts FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.created_at >= ?", (start_iso,))).fetchone()['pts'] or 0

                top_leaders_by_success = (await conn.execute("SELECT op.discord_id, COUNT(DISTINCT op.report_id) as ops, SUM(op.leadership_points_gained) as lead_pts FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.created_at >= ? GROUP BY op.discord_id HAVING lead_pts > 0 ORDER BY lead_pts DESC LIMIT 5", (start_iso,))).fetchall()
                top_participants       = (await conn.execute("SELECT op.discord_id, COUNT(DISTINCT op.report_id) as ops, SUM(op.promo_points_gained) as promo_pts FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE r.created_at >= ? GROUP BY op.discord_id HAVING promo_pts > 0 ORDER BY promo_pts DESC LIMIT 5", (start_iso,))).fetchall()
                top_airforce           = (await conn.execute("SELECT op.discord_id, COUNT(DISTINCT op.report_id) as ops, SUM(op.af_points_gained) as af_pts FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_airforce = 1 AND r.created_at >= ? GROUP BY op.discord_id HAVING af_pts > 0 ORDER BY af_pts DESC LIMIT 5", (start_iso,))).fetchall()

                all_ids  = {r['discord_id'] for r in top_leaders_by_success + top_participants + top_airforce}
                name_map = (await self._fetch_name_map(conn, all_ids))

            title = f"SQUAD STATS - {'ALL TIME' if days is None else f'LAST {days} DAYS'}"
            embed = create_embed(title=title, description="Overall stats")
            lines = [
                "**OPERATIONS**",
                f"Total: `{total_ops}`",
                f"Successful: `{succ_ops}` ({(succ_ops / total_ops * 100 if total_ops else 0):.1f}%)",
                f"Failed: `{fail_ops}` ({(fail_ops / total_ops * 100 if total_ops else 0):.1f}%)",
                f"Participants: `{total_parts}`",
                "",
                "**BY TYPE**",
                *(by_type_lines or ["None"]),
                "",
                "**MEMBERS**",
                f"Active: `{active_members}/{total_members}` ({(active_members / total_members * 100 if total_members else 0):.1f}%)",
                f"Most Active: {most_name} (`{most_ops}` ops)",
                f"Avg/Member: `{avg_per_member:.1f}` ops",
                f"Total Playtime: `{format_playtime(total_playtime)}`",
                "",
                "**POINTS**",
                f"Promotional pts: `{promo_pts}`",
                f"Air Force pts: `{af_pts}`",
                f"Leadership pts: `{lead_pts}`",
                "",
                "**LEADERSHIP**",
                f"Ops Led: `{leader_ops}`",
                f"Win Rate: `{leader_win_rate:.1f}%`",
                f"Total Pts: `{leader_pts}`",
                "",
                "**TOP 5 LEADERS**",
                *([f"{i}. {name_map.get(l['discord_id'], str(l['discord_id']))}: `{int(l['lead_pts'] or 0)}` pts" for i, l in enumerate(top_leaders_by_success, 1)] or ["No leaders yet"]),
                "",
                "**TOP 5 PARTICIPANTS**",
                *([f"{i}. {name_map.get(p['discord_id'], str(p['discord_id']))}: `{int(p['promo_pts'] or 0)}` pts" for i, p in enumerate(top_participants, 1)] or ["No participants yet"]),
                "",
                "**TOP 5 AIRFORCE**",
                *([f"{i}. {name_map.get(a['discord_id'], str(a['discord_id']))}: `{int(a['af_pts'] or 0)}` pts" for i, a in enumerate(top_airforce, 1)] or ["No airforce yet"]),
            ]
            embed.clear_fields()
            embed.add_field(name="", value="\n".join(lines), inline=False)
            await interaction.followup.send(embed=embed)
        except db_errors.Error as e:
            logger.error(f"Analytics DB error: {e}", exc_info=True)
            await interaction.followup.send(**plain("Database error while generating overview."), ephemeral=True)

    @stats.command(name="member", description="Member stats")
    @require_permissions(admin=True, acl="view_analytics")
    @app_commands.describe(user="Member to show", days="Days to include (default ALL TIME since join)")
    async def member(self, interaction: Interaction, user: discord.User, days: Optional[int] = None):
        await interaction.response.defer(ephemeral=False)
        if days is not None and days <= 0:
            await interaction.followup.send(**plain("Days must be at least 1."), ephemeral=True)
            return
        uid = str(user.id)
        try:
            async with aget_db_connection() as conn:
                member_row = (await conn.execute("SELECT ig_name, rank, is_af, af_rank, date_added, last_online FROM members WHERE discord_id = ?", (uid,))).fetchone()
                if not member_row:
                    await interaction.followup.send(**plain(f"{user.mention} not found in database."), ephemeral=True)
                    return
                ig             = member_row['ig_name']
                rank           = member_row['rank']
                af_rank        = member_row['af_rank']
                is_af_flag     = bool(member_row['is_af'])
                date_added     = member_row['date_added']
                last_online    = member_row['last_online']

                rank_stats_row   = (await conn.execute("SELECT promo_points, af_points, leadership_points FROM rank_stats WHERE discord_id = ? AND rank = ?", (uid, rank))).fetchone()
                career_stats_row = (await conn.execute("SELECT promo_points, af_points, leadership_points FROM points WHERE discord_id = ?", (uid,))).fetchone()

                if days is None:
                    start_iso = date_added or "1900-01-01 00:00:00"
                    now_iso   = datetime.now(timezone.utc).isoformat()
                    window_playtime = (await self._fetch_playtime_in_window(conn, uid, "1900-01-01"))
                else:
                    start_iso_calc, now_iso = self._period_bounds(days)
                    start_iso = date_added if date_added and date_added > start_iso_calc else start_iso_calc
                    window_playtime = (await self._fetch_playtime_in_window(conn, uid, start_iso))

                total_ops    = (await conn.execute("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ?", (uid, start_iso))).fetchone()['cnt'] or 0
                leader_ops   = (await conn.execute("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.created_at >= ?", (uid, start_iso))).fetchone()['cnt'] or 0
                af_ops       = (await conn.execute("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_airforce = 1 AND r.created_at >= ?", (uid, start_iso))).fetchone()['cnt'] or 0
                reporter_ops = (await conn.execute("SELECT COUNT(*) as cnt FROM reports WHERE reporter_id = ? AND created_at >= ?", (uid, start_iso))).fetchone()['cnt'] or 0

                pts_row   = (await conn.execute("""
                    SELECT SUM(promo) as promo, SUM(af) as af, SUM(lead) as lead FROM (
                        SELECT promo_points_gained as promo, af_points_gained as af, leadership_points_gained as lead
                        FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ?
                        UNION ALL
                        SELECT
                            CASE WHEN point_type='promo' THEN amount ELSE 0 END,
                            CASE WHEN point_type='af' THEN amount ELSE 0 END,
                            CASE WHEN point_type='leadership' THEN amount ELSE 0 END
                        FROM manual_points_awards WHERE discord_id = ? AND created_at >= ?
                    )""", (uid, start_iso, uid, start_iso))).fetchone()
                promo_pts = pts_row['promo'] or 0
                af_pts    = pts_row['af'] or 0
                lead_pts  = pts_row['lead'] or 0

                extra_requests = (await conn.execute("SELECT participants FROM extra_points_requests WHERE status = 'approved' AND approved_at IS NOT NULL AND approved_at >= ?", (start_iso,))).fetchall()
                promo_pts += sum(1 for req in extra_requests if req['participants'] and uid in [p.strip() for p in req['participants'].split(',')])

                leader_success  = (await conn.execute(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.created_at >= ? AND ({self._success_clause()})", (uid, start_iso))).fetchone()['succ'] or 0
                leader_win_rate = (leader_success / leader_ops * 100) if leader_ops else 0

                byop = (await conn.execute("SELECT r.report_type, COUNT(*) as ops, SUM(op.promo_points_gained) as promo FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? GROUP BY r.report_type ORDER BY ops DESC", (uid, start_iso))).fetchall()
                byop_op_types = [r['report_type'] for r in byop]
                byop_leaders_data = (await conn.execute(
                    f"SELECT r.report_type, COUNT(*) as led, SUM(CASE WHEN ({self._success_clause()}) THEN 1 ELSE 0 END) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.report_type IN ({','.join(['?'] * len(byop_op_types))}) AND r.created_at >= ? GROUP BY r.report_type",
                    [uid] + byop_op_types + [start_iso]
                )).fetchall() if byop_op_types else []
                byop_leaders = {r['report_type']: {'led': r['led'], 'succ': r['succ'] or 0} for r in byop_leaders_data}
                for t in byop_op_types:
                    byop_leaders.setdefault(t, {'led': 0, 'succ': 0})

                aerial_ops_with_cover = {}
                try:
                    aerial_ops_with_cover = {r['report_type']: r['covered'] for r in (await conn.execute(
                        "SELECT r.report_type, COUNT(*) as covered FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? AND op.was_airforce = 1 AND r.report_type IN ('warzone', 'andromada_ops', 'ammo_transport', 'andromada_terrors_ops') GROUP BY r.report_type",
                        (uid, start_iso)
                    )).fetchall()}
                except Exception:
                    pass


            start_dt  = datetime.fromisoformat(start_iso.replace('Z', '+00:00')).astimezone(timezone.utc)
            end_dt    = datetime.fromisoformat(now_iso.replace('Z', '+00:00')).astimezone(timezone.utc)
            date_range = f"{start_dt.strftime('%b %d')} - {end_dt.strftime('%b %d')}"
            description = f"Activity from {start_dt.strftime('%b %d, %Y')} to {end_dt.strftime('%b %d, %Y')} (All time)" if days is None else f"Last {days} days | {date_range}"

            embed = create_embed(title=f"Member Stats - {ig}", description=description, user=user)

            rank_promo  = rank_stats_row['promo_points']    if rank_stats_row  else 0
            rank_af     = rank_stats_row['af_points']       if rank_stats_row  else 0
            rank_lead   = rank_stats_row['leadership_points'] if rank_stats_row else 0
            career_promo = career_stats_row['promo_points']    if career_stats_row else 0
            career_af    = career_stats_row['af_points']       if career_stats_row else 0
            career_lead  = career_stats_row['leadership_points'] if career_stats_row else 0
            total_pts    = promo_pts + lead_pts + (af_pts if is_af_flag else 0)

            no_result_ops = {'training', 'af_training', 'patrol'}
            aerial_ops    = {'warzone', 'andromada_ops', 'ammo_transport', 'andromada_terrors_ops'}

            online_cog = self.bot.get_cog("Online")
            is_online  = (
                online_cog is not None
                and uid in getattr(online_cog, "_previously_online", set())
            )

            playtime_label = f"Playtime ({'this period' if days else 'all time'})"

            lines = [
                f"Name: `{ig}`",
                f"Rank: `{rank}`",
                *(([f"AF Rank: `{af_rank}`"] if is_af_flag and af_rank and af_rank != 'None' else [])),
                f"Member Since: {self._format_date(date_added)}",
                f"Last Online: {self._fmt_last_online(last_online, is_online)}",
                f"{playtime_label}: `{format_playtime(window_playtime)}`",
                "",
                "**PARTICIPATION**",
                f"Total ops: `{total_ops}`",
                f"Leader: `{leader_ops}` ops",
                *(([f"AF: `{af_ops}` ops"] if is_af_flag else [])),
                f"Reporter: `{reporter_ops}` ops",
                "",
                "**POINTS EARNED**",
                f"Promotional pts: `{promo_pts}`",
                *(([f"Air Force pts: `{af_pts}`"] if is_af_flag else [])),
                f"Leadership pts: `{lead_pts}`",
                f"Total: `{total_pts}`",
                "",
                "**CURRENT TOTALS**",
                f"Rank: Promo `{rank_promo}` | Lead `{rank_lead}`" + (f" | AF `{rank_af}`" if is_af_flag else ""),
                f"Career: Promo `{career_promo}` | Lead `{career_lead}`" + (f" | AF `{career_af}`" if is_af_flag else ""),
                "",
                "**BY OPERATION**",
            ]
            for r in byop:
                op_name = OPERATION_TYPES.get(r['report_type'], r['report_type'])
                led     = byop_leaders[r['report_type']]['led']
                succ    = byop_leaders[r['report_type']]['succ']
                if r['report_type'] in no_result_ops:
                    line = f"{op_name}: `{r['ops']}` ops" + (f" (hosted `{led}`)" if led else "")
                elif led == 0:
                    line = f"{op_name}: `{r['ops']}` ops"
                else:
                    win_rate = (succ / led * 100) if led else 0
                    line = f"{op_name}: `{r['ops']}` ops (led `{led}`, win: `{win_rate:.0f}%`)"
                if is_af_flag and r['report_type'] in aerial_ops and r['report_type'] in aerial_ops_with_cover:
                    flown = aerial_ops_with_cover[r['report_type']]
                    line  = (line[:-1] + f", flown `{flown}`)") if line.endswith(')') else (line + f" (flown `{flown}`)")
                lines.append(line)
            lines.append(f"Total Leadership: `{leader_ops}` ops led (win: `{leader_win_rate:.0f}%`)")

            embed.add_field(name="", value="\n".join(lines), inline=False)
            await interaction.followup.send(embed=embed)
        except db_errors.Error as e:
            logger.error(f"Analytics DB error: {e}", exc_info=True)
            await interaction.followup.send(**plain("Database error while generating member stats."), ephemeral=True)

    @stats.command(name="operation", description="Operation-type stats")
    @require_permissions(admin=True, acl="view_analytics")
    @app_commands.describe(op_type="Operation type", days="Days to include (default all time)")
    @app_commands.autocomplete(op_type=operation_type_autocomplete)
    async def operation(self, interaction: Interaction, op_type: str, days: Optional[int] = None):
        await interaction.response.defer(ephemeral=False)
        if days is not None and days <= 0:
            await interaction.followup.send(**plain("Days must be at least 1."), ephemeral=True)
            return
        if op_type not in OPERATION_TYPES:
            await interaction.followup.send(**plain("The specified operation type was not found."), ephemeral=True)
            return
        start_iso, _ = self._period_bounds(days)
        try:
            async with aget_db_connection() as conn:
                total_ops   = (await conn.execute("SELECT COUNT(*) as cnt FROM reports WHERE report_type = ? AND created_at >= ?", (op_type, start_iso))).fetchone()['cnt'] or 0
                res         = (await conn.execute("SELECT result, COUNT(*) as cnt FROM reports WHERE report_type = ? AND created_at >= ? GROUP BY result", (op_type, start_iso))).fetchall()
                res_map     = {r['result']: r['cnt'] for r in res}
                parts_row   = (await conn.execute("SELECT COUNT(*) as cnt, COUNT(DISTINCT discord_id) as unique_count FROM operation_participants WHERE report_id IN (SELECT id FROM reports WHERE report_type = ? AND created_at >= ?)", (op_type, start_iso))).fetchone()
                total_parts  = parts_row['cnt'] or 0
                unique_members = parts_row['unique_count'] or 0
                avg_per_op   = total_parts / total_ops if total_ops else 0
                pts_row      = (await conn.execute("SELECT SUM(promo_points_gained) as promo, SUM(af_points_gained) as af, SUM(leadership_points_gained) as lead FROM operation_participants WHERE report_id IN (SELECT id FROM reports WHERE report_type = ? AND created_at >= ?)", (op_type, start_iso))).fetchone()
                promo_pts    = pts_row['promo'] or 0
                af_pts       = pts_row['af'] or 0
                lead_pts     = pts_row['lead'] or 0
                leader_ops   = (await conn.execute("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.report_type = ? AND r.created_at >= ?", (op_type, start_iso))).fetchone()['cnt'] or 0
                leader_success = (await conn.execute(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.report_type = ? AND r.created_at >= ? AND ({self._success_clause()})", (op_type, start_iso))).fetchone()['succ'] or 0
                leader_win_rate = (leader_success / leader_ops * 100) if leader_ops else 0

                aerial_op_types = {'warzone', 'andromada_ops', 'ammo_transport', 'andromada_terrors_ops'}

                top_leaders      = (await conn.execute("SELECT op.discord_id, COUNT(*) as ops FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_leader = 1 AND r.report_type = ? AND r.created_at >= ? GROUP BY op.discord_id ORDER BY ops DESC LIMIT 5", (op_type, start_iso))).fetchall()
                top_participants = (await conn.execute("SELECT op.discord_id, COUNT(*) as ops FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE r.report_type = ? AND r.created_at >= ? GROUP BY op.discord_id ORDER BY ops DESC LIMIT 5", (op_type, start_iso))).fetchall()
                top_airforce     = (await conn.execute("SELECT op.discord_id, COUNT(*) as ops FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.was_airforce = 1 AND r.report_type = ? AND r.created_at >= ? GROUP BY op.discord_id ORDER BY ops DESC LIMIT 5", (op_type, start_iso))).fetchall() if op_type in aerial_op_types or op_type.startswith('af_') else []

                all_ids  = {r['discord_id'] for r in top_leaders + top_participants + top_airforce}
                name_map = (await self._fetch_name_map(conn, all_ids))

            title = f"{OPERATION_TYPES.get(op_type, op_type).upper()} - {'ALL TIME' if days is None else f'LAST {days} DAYS'}"
            embed = create_embed(title=title)
            no_result_ops = {'training', 'af_training', 'patrol'}
            lines = [f"Total: `{total_ops}` ops", ""]
            if op_type not in no_result_ops:
                lines.append("**RESULTS**")
                if op_type == 'warzone':
                    for o, label in (('4-0', 'Win'), ('3-1', 'Win'), ('2-2', 'Draw'), ('1-3', 'Loss'), ('0-4', 'Loss')):
                        cnt = res_map.get(o, 0)
                        lines.append(f"`{o}` {label}: `{cnt}` ops ({(cnt / total_ops * 100 if total_ops else 0):.1f}%)")
                elif op_type == 'af_warzone':
                    for label, key in (("Success", "Success"), ("Draw", "Draw"), ("Failure", "Failure")):
                        cnt = res_map.get(key, 0)
                        lines.append(f"{label}: `{cnt}` ({(cnt / total_ops * 100 if total_ops else 0):.1f}%)")
                else:
                    for label, key in (("Success", "Success"), ("Failure", "Failure")):
                        cnt = res_map.get(key, 0)
                        lines.append(f"{label}: `{cnt}` ({(cnt / total_ops * 100 if total_ops else 0):.1f}%)")
                lines.append("")
            lines += [
                "**PARTICIPATION**",
                f"Total: `{total_parts}`",
                f"Avg/Activity: `{avg_per_op:.1f}`",
                *(([f"Top Leader: {name_map.get(top_leaders[0]['discord_id'], top_leaders[0]['discord_id'])}"] if top_leaders else [])),
                f"Unique: `{unique_members}` members",
                "",
                "**LEADERSHIP**",
                f"Ops Led: `{leader_ops}`",
                *(([f"Win Rate: `{leader_win_rate:.1f}%` (`{leader_success}/{leader_ops}`)"] if op_type not in no_result_ops else [])),
                f"Total Pts: `{lead_pts}`",
                "",
                "**POINTS**",
            ]
            if op_type == 'af_training':
                lines += [f"Leadership pts: `{lead_pts}`", f"Air Force pts: `{af_pts}`"]
            elif op_type in {'training', 'patrol'}:
                lines += [f"Promotional pts: `{promo_pts}`", f"Leadership pts: `{lead_pts}`"]
            else:
                lines += [f"Promotional pts: `{promo_pts}`", f"Leadership pts: `{lead_pts}`", f"Air Force pts: `{af_pts}`"]
            lines += [
                "",
                "**TOP 5 LEADERS**",
                *([f"{i}. {name_map.get(r['discord_id'], str(r['discord_id']))}: `{r['ops']}` ops" for i, r in enumerate(top_leaders, 1)] or ["No leaders yet"]),
                "",
                "**TOP 5 PARTICIPANTS**",
                *([f"{i}. {name_map.get(r['discord_id'], str(r['discord_id']))}: `{r['ops']}` ops" for i, r in enumerate(top_participants, 1)] or ["No participants yet"]),
            ]
            if op_type in aerial_op_types:
                lines += [
                    "",
                    "**TOP 5 AIRFORCE**",
                    *([f"{i}. {name_map.get(r['discord_id'], str(r['discord_id']))}: `{r['ops']}` ops" for i, r in enumerate(top_airforce, 1)] or ["No airforce yet"]),
                ]
            embed.add_field(name="", value="\n".join(lines), inline=False)
            await interaction.followup.send(embed=embed)
        except db_errors.Error as e:
            logger.error(f"Analytics DB error: {e}", exc_info=True)
            await interaction.followup.send(**plain("Database error while generating operation stats."), ephemeral=True)

    @stats.command(name="recruitment", description="Recruitment wave stats and charts")
    @require_permissions(admin=True, acl="view_analytics")
    @app_commands.describe(waves="Number of most recent waves to include (default ALL)")
    async def recruitment(self, interaction: Interaction, waves: Optional[int] = None):
        await interaction.response.defer(ephemeral=False)
        if waves is not None and not 1 <= waves <= 100:
            await send_private_error(interaction, "Waves must be between 1 and 100.")
            return
        cog = self.bot.get_cog("Recruitment")
        if not cog:
            await send_private_error(interaction, "The recruitment system is not loaded.")
            return
        try:
            wave_stats = await cog.collect_wave_stats(limit=waves + 1 if waves else None)
        except discord.Forbidden:
            await send_private_error(interaction, "I can't read the recruitment logs channel's threads.")
            return
        if wave_stats is None:
            await send_private_error(interaction, MSG_NOT_SET_UP_STAFF)
            return

        rows = _build_wave_rows(wave_stats)
        if waves:
            rows = rows[-waves:]
        if not rows:
            await send_private_error(interaction, "No recruitment waves found yet.")
            return

        closed     = [r for r in rows if not r.is_open]
        gaps       = [r.gap_days for r in rows if r.gap_days is not None]
        total_apps = sum(r.apps for r in rows)
        accepted   = sum(r.accepted for r in rows)
        decided    = accepted + sum(r.denied for r in rows)
        avg        = lambda values: (sum(values) / len(values)) if values else 0

        embed = create_embed(
            title=f"RECRUITMENT - {'ALL WAVES' if not waves else f'LAST {len(rows)} WAVES'}",
            description=_recruitment_table(rows),
        )
        embed.add_field(
            name="Summary",
            value="\n".join([
                f"Total apps: `{total_apps}`",
                f"Avg per wave: `{avg([r.apps for r in closed]):.2f}`",
                f"Avg wave length: `{avg([r.length_days for r in closed]):.2f}` days",
                f"Avg time between waves: `{avg(gaps):.2f}` days",
                f"Accepted: `{accepted}/{decided}` ({(accepted / decided * 100 if decided else 0):.1f}%)",
            ]),
            inline=False,
        )

        buf = await asyncio.to_thread(_generate_recruitment_chart, rows)
        if buf:
            embed.set_image(url="attachment://recruitment_waves.png")
            await interaction.followup.send(embed=embed, file=discord.File(buf, filename="recruitment_waves.png"))
        else:
            await interaction.followup.send(embed=embed)

    @tasks.loop(seconds=604800)
    async def send_weekly_reports(self):
        if not WEEKLY_REPORT.get('enabled', True):
            return
        try:
            now             = datetime.now(timezone.utc)
            one_week_ago    = now - timedelta(days=7)
            async with aget_db_connection() as conn:
                members = (await conn.execute(
                    "SELECT discord_id FROM members WHERE status = 'Active' AND date_added < ?",
                    (one_week_ago.isoformat(),)
                )).fetchall()
            for member_row in members:
                discord_id = member_row['discord_id']
                try:
                    user = await self.bot.fetch_user(int(discord_id))
                    await self._send_member_weekly_report(user, discord_id)
                except Exception as e:
                    logger.error(f"Failed to send weekly report to {discord_id}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Weekly report scheduler error: {e}", exc_info=True)

    async def _send_member_weekly_report(self, user: discord.User, discord_id: str):
        now                  = datetime.now(timezone.utc)
        this_week_start_iso  = (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        last_week_start_iso  = (now - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
        last_week_end_iso    = (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')

        try:
            async with aget_db_connection() as conn:
                member_row = (await conn.execute("SELECT ig_name, rank, is_af, af_rank, date_added FROM members WHERE discord_id = ?", (discord_id,))).fetchone()
                ig_name    = member_row['ig_name']   if member_row else str(user)
                rank       = member_row['rank']      if member_row else 'Unknown'
                af_rank    = member_row['af_rank']   if member_row else None
                is_af_flag = bool(member_row['is_af']) if member_row else False

                async def _cnt(query, params):
                    r = (await conn.execute(query, params)).fetchone()
                    return r[list(r.keys())[0]] if r else 0

                week_start_date = (now - timedelta(days=7)).strftime('%Y-%m-%d')
                playtime_row    = (await conn.execute(
                    "SELECT COALESCE(SUM(minutes), 0) as total FROM playtime_log WHERE discord_id = ? AND log_date >= ?",
                    (discord_id, week_start_date)
                )).fetchone()
                weekly_playtime = playtime_row['total'] if playtime_row else 0

                total_ops_this    = await _cnt("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ?", (discord_id, this_week_start_iso))
                leader_ops_this   = await _cnt("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.created_at >= ?", (discord_id, this_week_start_iso))
                af_ops_this       = await _cnt("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_airforce = 1 AND r.created_at >= ?", (discord_id, this_week_start_iso))
                reporter_ops_this = await _cnt("SELECT COUNT(*) as cnt FROM reports WHERE reporter_id = ? AND created_at >= ?", (discord_id, this_week_start_iso))
                succ_this         = await _cnt(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? AND ({self._success_clause()})", (discord_id, this_week_start_iso))

                pts_this      = (await conn.execute("""
                    SELECT SUM(promo) as promo, SUM(af) as af, SUM(lead) as lead FROM (
                        SELECT promo_points_gained as promo, af_points_gained as af, leadership_points_gained as lead
                        FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ?
                        UNION ALL
                        SELECT
                            CASE WHEN point_type='promo' THEN amount ELSE 0 END,
                            CASE WHEN point_type='af' THEN amount ELSE 0 END,
                            CASE WHEN point_type='leadership' THEN amount ELSE 0 END
                        FROM manual_points_awards WHERE discord_id = ? AND created_at >= ?
                    )""", (discord_id, this_week_start_iso, discord_id, this_week_start_iso))).fetchone()
                promo_pts_this = pts_this['promo'] or 0
                af_pts_this    = pts_this['af'] or 0
                lead_pts_this  = pts_this['lead'] or 0

                extra_requests = (await conn.execute("SELECT participants FROM extra_points_requests WHERE status = 'approved' AND approved_at IS NOT NULL AND approved_at >= ?", (this_week_start_iso,))).fetchall()
                promo_pts_this += sum(1 for req in extra_requests if req['participants'] and str(discord_id) in [p.strip() for p in req['participants'].split(',')])
                total_pts_this = promo_pts_this + af_pts_this + lead_pts_this

                leader_success_this  = await _cnt(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.created_at >= ? AND ({self._success_clause()})", (discord_id, this_week_start_iso))
                leader_win_rate_this = (leader_success_this / leader_ops_this * 100) if leader_ops_this else 0
                success_rate_this    = (succ_this / total_ops_this * 100) if total_ops_this else 0

                byop_this       = (await conn.execute("SELECT r.report_type, COUNT(*) as ops, SUM(op.promo_points_gained) as promo FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? GROUP BY r.report_type ORDER BY ops DESC", (discord_id, this_week_start_iso))).fetchall()
                byop_op_types   = [r['report_type'] for r in byop_this]
                byop_leaders_data_this = (await conn.execute(
                    f"SELECT r.report_type, COUNT(*) as led, SUM(CASE WHEN ({self._success_clause()}) THEN 1 ELSE 0 END) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.report_type IN ({','.join(['?'] * len(byop_op_types))}) AND r.created_at >= ? GROUP BY r.report_type",
                    [discord_id] + byop_op_types + [this_week_start_iso]
                )).fetchall() if byop_op_types else []
                byop_leaders_this = {r['report_type']: {'led': r['led'], 'succ': r['succ'] or 0} for r in byop_leaders_data_this}
                for t in byop_op_types:
                    byop_leaders_this.setdefault(t, {'led': 0, 'succ': 0})

                aerial_ops_with_cover = {}
                if is_af_flag:
                    try:
                        aerial_ops_with_cover = {r['report_type']: r['covered'] for r in (await conn.execute(
                            "SELECT r.report_type, COUNT(*) as covered FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? AND op.was_airforce = 1 AND r.report_type IN ('warzone', 'andromada_ops', 'ammo_transport', 'andromada_terrors_ops') GROUP BY r.report_type",
                            (discord_id, this_week_start_iso)
                        )).fetchall()}
                    except Exception:
                        pass

                total_ops_last    = await _cnt("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? AND r.created_at < ?", (discord_id, last_week_start_iso, last_week_end_iso))
                leader_ops_last   = await _cnt("SELECT COUNT(DISTINCT op.report_id) as cnt FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.created_at >= ? AND r.created_at < ?", (discord_id, last_week_start_iso, last_week_end_iso))
                succ_last         = await _cnt(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? AND r.created_at < ? AND ({self._success_clause()})", (discord_id, last_week_start_iso, last_week_end_iso))

                pts_last      = (await conn.execute("SELECT SUM(promo_points_gained) as promo, SUM(af_points_gained) as af, SUM(leadership_points_gained) as lead FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND r.created_at >= ? AND r.created_at < ?", (discord_id, last_week_start_iso, last_week_end_iso))).fetchone()
                promo_pts_last = pts_last['promo'] or 0
                af_pts_last    = pts_last['af'] or 0
                lead_pts_last  = pts_last['lead'] or 0
                total_pts_last = promo_pts_last + af_pts_last + lead_pts_last

                leader_success_last  = await _cnt(f"SELECT COUNT(DISTINCT op.report_id) as succ FROM operation_participants op JOIN reports r ON r.id = op.report_id WHERE op.discord_id = ? AND op.was_leader = 1 AND r.created_at >= ? AND r.created_at < ? AND ({self._success_clause()})", (discord_id, last_week_start_iso, last_week_end_iso))
                leader_win_rate_last = (leader_success_last / leader_ops_last * 100) if leader_ops_last else 0
                success_rate_last    = (succ_last / total_ops_last * 100) if total_ops_last else 0

            week_start_str = self._format_date(this_week_start_iso)
            week_end_str   = self._format_date(now.strftime('%Y-%m-%d %H:%M:%S'))
            embed = create_embed(
                title="Your Weekly Report",
                description=f"Hello {ig_name}! 👋\nActivity from {week_start_str} to {week_end_str}",
                user=user
            )

            no_result_ops = {'training', 'af_training', 'patrol'}
            aerial_ops    = {'warzone', 'andromada_ops', 'ammo_transport', 'andromada_terrors_ops'}

            lines = [
                f"Rank: `{rank}`",
                *(([f"AF Rank: `{af_rank}`"] if is_af_flag and af_rank and af_rank != 'None' else [])),
                f"Playtime this week: `{format_playtime(weekly_playtime)}`",
                "",
                "**PARTICIPATION**",
                f"Total Ops: `{total_ops_this}`",
                f"Leader: `{leader_ops_this}` ops",
                *(([f"AF: `{af_ops_this}` ops"] if is_af_flag else [])),
                f"Reporter: `{reporter_ops_this}` ops",
                "",
                "**POINTS EARNED**",
                f"Promotional pts: `{promo_pts_this}`",
                *(([f"Air Force pts: `{af_pts_this}`"] if is_af_flag else [])),
                f"Leadership pts: `{lead_pts_this}`",
                f"Total: `{total_pts_this}`",
                "",
                "**BY OPERATION**",
            ]
            for r in byop_this:
                op_name = OPERATION_TYPES.get(r['report_type'], r['report_type'])
                led     = byop_leaders_this[r['report_type']]['led']
                succ    = byop_leaders_this[r['report_type']]['succ']
                if r['report_type'] in no_result_ops:
                    line = f"{op_name}: {r['ops']} ops" + (f" (hosted `{led}`)" if led else "")
                elif led == 0:
                    line = f"{op_name}: `{r['ops']}` ops"
                else:
                    win_rate = (succ / led * 100) if led else 0
                    line = f"{op_name}: `{r['ops']}` ops (led `{led}`, win: `{win_rate:.0f}%`)"
                if is_af_flag and r['report_type'] in aerial_ops and r['report_type'] in aerial_ops_with_cover:
                    covered = aerial_ops_with_cover[r['report_type']]
                    line = (line[:-1] + f", flown `{covered}`)") if line.endswith(')') else (line + f" (flown `{covered}`)")
                lines.append(line)
            lines.append(f"Total Leadership: `{leader_ops_this}` ops led (win: `{leader_win_rate_this:.0f}%`)")

            def change_str(new, old):
                if old == 0:
                    return "∞" if new > 0 else "0%"
                pct  = (new - old) / old * 100
                sign = "+" if pct > 0 else ""
                return f"{sign}{pct:.0f}%"

            lines += [
                "",
                "**COMPARED TO LAST WEEK**",
                f"Ops: `{total_ops_last}` to `{total_ops_this}`  {change_str(total_ops_this, total_ops_last)}",
                f"Points: `{total_pts_last}` to `{total_pts_this}`  {change_str(total_pts_this, total_pts_last)}",
                f"Success: `{success_rate_last:.0f}%` to `{success_rate_this:.0f}%`  {change_str(success_rate_this, success_rate_last)}",
                *(([f"Leadership: `{leader_win_rate_last:.0f}%` to `{leader_win_rate_this:.0f}%`  {change_str(leader_win_rate_this, leader_win_rate_last)}"] if leader_ops_last > 0 or leader_ops_this > 0 else [])),
            ]

            activity_eval = self._evaluate_activity(total_pts_this, total_pts_last, ACTIVITY_THRESHOLDS)
            pt_diff       = total_pts_this - total_pts_last
            pt_diff_str   = f"up {pt_diff} pts from last week" if pt_diff > 0 else (f"down {abs(pt_diff)} pts from last week" if pt_diff < 0 else "no change from last week")
            level_key     = activity_eval['level'].split(' ')[0]
            feedback      = random.choice(FEEDBACK_MESSAGES[level_key]) if level_key in FEEDBACK_MESSAGES else ""

            lines += [
                "",
                "**FEEDBACK**",
                f"Activity: {activity_eval['level']} ({pt_diff_str})",
                "",
                feedback,
            ]

            embed.add_field(name="", value="\n".join(lines), inline=False)
            try:
                await user.send(embed=embed)
            except discord.Forbidden:
                pass
            except Exception as e:
                logger.error(f"Error sending DM to {discord_id}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error generating weekly report for {discord_id}: {e}", exc_info=True)

    @send_weekly_reports.before_loop
    async def before_weekly_reports(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(86400)


async def setup(bot: commands.Bot) -> None:
    cog = Analytics(bot)
    cog.send_weekly_reports.start()
    await bot.add_cog(cog)
