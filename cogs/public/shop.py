import asyncio
import logging
import re
from typing import List, Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks

from cogs.public.casino import fmt, parse_amount
from database.db import get_channel_ids
from utils import casino_db as cdb
from utils import shop_db as sdb
from utils.helpers import create_embed, get_user_display_name, is_admin_member, plain, require_permissions
from utils.settings import CASINO_CONFIG, DEVELOPER_ID, SHOP_CONFIG

logger = logging.getLogger(__name__)

PERMISSION = "manage_shop"
MANAGE_NEEDS = {
    "add": ("item", "price"),
    "edit": ("item",),
    "restock": ("item", "amount"),
    "remove": ("item",),
    "orders": (),
}
STATUS_TEXT = {"pending": "Waiting for HQ", "fulfilled": "Confirmed", "rejected": "Rejected (refunded)"}


def _staff_can(bot: commands.Bot, user: discord.abc.User, guild: Optional[discord.Guild]) -> bool:
    if user.id == DEVELOPER_ID:
        return True
    admin = bot.get_cog("Admin")
    if admin is None:
        return False
    try:
        if is_admin_member(user) and admin._has_acl_permission(user.id, PERMISSION):
            return True
        if guild is not None:
            roles = getattr(user, "roles", [])
            for role_id in admin._get_admin_roles():
                role = guild.get_role(role_id)
                if role and role in roles and admin._has_role_acl_permission(role_id, PERMISSION):
                    return True
    except Exception as e:
        logger.error(f"Shop permission check failed: {e}", exc_info=True)
    return False


def _role_label(role_id, name: str, guild: Optional[discord.Guild]) -> str:
    if role_id and guild is not None and guild.get_role(int(role_id)) is not None:
        return f"<@&{role_id}>"
    return f"**{name}**"


def _perks(it: dict) -> List[str]:
    perks = []
    if it.get("daily_bonus"):
        perks.append(f"+{it['daily_bonus']}% daily reward")
    if it.get("win_bonus"):
        perks.append(f"+{it['win_bonus']}% casino & trivia winnings")
        perks.append(f"{CASINO_CONFIG['perk_cooldown_seconds']}s game cooldown instead of {CASINO_CONFIG['cooldown_seconds']}s")
        perks.append(f"+{CASINO_CONFIG['perk_win_chance_bonus']}% win chance on slots & roll")
    if it.get("role_id") and it.get("description"):
        perks.extend(p.strip() for p in re.split(r"[;\n]+", it["description"]) if p.strip())
    return perks


def _item_line(it: dict, guild: Optional[discord.Guild] = None) -> str:
    line = f"{_role_label(it.get('role_id'), it['name'], guild)} - {fmt(it['price'])} \u00b7 {it['unit']}"
    extra = []
    if it["stock"] is not None:
        extra.append("**OUT OF STOCK**" if it["stock"] <= 0 else f"{it['stock']:,} in stock")
    if extra:
        line += " \u00b7 " + " \u00b7 ".join(extra)
    if it.get("role_id"):
        perks = _perks(it)
        if perks:
            line += "\n" + "\n".join(f"\u2022 {p}" for p in perks)
    elif it["description"]:
        line += f"\n*{it['description']}*"
    return line


def _unit_for_role(days: Optional[int]) -> str:
    return f"{days} days" if days else "permanent"


def _ts(iso: str) -> int:
    return int(sdb.parse_iso(iso).timestamp())


def _clean_role(role: discord.Role, guild: discord.Guild) -> Optional[str]:
    if role.is_default() or role.managed:
        return "That role can't be given by the bot (everyone / bot / integration role)."
    if role >= guild.me.top_role:
        return f"I can't give {role.mention} because it is above my highest role. Move my role higher first."
    return None


class RejectModal(discord.ui.Modal, title="Reject order"):
    reason = discord.ui.TextInput(label="Reason (the buyer will see it)", required=False, max_length=200,
                                  style=discord.TextStyle.short)

    def __init__(self, order_id: int):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: Interaction) -> None:
        cog: Optional["Shop"] = interaction.client.get_cog("Shop")
        if cog is None:
            await interaction.response.send_message("The shop is not available right now.", ephemeral=True)
            return
        await cog.decide(interaction, self.order_id, confirm=False, note=str(self.reason).strip() or None)


