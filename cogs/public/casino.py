import asyncio
import io
import logging
import re
import time
from typing import Dict, List, Optional, Set

import discord
from discord import app_commands, Interaction, User
from discord.ext import commands

from utils import casino_db as cdb
from utils import casino_engine as eng
from utils import casino_visuals as vis
from utils import shop_db
from utils.helpers import create_embed, get_user_display_name, plain, require_permissions, split_signed
from utils.settings import CASINO_BET_LIMITS, CASINO_CONFIG, CASINO_COOLDOWN_GAMES, CASINO_CURRENCY, EMBED_COLOR_HEX

logger = logging.getLogger(__name__)

THEME = discord.Color(int(EMBED_COLOR_HEX.lstrip("#"), 16))

GAMES = [
    app_commands.Choice(name="Slots", value="slots"),
    app_commands.Choice(name="Roll (d100)", value="roll"),
    app_commands.Choice(name="Fortune Wheel", value="wheel"),
    app_commands.Choice(name="Poker", value="poker"),
    app_commands.Choice(name="Blackjack", value="blackjack"),
]


def fmt(n: int) -> str:
    return f"${n:,}"


def fmt_signed(n: int) -> str:
    return f"+{n:,}" if n > 0 else f"{n:,}"


_AMOUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmb]?)$")
_SUFFIX = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_amount(text: str, balance: Optional[int] = None) -> int:
    raw = (text or "").strip().lower().replace(",", "").replace("_", "")
    if raw in ("all", "max", "allin", "all-in"):
        if balance is None:
            raise ValueError("`all` can't be used here.")
        return balance
    if raw == "half":
        if balance is None:
            raise ValueError("`half` can't be used here.")
        return balance // 2
    m = _AMOUNT_RE.match(raw)
    if not m:
        raise ValueError("I couldn't read that amount. Use a number like `5000`, `10k`, `1.5m`, `half` or `all`.")
    value = int(float(m.group(1)) * _SUFFIX[m.group(2)])
    return value


def discord_ts(seconds_from_now: int, style: str = "R") -> str:
    return f"<t:{int(time.time()) + seconds_from_now}:{style}>"


def _result_color(profit: int) -> discord.Color:
    return THEME


def win_bonus_pct(user) -> int:
    try:
        return shop_db.win_bonus_for_roles([r.id for r in getattr(user, "roles", []) if r is not None])
    except Exception as e:
        logger.error(f"Win bonus lookup failed: {e}", exc_info=True)
        return 0


def daily_bonus_pct(user) -> int:
    try:
        return shop_db.daily_bonus_for_roles([r.id for r in getattr(user, "roles", []) if r is not None])
    except Exception as e:
        logger.error(f"Daily bonus lookup failed: {e}", exc_info=True)
        return 0


def boost(bet: int, payout: int, pct: int) -> int:
    if pct <= 0 or payout <= bet:
        return payout
    return payout + (payout - bet) * pct // 100


def _stats_line(acc: Dict) -> str:
    net = acc["total_won"] - acc["total_wagered"]
    return (f"{acc['games_played']:,} played \u00b7 {acc['games_won']:,} won \u00b7 {acc['games_lost']:,} lost \u00b7 "
            f"net {fmt_signed(net)}")


class HandView(discord.ui.View):
    def __init__(self, cog: "Casino", owner: User, game, game_id: int, balance: int):
        super().__init__(timeout=120)
        self.cog, self.owner, self.game, self.game_id, self.balance = cog, owner, game, game_id, balance
        self.message: Optional[discord.Message] = None
        self.frame = 0
        self.settled = False
        self.bonus = 0
        self.bonus_pct = 0

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("This isn't your hand.", ephemeral=True)
            return False
        return True


