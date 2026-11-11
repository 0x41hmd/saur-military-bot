import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from database.db import get_db_connection, transaction
from utils.settings import CASINO_CONFIG

logger = logging.getLogger(__name__)


class CasinoError(Exception):
    expected = True


class InsufficientFunds(CasinoError):
    def __init__(self, balance: int, needed: int):
        self.balance, self.needed = balance, needed
        super().__init__(f"You need {needed:,} credits but only have {balance:,}.")


class DailyAlreadyClaimed(CasinoError):
    def __init__(self, seconds_left: int):
        self.seconds_left = seconds_left
        super().__init__("You already claimed today's reward.")


class WheelLimitReached(CasinoError):
    def __init__(self, seconds_left: int):
        self.seconds_left = seconds_left
        super().__init__("You used all your wheel spins for today.")


def _offset() -> timedelta:
    return timedelta(hours=CASINO_CONFIG["day_reset_utc_offset"])


def casino_day(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now + _offset()).strftime("%Y-%m-%d")


def seconds_until_reset(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    local = now + _offset()
    next_midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_midnight - local).total_seconds()))


def _ensure_account(conn, uid: str) -> Any:
    row = conn.execute("SELECT * FROM casino_accounts WHERE discord_id = ?", (uid,)).fetchone()
    if row:
        return row
    start = int(CASINO_CONFIG["starting_balance"])
    conn.execute("INSERT INTO casino_accounts (discord_id, balance) VALUES (?, ?)", (uid, start))
    conn.execute(
        "INSERT INTO casino_transactions (discord_id, kind, amount, balance_after, note) "
        "VALUES (?, 'start_balance', ?, ?, 'Welcome bonus')",
        (uid, start, start),
    )
    return conn.execute("SELECT * FROM casino_accounts WHERE discord_id = ?", (uid,)).fetchone()


def _move(conn, uid: str, delta: int, kind: str, game_id: Optional[int] = None,
          actor: Optional[str] = None, note: Optional[str] = None) -> int:
    acc = _ensure_account(conn, uid)
    new_balance = acc["balance"] + delta
    if new_balance < 0:
        raise InsufficientFunds(acc["balance"], -delta)
    conn.execute(
        "UPDATE casino_accounts SET balance = ?, updated_at = datetime('now') WHERE discord_id = ?",
        (new_balance, uid),
    )
    conn.execute(
        "INSERT INTO casino_transactions (discord_id, kind, amount, balance_after, game_id, actor_id, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, kind, delta, new_balance, game_id, actor, note),
    )
    return new_balance


def _record_result(conn, uid: str, bet: int, payout: int) -> None:
    profit = payout - bet
    conn.execute(
        """UPDATE casino_accounts SET
               games_played  = games_played + 1,
               games_won     = games_won  + ?,
               games_lost    = games_lost + ?,
               total_wagered = total_wagered + ?,
               total_won     = total_won + ?,
               biggest_win   = MAX(biggest_win, ?)
           WHERE discord_id = ?""",
        (1 if profit > 0 else 0, 1 if profit < 0 else 0, bet, payout, max(profit, 0), uid),
    )


def get_account(uid: int) -> Dict[str, Any]:
    with transaction() as conn:
        return dict(_ensure_account(conn, str(uid)))


def get_balance(uid: int) -> int:
    return get_account(uid)["balance"]


