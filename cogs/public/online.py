from __future__ import annotations
import asyncio

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks
from bs4 import BeautifulSoup

from database.db import get_db_connection
from utils.helpers import create_embed, plain
from utils.settings import RANK_ORDER, RANK_TAG_MAP


logger = logging.getLogger(__name__)


_REQUEST_TIMEOUT        = aiohttp.ClientTimeout(total=20, connect=5)
_FETCH_ATTEMPTS         = 2
_FETCH_RETRY_DELAY      = 3
_MAX_FAILED_POLLS       = 3
_PLAYER_TABLE_INDEX     = 5
_POLL_INTERVAL_MINUTES  = 1
_FLUSH_INTERVAL_TICKS   = 5


_MARKDOWN_RE = re.compile(r"([*_`~|\[\]])")


def _escape_markdown(text: str) -> str:
    return _MARKDOWN_RE.sub(r"\\\1", text)


def _tag_pattern(tag: str) -> str:
    t = re.escape(tag)
    variants = [
        rf"\[{t}\]",
        rf"\({t}\)",
        rf"\{{{t}\}}",
        rf"\*{t}\*",
        rf"{t}-",
        rf"-{t}",
        rf"\*{t}-",
        rf"-\*{t}",
        rf"-{t}\*",
        rf"\*-{t}",
        rf"{t}\*-",
        rf"-\*{t}\*",
        rf"\*{t}\*-",
        rf"{t}_",
        rf"_{t}",
    ]
    return "|".join(variants)


def _compile_military_pattern() -> re.Pattern:
    parts = [_tag_pattern(tag) for tag in RANK_TAG_MAP.values()]
    return re.compile("|".join(parts), re.IGNORECASE)


_MILITARY_RE = _compile_military_pattern()
_LIO_RE      = re.compile(_tag_pattern("LIO"), re.IGNORECASE)
_SAUR_RE     = re.compile(
    r"\[SAUR\]|\(SAUR\)|\*SAUR\*|\{SAUR\}|Castillo|AeroXbird",
    re.IGNORECASE,
)
_SWAT_RE     = re.compile(
    r"\*SWAT\*|\[SWAT\]|\(SWAT\)|\{SWAT\}|\[LA\]",
    re.IGNORECASE,
)
_TERROR_RE   = re.compile(
    r"\|T\b|\bT-A\b|\bTRN-|\bT\||-TRN\b"
    r"|\bAMBU-|-AMBU\b|\[AMBU\]|\(AMBU\)|\*AMBU\*",
    re.IGNORECASE,
)

_RANK_PATTERNS: dict[str, re.Pattern] = {
    rank: re.compile(_tag_pattern(tag), re.IGNORECASE)
    for rank, tag in RANK_TAG_MAP.items()
}
_RANK_INDEX: dict[str, int] = {rank: idx for idx, rank in enumerate(RANK_ORDER)}


type PlayerList = list[str]
type Stats = dict[str, PlayerList]


def _categorise_player(name: str) -> list[str]:
    categories: list[str] = []

    if _SAUR_RE.search(name):
        categories.append("saur")

    if _SWAT_RE.search(name):
        categories.append("swat")
    elif _TERROR_RE.search(name):
        categories.append("terrorists")
    elif _MILITARY_RE.search(name) or _LIO_RE.search(name):
        categories.append("military")

    return categories or ["others"]


def _extract_rank(name: str) -> Optional[str]:
    for rank in RANK_ORDER:
        if _RANK_PATTERNS[rank].search(name):
            return rank
    return None


def _sort_by_rank(players: PlayerList) -> PlayerList:
    return sorted(
        players,
        key=lambda n: _RANK_INDEX.get(_extract_rank(n), len(RANK_ORDER)),
    )


_FIELD_CHAR_LIMIT = 1024


def _fmt_players(players: PlayerList, *, truncate: bool = False) -> str:
    if not players:
        return "Nobody is online"

    lines = [_escape_markdown(name) for name in players]

    if not truncate:
        return "\n".join(lines)

    suffix_template = "\n… and {} more"
    built: list[str] = []
    used = 0

    for i, line in enumerate(lines):
        entry = ("\n" if built else "") + line
        remaining = len(lines) - i
        suffix_len = len(suffix_template.format(remaining)) if remaining > 1 else 0
        if used + len(entry) + suffix_len > _FIELD_CHAR_LIMIT:
            built.append(suffix_template.format(len(lines) - i))
            break
        built.append(entry)
        used += len(entry)

    return "".join(built)


