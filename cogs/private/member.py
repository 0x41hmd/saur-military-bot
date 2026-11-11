import logging
import re
from database import errors as db_errors
from datetime import datetime, timezone

import discord
from discord import app_commands, User, Interaction, ButtonStyle
from discord.app_commands import describe, Choice
from discord.ext import commands
from discord.ui import View, Button

from database.db import aget_db_connection
from database.db import get_db_connection
from utils.helpers import create_embed, member_cache, require_permissions, get_user_display_name, plain, format_playtime
from utils.welcome import send_welcome
from utils.settings import (
    RANK_ORDER, AF_RANK_ORDER, COUNTRIES,
    EMBED_THUMBNAIL, EMBED_FOOTER_TEXT
)

logger = logging.getLogger(__name__)

MIN_NAME_LENGTH      = 3
MAX_NAME_LENGTH      = 32
MEMBER_LIST_PER_PAGE = 15
AUTOCOMPLETE_MAX_RESULTS = 25
REASON_MIN_LENGTH    = 5


_PLAYTIME_RE = re.compile(r"^\s*([+\-\u2212]?)\s*(\d+)\s*;\s*(\d+)\s*$")


def _parse_playtime(text: str):
    m = _PLAYTIME_RE.match(text or "")
    if not m or int(m.group(3)) > 59:
        return None
    mode = {"+": "add", "": "set"}.get(m.group(1), "remove")
    return mode, int(m.group(2)) * 60 + int(m.group(3))


def _resolve_country(country: str) -> str | None:
    if country in COUNTRIES:
        return country
    for code, data in COUNTRIES.items():
        if data["name"].lower() == country.lower():
            return code
    for code, data in COUNTRIES.items():
        if country.lower() in data["name"].lower():
            return code
    return None


