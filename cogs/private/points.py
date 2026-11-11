import logging
from datetime import datetime, timezone
from typing import Tuple, Optional

from discord.ext import commands
from discord import app_commands, User, Interaction

from database.db import aget_db_connection
from database.db import get_db_connection
from utils.helpers import require_permissions, member_cache, get_user_display_name, plain, split_signed

logger = logging.getLogger(__name__)

VALID_POINT_TYPES = ('promo', 'leadership', 'af')
VALID_ACTIONS     = ('add', 'remove', 'set')
POINT_TYPE_LABELS = {
    'promo':      'Promotional Points',
    'leadership': 'Leadership Points',
    'af':         'Air Force Points',
}
COLUMN_MAP = {
    'promo':      'promo_points',
    'leadership': 'leadership_points',
    'af':         'af_points',
}


class PointsError(Exception):
    pass

class ValidationError(PointsError):
    pass

class DatabaseError(PointsError):
    pass


def validate_points_command(user: User, point_type: str, amount: int, action: str) -> Tuple[bool, str]:
    if user.bot:
        return False, "Cannot modify points for bots."
    if amount < 0:
        return False, "Amount must be non-negative."
    if point_type not in VALID_POINT_TYPES:
        return False, f"Invalid point type: {point_type}. Must be one of: {', '.join(VALID_POINT_TYPES)}"
    if action not in VALID_ACTIONS:
        return False, f"Invalid action: {action}. Must be one of: {', '.join(VALID_ACTIONS)}"
    return True, ""


class Points(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def point_type_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=ptype)
            for ptype, label in POINT_TYPE_LABELS.items()
            if current.lower() in label.lower()
        ][:8]

    async def update_points(self, discord_id: str, point_type: str, amount: int, action: str, performed_by: str) -> int:
        try:
            async with aget_db_connection() as conn:
                member_row = (await conn.execute(
                    "SELECT rank FROM members WHERE discord_id = ?", (discord_id,)
                )).fetchone()
                if not member_row:
                    raise ValidationError("Member not found in database")

                rank = member_row['rank']
                col  = COLUMN_MAP[point_type]

                (await conn.execute(
                    "INSERT OR IGNORE INTO rank_stats (discord_id, rank, promo_points, leadership_points, af_points) VALUES (?, ?, 0, 0, 0)",
                    (discord_id, rank)
                ))
                (await conn.execute(
                    "INSERT OR IGNORE INTO points (discord_id) VALUES (?)", (discord_id,)
                ))

                result        = (await conn.execute(f"SELECT {col} FROM rank_stats WHERE discord_id = ? AND rank = ?", (discord_id, rank))).fetchone()
                current_value = result[0] if result else 0

                if action == 'set':
                    new_value = max(0, amount)
                    delta     = new_value - current_value
                elif action == 'add':
                    new_value = current_value + amount
                    delta     = amount
                else:
                    new_value = max(0, current_value - amount)
                    delta     = -(min(amount, current_value))

                (await conn.execute(
                    f"UPDATE rank_stats SET {col} = ? WHERE discord_id = ? AND rank = ?",
                    (new_value, discord_id, rank)
                ))

                if delta != 0:
                    (await conn.execute(
                        "INSERT INTO manual_points_awards (discord_id, point_type, amount, awarded_by, created_at) VALUES (?, ?, ?, ?, ?)",
                        (discord_id, point_type, delta, performed_by, datetime.now(timezone.utc).isoformat())
                    ))

                (await conn.commit())
                logger.info(f"Points updated: user={discord_id}, type={point_type}, action={action}, amount={amount}, new_total={new_value}")

                try:
                    member_cache.invalidate(f"profile:{discord_id}")
                except (KeyError, AttributeError) as e:
                    logger.debug(f"Cache invalidation skipped: {e}")

                return new_value

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Failed to update points: {e}", exc_info=True)
            raise DatabaseError(f"Database operation failed: {e}")

    points = app_commands.Group(name="points", description="Points management (HQ only)")

    @points.command(name="manage", description="Manage member points (HQ only)")
    @require_permissions(admin=True, acl="manage_points")
    @app_commands.describe(
        user="Target member",
        point_type="Type of points",
        amount="+5 adds, -5 removes, 5 sets the exact value",
        reason="Optional reason"
    )
    @app_commands.autocomplete(point_type=point_type_autocomplete)
    async def points_manage(self, interaction: Interaction, user: User, point_type: str, amount: str, reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=False)

        action, amount_text = split_signed(amount)
        try:
            amount = int(amount_text.replace(",", "").replace("_", ""))
        except ValueError:
            await interaction.followup.send(**plain("Amount must be a whole number like `+5`, `-5` or `5` (set)."), ephemeral=True)
            return

        try:
            is_valid, error_msg = validate_points_command(user, point_type, amount, action)
            if not is_valid:
                await interaction.followup.send(error_msg, ephemeral=True)
                return

            new_total    = await self.update_points(str(user.id), point_type, amount, action, str(interaction.user.id))
            display_type = POINT_TYPE_LABELS.get(point_type, point_type.capitalize())

            logs_cog = self.bot.get_cog("Logs")
            if logs_cog:
                change = {"add": f"+{amount}", "remove": f"-{amount}"}.get(action, f"set to {amount}")
                log_details = {"Type": display_type, "Change": change, "New Total": str(new_total)}
                if reason:
                    log_details["Reason"] = reason
                await logs_cog.log_action(
                    action="Points Updated",
                    description=f"{get_user_display_name(user)}'s points were adjusted",
                    user=interaction.user,
                    target_user=user,
                    details=log_details,
                )

            if action == 'add':
                change_text = f"Added {amount} pts"
            elif action == 'remove':
                change_text = f"Removed {amount} pts"
            else:
                change_text = f"Set to {amount} pts"

            text = f"{display_type} points adjusted for {user.mention}.\nChange: `{change_text}` \u00b7 New total: `{new_total}`"
            if reason:
                text += f" (reason: {reason})"
            await interaction.followup.send(**plain(text))

        except ValidationError as e:
            logger.warning(f"Validation error in points manage: {e}")
            await interaction.followup.send(**plain(f"Validation error: {e}"), ephemeral=True)
        except DatabaseError as e:
            logger.error(f"Database error in points manage: {e}")
            await interaction.followup.send(**plain("A database error occurred. Please contact HQ technical staff."), ephemeral=True)
        except Exception as e:
            logger.error(f"Unexpected error in points manage: {e}", exc_info=True)
            await interaction.followup.send(**plain(f"An unexpected error occurred: {e}"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Points(bot))
