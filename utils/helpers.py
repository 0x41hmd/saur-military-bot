import asyncio
import time
import threading
import logging
from typing import List, Tuple, Optional, Callable, Any, Dict
from functools import wraps, lru_cache
from collections import OrderedDict

import discord
from discord import Interaction
from discord.ext import commands

from database.db import get_db_connection, get_config_value, set_config_value, get_channel_ids
from utils.settings import (
    DEVELOPER_ID, RANK_TAG_MAP, PERFORMANCE_CONFIG, EMBED_COLOR_HEX, EMBED_THUMBNAIL, EMBED_FOOTER_TEXT
)

logger = logging.getLogger(__name__)


class ThreadSafeCache:
    def __init__(self, ttl_seconds: int = 300, max_size: int = 200):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._lock = threading.RLock()
        self._cache: OrderedDict = OrderedDict()
        self._access_count = 0

    def _prune_expired(self) -> None:
        now = time.time()
        for key in [k for k, (ts, _) in self._cache.items() if now - ts > self.ttl]:
            del self._cache[key]

    def _enforce_size_limit(self) -> None:
        while len(self._cache) > self.max_size:
            self._cache.popitem(last=False)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time(), value)
            self._cache.move_to_end(key)
            self._access_count += 1
            if self._access_count % 100 == 0:
                self._prune_expired()
                self._enforce_size_limit()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            self._access_count += 1
            if key not in self._cache:
                return None
            timestamp, value = self._cache[key]
            if time.time() - timestamp > self.ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    def invalidate(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)


member_cache = ThreadSafeCache(
    ttl_seconds=PERFORMANCE_CONFIG['member_cache_ttl_seconds'],
    max_size=PERFORMANCE_CONFIG['member_cache_max_entries']
)
config_cache = ThreadSafeCache(ttl_seconds=300, max_size=50)