class Member(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def rank_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=r, value=r)
            for r in RANK_ORDER if current.lower() in r.lower()
        ][:AUTOCOMPLETE_MAX_RESULTS]

    async def country_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=c, value=c)
            for c, data in COUNTRIES.items()
            if current.lower() in c.lower() or current.lower() in data["name"].lower()
        ][:AUTOCOMPLETE_MAX_RESULTS]

    async def af_rank_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=r, value=r)
            for r in ["None"] + AF_RANK_ORDER if current.lower() in r.lower()
        ][:AUTOCOMPLETE_MAX_RESULTS]

    async def status_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=s, value=s)
            for s in ("Active", "Reserve") if current.lower() in s.lower()
        ][:AUTOCOMPLETE_MAX_RESULTS]

    member = app_commands.Group(name="member", description="Manage members (HQ only)")

    @member.command(name="add", description="Add new member (HQ only)")
    @require_permissions(admin=True, acl="manage_members")
    @describe(
        user="Discord user (real user only)",
        ig_name="In-game name (3-32 chars)",
        account_name="Platform account name (3-32 chars)",
        rank="Starting rank",
        country="Country (optional)",
    )
    @app_commands.autocomplete(rank=rank_autocomplete, country=country_autocomplete)
    async def member_add(self, interaction: Interaction, user: User, ig_name: str, account_name: str, rank: str, country: str = None):
        if user.bot:
            await interaction.response.send_message(**plain("Cannot add bots."), ephemeral=True)
            return

        ig_name      = ig_name.strip()
        account_name = account_name.strip()

        if not ig_name:
            await interaction.response.send_message(**plain(f"ig_name is required ({MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} chars)."), ephemeral=True)
            return
        if not account_name:
            await interaction.response.send_message(**plain(f"account_name is required ({MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} chars)."), ephemeral=True)
            return
        if not rank or not rank.strip():
            await interaction.response.send_message(**plain("rank is required."), ephemeral=True)
            return
        if not (MIN_NAME_LENGTH <= len(ig_name) <= MAX_NAME_LENGTH):
            await interaction.response.send_message(**plain(f"ig_name must be {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} characters."), ephemeral=True)
            return
        if not (MIN_NAME_LENGTH <= len(account_name) <= MAX_NAME_LENGTH):
            await interaction.response.send_message(**plain(f"account_name must be {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} characters."), ephemeral=True)
            return
        if rank not in RANK_ORDER:
            await interaction.response.send_message(**plain(f"Must be one of: {', '.join(RANK_ORDER)}"), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        try:
            async with aget_db_connection() as conn:
                if (await conn.execute("SELECT 1 FROM members WHERE discord_id = ?", (str(user.id),))).fetchone():
                    await interaction.followup.send(**plain(f"{user.mention} is already in the database."), ephemeral=True)
                    return

                if country:
                    resolved = _resolve_country(country.strip())
                    if not resolved:
                        await interaction.followup.send(**plain("Invalid country. Use a code (US, EG) or a name (United States, Egypt)."), ephemeral=True)
                        return
                    country = resolved

                (await conn.execute(
                    "INSERT INTO members (discord_id, discord_name, ig_name, account_name, rank, country, is_af, af_rank, last_updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(user.id), user.name, ig_name, account_name, rank, country, 0, 'None', datetime.now(timezone.utc))
                ))
                (await conn.execute("INSERT INTO points (discord_id) VALUES (?)", (str(user.id),)))
                (await conn.execute(
                    "INSERT OR IGNORE INTO rank_stats (discord_id, rank, promo_points, leadership_points, af_points) VALUES (?, ?, 0, 0, 0)",
                    (str(user.id), rank)
                ))
                (await conn.execute("INSERT OR IGNORE INTO career_stats (discord_id) VALUES (?)", (str(user.id),)))
                (await conn.commit())

            try:
                member_cache.invalidate(f"profile:{user.id}")
                member_cache.invalidate("all_members")
            except (AttributeError, KeyError):
                pass

            embed = create_embed(title="Member Added", description=f"{user.mention}", user=user)
            embed.add_field(
                name="Information",
                value="\n".join([
                    f"Rank: `{rank}`",
                    f"IGN: `{ig_name}`",
                    f"Account: `{account_name}`",
                    f"Country: `{country or 'Not specified'}`",
                    "Status: `Active`",
                ]),
                inline=False
            )
            await interaction.followup.send(embed=embed)

            logs_cog = self.bot.get_cog("Logs")
            if logs_cog:
                await logs_cog.log_action(
                    action="Member Added",
                    description=f"{get_user_display_name(user)} was added to the roster",
                    user=interaction.user,
                    target_user=user,
                    details={"Rank": rank, "IGN": ig_name, "Account": account_name, "Country": country or "Not specified"},
                )

            await send_welcome(self.bot, user, rank, ig_name, fallback_channel=interaction.channel)

        except Exception as e:
            logger.error(f"[MEMBER-ADD] {type(e).__name__}: {e}", exc_info=True)
            await interaction.followup.send(**plain(f"Failed to add member: {e}"), ephemeral=True)

    @member.command(name="update", description="Update a member's information (HQ only)")
    @require_permissions(admin=True, acl="manage_members")
    @describe(
        user="The member to update (real user only)",
        ig_name="New in-game name (3-32 chars) - optional",
        account_name="New platform account name (3-32 chars) - optional",
        country="New country (optional)",
        status="Active or Reserve - optional",
        rank="New rank (optional)",
        af_rank="New Air Force rank or 'None' to remove - optional",
        playtime="Hours;minutes. 12;30 sets it, +1;15 adds, -1;15 removes - optional"
    )
    @app_commands.autocomplete(
        country=country_autocomplete,
        status=status_autocomplete,
        rank=rank_autocomplete,
        af_rank=af_rank_autocomplete
    )
    async def member_update(self, interaction: Interaction, user: User, ig_name: str = None, account_name: str = None, country: str = None, status: str = None, rank: str = None, af_rank: str = None, playtime: str = None):
        if user.bot:
            await interaction.response.send_message(**plain("Cannot update bots."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        if not any(v is not None for v in (ig_name, account_name, country, status, rank, af_rank, playtime)):
            await interaction.followup.send(**plain("Provide at least one field to update."), ephemeral=True)
            return

        updates = {}

        if ig_name:
            ig_name = ig_name.strip()
            if not (MIN_NAME_LENGTH <= len(ig_name) <= MAX_NAME_LENGTH):
                await interaction.followup.send(**plain(f"ig_name must be {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} characters."), ephemeral=True)
                return
            updates["ig_name"] = ig_name

        if account_name:
            account_name = account_name.strip()
            if not (MIN_NAME_LENGTH <= len(account_name) <= MAX_NAME_LENGTH):
                await interaction.followup.send(**plain(f"account_name must be {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} characters."), ephemeral=True)
                return
            updates["account_name"] = account_name

        if country:
            resolved = _resolve_country(country.strip())
            if not resolved:
                await interaction.followup.send(**plain("Use code (US, EG) or country name (United States, Egypt)."), ephemeral=True)
                return
            country = resolved
            updates["country"] = country

        if status:
            status = status.strip()
            if status not in ("Active", "Reserve"):
                await interaction.followup.send(**plain("Status must be 'Active' or 'Reserve'."), ephemeral=True)
                return
            updates["status"] = status

        playtime_parsed = None
        if playtime is not None:
            playtime_parsed = _parse_playtime(playtime)
            if not playtime_parsed:
                await interaction.followup.send(
                    **plain("Playtime must be hours;minutes with minutes 0-59, like `12;30`. Use `+1;15` to add or `-1;15` to remove."),
                    ephemeral=True)
                return

        rank_value = None
        if rank:
            rank = rank.strip()
            if rank not in RANK_ORDER:
                await interaction.followup.send(**plain(f"Must be one of: {', '.join(RANK_ORDER)}"), ephemeral=True)
                return
            rank_value = rank

        af_rank_value = None
        is_af_value   = None
        if af_rank:
            af_rank = af_rank.strip()
            if af_rank.lower() == "none":
                af_rank_value = 'None'
                is_af_value   = 0
            else:
                if af_rank not in AF_RANK_ORDER:
                    await interaction.followup.send(**plain(f"Must be one of: None, {', '.join(AF_RANK_ORDER)}"), ephemeral=True)
                    return
                af_rank_value = af_rank
                is_af_value   = 1

        try:
            async with aget_db_connection() as conn:
                row = (await conn.execute(
                    "SELECT rank, is_af, af_rank, ig_name, account_name, country FROM members WHERE discord_id = ?",
                    (str(user.id),)
                )).fetchone()
                if not row:
                    await interaction.followup.send(**plain(f"Member {user.mention} not found."), ephemeral=True)
                    return

                old_rank    = row['rank']
                old_is_af   = bool(row['is_af'])
                old_af_rank = row['af_rank']

                if updates:
                    placeholders = ", ".join(f"{k} = ?" for k in updates)
                    (await conn.execute(
                        f"UPDATE members SET {placeholders} WHERE discord_id = ?",
                        list(updates.values()) + [str(user.id)]
                    ))

                rank_changed = False
                if rank_value and rank_value != old_rank:
                    (await conn.execute(
                        "UPDATE members SET rank = ?, last_updated = CURRENT_TIMESTAMP WHERE discord_id = ?",
                        (rank_value, str(user.id))
                    ))
                    rank_changed = True

                af_changed = False
                if af_rank_value is not None or is_af_value is not None:
                    old_af_rank_str = old_af_rank or 'None'
                    new_af_rank_str = af_rank_value or 'None'
                    af_status_changing = (is_af_value == 0 and old_is_af) or (is_af_value == 1 and not old_is_af)
                    af_rank_changing   = old_af_rank_str != new_af_rank_str
                    if af_status_changing or af_rank_changing:
                        af_changed = True
                    if is_af_value == 0:
                        (await conn.execute("UPDATE members SET af_rank = 'None', is_af = 0 WHERE discord_id = ?", (str(user.id),)))
                    else:
                        (await conn.execute("UPDATE members SET af_rank = ?, is_af = 1 WHERE discord_id = ?", (af_rank_value, str(user.id))))

                playtime_change = None
                if playtime_parsed:
                    pt_mode, pt_minutes = playtime_parsed
                    cur_total = (await conn.execute(
                        "SELECT COALESCE(SUM(minutes), 0) AS t FROM playtime_log WHERE discord_id = ?",
                        (str(user.id),)
                    )).fetchone()["t"] or 0
                    if pt_mode == "add":
                        new_total = cur_total + pt_minutes
                    elif pt_mode == "remove":
                        new_total = max(0, cur_total - pt_minutes)
                    else:
                        new_total = pt_minutes
                    delta = new_total - cur_total
                    (await conn.execute(
                        "INSERT INTO playtime_log (discord_id, log_date, minutes) VALUES (?, ?, ?) "
                        "ON CONFLICT(discord_id, log_date) DO UPDATE SET minutes = minutes + excluded.minutes",
                        (str(user.id), datetime.now(timezone.utc).strftime("%Y-%m-%d"), delta)
                    ))
                    (await conn.execute("UPDATE members SET playtime_minutes = ? WHERE discord_id = ?", (new_total, str(user.id))))
                    playtime_change = (cur_total, new_total)

                current_rank = rank_value or old_rank
                (await conn.execute(
                    "INSERT OR IGNORE INTO rank_stats (discord_id, rank, promo_points, leadership_points, af_points) VALUES (?, ?, 0, 0, 0)",
                    (str(user.id), current_rank)
                ))

                if rank_changed:
                    await self.transfer_global_rank_stats(conn, str(user.id), old_rank, current_rank)
                    try:
                        (await conn.execute("DELETE FROM promotion_boosts WHERE discord_id = ?", (str(user.id),)))
                        (await conn.execute("DELETE FROM promotion_delays WHERE discord_id = ?", (str(user.id),)))
                    except db_errors.Error:
                        pass

                if af_changed:
                    await self.transfer_af_rank_stats(conn, str(user.id))

                (await conn.commit())

            label_map = {
                'ig_name':      'IGN',
                'account_name': 'Account',
                'country':      'Country',
                'status':       'Status',
                'rank':         'Rank',
                'af_rank':      'AF Rank',
            }
            old_fields = {
                'ig_name':      row['ig_name'],
                'account_name': row['account_name'],
                'country':      row['country'],
                'rank':         old_rank,
            }

            response_parts = []
            for key, new_value in updates.items():
                old_value = old_fields.get(key)
                label     = label_map.get(key, key.replace("_", " ").capitalize())
                response_parts.append(f"{label}: `{old_value}` to `{new_value}`" if old_value else f"{label}: `{new_value}`")

            if rank_value:
                response_parts.append(f"Rank: `{old_rank}` to `{rank_value}`")
            if af_rank_value is not None:
                response_parts.append(f"AF Rank: `{old_af_rank or 'None'}` to `{'None' if af_rank_value == 'None' else af_rank_value}`")

            if playtime_change:
                response_parts.append(f"Playtime: `{format_playtime(playtime_change[0])}` to `{format_playtime(playtime_change[1])}`")
                try:
                    member_cache.invalidate(f"profile:{user.id}")
                except (AttributeError, KeyError):
                    pass

            changes = "".join(f"\n\u2022 {part}" for part in response_parts)
            await interaction.followup.send(**plain(f"{user.mention} was updated.{changes}"))

            logs_cog = self.bot.get_cog("Logs")
            if logs_cog:
                description_parts = []
                if rank_changed:
                    description_parts.append(f"Rank changed from {old_rank} to {rank_value}")
                if af_changed:
                    description_parts.append(f"Air Force Rank changed from {old_af_rank or 'None'} to {af_rank_value or 'None'}")
                if playtime_change:
                    description_parts.append(f"Playtime changed from {format_playtime(playtime_change[0])} to {format_playtime(playtime_change[1])}")
                for key, new_value in updates.items():
                    old_value = old_fields.get(key)
                    if old_value != new_value and key != 'rank':
                        description_parts.append(f"{label_map.get(key, key.replace('_', ' ').title())} changed from {old_value} to {new_value}")
                if description_parts:
                    member_name = get_user_display_name(user)
                    await logs_cog.log_action(
                        action="Member Updated",
                        description=f"{member_name} has been updated: " + ", ".join(description_parts),
                        user=interaction.user,
                        target_user=user
                    )

        except Exception as e:
            logger.error(f"[MEMBER-UPDATE] {type(e).__name__}: {e}", exc_info=True)
            await interaction.followup.send(**plain(f"Update failed: {e}"), ephemeral=True)

    async def transfer_global_rank_stats(self, conn: object, discord_id: str, old_rank: str, new_rank: str) -> None:
        current_stats = (await conn.execute(
            "SELECT * FROM rank_stats WHERE discord_id = ? AND rank = ?", (discord_id, old_rank)
        )).fetchone()
        if not current_stats:
            return

        career_cols = [
            'warzone', 'doomsday', 'ammo_transport', 'bank_ops', 'casino_ops',
            'securicar_ops', 'andromada_ops', 'andromada_terrors_ops', 'assault_ops',
            'training', 'patrol', 'extra_points',
        ]

        career_updates = ", ".join(f"total_{col} = total_{col} + {current_stats[col] or 0}" for col in career_cols)
        (await conn.execute(f"UPDATE career_stats SET {career_updates} WHERE discord_id = ?", (discord_id,)))

        promo = current_stats['promo_points'] or 0
        lead  = current_stats['leadership_points'] or 0
        if promo or lead:
            (await conn.execute(
                "UPDATE points SET promo_points = promo_points + ?, leadership_points = leadership_points + ? WHERE discord_id = ?",
                (promo, lead, discord_id)
            ))

        (await conn.execute(
            "UPDATE rank_stats SET warzone = 0, doomsday = 0, ammo_transport = 0, bank_ops = 0, casino_ops = 0, "
            "securicar_ops = 0, andromada_ops = 0, andromada_terrors_ops = 0, assault_ops = 0, training = 0, "
            "patrol = 0, extra_points = 0, promo_points = 0, leadership_points = 0 WHERE discord_id = ? AND rank = ?",
            (discord_id, old_rank)
        ))
        (await conn.execute(
            "INSERT OR IGNORE INTO rank_stats (discord_id, rank, promo_points, leadership_points, af_points) VALUES (?, ?, 0, 0, 0)",
            (discord_id, new_rank)
        ))

    async def transfer_af_rank_stats(self, conn: object, discord_id: str) -> None:
        current_stats = (await conn.execute(
            "SELECT * FROM rank_stats WHERE discord_id = ?", (discord_id,)
        )).fetchone()
        if not current_stats:
            return

        for col in ('af_warzone', 'af_counter', 'af_training'):
            value = current_stats[col] if col in current_stats else 0
            if value > 0:
                (await conn.execute(f"UPDATE career_stats SET total_{col} = total_{col} + ? WHERE discord_id = ?", (value, discord_id)))

        af_points = current_stats['af_points'] if "af_points" in current_stats else 0
        if af_points > 0:
            (await conn.execute("UPDATE points SET af_points = af_points + ? WHERE discord_id = ?", (af_points, discord_id)))

        (await conn.execute(
            "UPDATE rank_stats SET af_warzone = 0, af_counter = 0, af_training = 0, af_points = 0 WHERE discord_id = ?",
            (discord_id,)
        ))

    @member.command(name="remove", description="Permanently remove a member (HQ only)")
    @require_permissions(admin=True, acl="manage_members")
    @describe(user="The member to remove (real user only)", reason="Reason for removal (min 5 chars)")
    async def member_remove(self, interaction: Interaction, user: User, reason: str):
        if user.bot:
            await interaction.response.send_message(**plain("Cannot remove bots."), ephemeral=True)
            return

        reason = reason.strip()
        if len(reason) < REASON_MIN_LENGTH:
            await interaction.response.send_message(**plain(f"Reason must be at least {REASON_MIN_LENGTH} characters."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        try:
            async with aget_db_connection() as conn:
                if not (await conn.execute("SELECT ig_name FROM members WHERE discord_id = ?", (str(user.id),))).fetchone():
                    await interaction.followup.send(**plain("This member is not in the database."), ephemeral=True)
                    return

                uid = str(user.id)
                (await conn.execute("PRAGMA foreign_keys = OFF"))
                try:
                    (await conn.execute("DELETE FROM points WHERE discord_id = ?",                    (uid,)))
                    (await conn.execute("DELETE FROM rank_stats WHERE discord_id = ?",                (uid,)))
                    (await conn.execute("DELETE FROM career_stats WHERE discord_id = ?",              (uid,)))
                    (await conn.execute("DELETE FROM leadership_history WHERE discord_id = ?",        (uid,)))
                    (await conn.execute("DELETE FROM operation_participants WHERE discord_id = ?",    (uid,)))
                    (await conn.execute("DELETE FROM promotion_boosts WHERE discord_id = ?",          (uid,)))
                    (await conn.execute("DELETE FROM promotion_delays WHERE discord_id = ?",          (uid,)))
                    (await conn.execute("DELETE FROM extra_points_requests WHERE submitted_by = ?",   (uid,)))
                    (await conn.execute("UPDATE reports SET reporter_id = NULL WHERE reporter_id = ?",                        (uid,)))
                    (await conn.execute("UPDATE promotion_boosts SET applied_by = NULL WHERE applied_by = ?",                 (uid,)))
                    (await conn.execute("UPDATE promotion_delays SET applied_by = NULL WHERE applied_by = ?",                 (uid,)))
                    (await conn.execute("UPDATE extra_points_requests SET approved_by = NULL WHERE approved_by = ?",          (uid,)))
                    (await conn.execute("DELETE FROM members WHERE discord_id = ?",                   (uid,)))
                    (await conn.commit())
                finally:
                    (await conn.execute("PRAGMA foreign_keys = ON"))

            await interaction.followup.send(**plain(f"{user.mention} was permanently removed (reason: {reason})"))

            logs_cog = self.bot.get_cog("Logs")
            if logs_cog:
                await logs_cog.log_action(
                    action="Member Removed",
                    description=f"{get_user_display_name(user)} has been removed (reason: {reason})",
                    user=interaction.user,
                    target_user=user
                )

        except Exception as e:
            logger.error(f"[MEMBER-REMOVE] {type(e).__name__}: {e}", exc_info=True)
            await interaction.followup.send(**plain(f"Removal failed: {e}"), ephemeral=True)

    @member.command(name="list", description="List members (HQ only)")
    @require_permissions(admin=True, acl="manage_members")
    @describe(af_filter="Filter by Air Force status", status_filter="Filter by member status")
    @app_commands.choices(
        af_filter=[
            Choice(name="Show all members",    value="all"),
            Choice(name="Air Force only",      value="yes"),
            Choice(name="Non-Air Force only",  value="no")
        ],
        status_filter=[
            Choice(name="All statuses",   value="all"),
            Choice(name="Active only",    value="Active"),
            Choice(name="Reserve only",   value="Reserve")
        ]
    )
    async def member_list(self, interaction: Interaction, af_filter: str = "all", status_filter: str = "all"):
        await interaction.response.defer(ephemeral=False)
        try:
            rank_order_index = {rank: idx for idx, rank in enumerate(RANK_ORDER)}
            query  = "SELECT discord_id, discord_name, ig_name, rank, status, is_af, af_rank, country FROM members WHERE 1=1"
            params = []
            if af_filter == "yes":
                query += " AND is_af = 1"
            elif af_filter == "no":
                query += " AND is_af = 0"
            if status_filter != "all":
                query += " AND status = ?"
                params.append(status_filter)

            async with aget_db_connection() as conn:
                rows = (await conn.execute(query, params)).fetchall()

            if not rows:
                await interaction.followup.send(**plain("No members found."), ephemeral=True)
                return

            sorted_rows = sorted(rows, key=lambda r: rank_order_index.get(r['rank'], 999))
            per_page    = MEMBER_LIST_PER_PAGE
            total_pages = (len(sorted_rows) + per_page - 1) // per_page

            class ListView(View):
                def __init__(self, initial_page: int = 0):
                    super().__init__(timeout=300)
                    self.current_page = initial_page
                    self._update_buttons()

                def _update_buttons(self):
                    for child in self.children:
                        if child.custom_id == "previous":
                            child.disabled = self.current_page == 0
                        elif child.custom_id == "next":
                            child.disabled = self.current_page >= total_pages - 1

                async def generate_embed(self):
                    start     = self.current_page * per_page
                    page_rows = sorted_rows[start:start + per_page]
                    embed     = create_embed(
                        title=f"Member List: Page {self.current_page + 1}/{total_pages}",
                        description=None,
                        user=None,
                        thumbnail=True,
                    )

                    grouped: dict = {}
                    for row in page_rows:
                        grouped.setdefault(row['rank'], []).append(row)

                    global_idx = start + 1
                    for rank, members in grouped.items():
                        lines = []
                        for row in members:
                            status_icon = f"[{row['status']}]"
                            af_part     = f" | AF: {row['af_rank']}" if row['is_af'] and row['af_rank'] and row['af_rank'] != 'None' else ""
                            lines.append(f"`{global_idx}.` {row['ig_name']}: <@{row['discord_id']}> {status_icon}{af_part}")
                            global_idx += 1

                        chunks, current, current_len = [], [], 0
                        for line in lines:
                            if current_len + len(line) + 1 > 1024:
                                chunks.append(current)
                                current, current_len = [], 0
                            current.append(line)
                            current_len += len(line) + 1
                        if current:
                            chunks.append(current)

                        for idx, chunk in enumerate(chunks):
                            field_name = f"**{rank}**" if idx == 0 else "\u200b"
                            embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

                    filter_parts = []
                    af_text = {"yes": "Air Force only", "no": "Non-AF only"}.get(af_filter, "")
                    if af_text:
                        filter_parts.append(af_text)
                    if status_filter != "all":
                        filter_parts.append(f"{status_filter} only")
                    filter_info = " · ".join(filter_parts) if filter_parts else "All members"
                    footer_text = f"{EMBED_FOOTER_TEXT} · {filter_info} · Total: {len(sorted_rows)}"
                    try:
                        embed.set_footer(text=footer_text, icon_url=EMBED_THUMBNAIL)
                    except (ValueError, AttributeError):
                        embed.set_footer(text=footer_text)
                    return embed

                @discord.ui.button(label="Previous", style=ButtonStyle.grey, custom_id="previous")
                async def previous(self, interaction: Interaction, button: Button):
                    if self.current_page > 0:
                        self.current_page -= 1
                        self._update_buttons()
                        await interaction.response.edit_message(embed=await self.generate_embed(), view=self)
                    else:
                        await interaction.response.defer()

                @discord.ui.button(label="Next", style=ButtonStyle.grey, custom_id="next")
                async def next(self, interaction: Interaction, button: Button):
                    if self.current_page < total_pages - 1:
                        self.current_page += 1
                        self._update_buttons()
                        await interaction.response.edit_message(embed=await self.generate_embed(), view=self)
                    else:
                        await interaction.response.defer()

            view  = ListView()
            embed = await view.generate_embed()
            await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            logger.error(f"[MEMBER-LIST] {type(e).__name__}: {e}", exc_info=True)
            await interaction.followup.send(**plain("Error fetching member list."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Member(bot))
