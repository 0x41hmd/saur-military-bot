import asyncio
import io
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import discord
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from discord.ext import commands
from matplotlib.patches import Patch

from database.db import get_db_connection
from utils.helpers import require_permissions, create_embed, plain, format_playtime
from utils.settings import (
    ACTIVITY_THRESHOLDS, ACTIVITY_COLORS,
    CHART_WIDTH, CHART_MIN_HEIGHT, CHART_MAX_HEIGHT, CHART_HEIGHT_MULTIPLIER,
    CHART_DPI, CHART_BACKGROUND, CHART_BORDER_COLOR, CHART_BORDER_WIDTH,
    CHART_GRID_COLOR, CHART_GRID_ALPHA,
    MAX_RECOMMENDATIONS, MAX_RECOMMENDATIONS_PER_CATEGORY,
)

logger = logging.getLogger(__name__)


EXCLUDED_ACCOUNTS = {"legend1337"}

_CSV_HEADER_TOKENS  = {'account', 'rank', 'time', 'play'}
_CSV_PATTERN        = re.compile(r'([^\-]+?)\s*-\s*([^\-\s]+)\s*-\s*([^\-]+?)\s*-\s*([\d:]+)')
_TIER_ORDER         = ("Exceptional", "High", "Moderate", "Low", "Inactive")
_TIER_LEVEL         = {t: i for i, t in enumerate(reversed(_TIER_ORDER))}


def classify_activity(points: float) -> str:
    for tier in ("Exceptional", "High", "Moderate", "Low"):
        if points >= ACTIVITY_THRESHOLDS[tier]:
            return tier
    return "Inactive"


def _parse_csv_accounts(csv: str) -> set:
    accounts = set()
    for match in _CSV_PATTERN.findall(csv):
        account = match[1].strip().lower()
        if account and account not in _CSV_HEADER_TOKENS and account not in EXCLUDED_ACCOUNTS and len(account) > 2:
            accounts.add(account)
    return accounts


def _build_activity_breakdown(members: List[dict]) -> Dict[str, List[dict]]:
    breakdown = {t: [] for t in _TIER_ORDER}
    for m in members:
        breakdown[classify_activity(m['total_points'])].append(m)
    return breakdown


def _get_recommendations(
    breakdown: Dict[str, List[dict]],
    slotted_ids: set,
) -> List[dict]:
    if not slotted_ids:
        return []

    all_members = [m for tier in _TIER_ORDER for m in breakdown[tier]]
    all_members.sort(key=lambda m: m['total_points'], reverse=True)

    without_slot = [m for m in all_members if m['discord_id'] not in slotted_ids
                    and (m.get('account_name') or '').lower() not in EXCLUDED_ACCOUNTS]
    with_slot    = [m for m in all_members if m['discord_id'] in slotted_ids
                    and (m.get('account_name') or '').lower() not in EXCLUDED_ACCOUNTS]

    min_tier = _TIER_LEVEL["Low"]
    recs = []
    for candidate_in in without_slot:
        if _TIER_LEVEL.get(classify_activity(candidate_in['total_points']), 0) < min_tier:
            continue
        for candidate_out in with_slot:
            if candidate_in['total_points'] > candidate_out['total_points']:
                recs.append({
                    "to":   candidate_in,
                    "from": candidate_out,
                    "gain": candidate_in['total_points'] - candidate_out['total_points'],
                })
    recs.sort(key=lambda x: x['gain'], reverse=True)
    return recs[:MAX_RECOMMENDATIONS]


