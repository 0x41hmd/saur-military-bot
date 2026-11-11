import json
import logging
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from database.db import get_db_connection, transaction
from utils import casino_db as cdb
from utils.settings import TRIVIA_CONFIG, TRIVIA_STREAK_REWARDS

logger = logging.getLogger(__name__)

YES_WORDS = {"yes", "y", "true"}
NO_WORDS = {"no", "n", "false"}

QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "trivia_questions.json"
_questions: Optional[List[Dict[str, Any]]] = None
_recent: Dict[int, List[int]] = {}


class TriviaError(Exception):
    expected = True


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_item(item: Any, qid: int) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise TriviaError("not an object")
    question, answer = item.get("question"), item.get("answer")
    if not isinstance(question, str) or not isinstance(answer, (str, bool)):
        raise TriviaError("needs a text `question` and an `answer`")

    question = " ".join(question.split())
    max_question = TRIVIA_CONFIG["max_question_length"]
    if not 5 <= len(question) <= max_question:
        raise TriviaError(f"the question must be between 5 and {max_question} characters")

    if isinstance(answer, bool):
        answer = "yes" if answer else "no"
    answer = answer.strip()
    if not answer:
        raise TriviaError("empty answer")

    wrong = item.get("wrong")
    if wrong is None and isinstance(item.get("options"), list):
        wrong = [str(o) for o in item["options"] if str(o).strip().lower() != answer.lower()]
    wrong = [str(w).strip() for w in (wrong or []) if str(w).strip()]

    if not wrong:
        low = answer.lower()
        if low not in YES_WORDS and low not in NO_WORDS:
            raise TriviaError("a question without `wrong` options must have a yes or no answer")
        kind, answer, choices = "yesno", ("yes" if low in YES_WORDS else "no"), []
    else:
        max_len, max_opts = TRIVIA_CONFIG["max_answer_length"], TRIVIA_CONFIG["max_options"]
        choices = [answer] + wrong
        if len(choices) > max_opts:
            raise TriviaError(f"at most {max_opts} options in total")
        if any(len(c) > max_len for c in choices):
            raise TriviaError(f"every option can be at most {max_len} characters")
        if len({c.lower() for c in choices}) != len(choices):
            raise TriviaError("two options are the same")
        kind = "choice"

    category = (item.get("category") or "").strip()[:40] or None
    return {"id": qid, "question": question, "kind": kind, "answer": answer,
            "choices": choices, "category": category}


def load_questions() -> List[Dict[str, Any]]:
    global _questions
    if _questions is not None:
        return _questions
    try:
        items = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            raise ValueError("the file must contain a list of questions")
    except Exception as e:
        logger.error(f"Could not read {QUESTIONS_FILE}: {e}")
        items = []

    out: List[Dict[str, Any]] = []
    seen = set()
    for i, item in enumerate(items, 1):
        try:
            q = _parse_item(item, len(out))
            key = q["question"].lower()
            if key in seen:
                raise TriviaError("duplicate question")
            seen.add(key)
            out.append(q)
        except TriviaError as e:
            logger.warning(f"trivia_questions.json entry #{i} skipped: {e}")
    _questions = out
    return out


def pick_question(uid: int) -> Optional[Dict[str, Any]]:
    qs = load_questions()
    if not qs:
        return None
    recent = _recent.setdefault(uid, [])
    pool = [q for q in qs if q["id"] not in recent] or qs
    q = random.choice(pool)
    recent.append(q["id"])
    keep = min(30, len(qs) // 2)
    recent[:] = recent[-keep:] if keep else []
    return q


def reward_for_streak(streak: int) -> int:
    reward = 0
    for need, amount in sorted(TRIVIA_STREAK_REWARDS):
        if streak >= need:
            reward = amount
    return reward


def next_tier(streak: int) -> Optional[Tuple[int, int]]:
    current = reward_for_streak(streak)
    for need, amount in sorted(TRIVIA_STREAK_REWARDS):
        if need > streak and amount > current:
            return need, amount
    return None


def with_bonus(base: int, bonus_pct: int) -> Tuple[int, int]:
    bonus = base * max(bonus_pct, 0) // 100
    return base + bonus, bonus


def settle(uid: int, result: str, picked: Optional[str], bonus_pct: int = 0) -> Dict[str, Any]:
    if result not in ("correct", "wrong", "timeout"):
        raise TriviaError(f"Unknown result: {result}")
    uid_s = str(uid)
    now = _iso_now()
    with transaction() as conn:
        acc = cdb._ensure_account(conn, uid_s)
        st = conn.execute("SELECT * FROM trivia_stats WHERE discord_id = ?", (uid_s,)).fetchone()
        prev_streak = st["streak"] if st else 0
        old_best = best = st["best_streak"] if st else 0
        best_at = st["best_streak_at"] if st else None
        earned = st["total_earned"] if st else 0

        base = bonus = reward = 0
        if result == "correct":
            streak = prev_streak + 1
            base = reward_for_streak(streak)
            reward, bonus = with_bonus(base, bonus_pct)
            if streak > best:
                best, best_at = streak, now
        else:
            streak = 0

        log_id = conn.execute(
            "INSERT INTO trivia_log (discord_id, result, picked, streak_after, reward, bonus_pct, bonus) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid_s, result, picked, streak, reward, bonus_pct if bonus else 0, bonus)).lastrowid

        balance = acc["balance"]
        if reward > 0:
            note = f"Trivia #{log_id} correct (streak {streak})" + (f", role bonus +{bonus_pct}%" if bonus else "")
            balance = cdb._move(conn, uid_s, reward, "trivia", note=note)

        conn.execute(
            """INSERT INTO trivia_stats (discord_id, answered, correct, streak, best_streak, best_streak_at,
                                         total_earned, last_played)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_id) DO UPDATE SET
                   answered = answered + 1, correct = correct + excluded.correct, streak = excluded.streak,
                   best_streak = excluded.best_streak, best_streak_at = excluded.best_streak_at,
                   total_earned = excluded.total_earned, last_played = excluded.last_played""",
            (uid_s, 1 if result == "correct" else 0, streak, best, best_at, earned + reward, now))
    return {"result": result, "streak": streak, "previous_streak": prev_streak, "best_streak": best,
            "new_best": result == "correct" and streak > old_best,
            "base": base, "bonus": bonus, "bonus_pct": bonus_pct if bonus else 0, "reward": reward,
            "balance": balance, "log_id": log_id}


def get_stats(uid: int) -> Dict[str, Any]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM trivia_stats WHERE discord_id = ?", (str(uid),)).fetchone()
    if row:
        return dict(row)
    return {"discord_id": str(uid), "answered": 0, "correct": 0, "streak": 0, "best_streak": 0,
            "best_streak_at": None, "total_earned": 0, "last_played": None}


def leaderboard(limit: int = 10) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trivia_stats WHERE best_streak > 0 "
            "ORDER BY best_streak DESC, COALESCE(best_streak_at, '9999') ASC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
