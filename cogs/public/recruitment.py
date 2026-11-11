import asyncio
import logging
import re
from collections import defaultdict
import dataclasses
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from database.db import get_config_value, set_config_value
from utils.helpers import (
    create_embed,
    get_channel_safe,
    is_admin_member,
    plain,
    require_permissions,
    send_private_error,
)
from utils.settings import DEVELOPER_ID

logger = logging.getLogger(__name__)

ACL_PERMISSION = "manage_recruitment"

LOGS_CHANNEL_KEY = "channel_recruitment_logs"
ANNOUNCE_CHANNEL_KEY = "channel_recruitment"
ANNOUNCE_MESSAGE_KEY = "recruitment_announcement_id"
MENU_MESSAGE_KEY = "recruitment_menu_message_id"

VOTE_FOR = "👍"
VOTE_AGAINST = "👎"
VOTE_EMOJIS: Tuple[str, ...] = (VOTE_FOR, VOTE_AGAINST)

WAVE_NAME_RE = re.compile(r"^Wave #(\d+) - Standard Applications$")
REINSTATE_THREAD_NAME = "Re-instatement Requests"
THREAD_AUTO_ARCHIVE_MINUTES = 10080

DASHBOARD_TITLE = "Recruitment"
APPLICATION_TITLE = "Standard Application"
REINSTATE_TITLE = "Re-instatement Request"

FIELD_APPLICANT = "Applicant"
FIELD_IGN = "In-Game Name"
FIELD_STATUS = "Status"

STATUS_PENDING = "Pending review"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_DENIED = "DENIED"


MSG_NOT_LOADED = "Recruitment is not loaded. Please contact an administrator."
MSG_NOT_SET_UP_PUBLIC = "Recruitment is not set up right now. Please contact HQ."
MSG_NOT_SET_UP_STAFF = "Recruitment isn't set up. An admin needs to run `/setup` first."

APPLY_BUTTON_ID = "recruitment:apply"
REINSTATE_BUTTON_ID = "recruitment:reinstate"

_MENTION_RE = re.compile(r"<@!?(\d+)>")
NO_PINGS = discord.AllowedMentions.none()


def decide(votes_for: int, votes_against: int) -> bool:
    return votes_for > votes_against


def parse_wave_number(thread_name: str) -> Optional[int]:
    match = WAVE_NAME_RE.match(thread_name or "")
    return int(match.group(1)) if match else None


def get_field(embed: discord.Embed, name: str) -> Optional[str]:
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None


def set_status(embed: discord.Embed, value: str) -> None:
    for index, field in enumerate(embed.fields):
        if field.name == FIELD_STATUS:
            embed.set_field_at(index, name=FIELD_STATUS, value=value, inline=False)
            return
    embed.add_field(name=FIELD_STATUS, value=value, inline=False)


def read_verdict(embed: discord.Embed) -> Optional[bool]:
    status = get_field(embed, FIELD_STATUS) or ""
    if f"**{STATUS_ACCEPTED}**" in status:
        return True
    if f"**{STATUS_DENIED}**" in status:
        return False
    return None


def read_applicant_id(embed: discord.Embed) -> Optional[int]:
    match = _MENTION_RE.search(get_field(embed, FIELD_APPLICANT) or "")
    return int(match.group(1)) if match else None


def clean_text(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_IMAGE_RE = re.compile(r"\.(png|jpe?g|gif|webp)(\?|$)", re.IGNORECASE)
SCREENSHOT_HELP = "Links to your /mypunishments, arrests and playtime screenshots"


def format_screenshots(text: str) -> str:
    return (text or "").strip()[:1000] or "None"


def first_image_link(text: str) -> Optional[str]:
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;)]}'\"")
        if _IMAGE_RE.search(url):
            return url
    return None