def _fetch_members(conn, start_date: Optional[datetime]) -> List[dict]:
    if start_date is None:
        rows = conn.execute("""
            SELECT
                m.discord_id,
                m.ig_name,
                m.account_name,
                m.rank,
                COALESCE(pl.total_minutes, 0) AS playtime_minutes,
                COALESCE(p.promo_points,      0) + COALESCE(rs.promo_points,      0) AS promo_pts,
                COALESCE(p.leadership_points, 0) + COALESCE(rs.leadership_points, 0) AS lead_pts,
                COALESCE(p.af_points,         0) + COALESCE(rs.af_points,         0) AS af_pts,
                COALESCE(p.promo_points,      0) + COALESCE(p.leadership_points,  0) + COALESCE(p.af_points, 0) +
                COALESCE(rs.promo_points,     0) + COALESCE(rs.leadership_points, 0) + COALESCE(rs.af_points, 0)
                    AS total_points
            FROM members m
            LEFT JOIN points      p  ON m.discord_id = p.discord_id
            LEFT JOIN rank_stats  rs ON m.discord_id = rs.discord_id AND m.rank = rs.rank
            LEFT JOIN (
                SELECT discord_id, SUM(minutes) AS total_minutes
                FROM playtime_log
                GROUP BY discord_id
            ) pl ON m.discord_id = pl.discord_id
            ORDER BY total_points DESC
        """).fetchall()
    else:
        dt_reports = start_date.strftime("%d/%m/%Y %H:%M")
        dt_manual  = start_date.strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute("""
            SELECT
                m.discord_id,
                m.ig_name,
                m.account_name,
                m.rank,
                COALESCE(pl.window_minutes, 0) AS playtime_minutes,
                COALESCE(ops.promo_pts,  0) + COALESCE(man.promo_pts,  0) AS promo_pts,
                COALESCE(ops.lead_pts,   0) + COALESCE(man.lead_pts,   0) AS lead_pts,
                COALESCE(ops.af_pts,     0) + COALESCE(man.af_pts,     0) AS af_pts,
                COALESCE(ops.promo_pts,  0) + COALESCE(ops.lead_pts,   0) + COALESCE(ops.af_pts, 0) +
                COALESCE(man.promo_pts,  0) + COALESCE(man.lead_pts,   0) + COALESCE(man.af_pts, 0)
                    AS total_points
            FROM members m
            LEFT JOIN (
                SELECT
                    op.discord_id,
                    SUM(op.promo_points_gained)      AS promo_pts,
                    SUM(op.leadership_points_gained) AS lead_pts,
                    SUM(op.af_points_gained)         AS af_pts
                FROM operation_participants op
                INNER JOIN reports r ON op.report_id = r.id
                WHERE r.date_time >= :dt_reports
                GROUP BY op.discord_id
            ) ops ON m.discord_id = ops.discord_id
            LEFT JOIN (
                SELECT
                    discord_id,
                    SUM(CASE WHEN point_type = 'promo'      THEN amount ELSE 0 END) AS promo_pts,
                    SUM(CASE WHEN point_type = 'leadership' THEN amount ELSE 0 END) AS lead_pts,
                    SUM(CASE WHEN point_type = 'af'         THEN amount ELSE 0 END) AS af_pts
                FROM manual_points_awards
                WHERE created_at >= :dt_manual
                GROUP BY discord_id
            ) man ON m.discord_id = man.discord_id
            LEFT JOIN (
                SELECT discord_id, SUM(minutes) AS window_minutes
                FROM playtime_log
                WHERE log_date >= :dt_log
                GROUP BY discord_id
            ) pl ON m.discord_id = pl.discord_id
            ORDER BY total_points DESC
        """, {"dt_reports": dt_reports, "dt_manual": dt_manual, "dt_log": start_date.strftime("%Y-%m-%d")}).fetchall()

    return [dict(row) for row in rows]


