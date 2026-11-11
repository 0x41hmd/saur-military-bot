import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

from discord import app_commands, User, Interaction
from discord.ext import commands

from database.db import aget_db_connection
from database.db import get_db_connection
from utils import casino_db
from utils.helpers import member_cache, create_embed, format_playtime
from utils.settings import COUNTRIES

logger = logging.getLogger(__name__)


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _build_stats_text(self, data: Dict[str, Any], prefix: str = "") -> str:
        p = prefix
        ops = (data.get(f"{p}bank_ops", 0) + data.get(f"{p}casino_ops", 0) +
               data.get(f"{p}securicar_ops", 0) + data.get(f"{p}andromada_ops", 0))
        ct = data.get(f"{p}andromada_terrors_ops", 0) + data.get(f"{p}assault_ops", 0)
        return (
            f"**Warzone:** {data.get(f'{p}warzone', 0)}\n"
            f"**Doomsday:** {data.get(f'{p}doomsday', 0)}\n"
            f"**Ammo Transports:** {data.get(f'{p}ammo_transport', 0)}\n"
            f"**Operations:** {ops}\n"
            f"**Counter Terrorism:** {ct}\n"
            f"**Trainings:** {data.get(f'{p}training', 0)}\n"
            f"**Patrols:** {data.get(f'{p}patrol', 0)}\n"
            f"**Extra Points:** {data.get(f'{p}extra_points', 0)}"
        )

    def _build_af_stats_text(self, data: Dict[str, Any], prefix: str = "") -> str:
        p = prefix
        return (
            f"**AF Warzone:** {data.get(f'{p}af_warzone', 0)}\n"
            f"**AF Counter Rented:** {data.get(f'{p}af_counter', 0)}\n"
            f"**AF Training:** {data.get(f'{p}af_training', 0)}"
        )

    def _format_country(self, country_code: Optional[str]) -> str:
        if not country_code or country_code not in COUNTRIES:
            return "Not specified"
        c = COUNTRIES[country_code]
        return f"{c['flag']} {c['name']}"

    def _parse_date(self, date_string: Optional[str]) -> Optional[datetime]:
        if not date_string:
            return None
        try:
            return datetime.fromisoformat(date_string.replace("Z", "+00:00")).astimezone(timezone.utc)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to parse date '{date_string}': {e}")
            return None

    def _service_days(self, date_string: Optional[str], label: str) -> str:
        dt = self._parse_date(date_string)
        if not dt:
            return f"**{label}:** Unknown"
        days = (datetime.now(timezone.utc) - dt).days
        return f"**{label}:** {days} days"

    def _format_date(self, date_string: Optional[str]) -> str:
        dt = self._parse_date(date_string)
        return dt.strftime("%Y-%m-%d") if dt else "Not specified"

    def _format_last_online(self, date_string: Optional[str], is_online: bool = False) -> str:
        if is_online:
            return "🟢 Online now"
        dt = self._parse_date(date_string)
        if not dt:
            return "🔴 Never"
        delta = datetime.now(timezone.utc) - dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return "🟢 Just now"
        if total_seconds < 3600:
            mins = total_seconds // 60
            return f"🔴 {mins}m ago"
        if total_seconds < 86400:
            hours = total_seconds // 3600
            mins  = (total_seconds % 3600) // 60
            return f"🔴 {hours}h {mins}m ago"
        days = delta.days
        return f"🔴 {days}d ago ({dt.strftime('%Y-%m-%d')})"

    async def _fetch_member_profile(self, discord_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with aget_db_connection() as conn:
                result = (await conn.execute("""
                    SELECT
                        m.*,
                        rs.warzone, rs.doomsday, rs.ammo_transport,
                        rs.bank_ops, rs.casino_ops, rs.securicar_ops, rs.andromada_ops,
                        rs.andromada_terrors_ops, rs.assault_ops,
                        rs.training, rs.patrol, rs.extra_points,
                        rs.promo_points AS rank_promo_points,
                        rs.leadership_points AS rank_leadership_points,
                        rs.af_points AS rank_af_points,
                        rs.af_warzone, rs.af_counter, rs.af_training,
                        cs.total_warzone, cs.total_doomsday, cs.total_ammo_transport,
                        cs.total_bank_ops, cs.total_casino_ops, cs.total_securicar_ops,
                        cs.total_andromada_ops,
                        cs.total_andromada_terrors_ops, cs.total_assault_ops,
                        cs.total_training, cs.total_patrol, cs.total_extra_points,
                        cs.total_af_warzone, cs.total_af_counter, cs.total_af_training,
                        p.promo_points AS lifetime_promo_points,
                        p.af_points AS lifetime_af_points,
                        p.leadership_points AS lifetime_leadership_points
                    FROM members m
                    LEFT JOIN rank_stats rs ON m.discord_id = rs.discord_id AND rs.rank = m.rank
                    LEFT JOIN career_stats cs ON m.discord_id = cs.discord_id
                    LEFT JOIN points p ON m.discord_id = p.discord_id
                    LEFT JOIN (
                        SELECT discord_id, COALESCE(SUM(minutes), 0) AS total_playtime
                        FROM playtime_log
                        GROUP BY discord_id
                    ) pl ON m.discord_id = pl.discord_id
                    WHERE m.discord_id = ?
                """, (discord_id,))).fetchone()
                return dict(result) if result else None
        except Exception as e:
            logger.error(f"Error fetching profile for {discord_id}: {e}", exc_info=True)
            raise

    @app_commands.command(name="profile", description="View your or another member's profile")
    @app_commands.describe(member="The member to view (leave blank for yourself)")
    async def profile(self, interaction: Interaction, member: Optional[User] = None):
        await interaction.response.defer(ephemeral=False)

        target = member or interaction.user
        target_id = str(target.id)

        try:
            cache_key = f"profile:{target_id}"
            member_data = member_cache.get(cache_key)

            if not member_data:
                member_data = await self._fetch_member_profile(target_id)
                if not member_data:
                    await interaction.followup.send(
                        f"Member {target.mention} not found in the database.",
                        ephemeral=True
                    )
                    return
                try:
                    member_cache.set(cache_key, member_data)
                except Exception as e:
                    logger.warning(f"Failed to cache profile for {target_id}: {e}")

            is_af = bool(member_data["is_af"])
            af_rank = member_data["af_rank"]
            af_rank_display = af_rank if af_rank and af_rank != "None" else "No AF Rank"

            try:
                async with aget_db_connection() as conn:
                    live_row = (await conn.execute("""
                        SELECT
                            m.last_online,
                            COALESCE(pl.total_playtime, 0) AS total_playtime
                        FROM members m
                        LEFT JOIN (
                            SELECT discord_id, SUM(minutes) AS total_playtime
                            FROM playtime_log
                            GROUP BY discord_id
                        ) pl ON m.discord_id = pl.discord_id
                        WHERE m.discord_id = ?
                    """, (target_id,))).fetchone()
                if live_row:
                    member_data['last_online']    = live_row['last_online']
                    member_data['total_playtime'] = live_row['total_playtime']
            except Exception as e:
                logger.warning(f"Failed to fetch live playtime for {target_id}: {e}")

            online_cog = self.bot.get_cog("Online")
            is_online = (
                online_cog is not None
                and target_id in getattr(online_cog, "_previously_online", set())
            )

            info_text = (
                f"• **In-Game name:** {member_data['ig_name']}\n"
                f"• **Account name:** {member_data['account_name']}\n"
                f"• **Current rank:** {member_data['rank']}\n"
                f"• {self._service_days(member_data['last_updated'], 'Service with current rank')}\n"
                f"• {self._service_days(member_data['date_added'], 'Total service')}\n"
                f"• **Recruitment date:** {self._format_date(member_data['date_added'])}\n"
                f"• **Country:** {self._format_country(member_data['country'])}\n"
                f"• **Status:** {member_data['status']}\n"
                f"• **Last online:** {self._format_last_online(member_data.get('last_online'), is_online)}\n"
                f"• **Playtime:** {format_playtime(member_data.get('total_playtime', 0))}\n"
                f"• **Air force:** {'Yes' if is_af else 'No'}"
            )
            if is_af:
                info_text += f"\n• **Air force rank:** {af_rank_display}"

            try:
                acc = (await asyncio.to_thread(casino_db.peek_account, target.id))
                credits = acc["balance"] if acc else 0
            except Exception as e:
                logger.warning(f"Failed to load casino credits for {target_id}: {e}")
                credits = 0
            info_text += f"\n• **Credits:** ${credits:,}"

            embed = create_embed(
                title=f"Service Record: {member_data['ig_name']}",
                description="",
                user=target
            )

            embed.add_field(name="`Service Info`", value=info_text, inline=False)
            embed.add_field(name="`Rank Stats`", value=self._build_stats_text(member_data), inline=True)
            embed.add_field(name="`Career Stats`", value=self._build_stats_text(member_data, "total_"), inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            if is_af:
                embed.add_field(name="`AF Rank Stats`", value=self._build_af_stats_text(member_data), inline=True)
                embed.add_field(name="`AF Career Stats`", value=self._build_af_stats_text(member_data, "total_"), inline=True)
                embed.add_field(name="\u200b", value="\u200b", inline=True)

            rank_points = (
                f"**Promotional Points:** {member_data.get('rank_promo_points', 0)}\n"
                f"**Leadership Points:** {member_data.get('rank_leadership_points', 0)}"
            )
            career_points = (
                f"**Promotional Points:** {member_data.get('lifetime_promo_points', 0)}\n"
                f"**Leadership Points:** {member_data.get('lifetime_leadership_points', 0)}"
            )
            if is_af:
                rank_points += f"\n**Air Force Points:** {member_data.get('rank_af_points', 0)}"
                career_points += f"\n**Air Force Points:** {member_data.get('lifetime_af_points', 0)}"

            embed.add_field(name="`Rank Points`", value=rank_points, inline=True)
            embed.add_field(name="`Career Points`", value=career_points, inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error displaying profile for {target_id}: {e}", exc_info=True)
            await interaction.followup.send(
                "An error occurred while retrieving the profile. Please contact HQ technical staff.",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