def _parse_player_table(html: str) -> Optional[Stats]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")

        if len(tables) <= _PLAYER_TABLE_INDEX:
            logger.debug("Player table not present (found %d tables), server likely empty", len(tables))
            return {"saur": [], "military": [], "swat": [], "terrorists": [], "others": []}

        stats: Stats = {"saur": [], "military": [], "swat": [], "terrorists": [], "others": []}

        for row in tables[_PLAYER_TABLE_INDEX].find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            name = cells[0].get_text().strip()
            if name:
                for category in _categorise_player(name):
                    stats[category].append(name)

        return stats

    except Exception:
        logger.exception("Error parsing server stats HTML")
        return None


def _get_all_online_names(stats: Stats) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for players in stats.values():
        for name in players:
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _flush_playtime(accumulated: dict[str, int]) -> None:
    if not accumulated:
        return

    now      = datetime.now(timezone.utc)
    now_iso  = now.isoformat()
    today    = now.strftime("%Y-%m-%d")

    try:
        with get_db_connection() as conn:
            placeholders = ",".join("?" * len(accumulated))
            conn.execute(
                f"UPDATE members SET last_online = ? WHERE discord_id IN ({placeholders})",
                [now_iso, *accumulated.keys()],
            )
            for discord_id, minutes in accumulated.items():
                conn.execute(
                    "UPDATE members SET playtime_minutes = playtime_minutes + ? WHERE discord_id = ?",
                    (minutes, discord_id),
                )
                conn.execute(
                    """
                    INSERT INTO playtime_log (discord_id, log_date, minutes)
                    VALUES (?, ?, ?)
                    ON CONFLICT(discord_id, log_date) DO UPDATE SET minutes = minutes + excluded.minutes
                    """,
                    (discord_id, today, minutes),
                )
            conn.commit()

        logger.debug("Flushed playtime for %d member(s)", len(accumulated))

    except Exception:
        logger.exception("Error flushing playtime to database")


def _match_members(online_names: list[str], name_map: dict[str, str]) -> set[str]:
    online_lower = [name.lower() for name in online_names]
    matched: set[str] = set()
    for ig_lower, discord_id in name_map.items():
        if any(ig_lower in online_name for online_name in online_lower):
            matched.add(discord_id)
    return matched

def _build_embed(stats: Stats) -> discord.Embed:
    saur       = stats.get("saur", [])
    military   = _sort_by_rank(stats.get("military", []))
    swat       = stats.get("swat", [])
    terrorists = stats.get("terrorists", [])
    others     = sorted(stats.get("others", []))

    total = len({*saur, *military, *swat, *terrorists, *others})

    embed = create_embed(title=f"🟢 SAUR: {total} Online")
    embed.add_field(name=f"`Staff [{len(saur)}]`",            value=_fmt_players(saur,       truncate=True), inline=False)
    embed.add_field(name=f"`Military [{len(military)}]`",     value=_fmt_players(military,   truncate=True), inline=True)
    embed.add_field(name=f"`SWAT [{len(swat)}]`",             value=_fmt_players(swat,       truncate=True), inline=True)
    embed.add_field(name=f"`Terrorists [{len(terrorists)}]`", value=_fmt_players(terrorists, truncate=True), inline=True)

    if others:
        chunk_size = len(others) // 3 + (len(others) % 3 > 0)
        for idx, i in enumerate(range(0, len(others), chunk_size)):
            embed.add_field(
                name=f"`Others [{len(others)}]`" if idx == 0 else "\u200b",
                value=_fmt_players(others[i : i + chunk_size], truncate=True),
                inline=True,
            )
    return embed


def _build_offline_embed(ip: str, port: str) -> discord.Embed:
    return create_embed(
        title="🔴 SAUR: Server Offline",
        description=(
            f"Could not fetch stats from `{ip}:{port}`. "
            "The server may be offline or temporarily unreachable."
        ),
        color=discord.Color.red(),
    )