def _generate_chart(
    members_sorted: List[Tuple[dict, str]],
    date_range: str,
    total_members: int,
    total_points: float,
    avg_points: float,
) -> Optional[io.BytesIO]:
    names  = [m['name'] for m, _ in members_sorted]
    points = [float(m['total_points']) for m, _ in members_sorted]
    levels = [lvl for _, lvl in members_sorted]
    if not names:
        return None

    try:
        plt.style.use('default')
        height = max(CHART_MIN_HEIGHT, min(CHART_MAX_HEIGHT, CHART_MIN_HEIGHT + len(names) * CHART_HEIGHT_MULTIPLIER))
        fig    = plt.figure(figsize=(CHART_WIDTH, height), dpi=CHART_DPI, facecolor=CHART_BACKGROUND)
        fig.patch.set_edgecolor(CHART_BORDER_COLOR)
        fig.patch.set_linewidth(CHART_BORDER_WIDTH)
        ax = plt.gca()
        ax.set_facecolor(CHART_BACKGROUND)
        ax.grid(axis='y', color=CHART_GRID_COLOR, linestyle='-', linewidth=0.7, alpha=CHART_GRID_ALPHA)
        for spine in ('top', 'right'):
            ax.spines[spine].set_visible(False)
        ax.spines['left'].set_color('#333333')
        ax.spines['bottom'].set_color('#333333')

        x_pos  = np.arange(len(names))
        colors = [ACTIVITY_COLORS.get(lvl, '#999999') for lvl in levels]
        bars   = ax.bar(x_pos, points, width=0.65, color=colors, alpha=0.92, edgecolor='#ffffff22', linewidth=1.5)

        for i, bar in enumerate(bars[:10]):
            if i < 3:
                bar.set_edgecolor('#ffffff');   bar.set_linewidth(2.8); bar.set_alpha(0.96)
            elif i < 7:
                bar.set_edgecolor('#ffffffaa'); bar.set_linewidth(2.2); bar.set_alpha(0.94)

        ax.set_xticks(x_pos)
        ax.set_xticklabels(names, fontsize=10, color='#000000', rotation=45, ha='right')
        plt.title('MILITARY ACTIVITY CHART', fontsize=24, fontweight='900', color='#000000', pad=25, alpha=0.98)
        plt.ylabel('Points', fontsize=14, fontweight='600', color='#333333', labelpad=15)

        max_pts = max(points) if points else 1
        for bar, pt in zip(bars, points):
            label = f"{pt:,.0f}" if pt >= 100 else f"{pt:.1f}"
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max_pts * 0.01,
                label, ha='center', va='bottom', fontsize=5.5, color='#000000', fontweight='bold',
            )

        present_levels = set(levels)
        legend_patches = [
            Patch(facecolor=ACTIVITY_COLORS[lvl], label=lvl, alpha=0.95)
            for lvl in _TIER_ORDER if lvl in present_levels
        ]
        ax.legend(handles=legend_patches, loc='upper right', frameon=False, fontsize=11,
                  labelcolor='#000000', handlelength=1.2)

        info = f"Total: {total_members} Members | Points: {total_points:,.0f} | Avg: {avg_points:.1f}"
        if date_range:
            info = f"{date_range.upper()} | {info}"
        plt.figtext(0.5, 0.96, info, ha='center', fontsize=11, color='#333333', fontweight='600', alpha=0.95)
        plt.figtext(
            0.5, 0.01,
            "Military Intelligence - Activity Analysis " + datetime.now().strftime("%Y-%m-%d"),
            ha='center', fontsize=9.5, color='#666666', style='italic',
        )
        plt.tight_layout(rect=[0.02, 0.03, 0.98, 0.95])

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=CHART_DPI, facecolor=CHART_BACKGROUND, bbox_inches='tight', pad_inches=0.25)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Chart generation failed: {e}", exc_info=True)
        return None
    finally:
        plt.close('all')


def _process_activity(csv: Optional[str], days: Optional[int]) -> Tuple[Optional[dict], Optional[str]]:
    now        = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=days)) if days else None
    date_range = (
        f"{start_date.strftime('%b %d, %Y')} to {now.strftime('%b %d, %Y')}"
        if start_date else "All Time"
    )

    with get_db_connection() as conn:
        members = _fetch_members(conn, start_date)

    if not members:
        return None, "No members found for the given parameters."

    for m in members:
        m['name'] = m.get('ig_name', 'Unknown')

    breakdown    = _build_activity_breakdown(members)
    total_points = sum(m['total_points'] for m in members)
    avg_points   = total_points / len(members) if members else 0
    active_count = sum(len(breakdown[t]) for t in ("Exceptional", "High", "Moderate"))
    total_playtime_minutes = sum(m.get('playtime_minutes', 0) for m in members)
    avg_playtime_minutes   = total_playtime_minutes / len(members) if members else 0

    category_stats = {
        t: (len(breakdown[t]), len(breakdown[t]) / len(members) * 100 if members else 0)
        for t in _TIER_ORDER
    }

    slotted_ids: set = set()
    if csv:
        csv_accounts = _parse_csv_accounts(csv)
        for tier in _TIER_ORDER:
            for m in breakdown[tier]:
                if (m.get('account_name') or '').lower() in csv_accounts:
                    slotted_ids.add(m['discord_id'])

    recommendations = _get_recommendations(breakdown, slotted_ids) if csv else []

    members_sorted: List[Tuple[dict, str]] = sorted(
        [(m, classify_activity(m['total_points'])) for m in members],
        key=lambda x: x[0]['total_points'], reverse=True,
    )

    chart = _generate_chart(
        members_sorted,
        date_range=date_range,
        total_members=len(members),
        total_points=total_points,
        avg_points=avg_points,
    )

    return {
        "members":               len(members),
        "active":                active_count,
        "total_points":          total_points,
        "avg_points":            avg_points,
        "total_playtime":        total_playtime_minutes,
        "avg_playtime":          avg_playtime_minutes,
        "category_stats":        category_stats,
        "date_range":            date_range,
        "recommendations":       recommendations,
        "slotted_ids":           slotted_ids,
        "chart":                 chart,
        "has_csv":               bool(csv),
    }, None