def require_permissions(admin: bool = False, acl: Optional[str] = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(self, interaction: Interaction, *args, **kwargs):
            if admin and not (await asyncio.to_thread(is_admin_member, interaction.user)):
                await interaction.response.send_message(
                    "This command requires administrator privileges.", ephemeral=True
                )
                return

            if acl:
                has_permission = False
                if interaction.user.id == DEVELOPER_ID:
                    has_permission = True
                elif (await asyncio.to_thread(is_admin_member, interaction.user)):
                    admin_cog = interaction.client.get_cog("Admin")
                    if admin_cog:
                        has_permission = (await asyncio.to_thread(admin_cog._has_acl_permission, interaction.user.id, acl))
                        if not has_permission and hasattr(interaction.user, 'roles'):
                            for role in interaction.user.roles:
                                if (await asyncio.to_thread(admin_cog._has_role_acl_permission, role.id, acl)):
                                    has_permission = True
                                    break

                if not has_permission:
                    await interaction.response.send_message(
                        f"You don't have the required permission: `{acl}`", ephemeral=True
                    )
                    return

            return await func(self, interaction, *args, **kwargs)
        return wrapper
    return decorator


@lru_cache(maxsize=128)
def _get_admin_config_cached(cache_key: str) -> Tuple[List[int], List[int]]:
    admin_members = []
    admin_roles = []
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT value FROM config WHERE key = 'admin_members'").fetchone()
            if row and row['value']:
                admin_members = [int(uid.strip()) for uid in row['value'].split(',') if uid.strip()]

            row = conn.execute("SELECT value FROM config WHERE key = 'admin_roles'").fetchone()
            if row and row['value']:
                admin_roles = [int(rid.strip()) for rid in row['value'].split(',') if rid.strip()]
    except Exception as e:
        logger.error(f"Error fetching admin config: {e}", exc_info=True)
    return (admin_members, admin_roles)


def is_admin_member(user: discord.User) -> bool:
    if user.id == DEVELOPER_ID:
        return True

    cache_key = f"admin_check_{int(time.time() // 60)}"
    admin_members, admin_roles = _get_admin_config_cached(cache_key)

    if user.id in admin_members:
        return True

    if hasattr(user, 'roles') and admin_roles:
        user_role_ids = [role.id for role in user.roles if role is not None]
        if any(role_id in admin_roles for role_id in user_role_ids):
            return True

    return False


def invalidate_admin_cache() -> None:
    _get_admin_config_cached.cache_clear()


async def get_channel_safe(
    bot: commands.Bot,
    channel_key: str,
    fallback: Optional[discord.TextChannel] = None
) -> Optional[discord.TextChannel]:
    try:
        cache_key = f"channel:{channel_key}"
        cached_id = config_cache.get(cache_key)

        if cached_id:
            channel = bot.get_channel(cached_id)
            if channel:
                return channel
            config_cache.invalidate(cache_key)

        channel_ids = (await asyncio.to_thread(get_channel_ids, channel_key))

        if not channel_ids:
            logger.warning(f"No channel IDs found for key: {channel_key}")
            return fallback

        for channel_id in channel_ids:
            channel = bot.get_channel(channel_id)
            if channel:
                config_cache.set(cache_key, channel_id)
                return channel

        logger.warning(f"No valid channels found for key: {channel_key}")
        return fallback

    except Exception as e:
        logger.error(f"Error getting channel for key '{channel_key}': {e}")
        return fallback


def create_embed(
    title: str,
    description: str = "",
    user: Optional[discord.User] = None,
    color: Optional[discord.Color] = None,
    thumbnail: bool = True,
    footer: bool = True
) -> discord.Embed:
    embed_color = color or discord.Color(int(EMBED_COLOR_HEX.lstrip("#"), 16))
    embed = discord.Embed(title=title, description=description, color=embed_color)

    if thumbnail:
        if user and hasattr(user, 'avatar') and user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        else:
            embed.set_thumbnail(url=EMBED_THUMBNAIL)

    if footer:
        embed.set_footer(text=EMBED_FOOTER_TEXT, icon_url=EMBED_THUMBNAIL)

    return embed


NO_PING = discord.AllowedMentions.none()


def plain(text: str) -> Dict[str, Any]:
    return {"content": text, "allowed_mentions": NO_PING}


async def send_private_error(interaction: Interaction, text: str) -> None:
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
    await interaction.followup.send(text, ephemeral=True)


def format_playtime(minutes) -> str:
    hours, mins = divmod(max(int(minutes or 0), 0), 60)
    return f"{hours}h {mins}m"


def format_rank_for_display(rank: str) -> str:
    return f"[{RANK_TAG_MAP.get(rank, rank[:3].upper())}] {rank}"


def convert_ids_to_ints(id_list: List[Any]) -> List[int]:
    if not id_list:
        return []
    cleaned = []
    for uid in id_list:
        if uid is None or uid == 'none':
            continue
        try:
            cleaned.append(int(uid))
        except (ValueError, TypeError):
            logger.debug(f"Skipping invalid ID: {uid}")
    return cleaned


async def ensure_persistent_menu(
    channel: discord.TextChannel,
    view_factory: Callable,
    embed_builder: Callable,
    db_key: str,
    bot: Optional[Any] = None
) -> Optional[discord.Message]:
    try:
        existing_id = (await asyncio.to_thread(get_config_value, db_key))
        if existing_id:
            try:
                message = await channel.fetch_message(int(existing_id))
                try:
                    await message.edit(
                        embed=embed_builder(),
                        view=view_factory(bot) if bot else view_factory()
                    )
                    return message
                except Exception as e:
                    logger.warning(f"Failed to update existing menu '{db_key}': {e}")
            except discord.NotFound:
                logger.info(f"Persistent menu message not found, creating new: {db_key}")
            except Exception as e:
                logger.error(f"Error fetching existing menu '{db_key}': {e}")

        message = await channel.send(
            embed=embed_builder(),
            view=view_factory(bot) if bot else view_factory()
        )
        (await asyncio.to_thread(set_config_value, db_key, str(message.id)))
        logger.info(f"Created persistent menu: {db_key} to {message.id}")
        return message

    except Exception as e:
        logger.error(f"Failed to ensure persistent menu '{db_key}': {e}")
        return None


async def validate_persistent_menus(bot_instance: Optional[commands.Bot] = None) -> None:
    menu_config = {
        'placeholder_message_id': 'channel_reporting',
        'ticket_menu_message_id': 'open_ticket_channel',
        'recruitment_menu_message_id': 'channel_recruitment',
    }

    for message_key, channel_key in menu_config.items():
        try:
            message_id_str = (await asyncio.to_thread(get_config_value, message_key))
            if not message_id_str:
                continue

            try:
                message_id = int(message_id_str)
            except (ValueError, TypeError):
                logger.warning(f"Invalid message ID for '{message_key}', removing")
                (await asyncio.to_thread(set_config_value, message_key, ""))
                continue

            if not bot_instance:
                continue

            channel = await get_channel_safe(bot_instance, channel_key)
            if not channel:
                logger.warning(f"Channel '{channel_key}' not available, removing menu config")
                (await asyncio.to_thread(set_config_value, message_key, ""))
                continue

            try:
                await channel.fetch_message(message_id)
            except discord.NotFound:
                logger.warning(f"Persistent menu '{message_key}' not found, removing config")
                (await asyncio.to_thread(set_config_value, message_key, ""))

        except Exception as e:
            logger.error(f"Error validating persistent menu '{message_key}': {e}")


def get_user_display_name(user) -> str:
    try:
        if hasattr(user, 'global_name') and user.global_name:
            return user.global_name
        if hasattr(user, 'name'):
            return user.name
        return str(user)
    except Exception:
        return "Unknown User"




def split_signed(text: str):
    raw = (text or "").strip()
    if raw[:1] == "+":
        return "add", raw[1:].strip()
    if raw[:1] in ("-", "\u2212"):
        return "remove", raw[1:].strip()
    return "set", raw
