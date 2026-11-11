import asyncio
import logging
import re
from typing import Optional, List, Tuple

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from database.db import get_db_connection
from utils.helpers import create_embed, invalidate_admin_cache, is_admin_member, plain
from utils.settings import DEVELOPER_ID

logger = logging.getLogger(__name__)


def _parse_csv_ids(s: Optional[str]) -> List[int]:
    if not s:
        return []
    return [int(x.strip()) for x in s.split(',') if x.strip()]


_SEP_RE = re.compile(r"[,\s;|]+")
_TARGET_RE = re.compile(r"<@&(\d+)>|<@!?(\d+)>|(\d{15,22})")


def _acl_text(perms) -> str:
    return ", ".join(f"`{p}`" for p in sorted(perms)) or "no permissions"


def _join_csv_ids(ids: List[int]) -> str:
    return ','.join(str(i) for i in ids)


class PermissionPicker(discord.ui.View):
    def __init__(self, cog: "Admin", origin: Interaction, action: str, targets: str, descriptions: dict) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.origin = origin
        self.action = action
        self.targets = targets
        self.chosen: List[str] = []
        self.message: Optional[discord.Message] = None
        options = [discord.SelectOption(label=perm, value=perm, description=desc[:100])
                   for perm, desc in descriptions.items()]
        options.append(discord.SelectOption(label="all", value="all", description="Every permission"))
        self.select = discord.ui.Select(placeholder="Pick permissions...", min_values=0,
                                        max_values=len(options), options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.origin.user.id:
            await interaction.response.send_message(**plain("This menu is not yours."), ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: Interaction) -> None:
        self.chosen = list(self.select.values)
        for option in self.select.options:
            option.default = option.value in self.chosen
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        perms = list(self.cog.VALID_PERMISSIONS) if "all" in self.chosen else list(self.chosen)
        if self.action in ("add_acl", "remove_acl") and not perms:
            await interaction.response.send_message(**plain("Pick at least one permission first."), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(**plain("Applying..."), view=None)
        await self.cog._run_manage(self.origin, self.action, self.targets, perms)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(**plain("Cancelled."), view=None)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(**plain("This menu timed out. Run the command again."), view=None)
            except discord.HTTPException:
                pass


class Admin(commands.Cog):
    VALID_PERMISSIONS = {
        "manage_tickets":    "Close tickets and manage ticket operations",
        "manage_promotions": "Approve/deny promotions and manage promotion boosts",
        "view_activity":     "Run activity analysis and checks",
        "view_analytics":    "View analytics, statistics, and reports",
        "manage_members":    "Add/update/remove members and manage roster",
        "manage_points":     "Manually adjust member points",
        "manage_casino":     "Add/remove/set members' casino credits",
        "manage_shop":       "Manage shop items and confirm/reject shop orders",
        "manage_reports":    "Approve/decline extra points, edit/delete operation reports",
        "manage_recruitment": "Vote on applications and close recruitment waves",
        "manage_admins":     "Add/remove admins and modify ACL permissions"
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _msg(self, title: str, description: str = None) -> dict:
        return plain(description or title)

    def _is_owner(self, user: discord.User) -> bool:
        return user.id == DEVELOPER_ID

    def _get_admin_roles(self) -> List[int]:
        try:
            with get_db_connection() as conn:
                row = conn.execute("SELECT value FROM config WHERE key = 'admin_roles'").fetchone()
                return _parse_csv_ids(row['value']) if row else []
        except Exception:
            return []

    def _get_admin_members(self) -> List[int]:
        try:
            with get_db_connection() as conn:
                row = conn.execute("SELECT value FROM config WHERE key = 'admin_members'").fetchone()
                return _parse_csv_ids(row['value']) if row else []
        except Exception:
            return []

    def _save_admin_roles(self, roles: List[int]) -> None:
        with get_db_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))", ("admin_roles", _join_csv_ids(roles)))
            conn.commit()
        invalidate_admin_cache()

    def _save_admin_members(self, members: List[int]) -> None:
        with get_db_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))", ("admin_members", _join_csv_ids(members)))
            conn.commit()
        invalidate_admin_cache()

    def _get_acl(self, key: str) -> List[str]:
        try:
            with get_db_connection() as conn:
                row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
                return [p.strip() for p in row['value'].split(',') if p.strip()] if row and row['value'] else []
        except Exception:
            return []

    def _save_acl(self, key: str, acl: List[str]) -> None:
        with get_db_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, datetime('now'))", (key, ','.join(acl) if acl else ""))
            conn.commit()

    def _delete_acl(self, key: str) -> None:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM config WHERE key = ?", (key,))
            conn.commit()

    def _get_admin_acl(self, user_id: int) -> List[str]:
        return self._get_acl(f"admin_acl_{user_id}")

    def _save_admin_acl(self, user_id: int, acl: List[str]) -> None:
        self._save_acl(f"admin_acl_{user_id}", acl)

    def _delete_admin_acl(self, user_id: int) -> None:
        self._delete_acl(f"admin_acl_{user_id}")

    def _has_acl_permission(self, user_id: int, permission: str) -> bool:
        return permission in self._get_admin_acl(user_id)

    def _get_role_acl(self, role_id: int) -> List[str]:
        return self._get_acl(f"admin_acl_role_{role_id}")

    def _save_role_acl(self, role_id: int, acl: List[str]) -> None:
        self._save_acl(f"admin_acl_role_{role_id}", acl)

    def _has_role_acl_permission(self, role_id: int, permission: str) -> bool:
        return permission in self._get_role_acl(role_id)

    def _get_all_users_with_acl(self) -> List[int]:
        try:
            with get_db_connection() as conn:
                rows = conn.execute("SELECT key FROM config WHERE key LIKE 'admin_acl_%' AND key NOT LIKE 'admin_acl_role_%'").fetchall()
                user_ids = []
                for row in rows:
                    try:
                        user_ids.append(int(row['key'].replace('admin_acl_', '')))
                    except ValueError:
                        continue
                return user_ids
        except Exception as e:
            logger.error(f"Error getting users with ACL: {e}", exc_info=True)
            return []

    admin = app_commands.Group(name="admin", description="Admin management commands")

    async def _can_manage_admins(self, interaction: Interaction) -> bool:
        if self._is_owner(interaction.user):
            return True
        if not interaction.guild:
            await interaction.response.send_message(**plain("This command can only be used in a server."), ephemeral=True)
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if member and (await asyncio.to_thread(is_admin_member, member)):
            return True
        await interaction.response.send_message(**plain("You do not have permission to manage admins."), ephemeral=True)
        return False

    def _get_all_roles_with_acl(self) -> List[int]:
        try:
            with get_db_connection() as conn:
                rows = conn.execute("SELECT key FROM config WHERE key LIKE 'admin_acl_role_%'").fetchall()
            ids = []
            for row in rows:
                try:
                    ids.append(int(row['key'].replace('admin_acl_role_', '')))
                except ValueError:
                    continue
            return ids
        except Exception as e:
            logger.error(f"Error getting roles with ACL: {e}", exc_info=True)
            return []

    async def _member_in_guild(self, guild: discord.Guild, user_id: int) -> bool:
        if guild.get_member(user_id):
            return True
        try:
            await guild.fetch_member(user_id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True

    def _resolve_targets(self, guild: discord.Guild, text: str, roles: List[int], members: List[int], removing: bool = False) -> List[Tuple[str, int]]:
        found: List[Tuple[str, int]] = []
        for match in _TARGET_RE.finditer(text or ""):
            role_id, user_id, raw = match.groups()
            if role_id:
                item = ("role", int(role_id))
            elif user_id:
                item = ("user", int(user_id))
            else:
                _id = int(raw)
                if removing and _id in roles:
                    item = ("role", _id)
                elif removing and (_id in members or self._get_admin_acl(_id)):
                    item = ("user", _id)
                else:
                    item = ("role", _id) if guild.get_role(_id) else ("user", _id)
            if item not in found:
                found.append(item)
        return found

    def _label(self, guild: discord.Guild, kind: str, _id: int) -> str:
        if kind == "role":
            role = guild.get_role(_id)
            return role.mention if role else f"deleted role `{_id}`"
        return f"<@{_id}>" if guild.get_member(_id) else f"<@{_id}> (not in the server)"

    def _parse_acl(self, text: Optional[str]) -> Tuple[List[str], List[str]]:
        perms: List[str] = []
        invalid: List[str] = []
        for token in (t.lower() for t in _SEP_RE.split(text or "") if t):
            if token == "all":
                return list(self.VALID_PERMISSIONS), []
            bucket = perms if token in self.VALID_PERMISSIONS else invalid
            if token not in bucket:
                bucket.append(token)
        return perms, invalid

    async def _log(self, interaction: Interaction, action: str, description: str, perms: Optional[List[str]] = None) -> None:
        logs_cog = self.bot.get_cog("Logs")
        if logs_cog:
            details = {"Permissions": ", ".join(perms)} if perms else None
            await logs_cog.log_action(action=action, description=description[:1000], user=interaction.user, details=details)

    def _forget_member(self, user_id: int) -> bool:
        members = self._get_admin_members()
        had = user_id in members or bool(self._get_admin_acl(user_id))
        if user_id in members:
            self._save_admin_members([m for m in members if m != user_id])
        self._delete_admin_acl(user_id)
        return had

    def _forget_role(self, role_id: int) -> bool:
        roles = self._get_admin_roles()
        had = role_id in roles or bool(self._get_role_acl(role_id))
        if role_id in roles:
            self._save_admin_roles([r for r in roles if r != role_id])
        self._delete_acl(f"admin_acl_role_{role_id}")
        return had

    async def acl_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        parts = re.split(r"([,\s;|]+)", current)
        prefix, last = "".join(parts[:-1]), parts[-1].lower()
        used = {t.lower() for t in _SEP_RE.split(prefix) if t}
        descriptions = {**self.VALID_PERMISSIONS, "all": "Every permission"}

        matching = [o for o in descriptions if o not in used and o.startswith(last)]
        choices, too_long = [], []
        for option in matching:
            value = prefix + option
            if len(value) > 100:
                too_long.append(option)
                continue
            label = f"{option} - {descriptions[option]}"
            if len(label) > 100:
                label = label[:99] + "\u2026"
            choices.append(app_commands.Choice(name=label, value=value))

        if too_long and len(current) <= 100:
            hint = "Too long to suggest, type these yourself: " + ", ".join(too_long)
            choices = choices[:24] + [app_commands.Choice(name=hint[:100], value=current)]
        return choices[:25]

    @admin.command(name="manage", description="Manage bot administrators (HQ/owner only)")
    @app_commands.describe(
        action="What do you want to do?",
        targets="Members and/or roles. Mention them or paste IDs, separated by spaces or commas",
        acl="Permissions, separated by spaces or commas, or 'all'. Leave empty to pick from a menu"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Add admins (and give permissions)",  value="add"),
        app_commands.Choice(name="Remove admins (and their permissions)", value="remove"),
        app_commands.Choice(name="Give permissions",                    value="add_acl"),
        app_commands.Choice(name="Take away permissions",               value="remove_acl"),
        app_commands.Choice(name="List all admins",                     value="list"),
        app_commands.Choice(name="Clean up (left members, deleted roles)", value="cleanup"),
    ])
    @app_commands.autocomplete(acl=acl_autocomplete)
    async def admin_manage(self, interaction: Interaction, action: str, targets: Optional[str] = None, acl: Optional[str] = None):
        if not await self._can_manage_admins(interaction):
            return

        if action in ("add", "add_acl", "remove_acl") and targets and not acl and interaction.guild:
            verb = "take away" if action == "remove_acl" else "give"
            view = PermissionPicker(self, interaction, action, targets, self.VALID_PERMISSIONS)
            await interaction.response.send_message(
                **plain(f"Pick the permissions to {verb}, then press Apply."), view=view, ephemeral=True)
            view.message = await interaction.original_response()
            return

        await interaction.response.defer(ephemeral=False)
        guild = interaction.guild
        if not guild:
            await interaction.followup.send(**plain("This command can only be used in a server."), ephemeral=True)
            return

        try:
            if action == "list":
                await self._list_admins(interaction)
                return
            if action == "cleanup":
                await self._cleanup(interaction)
                return

            if not targets:
                await interaction.followup.send(**plain("`targets` is required for this action. Mention members or roles, or paste their IDs."), ephemeral=True)
                return

            perms, invalid = self._parse_acl(acl)
            if invalid:
                valid_list = "\n".join(f"\u2022 `{perm}`: {desc}" for perm, desc in self.VALID_PERMISSIONS.items())
                await interaction.followup.send(**plain(f"Invalid permissions: {', '.join(invalid)}\n\n**Valid permissions:**\n{valid_list}\n\u2022 `all`: every permission"), ephemeral=True)
                return
            if action in ("add_acl", "remove_acl") and not perms:
                await interaction.followup.send(**plain("`acl` is required for this action."), ephemeral=True)
                return
        except Exception as e:
            logger.error(f"Admin manage error ({action}): {e}", exc_info=True)
            await interaction.followup.send(**plain(f"An error occurred: {e}"), ephemeral=True)
            return

        await self._run_manage(interaction, action, targets, perms)

    async def _run_manage(self, interaction: Interaction, action: str, targets: str, perms: List[str]) -> None:
        guild = interaction.guild
        try:
            roles = (await asyncio.to_thread(self._get_admin_roles))
            members = (await asyncio.to_thread(self._get_admin_members))
            found = (await asyncio.to_thread(self._resolve_targets, guild, targets, roles, members, removing=(action == "remove")))
            if not found:
                await interaction.followup.send(**plain("I couldn't find any members or roles in `targets`. Mention them or paste their IDs."), ephemeral=True)
                return

            done: List[str] = []
            skipped: List[str] = []

            for kind, _id in found:
                label = self._label(guild, kind, _id)
                is_role = kind == "role"
                current = (await asyncio.to_thread(self._get_role_acl, _id)) if is_role else (await asyncio.to_thread(self._get_admin_acl, _id))
                is_admin = (_id in roles) if is_role else (_id in members or bool(current))

                if action == "add":
                    exists = guild.get_role(_id) if is_role else await self._member_in_guild(guild, _id)
                    if not exists:
                        skipped.append(f"{label}: not found in this server")
                        continue
                    status = "already an admin"
                    if is_role and _id not in roles:
                        roles.append(_id)
                        status = "added"
                    elif not is_role and _id not in members:
                        members.append(_id)
                        status = "added"
                    if perms:
                        new_acl = sorted(set(current) | set(perms))
                        (self._save_role_acl if is_role else self._save_admin_acl)(_id, new_acl)
                        status += f", permissions: {_acl_text(new_acl)}"
                    done.append(f"\u2022 {label}: {status}")

                elif action == "remove":
                    if not is_admin:
                        skipped.append(f"{label}: not an admin")
                        continue
                    if is_role:
                        roles = [r for r in roles if r != _id]
                        (await asyncio.to_thread(self._delete_acl, f"admin_acl_role_{_id}"))
                    else:
                        members = [m for m in members if m != _id]
                        (await asyncio.to_thread(self._delete_admin_acl, _id))
                    done.append(f"\u2022 {label}: removed" + (f" (had: {_acl_text(current)})" if current else ""))

                else:
                    if not is_admin:
                        skipped.append(f"{label}: not an admin (use Add admins first)")
                        continue
                    new_acl = sorted(set(current) | set(perms)) if action == "add_acl" else [p for p in current if p not in perms]
                    (self._save_role_acl if is_role else self._save_admin_acl)(_id, new_acl)
                    done.append(f"\u2022 {label}: {_acl_text(new_acl)}")

            (await asyncio.to_thread(self._save_admin_roles, roles))
            (await asyncio.to_thread(self._save_admin_members, members))

            titles = {
                "add":        "Admins added",
                "remove":     "Admins removed",
                "add_acl":    "Permissions given",
                "remove_acl": "Permissions removed",
            }
            lines = []
            if done:
                lines.append(f"**{titles[action]}** ({len(done)})")
                lines.extend(done)
            if skipped:
                lines.append(f"**Skipped** ({len(skipped)})")
                lines.extend(f"\u2022 {x}" for x in skipped)
            await interaction.followup.send(**plain("\n".join(lines)[:1900]))

            if done:
                log_titles = {
                    "add":        "Admins Added",
                    "remove":     "Admins Removed",
                    "add_acl":    "Admin Permissions Added",
                    "remove_acl": "Admin Permissions Removed",
                }
                await self._log(interaction, log_titles[action], "\n".join(done), perms if action != "remove" else None)

        except Exception as e:
            logger.error(f"Admin manage error ({action}): {e}", exc_info=True)
            await interaction.followup.send(**plain(f"An error occurred: {e}"), ephemeral=True)


    async def _list_admins(self, interaction: Interaction) -> None:
        guild = interaction.guild
        roles = (await asyncio.to_thread(self._get_admin_roles))
        members = (await asyncio.to_thread(self._get_admin_members))
        all_acl_users = (await asyncio.to_thread(self._get_all_users_with_acl))
        embed = create_embed(title="Admins List")
        counter = 1

        def perms_text(perms: List[str]) -> str:
            return "\n".join(f"{i}. `{perm}`" for i, perm in enumerate(perms, 1)) if perms else "No permissions"

        for rid in roles:
            r = guild.get_role(rid)
            name = r.name if r else f"Deleted role ({rid})"
            embed.add_field(name=f"{counter}. {name}", value=perms_text((await asyncio.to_thread(self._get_role_acl, rid))), inline=False)
            counter += 1

        for mid in members:
            m = guild.get_member(mid)
            name = m.display_name if m else f"Left the server ({mid})"
            embed.add_field(name=f"{counter}. {name}", value=perms_text((await asyncio.to_thread(self._get_admin_acl, mid))), inline=False)
            counter += 1

        for uid in all_acl_users:
            if uid in members:
                continue
            acl_perms = (await asyncio.to_thread(self._get_admin_acl, uid))
            if acl_perms:
                m = guild.get_member(uid)
                name = m.display_name if m else f"Left the server ({uid})"
                embed.add_field(name=f"{counter}. {name} (permissions only)", value=perms_text(acl_perms), inline=False)
                counter += 1

        if counter == 1:
            embed.description = "No administrators configured. Use `/admin manage` with the Add admins action."
        await interaction.followup.send(embed=embed)

    async def _cleanup(self, interaction: Interaction) -> None:
        guild = interaction.guild
        roles = (await asyncio.to_thread(self._get_admin_roles))
        members = (await asyncio.to_thread(self._get_admin_members))

        gone_members = [m for m in dict.fromkeys(members + (await asyncio.to_thread(self._get_all_users_with_acl))) if not await self._member_in_guild(guild, m)]
        gone_roles = [r for r in dict.fromkeys(roles + (await asyncio.to_thread(self._get_all_roles_with_acl))) if not guild.get_role(r)]

        for uid in gone_members:
            (await asyncio.to_thread(self._forget_member, uid))
        for rid in gone_roles:
            (await asyncio.to_thread(self._forget_role, rid))

        if not gone_members and not gone_roles:
            await interaction.followup.send(**plain("Nothing to clean up. Every admin member and role still exists."))
            return

        lines = [f"**Cleaned up** ({len(gone_members) + len(gone_roles)})"]
        lines += [f"\u2022 <@{uid}> (not in the server)" for uid in gone_members]
        lines += [f"\u2022 deleted role `{rid}`" for rid in gone_roles]
        await interaction.followup.send(**plain("\n".join(lines)[:1900]))
        await self._log(interaction, "Admins Removed", "\n".join(lines[1:]))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            if member.id == DEVELOPER_ID:
                return
            if (await asyncio.to_thread(self._forget_member, member.id)):
                logger.info(f"Removed admin access of {member.id} because they left the server")
                logs_cog = self.bot.get_cog("Logs")
                if logs_cog:
                    await logs_cog.log_action(
                        action="Admins Removed",
                        description=f"<@{member.id}> left the server, so their admin access was removed",
                    )
        except Exception as e:
            logger.error(f"on_member_remove admin cleanup failed: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        try:
            if (await asyncio.to_thread(self._forget_role, role.id)):
                logger.info(f"Removed admin role {role.id} because the role was deleted")
                logs_cog = self.bot.get_cog("Logs")
                if logs_cog:
                    await logs_cog.log_action(
                        action="Admins Removed",
                        description=f"The role **{role.name}** was deleted, so it was removed from the admin roles",
                    )
        except Exception as e:
            logger.error(f"on_guild_role_delete admin cleanup failed: {e}", exc_info=True)

    @commands.hybrid_command(name="hqcommands", description="Show the HQ admin commands you have access to")
    async def hq_commands(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        is_owner = self._is_owner(ctx.author)
        is_admin = (await asyncio.to_thread(is_admin_member, ctx.author))
        if not (is_owner or is_admin):
            await ctx.send(**self._msg("Access Denied", "You do not have permission to view HQ commands."))
            return
        try:
            description = "All available administrator commands for managing the bot." if is_owner else "Commands you have access to:"
            embed = create_embed(title="HQ Admin Commands", description=description)

            def has_permission(permission: str) -> bool:
                if is_owner:
                    return True
                if is_admin and self._has_acl_permission(ctx.author.id, permission):
                    return True
                for role_id in self._get_admin_roles():
                    role = ctx.guild.get_role(role_id)
                    if role and role in ctx.author.roles and self._has_role_acl_permission(role_id, permission):
                        return True
                return False

            if is_owner:
                embed.add_field(name="Server Setup", value="`/setup` - Configure all bot channels interactively", inline=False)
            if (await asyncio.to_thread(has_permission, "manage_admins")):
                embed.add_field(name="Admin Management", value="`/admin manage <action> [targets] [acl]` - Manage administrators in bulk\n  Actions: add, remove, give permissions, take away permissions, list, clean up\n  Targets: mention several members or roles (or paste IDs), separated by spaces or commas", inline=False)
            if (await asyncio.to_thread(has_permission, "manage_promotions")):
                embed.add_field(name="Promotions", value="`/promotion check <member>` - Check promotion eligibility\n`/promotion pending` - Show pending promotions\n`/promotion manage <action> [member] [boost/delay]` - Manage promotions", inline=False)
            if (await asyncio.to_thread(has_permission, "manage_members")):
                embed.add_field(name="Member Management", value="`/member add <discord_user> <ig_name> <rank> [account] [country] [status]`\n`/member update <discord_user> [field] [value]`\n`/member remove <discord_user>`\n`/member list` - View all members", inline=False)
            if (await asyncio.to_thread(has_permission, "manage_points")):
                embed.add_field(name="Points Management", value="`/points manage <user> <point_type> <amount>` - Adjust member points (+5 add, -5 remove, 5 set)", inline=False)
            if (await asyncio.to_thread(has_permission, "manage_casino")):
                embed.add_field(name="Casino Management", value="`/casino manage <user> <amount> [reason]` - +10k adds, -10k removes, 10k sets a member's casino credits (logged)", inline=False)
            if (await asyncio.to_thread(has_permission, "manage_shop")):
                embed.add_field(name="Shop Management", value="`/shop manage <action>` - add, edit, restock or remove items and sections, or see orders waiting for HQ", inline=False)
            if (await asyncio.to_thread(has_permission, "view_activity")):
                embed.add_field(name="Activity Analysis", value="`/activity-check <csv> <days>` - Analyze activity and get transfer recommendations", inline=False)
            if (await asyncio.to_thread(has_permission, "view_analytics")):
                embed.add_field(name="Analytics & Statistics", value="`/analyze overview [days]` - Squad overview statistics\n`/analyze member <discord_user> [days]` - Individual member stats\n`/analyze operation <operation> [days]` - Operation-type statistics", inline=False)
            embed.add_field(name="Public Commands", value="`/profile [member]` - View member profile\n`/leaderboard <field>` - View activity leaderboards (Activity, Leaders, Airforce, Warzone, Doomsday, Casino, Trivia)\n`/online` - Check server online status\n`/balance` `/dailyreward` `/casino play <game> <amount>` - Casino\n`/trivia play` - Trivia\n`/shop list` `/shop buy <item> [quantity]` `/shop orders` - Shop", inline=False)

            if not is_owner:
                user_perms = list((await asyncio.to_thread(self._get_admin_acl, ctx.author.id))) if is_admin else []
                for role_id in (await asyncio.to_thread(self._get_admin_roles)):
                    role = ctx.guild.get_role(role_id)
                    if role and role in ctx.author.roles:
                        user_perms.extend((await asyncio.to_thread(self._get_role_acl, role_id)))
                user_perms = list(set(user_perms))
                if user_perms:
                    embed.add_field(name="Your Permissions", value=", ".join([f"`{perm}`" for perm in sorted(user_perms)]), inline=False)

            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"HQ commands error: {e}", exc_info=True)
            await ctx.send(**self._msg("Error", f"An error occurred: {e}"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