class OrderButton(discord.ui.DynamicItem[discord.ui.Button], template=r"shop:(?P<action>confirm|reject):(?P<id>\d+)"):
    def __init__(self, action: str, order_id: int):
        confirm = action == "confirm"
        super().__init__(discord.ui.Button(
            label="Confirm" if confirm else "Reject",
            style=discord.ButtonStyle.success if confirm else discord.ButtonStyle.danger,
            custom_id=f"shop:{action}:{order_id}"))
        self.action = action
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction: Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: Interaction) -> None:
        cog: Optional["Shop"] = interaction.client.get_cog("Shop")
        if cog is None:
            await interaction.response.send_message("The shop is not available right now.", ephemeral=True)
            return
        if not (await asyncio.to_thread(_staff_can, interaction.client, interaction.user, interaction.guild)):
            await interaction.response.send_message(**plain("Only HQ with the `manage_shop` permission can do this."),
                                                    ephemeral=True)
            return
        if self.action == "reject":
            await interaction.response.send_modal(RejectModal(self.order_id))
        else:
            await cog.decide(interaction, self.order_id, confirm=True)


def _order_view(order_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(OrderButton("confirm", order_id))
    view.add_item(OrderButton("reject", order_id))
    return view


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(OrderButton)
        self.expire_roles.change_interval(minutes=SHOP_CONFIG["role_check_minutes"])
        self.expire_roles.start()

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(OrderButton)
        self.expire_roles.cancel()

    async def _audit(self, action: str, description: str, user: discord.abc.User,
                     target: Optional[discord.abc.User] = None, details: Optional[dict] = None) -> None:
        logs_cog = self.bot.get_cog("Logs")
        if not logs_cog:
            return
        try:
            await logs_cog.log_action(action=action, description=description, user=user,
                                      target_user=target, details=details or {})
        except Exception as e:
            logger.error(f"Shop audit log failed: {e}", exc_info=True)

    def _order_embed(self, order: dict, buyer: Optional[discord.abc.User] = None) -> discord.Embed:
        embed = create_embed(title=f"Shop Order #{order['id']}", user=None,
                             thumbnail=False)
        embed.add_field(name="Buyer", value=f"<@{order['discord_id']}>", inline=True)
        embed.add_field(name="Item", value=f"{order['quantity']:,} x {order['item_name']}", inline=True)
        embed.add_field(name="Category", value=order["category"], inline=True)
        embed.add_field(name="Unit price", value=f"{fmt(order['unit_price'])} \u00b7 {order['unit'] or 'per one'}", inline=True)
        embed.add_field(name="Total paid", value=fmt(order["total"]), inline=True)
        embed.add_field(name="Status", value=STATUS_TEXT[order["status"]], inline=True)
        if order["role_id"]:
            when = f"{order['duration_days']} days" if order["duration_days"] else "permanent"
            embed.add_field(name="Role", value=f"<@&{order['role_id']}> ({when}). Given automatically when you confirm.",
                            inline=False)
            if order["expires_at"] and order["status"] == "fulfilled":
                embed.add_field(name="Role expires", value=f"<t:{_ts(order['expires_at'])}:R>", inline=True)
        else:
            if order["status"] == "pending":
                embed.add_field(name="To do", value="Hand the items over in game, then press **Confirm**.", inline=False)
        if order["handled_by"]:
            embed.add_field(name="Handled by", value=f"<@{order['handled_by']}>", inline=True)
        if order["note"]:
            embed.add_field(name="Note", value=order["note"], inline=False)
        return embed

    async def _post_order(self, order: dict) -> Optional[discord.Message]:
        ids = (await asyncio.to_thread(get_channel_ids, SHOP_CONFIG["order_channel_key"]))
        if not ids:
            return None
        channel = self.bot.get_channel(ids[0])
        if channel is None:
            return None
        try:
            msg = await channel.send(embed=self._order_embed(order), view=_order_view(order["id"]))
            (await asyncio.to_thread(sdb.set_order_message, order["id"], channel.id, msg.id))
            return msg
        except discord.HTTPException as e:
            logger.error(f"Could not post shop order #{order['id']}: {e}", exc_info=True)
            return None

    async def _refresh_message(self, order: dict) -> None:
        if not order.get("channel_id") or not order.get("message_id"):
            return
        channel = self.bot.get_channel(int(order["channel_id"]))
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(order["message_id"]))
            await msg.edit(embed=self._order_embed(order), view=None)
        except discord.HTTPException:
            pass

    async def _dm(self, user_id: int, text: str) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(text)
        except Exception:
            pass

    async def decide(self, interaction: Interaction, order_id: int, confirm: bool, note: Optional[str] = None) -> None:
        if not (await asyncio.to_thread(_staff_can, self.bot, interaction.user, interaction.guild)):
            await interaction.response.send_message(**plain("Only HQ with the `manage_shop` permission can do this."),
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        lock = self._locks.setdefault(order_id, asyncio.Lock())
        async with lock:
            try:
                order = (await asyncio.to_thread(sdb.get_order, order_id))
                if not order:
                    await interaction.followup.send(**plain("That order does not exist."), ephemeral=True)
                    return
                if order["status"] != "pending":
                    await interaction.followup.send(**plain(f"Order #{order_id} was already {order['status']}."),
                                                    ephemeral=True)
                    await self._refresh_message(order)
                    return

                if confirm and order["role_id"]:
                    guild = interaction.guild
                    role = guild.get_role(int(order["role_id"])) if guild else None
                    member = None
                    if guild:
                        member = guild.get_member(int(order["discord_id"]))
                        if member is None:
                            try:
                                member = await guild.fetch_member(int(order["discord_id"]))
                            except discord.HTTPException:
                                member = None
                    if role is None or member is None:
                        await interaction.followup.send(**plain(
                            "I can't give the role: the role was deleted or the buyer left the server. "
                            "Reject the order to refund them."), ephemeral=True)
                        return
                    try:
                        await member.add_roles(role, reason=f"Shop order #{order_id} confirmed by {interaction.user}")
                    except discord.HTTPException as e:
                        logger.error(f"Shop role grant failed for order #{order_id}: {e}", exc_info=True)
                        await interaction.followup.send(**plain(
                            "I couldn't give the role (check my permissions and that my role is above it). "
                            "The order is still pending."), ephemeral=True)
                        return

                order = (await asyncio.to_thread(sdb.resolve_order, order_id, confirm, interaction.user.id, note))
            except (sdb.ShopError, cdb.CasinoError) as e:
                await interaction.followup.send(**plain(str(e)), ephemeral=True)
                return
            except Exception as e:
                logger.error(f"Shop decision error on order #{order_id}: {e}", exc_info=True)
                await interaction.followup.send(**plain("A database error occurred. Please contact HQ technical staff."),
                                                ephemeral=True)
                return
        self._locks.pop(order_id, None)

        await self._refresh_message(order)
        buyer = self.bot.get_user(int(order["discord_id"]))
        details = {"Order": f"#{order_id}", "Item": f"{order['quantity']:,} x {order['item_name']}",
                   "Total": f"{order['total']:,}"}
        if note:
            details["Reason"] = note
        if confirm:
            await self._audit("Shop Order Confirmed", f"Order #{order_id} was confirmed", interaction.user, buyer, details)
            await self._dm(int(order["discord_id"]),
                           f"Your shop order #{order_id} ({order['quantity']:,} x {order['item_name']}) was **confirmed**.")
            await interaction.followup.send(**plain(f"Order #{order_id} confirmed."), ephemeral=True)
        else:
            await self._audit("Shop Order Rejected", f"Order #{order_id} was rejected and refunded", interaction.user, buyer, details)
            await self._dm(int(order["discord_id"]),
                           f"Your shop order #{order_id} ({order['quantity']:,} x {order['item_name']}) was **rejected** and "
                           f"{order['total']:,} credits were refunded." + (f"\nReason: {note}" if note else ""))
            await interaction.followup.send(**plain(f"Order #{order_id} rejected, {order['total']:,} credits refunded."),
                                            ephemeral=True)

    @tasks.loop(minutes=10)
    async def expire_roles(self) -> None:
        try:
            due = (await asyncio.to_thread(sdb.due_role_expiries))
        except Exception as e:
            logger.error(f"Shop expiry query failed: {e}", exc_info=True)
            return
        for order in due:
            try:
                role_found = False
                removed_from = None
                for guild in self.bot.guilds:
                    role = guild.get_role(int(order["role_id"]))
                    if role is None:
                        continue
                    role_found = True
                    member = guild.get_member(int(order["discord_id"]))
                    if member and role in member.roles:
                        await member.remove_roles(role, reason=f"Shop order #{order['id']} expired")
                        removed_from = member
                    break
                (await asyncio.to_thread(sdb.mark_role_removed, order["id"]))
                if role_found and removed_from:
                    await self._audit("Shop Role Expired", f"{get_user_display_name(removed_from)} lost a shop role",
                                      self.bot.user, removed_from,
                                      {"Order": f"#{order['id']}", "Item": order["item_name"]})
                    await self._dm(removed_from.id, f"Your **{order['item_name']}** role has expired.")
            except discord.HTTPException as e:
                logger.warning(f"Could not remove expired shop role for order #{order['id']}: {e}")
            except Exception as e:
                logger.error(f"Shop expiry error on order #{order['id']}: {e}", exc_info=True)

    @expire_roles.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    async def item_autocomplete(self, interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        try:
            items = (await asyncio.to_thread(sdb.list_items))
        except Exception:
            return []
        cur = current.lower()
        out = []
        for it in items:
            if cur and cur not in it["name"].lower():
                continue
            label = f"{it['name']} - {it['price']:,} {it['unit']}"
            perks = _perks(it)
            if perks:
                tail = " | " + ", ".join(perks)
                if len(tail) > 70:
                    tail = tail[:67] + "..."
                label = label[:max(0, 100 - len(tail))] + tail
            out.append(app_commands.Choice(name=label[:100], value=it["name"]))
            if len(out) >= 25:
                break
        return out

    async def category_autocomplete(self, interaction: Interaction, current: str) -> List[app_commands.Choice[str]]:
        try:
            cats = (await asyncio.to_thread(sdb.categories))
        except Exception:
            return []
        cur = current.lower()
        return [app_commands.Choice(name=c[:100], value=c[:100]) for c in cats if cur in c.lower()][:25]

    shop = app_commands.Group(name="shop", description="Buy weapons, armours, roles and more with your credits")

    @shop.command(name="list", description="See what is for sale")
    @app_commands.describe(category="Only show one category")
    @app_commands.autocomplete(category=category_autocomplete)
    async def shop_list(self, interaction: Interaction, category: Optional[str] = None):
        items = (await asyncio.to_thread(sdb.list_items, category))
        if not items:
            msg = f"There is nothing in **{category}** right now." if category else "The shop is empty right now."
            await interaction.response.send_message(**plain(msg), ephemeral=True)
            return
        embed = create_embed(title="Shop", user=None, thumbnail=False)
        embed.description = "Buy with `/shop buy item quantity`. Roles are yours instantly. For everything else HQ confirms the order, and if they reject it you get your credits back."
        by_cat = {}
        for it in items:
            by_cat.setdefault(it["category"], []).append(_item_line(it, interaction.guild))

        used, truncated = len(embed.description), False
        for cat, lines in by_cat.items():
            chunks, cur = [], ""
            for line in lines:
                if len(cur) + len(line) + 2 > 1000:
                    chunks.append(cur)
                    cur = ""
                cur += ("\n\n" if cur else "") + line
            if cur:
                chunks.append(cur)
            for i, chunk in enumerate(chunks):
                if len(embed.fields) >= 24 or used + len(chunk) > 5200:
                    truncated = True
                    break
                embed.add_field(name=cat if i == 0 else f"{cat} (continued)", value=chunk, inline=False)
                used += len(chunk) + len(cat)
            if truncated:
                break
        if truncated:
            embed.set_footer(text="Too many items to show at once. Use the category option to filter.")
        await interaction.response.send_message(embed=embed)

    @shop.command(name="orders", description="Your recent shop orders")
    async def shop_orders(self, interaction: Interaction):
        orders = (await asyncio.to_thread(sdb.user_orders, interaction.user.id, 10))
        if not orders:
            await interaction.response.send_message(**plain("You haven't bought anything yet. Try `/shop list`."),
                                                    ephemeral=True)
            return
        lines = []
        for o in orders:
            label = _role_label(o.get("role_id"), o["item_name"], interaction.guild)
            line = f"`#{o['id']}` {o['quantity']:,} x {label} - {fmt(o['total'])} - {STATUS_TEXT[o['status']]}"
            if o["role_id"] and o["status"] == "fulfilled" and o["expires_at"] and not o["role_removed"]:
                line += f" (ends <t:{_ts(o['expires_at'])}:R>)"
            if o["note"] and o["status"] == "rejected":
                line += f"\n> {o['note']}"
            lines.append(line)
        embed = create_embed(title="Your shop orders", user=None, thumbnail=False)
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @shop.command(name="buy", description="Buy an item from the shop")
    @app_commands.describe(item="What to buy (start typing to search)", quantity="How many (per bullet, per one...). Roles are always 1")
    @app_commands.autocomplete(item=item_autocomplete)
    async def shop_buy(self, interaction: Interaction, item: str, quantity: app_commands.Range[int, 1, 1_000_000] = 1):
        found = (await asyncio.to_thread(sdb.find_item, item))
        if not found:
            await interaction.response.send_message(
                **plain(f"I couldn't find **{item}** in the shop. Use `/shop list` to see what is for sale."),
                ephemeral=True)
            return
        role = None
        if found["role_id"]:
            role = interaction.guild.get_role(int(found["role_id"])) if interaction.guild else None
            if role is None:
                await interaction.response.send_message(**plain(
                    "This role is not available right now (it was deleted or you are not in the server). Tell HQ."),
                    ephemeral=True)
                return
            err = _clean_role(role, interaction.guild)
            if err:
                await interaction.response.send_message(**plain(
                    "This role can't be given right now. Tell HQ so they can fix it."), ephemeral=True)
                logger.warning(f"Shop role item {found['name']!r} can't be given: {err}")
                return
            if role in getattr(interaction.user, "roles", []):
                await interaction.response.send_message(**plain(f"You already have {role.mention}."), ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        try:
            order, balance = (await asyncio.to_thread(sdb.place_order, interaction.user.id, found["id"], quantity))
        except (sdb.ShopError, cdb.CasinoError) as e:
            await interaction.followup.send(**plain(str(e)), ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Shop purchase error: {e}", exc_info=True)
            await interaction.followup.send(**plain("Something went wrong and you were not charged. Please contact HQ technical staff."),
                                            ephemeral=True)
            return

        if role is not None:
            await self._instant_role(interaction, order, role, balance, _perks(found))
            return

        msg = await self._post_order(order)
        await self._audit("Shop Purchase", f"{get_user_display_name(interaction.user)} placed order #{order['id']}",
                          interaction.user, None,
                          {"Order": f"#{order['id']}", "Item": f"{quantity:,} x {order['item_name']}",
                           "Total": f"{order['total']:,}", "Balance": f"{balance:,}"})
        text = (f"Order **#{order['id']}** placed: {quantity:,} x **{order['item_name']}** for {fmt(order['total'])}.\n"
                f"Wallet: {fmt(balance)}. HQ will confirm it soon. If they reject it you get your credits back. "
                f"Track it with `/shop orders`.")
        if msg is None:
            text += "\n*(HQ has not set an orders channel yet, so they will find it in `/shop manage action:orders`.)*"
        await interaction.followup.send(**plain(text), ephemeral=True)

    async def _instant_role(self, interaction: Interaction, order: dict, role: discord.Role, balance: int,
                            perks: Optional[List[str]] = None) -> None:
        bot_id = self.bot.user.id
        try:
            await interaction.user.add_roles(role, reason=f"Shop order #{order['id']} (instant role purchase)")
        except discord.HTTPException as e:
            logger.error(f"Instant role grant failed for order #{order['id']}: {e}", exc_info=True)
            try:
                (await asyncio.to_thread(sdb.resolve_order, order["id"], False, bot_id, "Could not give the role, refunded automatically"))
                balance += order["total"]
            except Exception as e2:
                logger.error(f"Refund after failed role grant failed for order #{order['id']}: {e2}", exc_info=True)
            await self._audit("Shop Role Failed", f"Could not give {order['item_name']} to {get_user_display_name(interaction.user)}",
                              interaction.user, None, {"Order": f"#{order['id']}", "Refunded": f"{order['total']:,}"})
            await interaction.followup.send(**plain(
                f"I couldn't give you the role, so you were refunded {fmt(order['total'])}. Tell HQ so they can check my permissions."), ephemeral=True)
            return

        try:
            done = (await asyncio.to_thread(sdb.resolve_order, order["id"], True, bot_id, "Instant role purchase"))
        except Exception as e:
            logger.error(f"Could not mark order #{order['id']} fulfilled after giving the role: {e}", exc_info=True)
            done = order
        details = {"Order": f"#{order['id']}", "Role": role.name, "Total": f"{order['total']:,}", "Balance": f"{balance:,}"}
        if done.get("expires_at"):
            details["Expires"] = done["expires_at"].replace("T", " ").replace("Z", " UTC")
        await self._audit("Shop Role Purchased", f"{get_user_display_name(interaction.user)} bought {order['item_name']}",
                          interaction.user, None, details)
        text = f"You bought {role.mention} for {fmt(order['total'])}. It is yours now!\nWallet: {fmt(balance)}."
        if perks:
            text += "\nPerks: " + " \u00b7 ".join(perks) + "."
        if done.get("expires_at"):
            text += f"\nIt ends <t:{_ts(done['expires_at'])}:R>."
        await interaction.followup.send(**plain(text), ephemeral=True)

    @shop.command(name="manage", description="Manage the shop (HQ only)")
    @require_permissions(admin=True, acl=PERMISSION)
    @app_commands.describe(
        action="What do you want to do?",
        item="add: the new item's name. edit / restock / remove: the item",
        new_name="edit: new name",
        price="add / edit: price per unit: 5000, 10k, 1.5m",
        category="add / edit: Weapons, Armours, Roles... (a new name creates a new section)",
        unit="add / edit: shown after the price: per bullet, per one... (roles: filled in from the days)",
        description="add / edit: short text shown in the shop. Role items: list extra perks separated by ; (edit: send - to clear)",
        stock="add: how many are available (empty = unlimited). edit: exact stock (-1 = unlimited)",
        amount="restock: how many to add",
        role="add / edit: make it a role item, HQ confirm gives this role",
        days="Role items: remove the role after this many days (add: empty = permanent, edit: 0 = permanent)",
        win_bonus="Role items: % extra on casino & trivia winnings while the member has the role (0 = none)",
        daily_bonus="Role items: % extra on /dailyreward while the member has the role (0 = none)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add an item",                value="add"),
        app_commands.Choice(name="Edit an item",               value="edit"),
        app_commands.Choice(name="Restock an item",            value="restock"),
        app_commands.Choice(name="Remove an item",             value="remove"),
        app_commands.Choice(name="Orders waiting for HQ",      value="orders"),
    ])
    @app_commands.autocomplete(item=item_autocomplete, category=category_autocomplete)
    async def shop_manage(self, interaction: Interaction, action: str, item: Optional[str] = None,
                          new_name: Optional[str] = None, price: Optional[str] = None,
                          category: Optional[str] = None, unit: Optional[str] = None,
                          description: Optional[str] = None, stock: Optional[app_commands.Range[int, -1]] = None,
                          amount: Optional[app_commands.Range[int, 1, 100_000_000]] = None,
                          role: Optional[discord.Role] = None,
                          days: Optional[app_commands.Range[int, 0, 3650]] = None,
                          win_bonus: Optional[app_commands.Range[int, 0, 100]] = None,
                          daily_bonus: Optional[app_commands.Range[int, 0, 500]] = None):
        needs = MANAGE_NEEDS.get(action)
        if needs is None:
            await interaction.response.send_message(**plain("Pick an action."), ephemeral=True)
            return
        given = {"item": item, "price": price, "amount": amount}
        missing = [k for k in needs if given[k] is None]
        if missing:
            await interaction.response.send_message(
                **plain(f"`{action}` needs: " + ", ".join(f"`{m}`" for m in missing) + "."), ephemeral=True)
            return

        if action == "add":
            await self._manage_add(interaction, item, price, category, unit, description,
                                   None if stock in (None, -1) else stock, role, days or None, win_bonus, daily_bonus)
        elif action == "edit":
            await self._manage_edit(interaction, item, new_name, price, category, unit, description, stock,
                                    role, days, win_bonus, daily_bonus)
        elif action == "restock":
            await self._manage_restock(interaction, item, amount)
        elif action == "remove":
            await self._manage_remove(interaction, item)
        else:
            await self._manage_orders(interaction)

    async def _manage_add(self, interaction: Interaction, name: str, price: str, category: Optional[str] = None,
                         unit: Optional[str] = None, description: Optional[str] = None,
                         stock: Optional[app_commands.Range[int, 0]] = None, role: Optional[discord.Role] = None,
                         days: Optional[app_commands.Range[int, 1, 3650]] = None,
                         win_bonus: Optional[app_commands.Range[int, 1, 100]] = None,
                         daily_bonus: Optional[app_commands.Range[int, 1, 500]] = None):
        try:
            value = parse_amount(price)
        except ValueError as e:
            await interaction.response.send_message(**plain(str(e)), ephemeral=True)
            return
        if (days or win_bonus or daily_bonus) and not role:
            await interaction.response.send_message(**plain("`days`, `win_bonus` and `daily_bonus` only work together with a `role`."), ephemeral=True)
            return
        if daily_bonus and daily_bonus > SHOP_CONFIG["max_daily_bonus"]:
            await interaction.response.send_message(**plain(f"The daily bonus can't be higher than {SHOP_CONFIG['max_daily_bonus']}%."), ephemeral=True)
            return
        if win_bonus and win_bonus > SHOP_CONFIG["max_win_bonus"]:
            await interaction.response.send_message(**plain(f"The win bonus can't be higher than {SHOP_CONFIG['max_win_bonus']}%."), ephemeral=True)
            return
        if role:
            err = _clean_role(role, interaction.guild)
            if err:
                await interaction.response.send_message(**plain(err), ephemeral=True)
                return
            stock = None
        category = (category or ("Roles" if role else "Other")).strip()[:40]
        unit = (unit or (_unit_for_role(days) if role else "per one")).strip()[:40]
        try:
            item_id = (await asyncio.to_thread(sdb.add_item, category, name.strip()[:60], value, unit, description, stock,
                                   role.id if role else None, days, win_bonus or 0, daily_bonus or 0))
        except sdb.ShopError as e:
            await interaction.response.send_message(**plain(str(e)), ephemeral=True)
            return
        await self._audit("Shop Item Added", f"{name} was added to the shop", interaction.user, None,
                          {"Item": name, "Category": category, "Price": f"{value:,} {unit}",
                           "Stock": "unlimited" if stock is None else f"{stock:,}"})
        await interaction.response.send_message(**plain(
            f"Added **{name}** to **{category}**: {fmt(value)} \u00b7 {unit}" +
            ("" if stock is None else f" \u00b7 {stock:,} in stock") +
            (f" \u00b7 +{win_bonus}% casino & trivia winnings" if win_bonus else "") +
            (f" \u00b7 +{daily_bonus}% daily reward" if daily_bonus else "") + f" (item #{item_id})."))

    async def _manage_edit(self, interaction: Interaction, item: str, new_name: Optional[str] = None,
                          price: Optional[str] = None, category: Optional[str] = None, unit: Optional[str] = None,
                          description: Optional[str] = None, stock: Optional[app_commands.Range[int, -1]] = None,
                          role: Optional[discord.Role] = None, days: Optional[app_commands.Range[int, 0, 3650]] = None,
                          win_bonus: Optional[app_commands.Range[int, 0, 100]] = None,
                          daily_bonus: Optional[app_commands.Range[int, 0, 500]] = None):
        found = (await asyncio.to_thread(sdb.find_item, item))
        if not found:
            await interaction.response.send_message(**plain(f"I couldn't find **{item}**."), ephemeral=True)
            return
        fields = {}
        if new_name:
            fields["name"] = new_name.strip()[:60]
        if price:
            try:
                fields["price"] = parse_amount(price)
            except ValueError as e:
                await interaction.response.send_message(**plain(str(e)), ephemeral=True)
                return
        if category:
            fields["category"] = category.strip()[:40]
        if description is not None:
            fields["description"] = None if description.strip() == "-" else description
        if stock is not None:
            fields["stock"] = None if stock == -1 else stock
        if role:
            err = _clean_role(role, interaction.guild)
            if err:
                await interaction.response.send_message(**plain(err), ephemeral=True)
                return
            fields["role_id"] = str(role.id)
        is_role = bool(fields.get("role_id") or found["role_id"])
        if days is not None:
            if not is_role:
                await interaction.response.send_message(**plain("`days` only works for role items."), ephemeral=True)
                return
            fields["duration_days"] = days or None
            if unit is None:
                fields["unit"] = _unit_for_role(days or None)
        if win_bonus is not None:
            if not is_role:
                await interaction.response.send_message(**plain("`win_bonus` only works for role items."), ephemeral=True)
                return
            fields["win_bonus"] = win_bonus
        if daily_bonus is not None:
            if not is_role:
                await interaction.response.send_message(**plain("`daily_bonus` only works for role items."), ephemeral=True)
                return
            fields["daily_bonus"] = daily_bonus
        if unit:
            fields["unit"] = unit.strip()[:40]
        if not fields:
            await interaction.response.send_message(**plain("Nothing to change. Fill in at least one option."), ephemeral=True)
            return
        try:
            (await asyncio.to_thread(sdb.update_item, found["id"], **fields))
        except sdb.ShopError as e:
            await interaction.response.send_message(**plain(str(e)), ephemeral=True)
            return
        shown = {k: ("unlimited" if v is None and k == "stock" else v) for k, v in fields.items()}
        await self._audit("Shop Item Edited", f"{found['name']} was edited", interaction.user, None,
                          {"Item": found["name"], **{k.replace("_", " ").title(): str(v) for k, v in shown.items()}})
        await interaction.response.send_message(**plain(f"Updated **{found['name']}**."))

    async def _manage_restock(self, interaction: Interaction, item: str, amount: app_commands.Range[int, 1, 100_000_000]):
        found = (await asyncio.to_thread(sdb.find_item, item))
        if not found:
            await interaction.response.send_message(**plain(f"I couldn't find **{item}**."), ephemeral=True)
            return
        try:
            new = (await asyncio.to_thread(sdb.add_stock, found["id"], amount))
        except sdb.ShopError as e:
            await interaction.response.send_message(**plain(str(e)), ephemeral=True)
            return
        if new is None:
            await interaction.response.send_message(**plain(
                f"**{found['name']}** has unlimited stock. Use `/shop manage` with action `edit` and `stock` to give it a limit."),
                ephemeral=True)
            return
        await self._audit("Shop Restocked", f"{found['name']} was restocked", interaction.user, None,
                          {"Item": found["name"], "Added": f"{amount:,}", "Stock": f"{new:,}"})
        await interaction.response.send_message(**plain(f"**{found['name']}** restocked: now {new:,} in stock."))

    async def _manage_remove(self, interaction: Interaction, item: str):
        found = (await asyncio.to_thread(sdb.find_item, item))
        if not found:
            await interaction.response.send_message(**plain(f"I couldn't find **{item}**."), ephemeral=True)
            return
        (await asyncio.to_thread(sdb.delete_item, found["id"]))
        await self._audit("Shop Item Removed", f"{found['name']} was removed from the shop", interaction.user, None,
                          {"Item": found["name"], "Category": found["category"]})
        await interaction.response.send_message(**plain(f"Removed **{found['name']}** from the shop."))

    async def _manage_orders(self, interaction: Interaction):
        orders = (await asyncio.to_thread(sdb.pending_orders, 20))
        if not orders:
            await interaction.response.send_message(**plain("No orders are waiting. All clear."), ephemeral=True)
            return
        lines = []
        for o in orders:
            where = ""
            if o["channel_id"] and o["message_id"] and interaction.guild:
                where = f" [open](https://discord.com/channels/{interaction.guild.id}/{o['channel_id']}/{o['message_id']})"
            label = _role_label(o.get("role_id"), o["item_name"], interaction.guild)
            lines.append(f"`#{o['id']}` <@{o['discord_id']}> - {o['quantity']:,} x {label} - {fmt(o['total'])}{where}")
        embed = create_embed(title="Pending shop orders", user=None, thumbnail=False)
        embed.description = "\n".join(lines)
        if not (await asyncio.to_thread(get_channel_ids, SHOP_CONFIG["order_channel_key"])):
            embed.set_footer(text="No orders channel is set. Run /setup to pick the orders channel so orders show up with buttons.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
