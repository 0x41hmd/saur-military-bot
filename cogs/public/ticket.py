import asyncio
import html
import io
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict

import discord
from discord.ext import commands

from database.db import aget_db_connection
from database.db import get_db_connection, get_config_value, set_config_value
from utils.helpers import create_embed, get_channel_safe, plain

logger = logging.getLogger(__name__)

TICKET_TYPES = [
    "Report a bug",
    "Referral request",
    "Report a teammate",
    "Report SWAT member",
    "Report Terrorists member",
    "Request a promotion",
    "Request a permission",
    "Request LOA (Leave of Absence)",
    "Contact the HQ team",
]

MAX_OPEN_TICKETS_PER_USER = 3
TICKET_CREATION_COOLDOWN = 60

GUEST_TICKET_TYPES = {"Contact the HQ team"}
GUEST_DENIED_TEXT = "You are not in the roster yet, so you can only open **Contact the HQ team** tickets."

TICKET_CHANNEL_PREFIX = "tickets"
TICKET_CATEGORY_KEY = "ticket_category_id"
TRANSCRIPT_CHANNEL_KEY = "ticket_transcript_channel"
CLOSE_DELAY_SECONDS = 5


class TicketRateLimiter:
    def __init__(self):
        self._last_creation: Dict[int, float] = {}

    def can_create_ticket(self, user_id: int) -> Tuple[bool, Optional[str]]:
        if user_id in self._last_creation:
            elapsed = time.time() - self._last_creation[user_id]
            if elapsed < TICKET_CREATION_COOLDOWN:
                remaining = int(TICKET_CREATION_COOLDOWN - elapsed)
                return False, f"Please wait {remaining} seconds before creating another ticket."

        try:
            with get_db_connection() as conn:
                result = conn.execute(
                    "SELECT COUNT(*) as count FROM tickets WHERE creator_id = ? AND status = 'open'",
                    (str(user_id),)
                ).fetchone()
                if (result['count'] if result else 0) >= MAX_OPEN_TICKETS_PER_USER:
                    return False, f"You already have {MAX_OPEN_TICKETS_PER_USER} open tickets. Please close one before opening another."
        except Exception as e:
            logger.error(f"Error checking open tickets: {e}")

        return True, None

    def record_creation(self, user_id: int):
        self._last_creation[user_id] = time.time()


def is_registered_member(user_id: int) -> bool:
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM members WHERE discord_id = ?", (str(user_id),)
            ).fetchone()
            return row is not None
    except Exception as e:
        logger.error(f"Error checking member registration: {e}")
        return False


def can_open_type(user_id: int, ticket_type: str) -> bool:
    return ticket_type in GUEST_TICKET_TYPES or is_registered_member(user_id)


class TicketSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a ticket type...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=t, value=t) for t in TICKET_TYPES],
            custom_id="ticket_select_type"
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        choice = self.values[0] if self.values else None
        if choice and not (await asyncio.to_thread(can_open_type, interaction.user.id, choice)):
            if isinstance(view, TicketSelectView):
                view.selections.pop(interaction.user.id, None)
            await interaction.response.send_message(GUEST_DENIED_TEXT, ephemeral=True)
            return
        if isinstance(view, TicketSelectView) and choice:
            view.selections[interaction.user.id] = choice
        await interaction.response.defer()


class TicketSelectView(discord.ui.View):
    def __init__(self, timeout: Optional[int] = None):
        super().__init__(timeout=timeout)
        self.selections: Dict[int, str] = {}
        self.add_item(TicketSelect())

    @discord.ui.button(
        label="Open Ticket",
        style=discord.ButtonStyle.green,
        custom_id="ticket_open_button",
        row=1
    )
    async def open_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Ticket")
        if not cog:
            await interaction.response.send_message(
                "Ticket system not loaded. Please contact an administrator.", ephemeral=True
            )
            return

        ticket_type = self.selections.get(interaction.user.id)
        if not ticket_type:
            await interaction.response.send_message(
                "Please select a ticket type from the dropdown first!", ephemeral=True
            )
            return

        if ticket_type not in TICKET_TYPES:
            await interaction.response.send_message(**plain("Invalid ticket type selected."), ephemeral=True)
            return

        if not (await asyncio.to_thread(can_open_type, interaction.user.id, ticket_type)):
            await interaction.response.send_message(GUEST_DENIED_TEXT, ephemeral=True)
            return

        await cog.create_ticket(interaction, ticket_type)


