import asyncio
import logging
from database import errors as db_errors
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands, Interaction, User, Embed
from discord.ext import commands, tasks

from database.db import aget_db_connection
from database.db import get_db_connection
from utils.helpers import require_permissions, create_embed, get_user_display_name, plain, format_rank_for_display

logger = logging.getLogger(__name__)


PROMOTION_REQUIREMENTS = {
    "Cadet": {
        "days": 7,            "min_days": 5,   "playtime_hours": 14,
        "points": 30,         "leadership_points": 0,
    },
    "Boot Camp Student": {
        "days": 7,           "min_days": 5,   "playtime_hours": 20,
        "points": 50,         "leadership_points": 0,
    },
    "Recruit": {
        "days": 13,           "min_days": 10,  "playtime_hours": 25,
        "points": 70,        "leadership_points": 0,
    },
    "Private": {
        "days": 25,           "min_days": 23,  "playtime_hours": 55,
        "points": 100,        "leadership_points": 0,
    },
    "Private First Class": {
        "days": 30,           "min_days": 27,  "playtime_hours": 65,
        "points": 100,        "leadership_points": 9,
    },
    "Specialist": {
        "days": 30,           "min_days": 27,  "playtime_hours": 75,
        "points": 100,        "leadership_points": 30,
    },
    "Corporal": {
        "days": float('inf'), "min_days": float('inf'),
        "playtime_hours": float('inf'),
        "points": float('inf'), "leadership_points": float('inf'),
    },
}

RANK_PROGRESSION = {
    "Cadet":                  "Boot Camp Student",
    "Boot Camp Student":      "Recruit",
    "Recruit":                "Private",
    "Private":                "Private First Class",
    "Private First Class":    "Specialist",
    "Specialist":             "Corporal",
}


