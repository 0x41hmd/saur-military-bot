import asyncio
import logging
import random
import time
from typing import Dict, List, Optional, Set

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from cogs.public.casino import discord_ts, fmt, win_bonus_pct
from utils import flags_data, trivia_db as tdb
from utils.helpers import create_embed
from utils.settings import TRIVIA_CONFIG

logger = logging.getLogger(__name__)


FLAG_IMAGE_URL = "https://flagcdn.com/w640/{code}.png"
LETTERS = "ABCD"
GRACE_SECONDS = 1.5
CLEANUP_DELAY = 8

CORRECT_LINES = ["Correct!", "Right answer!", "Well done!", "Spot on!", "Nice one!"]
WRONG_LINES = ["Wrong answer.", "Not quite.", "Incorrect.", "Bendl down!"]
TIMEOUT_LINES = ["Time's up."]
MOTIVATION_STREAK = ["Keep the streak going.", "Stay focused.", "Don't break it now.", "You're on a roll."]
MOTIVATION_LOSS = ["Shake it off and go again.", "Every miss is a lesson.", "Try again, you've got this.",
                   "Time to rebuild that streak."]


class Trivia(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._active: Set[int] = set()
        self._last: Dict[int, float] = {}

    @staticmethod
    def _correct_label(q: Dict) -> str:
        return q["answer"].capitalize() if q["kind"] == "yesno" else q["answer"]

    def _flag_embed(self, user: discord.abc.User, q: Dict, limit: int) -> discord.Embed:
        embed = create_embed(title="Guess the flag", user=user,
                             description=f"Type the country name.\n\nEnds {discord_ts(limit)}")
        embed.set_image(url=FLAG_IMAGE_URL.format(code=q["code"].lower()))
        return embed

    def _question_text(self, user: discord.abc.User, q: Dict, values: List[str], limit: int) -> str:
        ends = f"ends {discord_ts(limit)}"
        if q["kind"] == "yesno":
            return f"{user.mention} **{q['question']}** (yes/no) \u2022 {ends}"
        opts = " ".join(f"**{LETTERS[i]}.** {v}" for i, v in enumerate(values))
        return f"{user.mention} **{q['question']}** {opts} \u2022 {ends}"

    def _result_text(self, user: discord.abc.User, q: Dict, res: Dict) -> str:
        correct = self._correct_label(q)
        if res["result"] == "correct":
            before = res["balance"] - res["reward"]
            bonus = f" (includes +{res['bonus_pct']}% bonus)" if res.get("bonus_pct") else ""
            best = " (new best!)" if res["new_best"] and res["streak"] > 1 else ""
            return (f"\u2705 {user.mention} **{random.choice(CORRECT_LINES)}**\n"
                    f"Your credits: {fmt(before)} + {fmt(res['reward'])} = **{fmt(res['balance'])}**{bonus}\n"
                    f"Streak: **{res['streak']}**{best}\n"
                    f"{random.choice(MOTIVATION_STREAK)}")
        lead = random.choice(TIMEOUT_LINES) if res["result"] == "timeout" else random.choice(WRONG_LINES)
        lost = f"\nYou lost your streak of **{res['previous_streak']}**." if res["previous_streak"] else ""
        return (f"\u274C {user.mention} **{lead}** The answer was **{correct}**.{lost}\n"
                f"Your credits: **{fmt(res['balance'])}**\n"
                f"{random.choice(MOTIVATION_LOSS)}")

    @staticmethod
    def _parse_answer(content: str, q: Dict, values: List[str]) -> Optional[int]:
        text = content.strip().lower()
        if not text:
            return None
        if q["kind"] == "flag":
            code = flags_data.match_country(text)
            if code is None:
                return None
            return 0 if code == q["code"] else 1
        if q["kind"] == "yesno":
            if text in ("yes", "y", "true"):
                return 0
            if text in ("no", "n", "false"):
                return 1
            return None
        if len(text) == 1 and text.upper() in LETTERS[: len(values)]:
            return LETTERS.index(text.upper())
        for i, v in enumerate(values):
            if text == v.strip().lower():
                return i
        return None

    async def _safe_delete(self, msg: Optional[discord.Message]) -> None:
        if not msg:
            return
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

    async def start(self, interaction: Interaction, game: str = "saur") -> None:
        uid = interaction.user.id
        if uid in self._active:
            await interaction.response.send_message("You already have a trivia question open. Answer it first.", ephemeral=True)
            return
        wait = TRIVIA_CONFIG["cooldown_seconds"] - (time.monotonic() - self._last.get(uid, -1e9))
        if wait > 0:
            await interaction.response.send_message(f"Easy there. Next question in {wait:.0f}s.", ephemeral=True)
            return

        self._active.add(uid)
        question_msg: Optional[discord.Message] = None
        flag_msg: Optional[discord.Message] = None
        answer_msgs: List[discord.Message] = []
        result_msg: Optional[discord.Message] = None
        try:
            await interaction.response.defer()

            if game == "flags":
                question = flags_data.pick_flag(uid)
            else:
                question = await asyncio.to_thread(tdb.pick_question, uid)
            if not question:
                await interaction.edit_original_response(content="There are no trivia questions right now.")
                await asyncio.sleep(5)
                await interaction.delete_original_response()
                return
            stats = await asyncio.to_thread(tdb.get_stats, uid)
            bonus_pct = (await asyncio.to_thread(win_bonus_pct, interaction.user))

            if question["kind"] == "flag":
                values = [question["answer"]]
                correct_index = 0
            elif question["kind"] == "yesno":
                values = ["yes", "no"]
                correct_index = 0 if question["answer"] == "yes" else 1
            else:
                values = list(question["choices"])
                random.shuffle(values)
                correct_index = values.index(question["answer"])

            limit = (TRIVIA_CONFIG.get("flag_time_limit_seconds", 12) if question["kind"] == "flag"
                     else TRIVIA_CONFIG["time_limit_seconds"])
            deadline = time.monotonic() + limit
            if question["kind"] == "flag":
                question_msg = await interaction.edit_original_response(
                    content=interaction.user.mention,
                    embed=self._flag_embed(interaction.user, question, limit))
            else:
                question_msg = await interaction.edit_original_response(
                    content=self._question_text(interaction.user, question, values, limit))

            def check(m: discord.Message) -> bool:
                return (m.author.id == uid and m.channel.id == interaction.channel_id
                        and self._parse_answer(m.content, question, values) is not None)

            picked: Optional[int] = None
            typed = ""
            timed_out = False
            try:
                msg = await self.bot.wait_for("message", check=check, timeout=limit + GRACE_SECONDS)
                answer_msgs.append(msg)
                picked = self._parse_answer(msg.content, question, values)
                typed = msg.content.strip()[:TRIVIA_CONFIG["max_answer_length"]]
                timed_out = time.monotonic() > deadline + GRACE_SECONDS
            except asyncio.TimeoutError:
                timed_out = True

            self._last[uid] = time.monotonic()
            self._active.discard(uid)

            if timed_out or picked is None:
                result = "timeout"
            else:
                result = "correct" if picked == correct_index else "wrong"
            try:
                res = await asyncio.to_thread(
                    tdb.settle, uid, result,
                    None if picked is None else (typed if question["kind"] == "flag" else values[picked]),
                    bonus_pct)
            except Exception as e:
                logger.error(f"Trivia settle failed: {e}", exc_info=True)
                result_msg = await interaction.channel.send(
                    f"{interaction.user.mention} Something went wrong saving that answer. Nothing was taken from you.")
                await asyncio.sleep(CLEANUP_DELAY)
                return

            result_msg = await interaction.channel.send(self._result_text(interaction.user, question, res))
            await asyncio.sleep(CLEANUP_DELAY)
        except Exception as e:
            logger.error(f"Trivia start error: {e}", exc_info=True)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Something went wrong. Please try again.", ephemeral=True)
                else:
                    await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)
            except discord.HTTPException:
                pass
        finally:
            self._active.discard(uid)
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                await self._safe_delete(question_msg)
            for m in answer_msgs:
                await self._safe_delete(m)
            await self._safe_delete(flag_msg)
            await self._safe_delete(result_msg)

    trivia = app_commands.Group(name="trivia", description="Answer questions to win credits")

    @trivia.command(name="play", description="Answer a trivia question to win credits")
    @app_commands.describe(game="Which game do you want to play?")
    @app_commands.choices(game=[
        app_commands.Choice(name="SAUR (yes/no questions)", value="saur"),
        app_commands.Choice(name="Guess the flag", value="flags"),
    ])
    async def trivia_play(self, interaction: Interaction, game: Optional[app_commands.Choice[str]] = None):
        await self.start(interaction, game.value if game else "saur")


async def setup(bot: commands.Bot):
    await bot.add_cog(Trivia(bot))