class CloseTicketView(discord.ui.View):
    def __init__(self, cog: Optional['Ticket'] = None, ticket_channel_id: int = 0):
        super().__init__(timeout=None)
        self.cog = cog
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.red,
        custom_id="ticket_close_button"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except Exception as e:
            logger.error(f"Error deferring interaction: {e}")
            return

        ticket_channel = interaction.channel
        cog = self.cog or interaction.client.get_cog("Ticket")
        if not ticket_channel or not cog:
            await interaction.followup.send(**plain("Could not find ticket channel."), ephemeral=True)
            return

        if not await self._can_close_ticket(interaction, ticket_channel):
            await interaction.followup.send(
                "You do not have permission to close this ticket.", ephemeral=True
            )
            return

        await cog.close_ticket(interaction, ticket_channel)

    async def _can_close_ticket(
        self,
        interaction: discord.Interaction,
        ticket_channel: discord.TextChannel
    ) -> bool:
        try:
            async with aget_db_connection() as conn:
                result = (await conn.execute(
                    "SELECT creator_id FROM tickets WHERE channel_id = ?",
                    (str(ticket_channel.id),)
                )).fetchone()
                if result and result['creator_id'] == str(interaction.user.id):
                    return True
        except Exception as e:
            logger.error(f"Error checking ticket creator: {e}")
            if interaction.user in ticket_channel.overwrites:
                if ticket_channel.overwrites[interaction.user].view_channel is True:
                    return True

        admin_cog = interaction.client.get_cog("Admin")
        if not admin_cog:
            return False

        if (await asyncio.to_thread(admin_cog._has_acl_permission, interaction.user.id, "manage_tickets")):
            return True

        if hasattr(interaction.user, 'roles'):
            for role in interaction.user.roles:
                if (await asyncio.to_thread(admin_cog._has_role_acl_permission, role.id, "manage_tickets")):
                    return True

        return False