def _check_eligible(
    days_held: int,
    playtime_minutes: int,
    req: dict,
    *,
    days_boost: int = 0,
    days_delay: int = 0,
    points: int = 0,
    lp: int = 0,
) -> tuple[bool, str]:
    full_days  = req.get("days", float('inf'))
    min_days   = req.get("min_days", full_days)
    pt_hours   = req.get("playtime_hours", float('inf'))
    req_pts    = req.get("points", 0)
    req_lp     = req.get("leadership_points", 0)

    if full_days == float('inf'):
        return False, ""

    points_ok = points >= req_pts
    lp_ok     = lp >= req_lp

    if not (points_ok and lp_ok):
        return False, ""

    playtime_hours_actual = playtime_minutes / 60.0

    adj_full_days = max(0, full_days - days_boost + days_delay)
    adj_min_days  = max(0, min_days  - days_boost + days_delay)

    if pt_hours != float('inf') and playtime_hours_actual >= pt_hours:
        if days_held >= adj_min_days:
            return True, "playtime"

    if days_held >= adj_full_days:
        return True, "days"

    return False, ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class Promotion(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._pending_embed_cache = None
        self._cache_timestamp = 0
        self._cache_ttl = 120
        self.auto_pending_embed.start()

    def _get_promotion_channel_id(self) -> Optional[int]:
        try:
            with get_db_connection() as conn:
                r = conn.execute("SELECT value FROM config WHERE key = 'promotion_channel'").fetchone()
                return int(r['value']) if r else None
        except (db_errors.Error, ValueError, KeyError):
            return None

    promotion = app_commands.Group(name="promotion", description="Promotion system commands")

    @staticmethod
    def _fill_rank_embed(
        embed: discord.Embed,
        rank: str,
        next_rank: str,
        reqs: dict,
        days_with_rank: int,
        playtime_minutes: int,
        points: int,
        lp: int,
        eligible: bool,
        path: str,
        days_boosted: int = 0,
        days_delayed: int = 0,
    ) -> None:
        adj_full = max(0, reqs["days"] - days_boosted + days_delayed)
        adj_min  = max(0, reqs["min_days"] - days_boosted + days_delayed)
        pt_hours = reqs["playtime_hours"]
        hours    = playtime_minutes / 60.0
        req_pts  = reqs["points"]
        req_lp   = reqs["leadership_points"]

        if eligible:
            how = "fast-track (playtime)" if path == "playtime" else "standard (days)"
            status = f"**Ready for promotion**\nQualified through the {how} path."
        else:
            needed = []
            if points < req_pts:
                needed.append(f"{_plural(req_pts - points, 'more promotional point')}")
            if lp < req_lp:
                needed.append(f"{_plural(req_lp - lp, 'more leadership point')}")
            time_ok = days_with_rank >= adj_full or (hours >= pt_hours and days_with_rank >= adj_min)
            if not time_ok:
                std_days  = max(0, adj_full - days_with_rank)
                fast_days = max(0, adj_min - days_with_rank)
                fast_hrs  = max(0.0, pt_hours - hours)
                needed.append(f"Standard path: {_plural(std_days, 'more day')}")
                fast = []
                if fast_days: fast.append(_plural(fast_days, 'more day'))
                if fast_hrs:  fast.append(f"{fast_hrs:.1f}h more playtime")
                needed.append("Fast-track path: " + " and ".join(fast))
            status = "**Not yet eligible**\nStill needed:\n" + "\n".join(f"\u2022 {n}" for n in needed)
        embed.add_field(name="`Status`", value=status, inline=False)

        embed.add_field(
            name="`Rank Path`",
            value=f"{format_rank_for_display(rank)} to {format_rank_for_display(next_rank)}",
            inline=False,
        )

        days_std = f"{days_with_rank}/{adj_full}"
        if days_boosted: days_std += f" (-{days_boosted}d)"
        if days_delayed: days_std += f" (+{days_delayed}d)"
        lines = [
            f"\u2022 **Days (standard):** {days_std}",
            f"\u2022 **Days (fast-track):** {days_with_rank}/{adj_min}",
            f"\u2022 **Playtime (fast-track):** {hours:.1f}h/{pt_hours:g}h",
            f"\u2022 **Promotional Points:** {points}/{req_pts:g}",
        ]
        if req_lp:
            lines.append(f"\u2022 **Leadership Points:** {lp}/{req_lp:g}")
        embed.add_field(name="`Requirements`", value="\n".join(lines), inline=False)


    @promotion.command(name="check", description="Check promotion eligibility for a specific member")
    @require_permissions(admin=True, acl="manage_promotions")
    @app_commands.describe(user="The member whose eligibility you want to check")
    async def check(self, interaction: Interaction, user: User):
        await interaction.response.defer(ephemeral=False)
        try:
            async with aget_db_connection() as conn:
                member = (await conn.execute(
                    "SELECT discord_id, ig_name, rank, status, last_updated, playtime_minutes "
                    "FROM members WHERE discord_id = ?",
                    (str(user.id),)
                )).fetchone()
                if not member:
                    await interaction.followup.send(**plain(f"{user.mention} not found in database."), ephemeral=True)
                    return
                result = (await conn.execute("""
                    SELECT
                        rs.promo_points, rs.leadership_points,
                        pb.days_decreased, pd.days_increased
                    FROM rank_stats rs
                    LEFT JOIN promotion_boosts pb ON rs.discord_id = pb.discord_id
                    LEFT JOIN promotion_delays pd ON rs.discord_id = pd.discord_id
                    WHERE rs.discord_id = ? AND rs.rank = ?
                """, (str(user.id), member['rank']))).fetchone()

                if not result:
                    result = (await conn.execute("""
                        SELECT pb.days_decreased, pd.days_increased
                        FROM promotion_boosts pb
                        LEFT JOIN promotion_delays pd ON pb.discord_id = pd.discord_id
                        WHERE pb.discord_id = ?
                        UNION ALL
                        SELECT NULL, pd.days_increased
                        FROM promotion_delays pd
                        WHERE NOT EXISTS (SELECT 1 FROM promotion_boosts pb WHERE pb.discord_id = pd.discord_id)
                        AND pd.discord_id = ?
                    """, (str(user.id), str(user.id)))).fetchone()

            promo_points      = result['promo_points'] or 0 if result else 0
            leadership_points = result['leadership_points'] or 0 if result else 0
            days_boosted      = result['days_decreased'] or 0 if result else 0
            days_delayed      = result['days_increased'] or 0 if result else 0
            playtime_minutes  = member['playtime_minutes'] or 0

            try:
                last_updated = datetime.fromisoformat(member['last_updated'].replace('Z', '+00:00'))
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError, AttributeError):
                last_updated = datetime.now(timezone.utc)

            days_with_rank = (datetime.now(timezone.utc) - last_updated).days

            reg_rank = member['rank']
            reg_reqs = PROMOTION_REQUIREMENTS.get(reg_rank)
            has_path = bool(reg_reqs) and reg_reqs["days"] != float('inf')

            eligible, path = False, ""
            if has_path:
                eligible, path = _check_eligible(
                    days_with_rank, playtime_minutes, reg_reqs,
                    days_boost=days_boosted, days_delay=days_delayed,
                    points=promo_points, lp=leadership_points,
                )

            embed = create_embed(
                title=f"Promotion Eligibility: {member['ig_name']}",
                description="",
                user=user,
            )
            embed.add_field(
                name="`Member Info`",
                value=(
                    f"\u2022 **Member:** {user.mention}\n"
                    f"\u2022 **In-Game name:** {member['ig_name']}\n"
                    f"\u2022 **Current rank:** {format_rank_for_display(reg_rank)}\n"
                    f"\u2022 **Time in rank:** {_plural(days_with_rank, 'day')}"
                ),
                inline=False,
            )

            if has_path:
                self._fill_rank_embed(
                    embed, reg_rank, RANK_PROGRESSION.get(reg_rank, "MAX"),
                    reg_reqs, days_with_rank, playtime_minutes,
                    promo_points, leadership_points, eligible, path,
                    days_boosted, days_delayed,
                )

                mods = []
                if days_boosted: mods.append(f"Boost: -{_plural(days_boosted, 'day')}")
                if days_delayed: mods.append(f"Delay: +{_plural(days_delayed, 'day')}")
                if mods:
                    embed.add_field(name="`Active Modifications`", value="\n".join(mods), inline=False)

                embed.add_field(
                    name="`How Promotion Works`",
                    value=(
                        "**Fast-track:** playtime threshold met and minimum days served\n"
                        "**Standard:** full days requirement met\n"
                        "Whichever is reached first counts. Points are required on both paths."
                    ),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="`Status`",
                    value="**Highest promotable rank**\nThere is no further automatic promotion from this rank.",
                    inline=False,
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in /promotion check: {e}", exc_info=True)
            await interaction.followup.send(**plain("An error occurred while checking eligibility."), ephemeral=True)


    @promotion.command(name="pending", description="Show members ready or pending promotion")
    @require_permissions(admin=True, acl="manage_promotions")
    async def pending(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=False)
        try:
            embed = await self._build_pending_embed()
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in /promotion pending: {e}", exc_info=True)
            await interaction.followup.send(**plain("Failed to generate list."), ephemeral=True)


    @promotion.command(name="manage", description="Manage promotion boosts/delays (HQ only)")
    @require_permissions(admin=True, acl="manage_promotions")
    @app_commands.describe(
        action="What do you want to do?",
        user="Target member (required for boost/delay/remove actions)",
        days="Days to add/subtract (required for add_boost / add_delay)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add boost (reduce days)",   value="add_boost"),
        app_commands.Choice(name="Add delay (increase days)", value="add_delay"),
        app_commands.Choice(name="Remove boost",              value="remove_boost"),
        app_commands.Choice(name="Remove delay",              value="remove_delay"),
        app_commands.Choice(name="List all active actions",   value="list"),
    ])
    async def manage(self, interaction: Interaction, action: str, user: Optional[User] = None, days: Optional[int] = None):
        await interaction.response.defer(ephemeral=False)
        try:
            async with aget_db_connection() as conn:
                if action in ("add_boost", "add_delay"):
                    if not user or days is None:
                        await interaction.followup.send(**plain(f"Both `user` and `days` are required for action: {action}"), ephemeral=True)
                        return
                    if days < 0:
                        await interaction.followup.send(**plain("Days must be non-negative."), ephemeral=True)
                        return

                    table  = "promotion_boosts" if action == "add_boost" else "promotion_delays"
                    column = "days_decreased"   if action == "add_boost" else "days_increased"
                    (await conn.execute(
                        f"INSERT OR REPLACE INTO {table} (discord_id, {column}, applied_by, applied_at) "
                        f"VALUES (?, ?, ?, datetime('now'))",
                        (str(user.id), days, str(interaction.user.id))
                    ))
                    (await conn.commit())

                    action_desc = "boosted" if action == "add_boost" else "delayed"
                    await interaction.followup.send(**plain(f"{user.mention} has been {action_desc} by `{days}` days."))

                    logs_cog = self.bot.get_cog("Logs")
                    if logs_cog:
                        boost_type = "promotion boost" if action == "add_boost" else "promotion delay"
                        await logs_cog.log_action(
                            action="Promotion Boost Added" if action == "add_boost" else "Promotion Delay Added",
                            description=f"{get_user_display_name(user)}'s {boost_type} has been set to {days} days",
                            user=interaction.user,
                            target_user=user
                        )

                elif action in ("remove_boost", "remove_delay"):
                    if not user:
                        await interaction.followup.send(**plain(f"`user` is required for action: {action}"), ephemeral=True)
                        return

                    table = "promotion_boosts" if action == "remove_boost" else "promotion_delays"
                    name  = "boost"            if action == "remove_boost" else "delay"

                    if not (await conn.execute(f"SELECT 1 FROM {table} WHERE discord_id = ?", (str(user.id),))).fetchone():
                        await interaction.followup.send(**plain(f"No active {name} found for {user.mention}."), ephemeral=True)
                        return

                    (await conn.execute(f"DELETE FROM {table} WHERE discord_id = ?", (str(user.id),)))
                    (await conn.commit())

                    await interaction.followup.send(**plain(f"{user.mention}'s {name} was removed."))

                    logs_cog = self.bot.get_cog("Logs")
                    if logs_cog:
                        boost_type = "promotion boost" if action == "remove_boost" else "promotion delay"
                        await logs_cog.log_action(
                            action="Promotion Boost Removed" if action == "remove_boost" else "Promotion Delay Removed",
                            description=f"{get_user_display_name(user)}'s {boost_type} has been removed",
                            user=interaction.user,
                            target_user=user
                        )

                elif action == "list":
                    boosts = (await conn.execute(
                        "SELECT discord_id, days_decreased, applied_by, applied_at FROM promotion_boosts"
                    )).fetchall()
                    delays = (await conn.execute(
                        "SELECT discord_id, days_increased, applied_by, applied_at FROM promotion_delays"
                    )).fetchall()

                    embed = create_embed(
                        title="Active Promotion Modifications",
                        description=None,
                        user=None,
                        thumbnail=True
                    )

                    if boosts:
                        lines = [
                            f"<@{r['discord_id']}> - `-{r['days_decreased']}d` - by <@{r['applied_by']}> - {r['applied_at']}"
                            for r in boosts
                        ]
                        embed.add_field(name=f"Boosts ({len(boosts)})", value="\n".join(lines)[:1000] or "None", inline=False)
                    else:
                        embed.add_field(name="Boosts", value="No active boosts", inline=False)

                    if delays:
                        lines = [
                            f"<@{r['discord_id']}> - `+{r['days_increased']}d` - by <@{r['applied_by']}> - {r['applied_at']}"
                            for r in delays
                        ]
                        embed.add_field(name=f"Delays ({len(delays)})", value="\n".join(lines)[:1000] or "None", inline=False)
                    else:
                        embed.add_field(name="Delays", value="No active delays", inline=False)

                    await interaction.followup.send(embed=embed)
                else:
                    await interaction.followup.send(**plain("Unknown action."), ephemeral=True)

        except Exception as e:
            logger.error(f"Manage command error ({action}): {e}", exc_info=True)
            await interaction.followup.send(**plain(f"Error: {e}"), ephemeral=True)


    @tasks.loop(hours=6)
    async def auto_pending_embed(self):
        ch_id = (await asyncio.to_thread(self._get_promotion_channel_id))
        if not ch_id:
            return
        try:
            channel = self.bot.get_channel(ch_id) or await self.bot.fetch_channel(ch_id)
            if channel:
                await channel.send(embed=await self._build_pending_embed())
        except Exception as e:
            logger.error(f"Auto pending embed failed: {e}", exc_info=True)

    @auto_pending_embed.before_loop
    async def before_auto_pending(self):
        await self.bot.wait_until_ready()


    async def _build_pending_embed(self) -> Embed:
        now_ts = time.time()
        if self._pending_embed_cache and (now_ts - self._cache_timestamp) < self._cache_ttl:
            return self._pending_embed_cache

        async with aget_db_connection() as conn:
            members = (await conn.execute(
                "SELECT m.discord_id, m.ig_name, m.rank, m.status, "
                "m.last_updated, m.playtime_minutes, "
                "rs.promo_points, rs.leadership_points "
                "FROM members m "
                "LEFT JOIN rank_stats rs ON m.discord_id = rs.discord_id AND m.rank = rs.rank "
                "ORDER BY m.rank, m.ig_name"
            )).fetchall()
            boosts = {r['discord_id']: r['days_decreased'] for r in (await conn.execute("SELECT discord_id, days_decreased FROM promotion_boosts")).fetchall()}
            delays = {r['discord_id']: r['days_increased'] for r in (await conn.execute("SELECT discord_id, days_increased FROM promotion_delays")).fetchall()}

        now = datetime.now(timezone.utc)
        ready_active, ready_reserve = [], []

        for m in members:
            did              = m['discord_id']
            is_active        = m['status'] == 'Active'
            playtime_minutes = m['playtime_minutes'] or 0

            try:
                lu = datetime.fromisoformat(m['last_updated'].replace('Z', '+00:00'))
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
            except Exception:
                lu = now
            days_held = (now - lu).days
            boost_d   = boosts.get(did, 0)
            delay_d   = delays.get(did, 0)
            lp        = m['leadership_points'] or 0

            r_rank = m['rank']
            r_req  = PROMOTION_REQUIREMENTS.get(r_rank)
            if r_req and r_req["days"] != float('inf'):
                eligible, path = _check_eligible(
                    days_held, playtime_minutes, r_req,
                    days_boost=boost_d, days_delay=delay_d,
                    points=m['promo_points'] or 0, lp=lp,
                )
                if eligible:
                    adj_full = max(0, r_req["days"] - boost_d + delay_d)
                    adj_min  = max(0, r_req["min_days"] - boost_d + delay_d)
                    entry = {
                        'did': did, 'name': m['ig_name'], 'rank': r_rank,
                        'next': RANK_PROGRESSION.get(r_rank, "MAX"),
                        'days': days_held,
                        'req_days': adj_min if path == "playtime" else adj_full,
                        'pts': m['promo_points'] or 0, 'req_pts': r_req['points'],
                        'lp': lp, 'req_lp': r_req['leadership_points'],
                        'playtime_h': playtime_minutes / 60.0,
                        'req_pt_h': r_req['playtime_hours'],
                        'path': path,
                        'boost': boost_d, 'delay': delay_d,
                    }
                    (ready_active if is_active else ready_reserve).append(entry)

        embed = create_embed(
            title="Promotion Pending Review",
            description=None,
            user=None,
            thumbnail=True
        )

        def add_ready_field(ready_list, field_name):
            if not ready_list:
                return
            lines = []
            for e in ready_list:
                if e['path'] == "playtime":
                    path_label = "Fast-track (playtime and minimum days met)"
                else:
                    path_label = "Standard (full days requirement met)"

                days_text = f"{e['days']}/{e['req_days']}"
                if e['boost']: days_text += f" (boost -{e['boost']}d)"
                if e['delay']: days_text += f" (delay +{e['delay']}d)"

                member_text  = f"<@{e['did']}>\n"
                member_text += f"IGN: {e['name']}\n"
                member_text += f"Rank: {format_rank_for_display(e['rank'])} to {format_rank_for_display(e['next'])}\n"
                member_text += f"Days: `{days_text}`\n"
                member_text += f"Points: `{e['pts']}/{e['req_pts']:g}`\n"
                member_text += f"Leadership Points: `{e['lp']}/{e['req_lp']:g}`\n"
                member_text += f"Playtime: `{e['playtime_h']:.1f}h/{e['req_pt_h']:g}h`\n"
                member_text += f"Path: {path_label}"
                lines.append(member_text)

            text = "\n\n".join(lines)
            if len(text) <= 1024:
                embed.add_field(name=field_name, value=text, inline=False)
                return

            chunks, current_chunk, current_len = [], [], 0
            for line in lines:
                line_len = len(line) + 2
                if current_len + line_len > 1024 and current_chunk:
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk, current_len = [line], line_len
                else:
                    current_chunk.append(line)
                    current_len += line_len
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))

            for i, chunk in enumerate(chunks):
                embed.add_field(
                    name=field_name if i == 0 else f"{field_name} (continued)",
                    value=chunk,
                    inline=False
                )

        add_ready_field(ready_active,  f"Ready: Active ({len(ready_active)})")
        add_ready_field(ready_reserve, f"Ready: Reserve ({len(ready_reserve)})")

        if not ready_active and not ready_reserve:
            embed.description = "No members meet promotion criteria at this time."

        self._pending_embed_cache = embed
        self._cache_timestamp = now_ts
        return embed

    def cog_unload(self):
        self.auto_pending_embed.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Promotion(bot))