class BlackjackView(HandView):
    def __init__(self, cog: "Casino", owner: User, game: eng.Blackjack, game_id: int, balance: int):
        super().__init__(cog, owner, game, game_id, balance)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        g = self.game
        bet = g.hand.bet if not g.finished else 0
        self.hit_btn.disabled = not g.can_hit()
        self.stand_btn.disabled = g.finished
        self.double_btn.disabled = not g.can_double() or self.balance < bet
        self.split_btn.disabled = not g.can_split() or self.balance < bet
        self.surrender_btn.disabled = not g.can_surrender()

    def _payload(self, results: Optional[List[Dict]] = None, note: str = ""):
        self.frame += 1
        g = self.game
        png = vis.render_blackjack(g, reveal=g.finished, results=results)
        name = f"blackjack_{self.frame}.png"
        file = discord.File(io.BytesIO(png), filename=name)

        if results is None:
            embed = create_embed(title="Blackjack", user=self.owner, color=THEME, thumbnail=False)
            embed.description = note or "Your move. Dealer stands on 17, blackjack pays 3:2."
            embed.add_field(name="Total bet", value=fmt(g.total_bet), inline=True)
            embed.add_field(name="Wallet", value=fmt(self.balance), inline=True)
        else:
            payout = sum(r["payout"] for r in results) + self.bonus
            profit = payout - g.total_bet
            lines = []
            for i, r in enumerate(results, 1):
                tag = f"Hand {i}: " if len(results) > 1 else ""
                lines.append(f"{tag}**{r['outcome'].upper()}**: bet {r['bet']:,}, returned {r['payout']:,}")
            embed = create_embed(title=("Blackjack: " + ("You win!" if profit > 0 else "Push" if profit == 0 else "You lose")),
                                 user=self.owner, color=_result_color(profit), thumbnail=False)
            if self.bonus:
                lines.append(f"**Win bonus +{self.bonus_pct}%** ({self.bonus:,} extra)")
            embed.description = "\n".join(lines)
            embed.add_field(name="Profit", value=f"`{fmt_signed(profit)}`", inline=True)
            embed.add_field(name="Wallet", value=fmt(self.balance), inline=True)
        embed.set_image(url=f"attachment://{name}")
        return embed, file

    async def finish(self, interaction: Optional[Interaction], timed_out: bool = False) -> None:
        if self.settled:
            return
        self.settled = True
        g = self.game
        results = g.settle()
        raw = sum(r["payout"] for r in results)
        self.bonus_pct = (await asyncio.to_thread(win_bonus_pct, self.owner))
        payout = boost(g.total_bet, raw, self.bonus_pct)
        self.bonus = payout - raw
        profit = payout - g.total_bet
        outcome = "win" if profit > 0 else ("push" if profit == 0 else "loss")
        details = {"dealer": g.dealer, "hands": [{"cards": h.cards, **r} for h, r in zip(g.hands, results)]}
        if self.bonus:
            details["win_bonus_pct"] = self.bonus_pct
            details["win_bonus"] = self.bonus
        try:
            self.balance = (await asyncio.to_thread(cdb.settle_game, self.game_id, payout, outcome, details))
        except Exception as e:
            logger.error(f"Blackjack settle failed: {e}", exc_info=True)
        self.cog._busy.discard(self.owner.id)
        self.cog._start_cooldown(self.owner.id, "blackjack", self.bonus_pct > 0)
        for item in self.children:
            item.disabled = True
        embed, file = self._payload(results, note="Time ran out, you stood on every hand." if timed_out else "")
        try:
            if interaction and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, attachments=[file], view=self)
            elif self.message:
                await self.message.edit(embed=embed, attachments=[file], view=self)
        except discord.HTTPException as e:
            logger.warning(f"Blackjack final edit failed: {e}")
        await self.cog._log_big_win(self.owner, "Blackjack", profit, payout)
        self.stop()

    async def _advance(self, interaction: Interaction) -> None:
        if self.game.finished:
            await self.finish(interaction)
            return
        self._refresh_buttons()
        embed, file = self._payload()
        await interaction.response.edit_message(embed=embed, attachments=[file], view=self)

    async def on_timeout(self) -> None:
        if self.settled:
            return
        g = self.game
        try:
            while not g.finished:
                g.stand()
        except Exception as e:
            logger.error(f"Blackjack timeout stand failed: {e}", exc_info=True)
        await self.finish(None, timed_out=True)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="\U0001F0CF", row=0)
    async def hit_btn(self, interaction: Interaction, button: discord.ui.Button):
        self.game.hit()
        await self._advance(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.success, emoji="\u270B", row=0)
    async def stand_btn(self, interaction: Interaction, button: discord.ui.Button):
        self.game.stand()
        await self._advance(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.danger, emoji="\u2716\uFE0F", row=0)
    async def double_btn(self, interaction: Interaction, button: discord.ui.Button):
        try:
            self.balance = (await asyncio.to_thread(cdb.add_to_bet, self.game_id, self.game.hand.bet, "blackjack double"))
        except cdb.CasinoError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        self.game.double()
        await self._advance(interaction)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.secondary, emoji="\u2702\uFE0F", row=1)
    async def split_btn(self, interaction: Interaction, button: discord.ui.Button):
        try:
            self.balance = (await asyncio.to_thread(cdb.add_to_bet, self.game_id, self.game.hand.bet, "blackjack split"))
        except cdb.CasinoError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        self.game.split()
        await self._advance(interaction)

    @discord.ui.button(label="Surrender", style=discord.ButtonStyle.secondary, emoji="\U0001F3F3\uFE0F", row=1)
    async def surrender_btn(self, interaction: Interaction, button: discord.ui.Button):
        self.game.surrender()
        await self._advance(interaction)