class Ticket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rate_limiter = TicketRateLimiter()
        self._closing: set = set()

    async def _get_admin_config(self) -> Tuple[List[int], List[int]]:
        admin_role_ids = []
        admin_member_ids = []
        try:
            async with aget_db_connection() as conn:
                row = (await conn.execute("SELECT value FROM config WHERE key = 'admin_roles'")).fetchone()
                if row and row['value']:
                    admin_role_ids = [int(r) for r in row['value'].split(',') if r.strip().isdigit()]

                row = (await conn.execute("SELECT value FROM config WHERE key = 'admin_members'")).fetchone()
                if row and row['value']:
                    admin_member_ids = [int(m) for m in row['value'].split(',') if m.strip().isdigit()]
        except Exception as e:
            logger.error(f"Error fetching admin configuration: {e}", exc_info=True)
        return admin_role_ids, admin_member_ids

    def _has_ticket_management_permission(self, admin_cog, entity_id: int, is_role: bool = False) -> bool:
        if not admin_cog:
            return False
        try:
            if is_role:
                return admin_cog._has_role_acl_permission(entity_id, "manage_tickets")
            return admin_cog._has_acl_permission(entity_id, "manage_tickets")
        except AttributeError:
            return False

    def _build_admin_overwrites(
        self,
        guild: discord.Guild,
        admin_cog,
        admin_role_ids: List[int],
        admin_member_ids: List[int],
        perms: discord.PermissionOverwrite
    ) -> dict:
        overwrites = {}
        for role_id in admin_role_ids:
            if not self._has_ticket_management_permission(admin_cog, role_id, is_role=True):
                continue
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = perms
        for member_id in admin_member_ids:
            if not self._has_ticket_management_permission(admin_cog, member_id, is_role=False):
                continue
            member = guild.get_member(member_id)
            if member:
                overwrites[member] = perms
        return overwrites

    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user = interaction.user

        if not (await asyncio.to_thread(can_open_type, user.id, ticket_type)):
            await interaction.followup.send(GUEST_DENIED_TEXT, ephemeral=True)
            return

        can_create, error_msg = (await asyncio.to_thread(self.rate_limiter.can_create_ticket, user.id))
        if not can_create:
            await interaction.followup.send(error_msg, ephemeral=True)
            return

        admin_role_ids, admin_member_ids = await self._get_admin_config()
        admin_cog = self.bot.get_cog("Admin")

        active_category = None
        saved_id = await asyncio.to_thread(get_config_value, TICKET_CATEGORY_KEY, None)
        if saved_id and str(saved_id).isdigit():
            found = guild.get_channel(int(saved_id))
            if isinstance(found, discord.CategoryChannel):
                active_category = found
        if not active_category:
            active_category = discord.utils.get(guild.categories, name="active-tickets")
            if active_category:
                await asyncio.to_thread(set_config_value, TICKET_CATEGORY_KEY, str(active_category.id))
        if not active_category:
            try:
                active_category = await guild.create_category("active-tickets", position=0)
                await asyncio.to_thread(set_config_value, TICKET_CATEGORY_KEY, str(active_category.id))
            except discord.Forbidden:
                await interaction.followup.send(
                    "Bot lacks permission to create category.", ephemeral=True
                )
                return
            except Exception as e:
                logger.error(f"Failed to create category: {e}", exc_info=True)
                await interaction.followup.send(**plain(f"Failed to create category: {e}"), ephemeral=True)
                return

        placeholder = f"pending-{uuid.uuid4().hex}"
        try:
            async with aget_db_connection() as conn:
                cursor = (await conn.execute(
                    "INSERT INTO tickets (channel_id, creator_id, ticket_type, status) VALUES (?, ?, ?, 'open')",
                    (placeholder, str(user.id), ticket_type)
                ))
                ticket_id = cursor.lastrowid
                (await conn.commit())
        except Exception as e:
            logger.error(f"Failed to reserve ticket number: {e}", exc_info=True)
            await interaction.followup.send(**plain("Failed to create ticket. Please try again."), ephemeral=True)
            return

        channel_name = f"{TICKET_CHANNEL_PREFIX}-{ticket_id:04d}"

        try:
            full_perms = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
            bot_perms = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True, attach_files=True, manage_channels=True
            )
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: full_perms,
                **(await asyncio.to_thread(self._build_admin_overwrites, guild, admin_cog, admin_role_ids, admin_member_ids, full_perms))
            }
            if guild.me:
                overwrites[guild.me] = bot_perms
            ticket_channel = await active_category.create_text_channel(
                channel_name,
                overwrites=overwrites,
                topic=f"Ticket #{ticket_id:04d} | {ticket_type} | opened by {user.id}"
            )
            logger.info(f"Created ticket channel: {ticket_channel.name} for user {user.id}")
        except Exception as e:
            (await asyncio.to_thread(self._discard_ticket_row, placeholder))
            if isinstance(e, discord.Forbidden):
                await interaction.followup.send(
                    "Bot lacks permission to create ticket channel.", ephemeral=True
                )
            else:
                logger.error(f"Failed to create ticket channel: {e}", exc_info=True)
                await interaction.followup.send(**plain(f"Failed to create ticket: {e}"), ephemeral=True)
            return

        try:
            async with aget_db_connection() as conn:
                (await conn.execute(
                    "UPDATE tickets SET channel_id = ? WHERE id = ?",
                    (str(ticket_channel.id), ticket_id)
                ))
                (await conn.commit())
        except Exception as e:
            logger.error(f"Failed to store ticket channel in database: {e}", exc_info=True)
            (await asyncio.to_thread(self._discard_ticket_row, placeholder))
            try:
                await ticket_channel.delete(reason="Ticket could not be saved")
            except Exception:
                pass
            await interaction.followup.send(**plain("Failed to create ticket. Please try again."), ephemeral=True)
            return

        embed = create_embed(
            title=f"Ticket #{ticket_id:04d}",
            description="HQ team will assist you shortly. Please describe your issue."
        )
        embed.add_field(name="Type", value=f"`{ticket_type}`", inline=False)
        embed.add_field(name="Opened by", value=f"`{user.display_name}`", inline=False)
        embed.add_field(name="Opened at", value=f"`{_fmt_dt(datetime.now(timezone.utc))}`", inline=False)

        mentions = [user.mention]
        for role_id in admin_role_ids:
            if not (await asyncio.to_thread(self._has_ticket_management_permission, admin_cog, role_id, is_role=True)):
                continue
            role = guild.get_role(role_id)
            if role:
                mentions.append(role.mention)
        for member_id in admin_member_ids:
            if not (await asyncio.to_thread(self._has_ticket_management_permission, admin_cog, member_id, is_role=False)):
                continue
            member = guild.get_member(member_id)
            if member:
                mentions.append(member.mention)

        try:
            ticket_message = await ticket_channel.send(
                content=" ".join(mentions),
                embed=embed,
                view=CloseTicketView(self, ticket_channel.id),
                allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False)
            )

            try:
                async with aget_db_connection() as conn:
                    (await conn.execute(
                        "UPDATE tickets SET message_id = ? WHERE channel_id = ?",
                        (str(ticket_message.id), str(ticket_channel.id))
                    ))
                    (await conn.commit())
            except Exception as e:
                logger.error(f"Failed to update ticket message_id: {e}")

        except Exception as e:
            logger.error(f"Error sending ticket messages: {e}", exc_info=True)

        logs_cog = self.bot.get_cog("Logs")
        if logs_cog:
            try:
                await logs_cog.log_action(
                    action="Ticket Created",
                    description=f"{user.mention} opened {ticket_channel.mention}",
                    user=user,
                    details={"Ticket": f"#{ticket_id:04d}", "Type": ticket_type},
                )
            except Exception as e:
                logger.error(f"Error logging ticket creation: {e}")

        self.rate_limiter.record_creation(user.id)
        await interaction.followup.send(**plain(f"Ticket created: {ticket_channel.mention}"), ephemeral=True)

    @staticmethod
    def _discard_ticket_row(channel_id: str):
        try:
            with get_db_connection() as conn:
                conn.execute("DELETE FROM tickets WHERE channel_id = ?", (channel_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to discard ticket row: {e}")

    async def _get_transcript_channel(self) -> Optional[discord.TextChannel]:
        try:
            return await get_channel_safe(self.bot, TRANSCRIPT_CHANNEL_KEY)
        except Exception as e:
            logger.error(f"Failed to get transcript channel: {e}", exc_info=True)
            return None

    async def close_ticket(self, interaction: discord.Interaction, ticket_channel: discord.TextChannel):
        guild = interaction.guild

        if ticket_channel.id in self._closing:
            await interaction.followup.send(**plain("This ticket is already being closed."), ephemeral=True)
            return

        try:
            async with aget_db_connection() as conn:
                row = (await conn.execute(
                    "SELECT id, creator_id, ticket_type, status, created_at FROM tickets WHERE channel_id = ?",
                    (str(ticket_channel.id),)
                )).fetchone()
        except Exception as e:
            logger.error(f"Failed to read ticket from database: {e}", exc_info=True)
            await interaction.followup.send(**plain("Could not read this ticket from the database."), ephemeral=True)
            return

        if not row:
            await interaction.followup.send(**plain("This channel is not a registered ticket."), ephemeral=True)
            return

        if row['status'] == 'closed':
            await interaction.followup.send(**plain("This ticket is already closed."), ephemeral=True)
            return

        transcript_channel = await self._get_transcript_channel()
        if not transcript_channel:
            await interaction.followup.send(
                **plain("No ticket transcript channel is configured. Run `/setup` first. The ticket was NOT closed."),
                ephemeral=True
            )
            return

        self._closing.add(ticket_channel.id)
        try:
            ticket_id = row['id']
            ticket_type = row['ticket_type']
            closed_at = datetime.now(timezone.utc)
            opened_at = _parse_db_time(row['created_at']) or ticket_channel.created_at

            creator = guild.get_member(int(row['creator_id']))
            creator_name = creator.display_name if creator else f"Unknown ({row['creator_id']})"

            try:
                messages = [m async for m in ticket_channel.history(limit=None, oldest_first=True)]
            except discord.Forbidden:
                await interaction.followup.send(
                    **plain("Bot cannot read this channel's history, so the ticket was NOT closed."),
                    ephemeral=True
                )
                return
            except Exception as e:
                logger.error(f"Failed to read ticket history: {e}", exc_info=True)
                await interaction.followup.send(**plain(f"Failed to read ticket history: {e}"), ephemeral=True)
                return

            human_messages = [m for m in messages if not m.author.bot]
            participants: Dict[int, Tuple[str, int]] = {}
            for m in human_messages:
                name, count = participants.get(m.author.id, (m.author.display_name, 0))
                participants[m.author.id] = (name, count + 1)

            header = {
                "Ticket": f"#{ticket_id:04d}",
                "Type": ticket_type,
                "Opened by": creator_name,
                "Closed by": interaction.user.display_name,
                "Opened": _fmt_dt(opened_at),
                "Closed": _fmt_dt(closed_at),
            }
            transcript_html = _build_transcript_html(header, messages)
            file_name = f"transcript-{TICKET_CHANNEL_PREFIX}-{ticket_id:04d}.html"
            data = transcript_html.encode("utf-8")

            if len(data) > guild.filesize_limit:
                await interaction.followup.send(
                    **plain("The transcript is too large to upload, so the ticket was NOT closed. Ask an admin."),
                    ephemeral=True
                )
                return

            lines = [
                f"**Ticket #{ticket_id:04d} Closed**",
                "",
                f"**Type:** `{ticket_type}`",
                f"**Opened by:** `{creator_name}`",
                f"**Closed by:** `{interaction.user.display_name}`",
                f"**Opened at:** `{_fmt_dt(opened_at)}`",
                f"**Closed at:** `{_fmt_dt(closed_at)}`",
                f"**Duration:** `{_fmt_duration(closed_at - opened_at)}`",
                f"**Messages:** `{len(human_messages)}`",
            ]

            if participants:
                ranked = sorted(participants.values(), key=lambda x: x[1], reverse=True)[:10]
                lines.append("**Participants:**")
                lines.extend(f"`{name}`: `{count}`" for name, count in ranked)

            first = next((m for m in human_messages if m.content.strip()), None)
            if first:
                preview = " ".join(first.clean_content.split()).replace("`", "'")
                if len(preview) > 300:
                    preview = preview[:297] + "..."
                lines.append(f"**First message:** `{first.author.display_name}`: `{preview}`")

            summary_text = "\n".join(lines)[:2000]

            try:
                await transcript_channel.send(
                    summary_text,
                    file=discord.File(io.BytesIO(data), filename=file_name),
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    **plain(f"Bot cannot post in {transcript_channel.mention}, so the ticket was NOT closed."),
                    ephemeral=True
                )
                return
            except Exception as e:
                logger.error(f"Failed to post transcript: {e}", exc_info=True)
                await interaction.followup.send(
                    **plain(f"Failed to post the transcript, so the ticket was NOT closed: {e}"),
                    ephemeral=True
                )
                return

            try:
                async with aget_db_connection() as conn:
                    (await conn.execute(
                        "UPDATE tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP, closed_by = ? WHERE channel_id = ?",
                        (str(interaction.user.id), str(ticket_channel.id))
                    ))
                    (await conn.commit())
            except Exception as e:
                logger.error(f"Failed to update ticket in database: {e}", exc_info=True)

            logs_cog = self.bot.get_cog("Logs")
            if logs_cog:
                try:
                    await logs_cog.log_action(
                        action="Ticket Closed",
                        description=f"Ticket #{ticket_id:04d} closed, transcript saved in {transcript_channel.mention}",
                        user=interaction.user,
                        details={"Ticket": f"#{ticket_id:04d}", "Type": ticket_type},
                    )
                except Exception as e:
                    logger.error(f"Error logging ticket closure: {e}")

            await interaction.followup.send(
                **plain(f"Ticket closed. Transcript saved in {transcript_channel.mention}."),
                ephemeral=True
            )

            try:
                await ticket_channel.send(
                    **plain(f"This ticket is closed. The channel will be deleted in {CLOSE_DELAY_SECONDS} seconds.")
                )
            except Exception:
                pass

            await asyncio.sleep(CLOSE_DELAY_SECONDS)
            try:
                await ticket_channel.delete(reason=f"Ticket #{ticket_id:04d} closed by {interaction.user}")
                logger.info(f"Closed and deleted ticket channel for ticket {ticket_id}")
            except discord.Forbidden:
                logger.warning(f"Bot lacks permission to delete ticket channel {ticket_channel.id}")
                try:
                    await ticket_channel.send(**plain("I could not delete this channel. An admin needs to remove it."))
                except Exception:
                    pass
            except discord.NotFound:
                pass
            except Exception as e:
                logger.error(f"Failed to delete ticket channel: {e}", exc_info=True)
        finally:
            self._closing.discard(ticket_channel.id)


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_duration(delta) -> str:
    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _parse_db_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _build_transcript_html(header: Dict[str, str], messages: List[discord.Message]) -> str:
    esc = html.escape
    meta = "".join(f"<div><b>{esc(k)}:</b> {esc(v)}</div>" for k, v in header.items())

    rows = []
    for m in messages:
        body = esc(m.clean_content).replace("\n", "<br>")
        extras = []
        for a in m.attachments:
            extras.append(f'<div class="att">Attachment: <a href="{esc(a.url)}">{esc(a.filename)}</a></div>')
        for e in m.embeds:
            bits = [x for x in (e.title, e.description) if x]
            if bits:
                extras.append(f'<div class="emb">{esc(" | ".join(bits))}</div>'.replace("\n", "<br>"))
        if m.stickers:
            extras.append(f'<div class="att">Sticker: {esc(", ".join(s.name for s in m.stickers))}</div>')

        badge = ' <span class="bot">BOT</span>' if m.author.bot else ""
        rows.append(
            '<div class="msg">'
            f'<div class="top"><span class="author">{esc(m.author.display_name)}</span>{badge} '
            f'<span class="id">({m.author.id})</span> '
            f'<span class="time">{esc(_fmt_dt(m.created_at))}</span></div>'
            f'<div class="body">{body}</div>{"".join(extras)}</div>'
        )

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>Ticket {esc(header.get("Ticket", ""))}</title>'
        '<style>'
        'body{font-family:Segoe UI,Arial,sans-serif;background:#313338;color:#dbdee1;margin:0;padding:24px}'
        '.head{background:#2b2d31;border-radius:8px;padding:16px;margin-bottom:16px;line-height:1.6}'
        '.msg{background:#2b2d31;border-radius:8px;padding:10px 14px;margin-bottom:8px}'
        '.author{font-weight:600;color:#fff}.id,.time{color:#949ba4;font-size:12px}'
        '.bot{background:#5865f2;color:#fff;border-radius:4px;padding:0 4px;font-size:10px}'
        '.body{margin-top:4px;word-wrap:break-word}.att,.emb{margin-top:6px;color:#b5bac1;font-size:13px}'
        'a{color:#00a8fc}'
        '</style></head><body>'
        f'<div class="head">{meta}<div><b>Messages:</b> {len(messages)}</div></div>'
        + "".join(rows) +
        '</body></html>'
    )


async def setup(bot: commands.Bot):
    await bot.add_cog(Ticket(bot))
