import asyncio
import logging
import time
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timezone

from discord import app_commands, Interaction
from discord.ext import commands

from database.db import aget_db_connection
from database.db import get_db_connection
from utils import casino_db as cdb
from utils import trivia_db as tdb
from utils.helpers import create_embed

logger = logging.getLogger(__name__)


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Optional[Dict[str, Tuple[str, List[str]]]] = None
        self._cache_time: float = 0
        self._cache_ttl: int = 300

    def _cache_valid(self) -> bool:
        return self._cache is not None and (time.time() - self._cache_time) < self._cache_ttl

    def _build_leaderboard(
        self,
        title: str,
        data: List[Dict],
        value_key: str,
        name_map: Dict[str, str]
    ) -> Tuple[str, List[str]]:
        data = [d for d in data if d.get(value_key, 0) > 0]
        unit = "ops" if any(k in value_key for k in ("count", "wz", "dd")) else "pts"
        lines = []

        for i, record in enumerate(data, start=1):
            name = name_map.get(record["discord_id"], str(record["discord_id"]))
            value = int(record[value_key] or 0)
            lines.append(f"{i}. **{name}**: {value} {unit}")

        for i in range(len(lines) + 1, 11):
            lines.append(f"{i}. Nobody yet")

        return title, lines

    async def _fetch_leaderboard_data(self) -> Dict[str, Tuple[str, List[str]]]:
        try:
            async with aget_db_connection() as conn:
                all_discord_ids = set()

                all_points = (await conn.execute("""
                    SELECT
                        COALESCE(rs.discord_id, p.discord_id) as discord_id,
                        COALESCE(rs.promo_points, 0) + COALESCE(p.promo_points, 0) as promo,
                        COALESCE(rs.leadership_points, 0) + COALESCE(p.leadership_points, 0) as lead,
                        COALESCE(rs.af_points, 0) + COALESCE(p.af_points, 0) as af
                    FROM (
                        SELECT rs.* FROM rank_stats rs
                        JOIN members m ON rs.discord_id = m.discord_id
                        WHERE rs.rank = m.rank
                    ) rs
                    LEFT JOIN points p ON rs.discord_id = p.discord_id
                    WHERE COALESCE(rs.promo_points, 0) + COALESCE(rs.leadership_points, 0) + COALESCE(rs.af_points, 0) +
                          COALESCE(p.promo_points, 0) + COALESCE(p.leadership_points, 0) + COALESCE(p.af_points, 0) > 0

                    UNION ALL

                    SELECT
                        p.discord_id,
                        COALESCE(p.promo_points, 0),
                        COALESCE(p.leadership_points, 0),
                        COALESCE(p.af_points, 0)
                    FROM points p
                    WHERE p.discord_id NOT IN (SELECT discord_id FROM rank_stats)
                    AND COALESCE(p.promo_points, 0) + COALESCE(p.leadership_points, 0) + COALESCE(p.af_points, 0) > 0
                """)).fetchall()

                top_activity, top_lead, top_af = [], [], []

                for row in all_points:
                    did = row["discord_id"]
                    all_discord_ids.add(did)
                    if row["promo"] > 0:
                        top_activity.append({"discord_id": did, "total_promo": row["promo"]})
                    if row["lead"] > 0:
                        top_lead.append({"discord_id": did, "total_lead": row["lead"]})
                    if row["af"] > 0:
                        top_af.append({"discord_id": did, "total_af": row["af"]})

                top_activity = sorted(top_activity, key=lambda x: x["total_promo"], reverse=True)[:10]
                top_lead     = sorted(top_lead,     key=lambda x: x["total_lead"],  reverse=True)[:10]
                top_af       = sorted(top_af,       key=lambda x: x["total_af"],    reverse=True)[:10]

                ops_results = (await conn.execute("""
                    SELECT 'wz' as op_type, op.discord_id, COUNT(*) as cnt
                    FROM operation_participants op
                    JOIN reports r ON r.id = op.report_id
                    WHERE r.report_type = 'warzone'
                    GROUP BY op.discord_id

                    UNION ALL

                    SELECT 'dd', op.discord_id, COUNT(*)
                    FROM operation_participants op
                    JOIN reports r ON r.id = op.report_id
                    WHERE r.report_type = 'doomsday'
                    GROUP BY op.discord_id
                """)).fetchall()

                top_wz, top_dd = [], []

                for row in ops_results:
                    all_discord_ids.add(row["discord_id"])
                    if row["op_type"] == "wz":
                        top_wz.append({"discord_id": row["discord_id"], "wz_count": row["cnt"]})
                    else:
                        top_dd.append({"discord_id": row["discord_id"], "dd_count": row["cnt"]})

                top_wz = sorted(top_wz, key=lambda x: x["wz_count"], reverse=True)[:10]
                top_dd = sorted(top_dd, key=lambda x: x["dd_count"], reverse=True)[:10]

                name_map = {}
                if all_discord_ids:
                    placeholders = ",".join("?" * len(all_discord_ids))
                    name_map = {
                        row["discord_id"]: row["ig_name"] or str(row["discord_id"])
                        for row in (await conn.execute(
                            f"SELECT discord_id, ig_name FROM members WHERE discord_id IN ({placeholders})",
                            list(all_discord_ids)
                        )).fetchall()
                    }
                    for did in all_discord_ids:
                        name_map.setdefault(did, str(did))

                return {
                    "activity": self._build_leaderboard("Top 10 Active Members Leaderboard",   top_activity, "total_promo", name_map),
                    "leaders":  self._build_leaderboard("Top 10 Squad Leaders Leaderboard",     top_lead,     "total_lead",  name_map),
                    "airforce": self._build_leaderboard("Top 10 Airforce Members Leaderboard",  top_af,       "total_af",    name_map),
                    "warzone":  self._build_leaderboard("Top 10 Warzone Players Leaderboard",   top_wz,       "wz_count",    name_map),
                    "doomsday": self._build_leaderboard("Top 10 Doomsday Players Leaderboard",  top_dd,       "dd_count",    name_map),
                }

        except Exception as e:
            logger.error(f"Error fetching leaderboard data: {e}", exc_info=True)
            raise

    @staticmethod
    def _live_leaderboard(field: str) -> Tuple[str, List[str]]:
        if field == "casino":
            title = "Top 10 Richest Casino Players"
            data = cdb.top_balances(10)
            fmt = lambda r: f"${int(r['balance']):,}"
        else:
            title = "Top 10 Trivia Streaks"
            data = tdb.leaderboard(10)
            fmt = lambda r: f"{int(r['best_streak'])} in a row"

        names: Dict[int, str] = {}
        ids = [r["discord_id"] for r in data]
        if ids:
            with get_db_connection() as conn:
                placeholders = ",".join("?" * len(ids))
                names = {
                    row["discord_id"]: row["ig_name"]
                    for row in conn.execute(
                        f"SELECT discord_id, ig_name FROM members WHERE discord_id IN ({placeholders})",
                        ids,
                    ).fetchall()
                    if row["ig_name"]
                }

        lines = []
        for i, r in enumerate(data, start=1):
            name = names.get(r["discord_id"]) or f"<@{r['discord_id']}>"
            lines.append(f"{i}. **{name}**: {fmt(r)}")

        for i in range(len(lines) + 1, 11):
            lines.append(f"{i}. Nobody yet")

        return title, lines

    @app_commands.command(name="leaderboard", description="Show leaderboard for a specific category")
    @app_commands.describe(field="Select which leaderboard to view")
    @app_commands.choices(field=[
        app_commands.Choice(name="Activity", value="activity"),
        app_commands.Choice(name="Leaders",  value="leaders"),
        app_commands.Choice(name="Airforce", value="airforce"),
        app_commands.Choice(name="Warzone",  value="warzone"),
        app_commands.Choice(name="Doomsday", value="doomsday"),
        app_commands.Choice(name="Casino",   value="casino"),
        app_commands.Choice(name="Trivia",   value="trivia"),
    ])
    async def leaderboard(self, interaction: Interaction, field: str):
        await interaction.response.defer(ephemeral=False)

        try:
            if field in ("casino", "trivia"):
                title, content = (await asyncio.to_thread(self._live_leaderboard, field))
            else:
                if not self._cache_valid():
                    self._cache = await self._fetch_leaderboard_data()
                    self._cache_time = time.time()
                title, content = self._cache.get(field, ("Error", ["Invalid field selected."]))

            embed = create_embed(title=title)
            embed.add_field(name="", value="\n".join(content), inline=False)
            embed.timestamp = datetime.now(timezone.utc)

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error displaying leaderboard: {e}", exc_info=True)
            await interaction.followup.send(
                "An error occurred while generating the leaderboard. Please try again later.",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