class PokerView(HandView):
    def __init__(self, cog: "Casino", owner: User, game: eng.Poker, game_id: int, balance: int):
        super().__init__(cog, owner, game, game_id, balance)
        self.hold_buttons: List[discord.ui.Button] = []
        for i in range(5):
            btn = discord.ui.Button(label=f"Card {i + 1}", style=discord.ButtonStyle.secondary, row=0 if i < 3 else 1)
            btn.callback = self._make_toggle(i)
            self.hold_buttons.append(btn)
            self.add_item(btn)
        draw = discord.ui.Button(label="DRAW", style=discord.ButtonStyle.success, emoji="\U0001F0CF", row=1)
        draw.callback = self._draw
        self.add_item(draw)

    def _payload(self, final: bool = False):
        self.frame += 1
        g = self.game
        name_hand, payout = g.result() if final else (None, 0)
        payout += self.bonus if final else 0
        png = vis.render_poker(g.cards, g.held, final, name_hand, payout, g.bet)
        name = f"poker_{self.frame}.png"
        file = discord.File(io.BytesIO(png), filename=name)
        if not final:
            embed = create_embed(title="Poker: Jacks or Better", user=self.owner, color=THEME, thumbnail=False)
            embed.description = "Tap the cards you want to **hold**, then press **DRAW**. You only get one draw."
            embed.add_field(name="Bet", value=fmt(g.bet), inline=True)
            embed.add_field(name="Wallet", value=fmt(self.balance), inline=True)
        else:
            profit = payout - g.bet
            embed = create_embed(
                title="Poker: " + (name_hand if name_hand else "No winning hand"),
                user=self.owner, color=_result_color(profit), thumbnail=False)
            embed.description = (f"**{name_hand}** pays x{eng.POKER_PAYS[name_hand]}!" if name_hand
                                 else "Nothing this time. You need a pair of Jacks or better.")
            if self.bonus:
                embed.description += f"\n**Win bonus +{self.bonus_pct}%** - {self.bonus:,} extra"
            embed.add_field(name="Profit", value=f"`{fmt_signed(profit)}`", inline=True)
            embed.add_field(name="Wallet", value=fmt(self.balance), inline=True)
        embed.set_image(url=f"attachment://{name}")
        return embed, file

    def _make_toggle(self, i: int):
        async def toggle(interaction: Interaction):
            self.game.toggle(i)
            btn = self.hold_buttons[i]
            held = self.game.held[i]
            btn.style = discord.ButtonStyle.success if held else discord.ButtonStyle.secondary
            btn.label = f"Card {i + 1}" + (" \u2714" if held else "")
            embed, file = self._payload()
            await interaction.response.edit_message(embed=embed, attachments=[file], view=self)
        return toggle

    async def _draw(self, interaction: Interaction):
        await self._finish(interaction)

    async def _finish(self, interaction: Optional[Interaction]) -> None:
        if self.settled:
            return
        self.settled = True
        g = self.game
        g.draw()
        hand_name, raw = g.result()
        self.bonus_pct = (await asyncio.to_thread(win_bonus_pct, self.owner))
        payout = boost(g.bet, raw, self.bonus_pct)
        self.bonus = payout - raw
        profit = payout - g.bet
        try:
            self.balance = (await asyncio.to_thread(cdb.settle_game, self.game_id, payout, "win" if profit > 0 else "loss",
                {"hand": hand_name, "cards": g.cards, "held": g.held,
                 **({"win_bonus_pct": self.bonus_pct, "win_bonus": self.bonus} if self.bonus else {})}))
        except Exception as e:
            logger.error(f"Poker settle failed: {e}", exc_info=True)
        self.cog._busy.discard(self.owner.id)
        for item in self.children:
            item.disabled = True
        embed, file = self._payload(final=True)
        try:
            if interaction and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, attachments=[file], view=self)
            elif self.message:
                await self.message.edit(embed=embed, attachments=[file], view=self)
        except discord.HTTPException as e:
            logger.warning(f"Poker final edit failed: {e}")
        await self.cog._log_big_win(self.owner, "Poker", profit, payout)
        self.stop()

    async def on_timeout(self) -> None:
        await self._finish(None)