def chunk_lines(lines: List[str], limit: int = 1000) -> List[str]:
    chunks: List[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


@dataclasses.dataclass
class WaveStats:
    number: int
    opened_at: datetime
    closed_at: Optional[datetime]
    applications: List[datetime] = dataclasses.field(default_factory=list)
    accepted: int = 0
    denied: int = 0


class ApplicationModal(discord.ui.Modal, title="Standard Application"):
    ign = discord.ui.TextInput(label="In-Game Name", placeholder="Your exact in-game name", max_length=32)
    account_name = discord.ui.TextInput(label="Account Name", placeholder="Your account name", max_length=32)
    age_nationality = discord.ui.TextInput(
        label="Age and Nationality",
        placeholder="e.g. 17 Egyptian",
        max_length=45,
    )
    description = discord.ui.TextInput(
        label="Describe Yourself as a Player",
        style=discord.TextStyle.paragraph,
        placeholder="Your experience, playstyle, strengths, and why you want to join.",
        max_length=1000,
    )
    screenshots = discord.ui.TextInput(
        label="Screenshot Links",
        style=discord.TextStyle.paragraph,
        placeholder=SCREENSHOT_HELP,
        max_length=800,
    )

    async def on_submit(self, interaction: Interaction) -> None:
        cog = await _defer_and_get_cog(interaction)
        if not cog:
            return
        await cog.submit_application(
            interaction,
            self.ign.value.strip(),
            self.account_name.value.strip(),
            self.age_nationality.value.strip(),
            self.description.value.strip(),
            self.screenshots.value.strip(),
        )

    async def on_error(self, interaction: Interaction, error: Exception) -> None:
        logger.error(f"Application modal error: {error}", exc_info=True)
        await _safe_reply(interaction, "Something went wrong submitting your application. Please try again later.")


class ReinstatementModal(discord.ui.Modal, title="Re-instatement Request"):
    prior = discord.ui.TextInput(
        label="Prior In-Game Name and Past Rank",
        placeholder="e.g. Keon Staff Sergeant (Sergeant or above)",
        max_length=75,
    )
    account_name = discord.ui.TextInput(label="Account Name", placeholder="Your account name", max_length=32)
    nationality = discord.ui.TextInput(label="Nationality", placeholder="e.g. Egyptian", max_length=40)
    description = discord.ui.TextInput(
        label="Describe Yourself and Why You Return",
        style=discord.TextStyle.paragraph,
        placeholder="Describe yourself as a player now, and why you are coming back.",
        max_length=1000,
    )
    screenshots = discord.ui.TextInput(
        label="Screenshot Links",
        style=discord.TextStyle.paragraph,
        placeholder=SCREENSHOT_HELP,
        max_length=800,
    )

    async def on_submit(self, interaction: Interaction) -> None:
        cog = await _defer_and_get_cog(interaction)
        if not cog:
            return
        await cog.submit_reinstatement(
            interaction,
            self.prior.value.strip(),
            self.account_name.value.strip(),
            self.nationality.value.strip(),
            self.description.value.strip(),
            self.screenshots.value.strip(),
        )

    async def on_error(self, interaction: Interaction, error: Exception) -> None:
        logger.error(f"Re-instatement modal error: {error}", exc_info=True)
        await _safe_reply(interaction, "Something went wrong submitting your request. Please try again later.")


async def _defer_and_get_cog(interaction: Interaction) -> Optional["Recruitment"]:
    await interaction.response.defer(ephemeral=True)
    cog: Optional["Recruitment"] = interaction.client.get_cog("Recruitment")
    if not cog:
        await interaction.followup.send(MSG_NOT_LOADED, ephemeral=True)
    return cog


async def _safe_reply(interaction: Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


class RecruitmentDashboardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _check_ready(self, interaction: Interaction) -> bool:
        cog: Optional["Recruitment"] = interaction.client.get_cog("Recruitment")
        if not cog or not await cog.get_logs_channel():
            await interaction.response.send_message(MSG_NOT_SET_UP_PUBLIC, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Apply Here", style=discord.ButtonStyle.green, custom_id=APPLY_BUTTON_ID)
    async def apply_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        if not await self._check_ready(interaction):
            return
        cog: "Recruitment" = interaction.client.get_cog("Recruitment")
        if not await cog.get_active_wave():
            await interaction.response.send_message(
                "Applications are closed right now. Please check back when a new wave opens.", ephemeral=True
            )
            return
        await interaction.response.send_modal(ApplicationModal())

    @discord.ui.button(label="Re-instatement", style=discord.ButtonStyle.grey, custom_id=REINSTATE_BUTTON_ID)
    async def reinstate_button(self, interaction: Interaction, button: discord.ui.Button) -> None:
        if await self._check_ready(interaction):
            await interaction.response.send_modal(ReinstatementModal())


def build_dashboard_embed(wave_number: Optional[int] = None) -> discord.Embed:
    if wave_number:
        title = f"\U0001F7E2 Now Recruiting: Wave #{wave_number}"
        status = "\U0001F7E2 **Applications are open.** Press **Apply Here** to send yours."
    else:
        title = f"\U0001F534 {DASHBOARD_TITLE}"
        status = "\U0001F534 **Recruitment is closed.** Check back when the next wave opens."

    return create_embed(
        title=title,
        description=(
            f"{status}\n\n"
            "**Apply Here**\nNew to the Military? Fill in the form while a wave is open and HQ will review "
            "your application.\n\n"
            "**Re-instatement**\nFormer members who held the rank of Sergeant or above can request to return.\n\n"
            "**Before you start**\nHave links ready to screenshots of your `/mypunishments`, your arrests and your playtime."
        ),
    )


class Recruitment(commands.Cog):
    recruitment = app_commands.Group(name="recruitment", description="Recruitment system", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._views_registered = False
        self._wave_lock = asyncio.Lock()
        self._reinstate_lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._views_registered:
            return
        self.bot.add_view(RecruitmentDashboardView())
        self._views_registered = True
        logger.info("Recruitment dashboard view registered")

    def has_recruitment_acl(self, member: discord.abc.User) -> bool:
        if member.id == DEVELOPER_ID:
            return True
        if not is_admin_member(member):
            return False
        admin_cog = self.bot.get_cog("Admin")
        if not admin_cog:
            return False
        if admin_cog._has_acl_permission(member.id, ACL_PERMISSION):
            return True
        return any(
            admin_cog._has_role_acl_permission(role.id, ACL_PERMISSION)
            for role in getattr(member, "roles", [])
        )

    async def get_logs_channel(self) -> Optional[discord.TextChannel]:
        channel = await get_channel_safe(self.bot, LOGS_CHANNEL_KEY)
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _threads_of(self, channel: discord.TextChannel) -> List[discord.Thread]:
        threads = list(channel.threads)
        known = {t.id for t in threads}
        try:
            async for thread in channel.archived_threads(limit=100):
                if thread.id not in known:
                    threads.append(thread)
        except discord.Forbidden:
            logger.warning("Missing permission to list archived threads in the recruitment logs channel")
        return threads

    async def _find_active_wave(self, logs: discord.TextChannel) -> Tuple[Optional[discord.Thread], int]:
        waves = []
        for thread in await self._threads_of(logs):
            number = parse_wave_number(thread.name)
            if number is not None:
                waves.append((number, thread))
        if not waves:
            return None, 0
        number, newest = max(waves, key=lambda w: w[0])
        if newest.locked:
            return None, number
        if newest.archived:
            await newest.edit(archived=False)
        return newest, number

    async def get_active_wave(self) -> Optional[discord.Thread]:
        logs = await self.get_logs_channel()
        if not logs:
            return None
        async with self._wave_lock:
            thread, _ = await self._find_active_wave(logs)
            return thread

    async def _get_or_create_reinstatement_thread(self, logs: discord.TextChannel) -> discord.Thread:
        async with self._reinstate_lock:
            for thread in await self._threads_of(logs):
                if thread.name == REINSTATE_THREAD_NAME:
                    if thread.archived:
                        await thread.edit(archived=False)
                    return thread
            return await logs.create_thread(
                name=REINSTATE_THREAD_NAME,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
                reason="Recruitment: re-instatement feed",
            )

    def _is_application(self, message: discord.Message) -> bool:
        return (
            message.author.id == self.bot.user.id
            and bool(message.embeds)
            and message.embeds[0].title == APPLICATION_TITLE
        )

    async def _wave_stats(self, number: int, thread: discord.Thread) -> WaveStats:
        stats = WaveStats(number=number, opened_at=thread.created_at, closed_at=None)
        summary_prefix = f"Wave #{number} - Final Results"
        async for message in thread.history(limit=None, oldest_first=True):
            if self._is_application(message):
                stats.applications.append(message.created_at)
                verdict = read_verdict(message.embeds[0])
                if verdict is True:
                    stats.accepted += 1
                elif verdict is False:
                    stats.denied += 1
            elif (
                stats.closed_at is None
                and message.author.id == self.bot.user.id
                and message.embeds
                and (message.embeds[0].title or "").startswith(summary_prefix)
            ):
                stats.closed_at = message.created_at
        if stats.closed_at is None and thread.locked:
            stats.closed_at = thread.archive_timestamp
        return stats

    async def collect_wave_stats(self, limit: Optional[int] = None) -> Optional[List[WaveStats]]:
        logs = await self.get_logs_channel()
        if not logs:
            return None
        threads = await self._threads_of(logs)
        waves = sorted(
            ((parse_wave_number(t.name), t) for t in threads if parse_wave_number(t.name) is not None),
            key=lambda w: w[0],
        )
        if limit:
            waves = waves[-limit:]
        return [await self._wave_stats(number, thread) for number, thread in waves]

    async def refresh_dashboard(self) -> None:
        try:
            from utils.menus import ensure_recruitment_menu
            await ensure_recruitment_menu(self.bot)
        except Exception as e:
            logger.error(f"Failed to refresh the recruitment menu: {e}", exc_info=True)

    async def _announce_open(self, wave_number: int) -> None:
        try:
            channel = await get_channel_safe(self.bot, ANNOUNCE_CHANNEL_KEY)
            if not channel:
                return
            await self._clear_announcement()

            text = f"@everyone We are recruiting!\n\nWave #{wave_number} is now open."
            menu_id = (await asyncio.to_thread(get_config_value, MENU_MESSAGE_KEY))
            if menu_id:
                text += f"\nApply here: {channel.get_partial_message(int(menu_id)).jump_url}"
            text += "\n\nHave links ready to screenshots of your /mypunishments, your arrests and your playtime."

            message = await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(everyone=True, roles=False, users=False),
            )
            (await asyncio.to_thread(set_config_value, ANNOUNCE_MESSAGE_KEY, str(message.id)))
        except Exception as e:
            logger.error(f"Failed to post the recruitment announcement: {e}", exc_info=True)

    async def _invite_reviewers(self, thread: discord.Thread) -> None:
        try:
            admin_cog = self.bot.get_cog("Admin")
            if not admin_cog:
                return
            roles = [
                role
                for role_id in (await asyncio.to_thread(admin_cog._get_admin_roles))
                if (await asyncio.to_thread(admin_cog._has_role_acl_permission, role_id, ACL_PERMISSION))
                and (role := thread.guild.get_role(role_id))
            ]
            if not roles:
                return
            await thread.send(
                " ".join(role.mention for role in roles)
                + f"\n\n{thread.name} is open. Applications will be posted here for voting.",
                allowed_mentions=discord.AllowedMentions(roles=roles, users=False, everyone=False),
            )
        except Exception as e:
            logger.error(f"Failed to invite reviewers to {thread.name}: {e}", exc_info=True)

    async def _clear_announcement(self) -> None:
        try:
            message_id = (await asyncio.to_thread(get_config_value, ANNOUNCE_MESSAGE_KEY))
            if not message_id:
                return
            (await asyncio.to_thread(set_config_value, ANNOUNCE_MESSAGE_KEY, ""))
            channel = await get_channel_safe(self.bot, ANNOUNCE_CHANNEL_KEY)
            if channel:
                try:
                    await channel.get_partial_message(int(message_id)).delete()
                except discord.NotFound:
                    pass
        except Exception as e:
            logger.error(f"Failed to remove the recruitment announcement: {e}", exc_info=True)

    @recruitment.command(name="open", description="Open a new recruitment wave")
    @require_permissions(acl=ACL_PERMISSION)
    async def recruitment_open(self, interaction: Interaction) -> None:
        await interaction.response.defer()

        logs = await self.get_logs_channel()
        if not logs:
            await send_private_error(interaction, MSG_NOT_SET_UP_STAFF)
            return

        async with self._wave_lock:
            existing, highest = await self._find_active_wave(logs)
            if existing:
                await send_private_error(
                    interaction, f"A wave is already open: {existing.mention}. Run `/recruitment close` first."
                )
                return
            try:
                thread = await logs.create_thread(
                    name=f"Wave #{highest + 1} - Standard Applications",
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
                    reason=f"Recruitment: wave opened by {interaction.user}",
                )
            except discord.Forbidden:
                await send_private_error(
                    interaction, "I can't create threads in the recruitment logs channel (need Create Public Threads)."
                )
                return

        logger.info(f"Recruitment wave opened: {thread.name} by {interaction.user.id}")
        await self._invite_reviewers(thread)
        await self.refresh_dashboard()
        await self._announce_open(parse_wave_number(thread.name) or highest + 1)
        await interaction.followup.send(
            f"**{thread.name}** is open: {thread.mention}\nApplicants can now use **Apply Here**."
        )

    async def submit_application(
        self, interaction: Interaction, ign: str, account_name: str, age_nationality: str, description: str, screenshots: str
    ) -> None:
        logs = await self.get_logs_channel()
        if not logs:
            await interaction.followup.send(MSG_NOT_SET_UP_PUBLIC, ephemeral=True)
            return

        user = interaction.user
        try:
            thread = await self.get_active_wave()
            if not thread:
                await interaction.followup.send(**plain("Applications closed while you were filling this in. Please check back when the next wave opens."), ephemeral=True)
                return
            embed = create_embed(title=APPLICATION_TITLE, user=user)
            embed.timestamp = discord.utils.utcnow()
            embed.add_field(name=FIELD_APPLICANT, value=user.mention, inline=True)
            embed.add_field(name=FIELD_IGN, value=ign, inline=True)
            embed.add_field(name="Account Name", value=account_name, inline=True)
            embed.add_field(name="Age and Nationality", value=age_nationality, inline=True)
            embed.add_field(name="Player Description", value=description, inline=False)
            embed.add_field(name="Screenshots", value=format_screenshots(screenshots), inline=False)
            image = first_image_link(screenshots)
            if image:
                embed.set_image(url=image)
            embed.add_field(name="Staff Vote", value=f"{VOTE_FOR} In Favor  ·  {VOTE_AGAINST} Against", inline=False)
            embed.add_field(name=FIELD_STATUS, value=STATUS_PENDING, inline=False)

            message = await thread.send(embed=embed, allowed_mentions=NO_PINGS)
            for emoji in VOTE_EMOJIS:
                await message.add_reaction(emoji)
        except discord.Forbidden:
            logger.error("Missing permissions to post an application", exc_info=True)
            await interaction.followup.send(**plain("I couldn't file your application (permissions problem). Please contact HQ."), ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Failed to post application: {e}", exc_info=True)
            await interaction.followup.send(**plain("Something went wrong filing your application. Please try again later."), ephemeral=True)
            return

        await interaction.followup.send(**plain("Your application has been submitted. You'll receive a DM with the result."), ephemeral=True)

    async def submit_reinstatement(
        self, interaction: Interaction, prior: str, account_name: str, nationality: str, description: str, screenshots: str
    ) -> None:
        logs = await self.get_logs_channel()
        if not logs:
            await interaction.followup.send(MSG_NOT_SET_UP_PUBLIC, ephemeral=True)
            return

        user = interaction.user
        try:
            thread = await self._get_or_create_reinstatement_thread(logs)
            embed = create_embed(title=REINSTATE_TITLE, user=user)
            embed.timestamp = discord.utils.utcnow()
            embed.add_field(name="Member", value=user.mention, inline=True)
            embed.add_field(name="Prior In-Game Name and Past Rank", value=prior, inline=True)
            embed.add_field(name="Account Name", value=account_name, inline=True)
            embed.add_field(name="Nationality", value=nationality, inline=True)
            embed.add_field(name="Player Description and Reason for Return", value=description, inline=False)
            embed.add_field(name="Screenshots", value=format_screenshots(screenshots), inline=False)
            image = first_image_link(screenshots)
            if image:
                embed.set_image(url=image)
            await thread.send(embed=embed, allowed_mentions=NO_PINGS)
        except Exception as e:
            logger.error(f"Failed to post re-instatement request: {e}", exc_info=True)
            await interaction.followup.send(**plain("Something went wrong sending your request. Please try again later."), ephemeral=True)
            return

        await interaction.followup.send(**plain("Your re-instatement request has been sent to HQ."), ephemeral=True)

    async def _wave_thread_for(self, channel_id: int) -> Optional[discord.Thread]:
        thread = self.bot.get_channel(channel_id)
        if not isinstance(thread, discord.Thread) or parse_wave_number(thread.name) is None:
            return None
        logs = await self.get_logs_channel()
        if not logs or thread.parent_id != logs.id:
            return None
        return thread

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        thread = await self._wave_thread_for(payload.channel_id)
        if not thread or thread.locked:
            return

        member = payload.member
        partial = thread.get_partial_message(payload.message_id)
        voter = discord.Object(id=payload.user_id)

        try:
            if member is None or member.bot or not (await asyncio.to_thread(self.has_recruitment_acl, member)):
                await partial.remove_reaction(payload.emoji, voter)
                return

            emoji = str(payload.emoji)
            if emoji not in VOTE_EMOJIS:
                await partial.remove_reaction(payload.emoji, voter)
                return

            for other in VOTE_EMOJIS:
                if other != emoji:
                    try:
                        await partial.remove_reaction(other, voter)
                    except discord.NotFound:
                        pass
        except discord.Forbidden:
            logger.warning("Missing 'Manage Messages' in the wave thread; cannot enforce voting rules")
        except Exception as e:
            logger.error(f"Error handling recruitment reaction: {e}", exc_info=True)

    async def _tally(self, message: discord.Message, auth_cache: Dict[int, bool]) -> Tuple[int, int]:
        guild = message.guild
        votes: Dict[int, List[str]] = defaultdict(list)
        for reaction in message.reactions:
            emoji = str(reaction.emoji)
            if emoji not in VOTE_EMOJIS:
                continue
            async for user in reaction.users():
                if user.id == self.bot.user.id or user.bot:
                    continue
                if user.id not in auth_cache:
                    member = guild.get_member(user.id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user.id)
                        except discord.NotFound:
                            member = None
                    auth_cache[user.id] = bool(member) and (await asyncio.to_thread(self.has_recruitment_acl, member))
                if auth_cache[user.id]:
                    votes[user.id].append(emoji)

        counts = {VOTE_FOR: 0, VOTE_AGAINST: 0}
        for user_id, emojis in votes.items():
            if len(emojis) == 1:
                counts[emojis[0]] += 1
            else:
                logger.warning(f"Ignoring conflicting votes from {user_id} on message {message.id}")
        return counts[VOTE_FOR], counts[VOTE_AGAINST]

    async def _dm_result(self, user_id: int, accepted: bool) -> bool:
        try:
            user = await self.bot.fetch_user(user_id)
            if accepted:
                embed = create_embed(
                    title="Application Accepted",
                    description="Congratulations, you have been accepted to the Military!",
                )
            else:
                embed = create_embed(
                    title="Application Result",
                    description="Unfortunately, your application was not accepted this time.",
                )
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.NotFound):
            return False
        except Exception as e:
            logger.error(f"Failed to DM applicant {user_id}: {e}")
            return False

    def _summary_embeds(self, wave_number: int, accepted: List[str], denied: List[str]) -> List[discord.Embed]:
        fields: List[Tuple[str, str]] = []
        for label, lines in ((f"Accepted ({len(accepted)})", accepted), (f"Denied ({len(denied)})", denied)):
            chunks = chunk_lines(lines) or ["None"]
            for i, chunk in enumerate(chunks):
                fields.append((label if i == 0 else f"{label} (cont.)", chunk))

        embeds: List[discord.Embed] = []
        for start in range(0, len(fields), 4):
            first = start == 0
            embed = create_embed(
                title=f"Wave #{wave_number} - Final Results" + ("" if first else " (cont.)"),
                description=f"{len(accepted) + len(denied)} application(s) reviewed." if first else "",
                thumbnail=first,
            )
            for name, value in fields[start:start + 4]:
                embed.add_field(name=name, value=value, inline=False)
            embeds.append(embed)
        return embeds

    @recruitment.command(name="close", description="Tally votes, notify applicants and close the current wave")
    @require_permissions(acl=ACL_PERMISSION)
    async def recruitment_close(self, interaction: Interaction) -> None:
        await interaction.response.defer()

        logs = await self.get_logs_channel()
        if not logs:
            await send_private_error(interaction, MSG_NOT_SET_UP_STAFF)
            return

        if self._wave_lock.locked():
            await send_private_error(interaction, "A wave is already being opened or closed. Try again in a moment.")
            return

        async with self._wave_lock:
            thread, wave_number = await self._find_active_wave(logs)
            if not thread:
                await send_private_error(interaction, "There is no active wave to close.")
                return

            applications = [m async for m in thread.history(limit=None, oldest_first=True) if self._is_application(m)]

            auth_cache: Dict[int, bool] = {}
            accepted_lines: List[str] = []
            denied_lines: List[str] = []
            newly_decided = dm_failed = 0
            failures: List[str] = []

            for message in applications:
                embed = message.embeds[0].copy()
                ign = clean_text(get_field(embed, FIELD_IGN) or "Unknown")
                applicant_id = read_applicant_id(embed)
                who = f"• {ign}: <@{applicant_id}>" if applicant_id else f"• {ign}"

                verdict = read_verdict(embed)
                if verdict is None:
                    try:
                        votes_for, votes_against = await self._tally(message, auth_cache)
                        verdict = decide(votes_for, votes_against)
                        label = STATUS_ACCEPTED if verdict else STATUS_DENIED
                        set_status(
                            embed,
                            f"**{label}**\n"
                            f"{VOTE_FOR} {votes_for}  ·  {VOTE_AGAINST} {votes_against}",
                        )

                        await message.clear_reactions()
                        await message.edit(embed=embed)
                        newly_decided += 1

                        if applicant_id and not await self._dm_result(applicant_id, verdict):
                            dm_failed += 1
                    except Exception as e:
                        logger.error(f"Failed to finalize application {message.id}: {e}", exc_info=True)
                        failures.append(f"{ign} ({message.jump_url})")
                        continue

                (accepted_lines if verdict else denied_lines).append(who)

            if failures:
                await send_private_error(
                    interaction,
                    "Some applications could not be finalized, so the wave was **left open**. "
                    "Check my permissions (Manage Messages) and run `/recruitment close` again; "
                    "finished applications won't be repeated.\n" + "\n".join(f"- {f}" for f in failures),
                )
                return

            for summary in self._summary_embeds(wave_number, accepted_lines, denied_lines):
                await thread.send(embed=summary, allowed_mentions=NO_PINGS)
            await thread.edit(locked=True, archived=True, reason="Recruitment: wave closed")

        await self._clear_announcement()
        await self.refresh_dashboard()

        note = f"\n{dm_failed} applicant(s) have DMs closed and couldn't be notified." if dm_failed else ""
        await interaction.followup.send(
            f"Wave #{wave_number} closed: **{len(accepted_lines)} accepted**, **{len(denied_lines)} denied** "
            f"({newly_decided} decided just now).{note}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Recruitment(bot))