def peek_account(uid: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM casino_accounts WHERE discord_id = ?", (str(uid),)).fetchone()
    return dict(row) if row else None


def daily_reward_amount(streak: int, bonus_pct: int = 0) -> int:
    bonus_days = min(max(streak - 1, 0), CASINO_CONFIG["daily_streak_cap"] - 1)
    amount = CASINO_CONFIG["daily_base"] + bonus_days * CASINO_CONFIG["daily_streak_bonus"]
    return amount + amount * max(bonus_pct, 0) // 100


def claim_daily(uid: int, bonus_pct: int = 0) -> Tuple[int, int, int]:
    uid_s = str(uid)
    now = datetime.now(timezone.utc)
    today = casino_day(now)
    yesterday = casino_day(now - timedelta(days=1))
    with transaction() as conn:
        acc = _ensure_account(conn, uid_s)
        if acc["last_daily"] == today:
            raise DailyAlreadyClaimed(seconds_until_reset(now))
        streak = acc["daily_streak"] + 1 if acc["last_daily"] == yesterday else 1
        amount = daily_reward_amount(streak, bonus_pct)
        note = f"Daily reward (streak {streak})" + (f", role bonus +{bonus_pct}%" if bonus_pct else "")
        balance = _move(conn, uid_s, amount, "daily", note=note)
        conn.execute(
            "UPDATE casino_accounts SET last_daily = ?, daily_streak = ? WHERE discord_id = ?",
            (today, streak, uid_s),
        )
    return amount, streak, balance


def daily_status(uid: int) -> Dict[str, Any]:
    acc = get_account(uid)
    claimed = acc["last_daily"] == casino_day()
    return {"claimed": claimed, "streak": acc["daily_streak"], "seconds_left": seconds_until_reset()}


def open_game(uid: int, game: str, bet: int) -> Tuple[int, int]:
    uid_s = str(uid)
    with transaction() as conn:
        _ensure_account(conn, uid_s)
        cur = conn.execute(
            "INSERT INTO casino_games (discord_id, game, bet, status) VALUES (?, ?, ?, 'open')",
            (uid_s, game, bet),
        )
        game_id = cur.lastrowid
        balance = _move(conn, uid_s, -bet, "bet", game_id=game_id, note=f"{game} stake")
    return game_id, balance


def add_to_bet(game_id: int, extra: int, note: str) -> int:
    with transaction() as conn:
        game = conn.execute("SELECT * FROM casino_games WHERE id = ? AND status = 'open'", (game_id,)).fetchone()
        if not game:
            raise CasinoError("This game is already finished.")
        balance = _move(conn, game["discord_id"], -extra, "bet", game_id=game_id, note=note)
        conn.execute("UPDATE casino_games SET bet = bet + ? WHERE id = ?", (extra, game_id))
    return balance


def settle_game(game_id: int, payout: int, outcome: str, details: Optional[Dict[str, Any]] = None) -> int:
    with transaction() as conn:
        game = conn.execute("SELECT * FROM casino_games WHERE id = ?", (game_id,)).fetchone()
        if not game or game["status"] != "open":
            raise CasinoError("This game is already finished.")
        uid = game["discord_id"]
        balance = _ensure_account(conn, uid)["balance"]
        if payout > 0:
            balance = _move(conn, uid, payout, "payout", game_id=game_id, note=f"{game['game']} {outcome}")
        conn.execute(
            """UPDATE casino_games SET payout = ?, profit = ?, outcome = ?, details = ?,
                   status = 'settled', settled_at = datetime('now') WHERE id = ?""",
            (payout, payout - game["bet"], outcome, json.dumps(details or {}), game_id),
        )
        _record_result(conn, uid, game["bet"], payout)
    return balance


def play_instant(uid: int, game: str, bet: int, payout: int, outcome: str,
                 details: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
    game_id, _ = open_game(uid, game, bet)
    balance = settle_game(game_id, payout, outcome, details)
    return game_id, balance


def refund_open_games() -> int:
    refunded = 0
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM casino_games WHERE status = 'open'").fetchall()
        for g in rows:
            if g["bet"] > 0:
                _move(conn, g["discord_id"], g["bet"], "refund", game_id=g["id"], note="Bot restarted mid-game")
            conn.execute(
                "UPDATE casino_games SET status = 'refunded', outcome = 'refunded', settled_at = datetime('now') WHERE id = ?",
                (g["id"],),
            )
            refunded += 1
    if refunded:
        logger.info(f"Casino: refunded {refunded} unfinished game(s)")
    return refunded


def wheel_spins_used(uid: int) -> int:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT spins FROM casino_wheel_spins WHERE discord_id = ? AND spin_day = ?",
            (str(uid), casino_day()),
        ).fetchone()
    return row["spins"] if row else 0


def buy_wheel_spin_and_settle(uid: int, prize_picker) -> Tuple[int, int, int, int]:
    uid_s = str(uid)
    cost = CASINO_CONFIG["wheel_spin_cost"]
    limit = CASINO_CONFIG["wheel_spins_per_day"]
    day = casino_day()
    with transaction() as conn:
        _ensure_account(conn, uid_s)
        row = conn.execute(
            "SELECT spins FROM casino_wheel_spins WHERE discord_id = ? AND spin_day = ?", (uid_s, day)
        ).fetchone()
        used = row["spins"] if row else 0
        if used >= limit:
            raise WheelLimitReached(seconds_until_reset())

        cur = conn.execute(
            "INSERT INTO casino_games (discord_id, game, bet, status) VALUES (?, 'wheel', ?, 'open')", (uid_s, cost)
        )
        game_id = cur.lastrowid
        _move(conn, uid_s, -cost, "wheel_spin", game_id=game_id, note=f"Wheel spin {used + 1}/{limit}")

        slice_index, prize = prize_picker()
        balance = _ensure_account(conn, uid_s)["balance"]
        if prize > 0:
            balance = _move(conn, uid_s, prize, "payout", game_id=game_id, note="Wheel prize")
        outcome = "win" if prize > cost else ("push" if prize == cost else "loss")
        conn.execute(
            """UPDATE casino_games SET payout = ?, profit = ?, outcome = ?, details = ?,
                   status = 'settled', settled_at = datetime('now') WHERE id = ?""",
            (prize, prize - cost, outcome, json.dumps({"slice": slice_index}), game_id),
        )
        _record_result(conn, uid_s, cost, prize)
        conn.execute(
            """INSERT INTO casino_wheel_spins (discord_id, spin_day, spins) VALUES (?, ?, 1)
               ON CONFLICT(discord_id, spin_day) DO UPDATE SET spins = spins + 1""",
            (uid_s, day),
        )
    return game_id, slice_index, prize, balance


def admin_adjust(uid: int, action: str, amount: int, actor_id: int, reason: Optional[str]) -> Tuple[int, int]:
    uid_s = str(uid)
    with transaction() as conn:
        current = _ensure_account(conn, uid_s)["balance"]
        if action == "add":
            delta = amount
        elif action == "remove":
            delta = -min(amount, current)
        elif action == "set":
            delta = amount - current
        else:
            raise CasinoError(f"Unknown action: {action}")
        if delta == 0:
            return current, 0
        new_balance = _move(conn, uid_s, delta, f"admin_{action}", actor=str(actor_id), note=reason)
    return new_balance, delta


def top_balances(limit: int = 10) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT discord_id, balance FROM casino_accounts WHERE balance > 0 ORDER BY balance DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