class Casino(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._busy: Set[int] = set()
        self._cooldowns: Dict[tuple, float] = {}
        self._win_streaks: Dict[int, int] = {}

    def _cooldown_left(self, uid: int, game: str) -> int:
        if game not in CASINO_COOLDOWN_GAMES:
            return 0
        return max(0, int(self._cooldowns.get((uid, game), 0) - time.time() + 0.999))

    def _start_cooldown(self, uid: int, game: str, perk: bool) -> None:
        secs = CASINO_CONFIG["perk_cooldown_seconds"] if perk else CASINO_CONFIG["cooldown_seconds"]
        self._cooldowns[(uid, game)] = time.time() + secs

    def _decide_win(self, uid: int, base_chance: int, perk: bool) -> bool:
        if self._win_streaks.get(uid, 0) >= CASINO_CONFIG["max_win_streak"]:
            return False
        chance = base_chance + (CASINO_CONFIG["perk_win_chance_bonus"] if perk else 0)
        return eng.rng.randint(1, 100) <= chance

    def _record_streak(self, uid: int, won: bool) -> None:
        self._win_streaks[uid] = self._win_streaks.get(uid, 0) + 1 if won else 0

    async def cog_load(self) -> None:
        try:
            (await asyncio.to_thread(cdb.refund_open_games))
        except Exception as e:
            logger.error(f"Casino startup refund failed: {e}", exc_info=True)

    async def _log_big_win(self, user: discord.abc.User, game: str, profit: int, payout: int,
                           label: Optional[str] = None) -> None:
        if profit < CASINO_CONFIG["jackpot_log_threshold"]:
            return
        logs_cog = self.bot.get_cog("Logs")
        if logs_cog:
            await logs_cog.log_action(
                action="Casino Jackpot Won",
                description=f"{get_user_display_name(user)} hit a big win at {game}",
                user=user,
                details={"Game": game, "Profit": fmt_signed(profit), "Payout": f"{payout:,}"},
            )

    async def _precheck(self, interaction: Interaction) -> bool:
        if interaction.user.id in self._busy:
            await interaction.response.send_message(
                "You're already in a game. Finish it first.", ephemeral=True)
            return False
        return True

    async def _validate_bet(self, interaction: Interaction, amount: Optional[str], balance: int, game: str) -> Optional[int]:
        if not amount:
            await interaction.response.send_message("You need to enter an `amount` to bet.", ephemeral=True)
            return None
        try:
            bet = parse_amount(amount, balance)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return None
        lo, hi = CASINO_BET_LIMITS[game]
        if amount.strip().lower() in ("all", "max", "allin", "all-in", "half") and bet > hi:
            bet = hi
        if bet < lo:
            await interaction.response.send_message(f"The minimum bet is {fmt(lo)}.", ephemeral=True)
            return None
        if bet > hi:
            await interaction.response.send_message(f"The maximum bet is {fmt(hi)}.", ephemeral=True)
            return None
        if bet > balance:
            await interaction.response.send_message(
                f"You only have {fmt(balance)}. Try `/dailyreward` or bet less.", ephemeral=True)
            return None
        return bet

    async def _animate(self, interaction: Interaction, user: User, title: str, spin_text: str,
                       renderer, final_embed: discord.Embed, name: str) -> None:
        loop = asyncio.get_running_loop()
        gif, png, seconds = await loop.run_in_executor(None, renderer)
        spin_embed = create_embed(title=title, description=spin_text, user=user, color=THEME, thumbnail=False)
        spin_embed.set_image(url=f"attachment://{name}.gif")
        await interaction.followup.send(embed=spin_embed, file=discord.File(io.BytesIO(gif), filename=f"{name}.gif"))
        await asyncio.sleep(max(1.5, seconds))
        final_embed.set_image(url=f"attachment://{name}_result.png")
        await interaction.edit_original_response(
            embed=final_embed, attachments=[discord.File(io.BytesIO(png), filename=f"{name}_result.png")])

    @app_commands.command(name="balance", description="Check your casino credits")
    @app_commands.describe(member="Whose wallet to look at (leave blank for yourself)")
    async def balance(self, interaction: Interaction, member: Optional[User] = None):
        target = member or interaction.user
        if target.bot:
            await interaction.response.send_message("Bots don't gamble.", ephemeral=True)
            return
        try:
            acc = (await asyncio.to_thread(cdb.get_account, target.id))
            daily = (await asyncio.to_thread(cdb.daily_status, target.id))
            used = (await asyncio.to_thread(cdb.wheel_spins_used, target.id))
        except Exception as e:
            logger.error(f"Balance error: {e}", exc_info=True)
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)
            return

        spins_left = max(0, CASINO_CONFIG["wheel_spins_per_day"] - used)
        embed = create_embed(title=f"{get_user_display_name(target)}'s Wallet", user=target, color=THEME)
        embed.description = f"## {fmt(acc['balance'])}\n{CASINO_CURRENCY}"
        embed.add_field(
            name="Daily reward",
            value=(f"Claimed \u00b7 resets {discord_ts(daily['seconds_left'])}" if daily["claimed"]
                   else "**Ready!** Use `/dailyreward`"),
            inline=True)
        embed.add_field(name="Streak", value=f"{acc['daily_streak']} day(s)", inline=True)
        d_pct, w_pct = (await asyncio.to_thread(daily_bonus_pct, target)), (await asyncio.to_thread(win_bonus_pct, target))
        if d_pct or w_pct:
            perks = ([f"Daily reward +{d_pct}%"] if d_pct else []) + ([f"Casino & trivia winnings +{w_pct}%"] if w_pct else [])
            embed.add_field(name="Role perks", value=" \u00b7 ".join(perks), inline=False)
        embed.add_field(name="Wheel spins left", value=f"{spins_left}/{CASINO_CONFIG['wheel_spins_per_day']}", inline=True)
        embed.add_field(name="Record", value=_stats_line(acc), inline=False)
        embed.add_field(name="Biggest win", value=fmt(acc["biggest_win"]), inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="dailyreward", description="Claim your daily casino credits")
    async def dailyreward(self, interaction: Interaction):
        try:
            pct = (await asyncio.to_thread(daily_bonus_pct, interaction.user))
            amount, streak, balance = (await asyncio.to_thread(cdb.claim_daily, interaction.user.id, pct))
        except cdb.DailyAlreadyClaimed as e:
            await interaction.response.send_message(
                f"You already claimed today's reward. Come back {discord_ts(e.seconds_left)}.", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Daily reward error: {e}", exc_info=True)
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)
            return

        cap = CASINO_CONFIG["daily_streak_cap"]
        next_amount = cdb.daily_reward_amount(streak + 1, pct)
        embed = create_embed(title="Daily Reward", user=interaction.user, color=THEME)
        embed.description = f"## +{fmt(amount)}" + (f"\n**Role bonus +{pct}%** included" if pct else "")
        embed.add_field(name="Streak", value=f"{streak} day(s)", inline=True)
        embed.add_field(name="Wallet", value=fmt(balance), inline=True)
        embed.add_field(
            name="Tomorrow",
            value=(f"{fmt(next_amount)} if you keep the streak" if streak < cap else f"{fmt(next_amount)} (max streak bonus!)"),
            inline=False)
        await interaction.response.send_message(embed=embed)

    casino = app_commands.Group(name="casino", description="Play casino games with your credits")

    @casino.command(name="play", description="Play a casino game")
    @app_commands.describe(
        game="Which game to play",
        amount="Your bet: 5000, 10k, 1.5m, half or all (not needed for the Fortune Wheel)")
    @app_commands.choices(game=GAMES)
    async def casino_play(self, interaction: Interaction, game: app_commands.Choice[str], amount: Optional[str] = None):
        g = game.value
        if not await self._precheck(interaction):
            return
        try:
            if g == "wheel":
                await self._play_wheel(interaction)
                return
            left = self._cooldown_left(interaction.user.id, g)
            if left:
                await interaction.response.send_message(
                    f"You're on cooldown for {game.name}. Try again {discord_ts(left)}.", ephemeral=True)
                return
            balance = (await asyncio.to_thread(cdb.get_balance, interaction.user.id))
            bet = await self._validate_bet(interaction, amount, balance, g)
            if bet is None:
                return
            if g == "slots":
                await self._play_slots(interaction, bet)
            elif g == "roll":
                await self._play_roll(interaction, bet)
            elif g == "poker":
                await self._play_poker(interaction, bet)
            elif g == "blackjack":
                await self._play_blackjack(interaction, bet)
        except cdb.InsufficientFunds as e:
            self._busy.discard(interaction.user.id)
            await self._safe_error(interaction, str(e))
        except Exception as e:
            self._busy.discard(interaction.user.id)
            logger.error(f"Casino play error ({g}): {e}", exc_info=True)
            await self._safe_error(interaction, "Something went wrong. If you lost credits, HQ can check the ledger.")

    async def _safe_error(self, interaction: Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _play_slots(self, interaction: Interaction, bet: int) -> None:
        pct = (await asyncio.to_thread(win_bonus_pct, interaction.user))
        won = self._decide_win(interaction.user.id, CASINO_CONFIG["slots_win_chance"], pct > 0)
        reels, mult, tier = eng.slots_outcome(won)
        base = int(bet * mult)
        payout = boost(bet, base, pct)
        bonus = payout - base
        profit = payout - bet
        outcome = "win" if profit > 0 else "loss"
        self._record_streak(interaction.user.id, profit > 0)
        _, balance = (await asyncio.to_thread(cdb.play_instant, interaction.user.id, "slots", bet, payout, outcome,
                                      {"reels": reels, "mult": mult, **({"win_bonus_pct": pct, "win_bonus": bonus} if bonus else {})}))
        self._busy.add(interaction.user.id)
        await interaction.response.defer()
        try:
            names = " | ".join(next(s[1] for s in eng.SLOT_SYMBOLS if s[0] == k) for k in reels)
            if tier == "JACKPOT":
                headline = "JACKPOT!!!"
            elif mult > 0:
                headline = f"{tier}! x{mult:g}"
            else:
                headline = "No match"
            final = create_embed(title=f"Slots: {headline}", user=interaction.user, color=_result_color(profit), thumbnail=False)
            final.description = f"**{names}**"
            final.add_field(name="Bet", value=fmt(bet), inline=True)
            final.add_field(name="Returned", value=fmt(payout), inline=True)
            final.add_field(name="Profit", value=f"`{fmt_signed(profit)}`", inline=True)
            if bonus:
                final.add_field(name="Win bonus", value=f"+{pct}% ({bonus:,} extra)", inline=True)
            final.add_field(name="Wallet", value=fmt(balance), inline=False)
            await self._animate(interaction, interaction.user, "Slots", "The reels are spinning...",
                                lambda: vis.render_slots(reels, mult > 1), final, "slots")
            self._start_cooldown(interaction.user.id, "slots", pct > 0)
            await self._log_big_win(interaction.user, "Slots", profit, payout)
        finally:
            self._busy.discard(interaction.user.id)

    async def _play_roll(self, interaction: Interaction, bet: int) -> None:
        pct = (await asyncio.to_thread(win_bonus_pct, interaction.user))
        won = self._decide_win(interaction.user.id, CASINO_CONFIG["roll_win_chance"], pct > 0)
        value = eng.roll_outcome(won)
        mult, tier = eng.roll_result(value)
        base = int(bet * mult)
        payout = boost(bet, base, pct)
        bonus = payout - base
        profit = payout - bet
        outcome = "win" if profit > 0 else "loss"
        self._record_streak(interaction.user.id, profit > 0)
        _, balance = (await asyncio.to_thread(cdb.play_instant, interaction.user.id, "roll", bet, payout, outcome,
                                      {"roll": value, "tier": tier, "mult": mult, **({"win_bonus_pct": pct, "win_bonus": bonus} if bonus else {})}))
        self._busy.add(interaction.user.id)
        await interaction.response.defer()
        try:
            final = create_embed(title=f"Roll: {value} ({tier})", user=interaction.user, color=_result_color(profit), thumbnail=False)
            final.description = ("Better luck next time. You need 51 or higher." if mult == 0
                                 else f"**x{mult:g}** payout!")
            final.add_field(name="Bet", value=fmt(bet), inline=True)
            final.add_field(name="Returned", value=fmt(payout), inline=True)
            final.add_field(name="Profit", value=f"`{fmt_signed(profit)}`", inline=True)
            if bonus:
                final.add_field(name="Win bonus", value=f"+{pct}% ({bonus:,} extra)", inline=True)
            final.add_field(name="Wallet", value=fmt(balance), inline=False)
            await self._animate(interaction, interaction.user, "Roll", "Rolling the dice...",
                                lambda: vis.render_roll(value), final, "roll")
            self._start_cooldown(interaction.user.id, "roll", pct > 0)
            await self._log_big_win(interaction.user, "Roll", profit, payout)
        finally:
            self._busy.discard(interaction.user.id)

    async def _play_wheel(self, interaction: Interaction) -> None:
        cost = CASINO_CONFIG["wheel_spin_cost"]
        limit = CASINO_CONFIG["wheel_spins_per_day"]
        try:
            game_id, slice_index, prize, balance = (await asyncio.to_thread(cdb.buy_wheel_spin_and_settle, interaction.user.id, eng.spin_wheel))
        except cdb.WheelLimitReached as e:
            await interaction.response.send_message(
                f"You used all {limit} wheel spins for today. They reset {discord_ts(e.seconds_left)}.", ephemeral=True)
            return
        except cdb.InsufficientFunds:
            bal = (await asyncio.to_thread(cdb.get_balance, interaction.user.id))
            await interaction.response.send_message(
                f"A spin costs {fmt(cost)} and you have {fmt(bal)}.", ephemeral=True)
            return

        self._busy.add(interaction.user.id)
        await interaction.response.defer()
        try:
            used = (await asyncio.to_thread(cdb.wheel_spins_used, interaction.user.id))
            profit = prize - cost
            if prize == eng.WHEEL_JACKPOT:
                headline = "JACKPOT!!!"
            elif prize == 0:
                headline = "BUST"
            else:
                headline = f"{prize:,} credits"
            final = create_embed(title=f"Fortune Wheel: {headline}", user=interaction.user,
                                 color=_result_color(profit), thumbnail=False)
            final.description = ("The wheel gave you nothing this time." if prize == 0
                                 else f"The wheel landed on **{prize:,}** credits!")
            final.add_field(name="Spin price", value=fmt(cost), inline=True)
            final.add_field(name="Prize", value=fmt(prize), inline=True)
            final.add_field(name="Profit", value=f"`{fmt_signed(profit)}`", inline=True)
            final.add_field(name="Wallet", value=fmt(balance), inline=True)
            final.add_field(name="Spins today", value=f"{used}/{limit}", inline=True)
            await self._animate(
                interaction, interaction.user, "Fortune Wheel",
                f"You paid {fmt(cost)} for a spin. Round and round she goes...",
                lambda: vis.render_wheel(slice_index), final, "wheel")
            await self._log_big_win(interaction.user, "Fortune Wheel", profit, prize)
        finally:
            self._busy.discard(interaction.user.id)

    async def _play_blackjack(self, interaction: Interaction, bet: int) -> None:
        game_id, balance = (await asyncio.to_thread(cdb.open_game, interaction.user.id, "blackjack", bet))
        self._busy.add(interaction.user.id)
        try:
            game = eng.Blackjack(bet)
            view = BlackjackView(self, interaction.user, game, game_id, balance)
            if game.finished:
                view.settled = False
                await interaction.response.defer()
                embed, file = view._payload(note="")
                msg = await interaction.followup.send(embed=embed, file=file, wait=True)
                view.message = msg
                await view.finish(None)
                return
            await interaction.response.defer()
            embed, file = view._payload()
            view.message = await interaction.followup.send(embed=embed, file=file, view=view, wait=True)
        except Exception:
            self._busy.discard(interaction.user.id)
            try:
                (await asyncio.to_thread(cdb.settle_game, game_id, bet, "refunded_error", {}))
            except Exception:
                pass
            raise

    async def _play_poker(self, interaction: Interaction, bet: int) -> None:
        game_id, balance = (await asyncio.to_thread(cdb.open_game, interaction.user.id, "poker", bet))
        self._busy.add(interaction.user.id)
        try:
            game = eng.Poker(bet)
            view = PokerView(self, interaction.user, game, game_id, balance)
            await interaction.response.defer()
            embed, file = view._payload()
            view.message = await interaction.followup.send(embed=embed, file=file, view=view, wait=True)
        except Exception:
            self._busy.discard(interaction.user.id)
            try:
                (await asyncio.to_thread(cdb.settle_game, game_id, bet, "refunded_error", {}))
            except Exception:
                pass
            raise

    @casino.command(name="manage", description="Add, remove or set a member's casino credits (HQ only)")
    @require_permissions(admin=True, acl="manage_casino")
    @app_commands.describe(
        user="The member",
        amount="+10k adds, -10k removes, 10k sets the exact balance (also 5000, 1.5m)",
        reason="Why (saved in the ledger and the audit log)")
    async def casino_manage(self, interaction: Interaction, user: User, amount: str,
                            reason: Optional[str] = None):
        if user.bot:
            await interaction.response.send_message(**plain("Bots don't have casino wallets."), ephemeral=True)
            return
        action, amount_text = split_signed(amount)
        try:
            value = parse_amount(amount_text)
        except ValueError as e:
            await interaction.response.send_message(**plain(str(e)), ephemeral=True)
            return
        if value < 0 or (action != "set" and value == 0):
            await interaction.response.send_message(**plain("The amount must be positive."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        try:
            before = (await asyncio.to_thread(cdb.get_balance, user.id))
            new_balance, delta = (await asyncio.to_thread(cdb.admin_adjust, user.id, action, value, interaction.user.id, reason))
        except Exception as e:
            logger.error(f"Casino manage error: {e}", exc_info=True)
            await interaction.followup.send(**plain("A database error occurred. Please contact HQ technical staff."), ephemeral=True)
            return

        titles = {"add": "Casino Credits Added", "remove": "Casino Credits Removed", "set": "Casino Credits Updated"}
        logs_cog = self.bot.get_cog("Logs")
        if logs_cog:
            details = {"Change": fmt_signed(delta), "Before": f"{before:,}", "New Balance": f"{new_balance:,}"}
            if reason:
                details["Reason"] = reason
            await logs_cog.log_action(
                action=titles[action],
                description=f"{get_user_display_name(user)}'s casino credits were adjusted",
                user=interaction.user, target_user=user, details=details)

        note = ""
        if action == "remove" and value > before:
            note = f" (they only had {before:,}, so it was capped)"
        text = (f"Casino credits adjusted for {user.mention}.\n"
                f"Change: `{fmt_signed(delta)}`{note} \u00b7 New balance: `{new_balance:,}`")
        if reason:
            text += f" (reason: {reason})"
        await interaction.followup.send(**plain(text))


async def setup(bot: commands.Bot):
    await bot.add_cog(Casino(bot))
    asyncio.get_running_loop().run_in_executor(None, vis.prewarm)