class Online(commands.Cog):
    SERVER_IP   = "193.70.80.143"
    SERVER_PORT = "22003"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._playtime_buffer: dict[str, int] = {}
        self._tick_count: int = 0
        self._failed_polls: int = 0
        self._name_map: dict[str, str] = {}
        self._previously_online: set[str] = set()
        self.playtime_tracker.start()


    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        self.playtime_tracker.cancel()
        logger.info("Playtime tracker stopped")
        if self._playtime_buffer:
            logger.info("Flushing %d pending playtime record(s) before shutdown", len(self._playtime_buffer))
            await asyncio.to_thread(_flush_playtime, self._playtime_buffer)
            self._playtime_buffer.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


    @property
    def _stats_url(self) -> str:
        return f"https://www.game-state.com/{self.SERVER_IP}:{self.SERVER_PORT}/"

    async def _fetch_stats(self, attempts: int = _FETCH_ATTEMPTS) -> Optional[Stats]:
        for attempt in range(1, attempts + 1):
            try:
                async with self.session.get(self._stats_url, timeout=_REQUEST_TIMEOUT) as response:
                    if response.status != 200:
                        logger.warning("Server stats returned HTTP %d", response.status)
                        return None
                    return _parse_player_table(await response.text())

            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out fetching server stats (attempt %d/%d, limit %ss)",
                    attempt, attempts, _REQUEST_TIMEOUT.total,
                )
            except aiohttp.ClientError as exc:
                logger.error(
                    "HTTP error fetching server stats (attempt %d/%d): %s",
                    attempt, attempts, exc,
                )
            except Exception:
                logger.exception("Unexpected error fetching server stats")
                return None

            if attempt < attempts:
                await asyncio.sleep(_FETCH_RETRY_DELAY)

        return None

    def _refresh_name_map(self) -> None:
        try:
            with get_db_connection() as conn:
                rows = conn.execute("SELECT discord_id, ig_name FROM members").fetchall()
            self._name_map = {row["ig_name"].lower(): row["discord_id"] for row in rows}
        except Exception:
            logger.exception("Error refreshing member name map")


    @tasks.loop(minutes=_POLL_INTERVAL_MINUTES)
    async def playtime_tracker(self) -> None:
        try:
            self._tick_count += 1
            should_flush = (self._tick_count % _FLUSH_INTERVAL_TICKS == 0)

            if should_flush or not self._name_map:
                await asyncio.to_thread(self._refresh_name_map)

            stats = await self._fetch_stats()

            if stats is None:
                self._failed_polls += 1

                if self._failed_polls < _MAX_FAILED_POLLS:
                    logger.warning(
                        "Server unreachable (%d/%d), keeping %d tracked member(s)",
                        self._failed_polls, _MAX_FAILED_POLLS, len(self._previously_online),
                    )
                elif self._previously_online:
                    logger.warning(
                        "Server unreachable, stopped tracking %d member(s): %s",
                        len(self._previously_online),
                        ", ".join(sorted(self._previously_online)),
                    )
                    self._previously_online.clear()
                else:
                    logger.warning("Server unreachable, playtime tracking paused")
            else:
                self._failed_polls = 0

                online_names     = _get_all_online_names(stats)
                currently_online = _match_members(online_names, self._name_map)

                id_to_name = {v: k for k, v in self._name_map.items()}

                logger.debug("Matched ig_names: %s", [id_to_name.get(d, d) for d in currently_online])

                joined = currently_online - self._previously_online
                left   = self._previously_online - currently_online

                for discord_id in joined:
                    logger.info("Tracking started: %s", id_to_name.get(discord_id, discord_id))
                for discord_id in left:
                    logger.info("Tracking stopped: %s", id_to_name.get(discord_id, discord_id))

                if currently_online:
                    logger.debug("Tracking %d member(s) online", len(currently_online))
                else:
                    logger.debug("No tracked members currently online")

                for discord_id in currently_online:
                    self._playtime_buffer[discord_id] = (
                        self._playtime_buffer.get(discord_id, 0) + _POLL_INTERVAL_MINUTES
                    )

                self._previously_online = currently_online

            if should_flush:
                await asyncio.to_thread(_flush_playtime, self._playtime_buffer)
                self._playtime_buffer.clear()

        except Exception:
            logger.exception("Unhandled error in playtime_tracker")

    @playtime_tracker.before_loop
    async def before_playtime_tracker(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.to_thread(self._refresh_name_map)
        stats = await self._fetch_stats()
        if stats:
            online_names = _get_all_online_names(stats)
            currently_online = _match_members(online_names, self._name_map)
            id_to_name = {v: k for k, v in self._name_map.items()}
            if currently_online:
                logger.info(
                    "Tracking started for %d member(s) already online: %s",
                    len(currently_online),
                    ", ".join(id_to_name.get(did, did) for did in sorted(currently_online)),
                )
            else:
                logger.info("Tracker started, no members currently online")
            self._previously_online = currently_online
        else:
            logger.warning("Tracker started, could not reach server for initial check")


    @app_commands.command(name="online", description="Check online players on the server")
    async def online(self, interaction: Interaction) -> None:
        await interaction.response.defer()

        try:
            stats = await self._fetch_stats(attempts=1)
        except Exception:
            logger.exception("Unhandled error in /online command")
            await interaction.followup.send(
                "An unexpected error occurred. Please try again later.",
                ephemeral=True,
            )
            return

        if stats:
            await interaction.followup.send(embed=_build_embed(stats))
        else:
            await interaction.followup.send(embed=_build_offline_embed(self.SERVER_IP, self.SERVER_PORT))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Online(bot))