def _build_embed(result: dict) -> discord.Embed:
    title = "ACTIVITY ANALYSIS"
    desc  = f"Period: **{result['date_range']}**"
    embed = create_embed(title=title, description=desc)

    embed.add_field(
        name="STATISTICS",
        value=(
            f"Total Members: `{result['members']}`\n"
            f"Active Members: `{result['active']}`\n"
            f"Total Points:   `{int(result['total_points']):,}`\n"
            f"Average Points: `{result['avg_points']:.1f}`\n"
            f"Total Playtime: `{format_playtime(result['total_playtime'])}`\n"
            f"Avg Playtime:   `{format_playtime(result['avg_playtime'])}`"
        ),
        inline=False,
    )

    embed.add_field(
        name="CATEGORY BREAKDOWN",
        value="\n".join(
            f"{t}: `{count}` (`{pct:.1f}%`)"
            for t, (count, pct) in result['category_stats'].items()
        ),
        inline=False,
    )

    if result['has_csv']:
        recs = result['recommendations']
        if not recs:
            rec_value = "No transfers needed." if result['slotted_ids'] else "No slotted members found in CSV."
        else:
            seen_to, lines_to, idx = set(), [], 1
            for rec in recs:
                m = rec['to']
                if m['discord_id'] not in seen_to and len(seen_to) < MAX_RECOMMENDATIONS_PER_CATEGORY:
                    seen_to.add(m['discord_id'])
                    lines_to.append(f"{idx}. {m['name'][:15]} ({classify_activity(m['total_points'])})")
                    idx += 1

            seen_from, lines_from, idx = set(), [], 1
            for rec in recs:
                m = rec['from']
                if m['discord_id'] not in seen_from and len(seen_from) < MAX_RECOMMENDATIONS_PER_CATEGORY:
                    seen_from.add(m['discord_id'])
                    lines_from.append(f"{idx}. {m['name'][:15]} ({classify_activity(m['total_points'])})")
                    idx += 1

            rec_value = (
                "• **TO MAIN GROUP:**\n"
                + ("\n".join(lines_to) if lines_to else "None")
                + "\n\n• **TO LAW ACADEMY:**\n"
                + ("\n".join(lines_from) if lines_from else "None")
            )
        embed.add_field(name="TRANSFER RECOMMENDATIONS", value=rec_value, inline=False)

    if result['chart']:
        embed.add_field(name="VISUALIZATION", value="*Full activity chart below*", inline=False)
        embed.set_image(url="attachment://activity_chart.png")
    else:
        embed.add_field(name="VISUALIZATION", value="Chart generation failed.", inline=False)

    return embed


class Activity(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.app_commands.command(
        name="activity-check",
        description="Analyze member activity. CSV enables transfer recommendations.",
    )
    @require_permissions(admin=True, acl="view_activity")
    @discord.app_commands.describe(
        days="Restrict analysis to the last N days (omit for all-time)",
        csv="CSV of slotted members, enables transfer recommendations",
    )
    async def activity(
        self,
        interaction: discord.Interaction,
        days: Optional[int] = None,
        csv:  Optional[str] = None,
    ):
        if days is not None and days < 1:
            await interaction.response.send_message(**plain("Days must be at least 1."), ephemeral=True)
            return

        await interaction.response.defer()

        try:
            loop   = asyncio.get_event_loop()
            result, error = await loop.run_in_executor(None, _process_activity, csv, days)

            if error:
                await interaction.followup.send(error, ephemeral=True)
                return

            embed = _build_embed(result)
            files = []
            if result['chart']:
                result['chart'].seek(0)
                files.append(discord.File(result['chart'], filename="activity_chart.png"))

            await interaction.followup.send(embed=embed, files=files)

        except Exception as e:
            logger.error(f"Activity command error: {e}", exc_info=True)
            await interaction.followup.send(**plain("An unexpected error occurred."), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Activity(bot))
