import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from database.db import get_db_connection, transaction
from utils import casino_db as cdb
from utils.settings import SHOP_CONFIG

logger = logging.getLogger(__name__)


class ShopError(Exception):
    expected = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def add_item(category: str, name: str, price: int, unit: str = "per one", description: Optional[str] = None,
             stock: Optional[int] = None, role_id: Optional[int] = None, duration_days: Optional[int] = None,
             win_bonus: int = 0, daily_bonus: int = 0) -> int:
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM shop_items WHERE name = ?", (name,)).fetchone():
            raise ShopError(f"There is already an item called **{name}**.")
        cur = conn.execute(
            "INSERT INTO shop_items (category, name, description, price, unit, stock, role_id, duration_days, win_bonus, daily_bonus) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (category, name, description, price, unit, stock, str(role_id) if role_id else None, duration_days,
             win_bonus, daily_bonus))
        return cur.lastrowid


def find_item(name: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM shop_items WHERE name = ?", (name.strip(),)).fetchone()
    return dict(row) if row else None


def list_items(category: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM shop_items WHERE category = ? COLLATE NOCASE ORDER BY price DESC, name", (category,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM shop_items ORDER BY category COLLATE NOCASE, price DESC, name").fetchall()
    return [dict(r) for r in rows]


def categories() -> List[str]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT DISTINCT category FROM shop_items ORDER BY category COLLATE NOCASE").fetchall()
    found = [r["category"] for r in rows]
    for c in SHOP_CONFIG["default_categories"]:
        if c.lower() not in (f.lower() for f in found):
            found.append(c)
    return found


_EDITABLE = {"category", "name", "description", "price", "unit", "stock", "role_id", "duration_days", "win_bonus", "daily_bonus"}


def update_item(item_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in _EDITABLE}
    if not fields:
        return
    with transaction() as conn:
        if "name" in fields and conn.execute(
                "SELECT 1 FROM shop_items WHERE name = ? AND id != ?", (fields["name"], item_id)).fetchone():
            raise ShopError(f"There is already an item called **{fields['name']}**.")
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE shop_items SET {sets} WHERE id = ?", (*fields.values(), item_id))


def _best_bonus(column: str, role_ids) -> int:
    ids = [str(r) for r in role_ids]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with get_db_connection() as conn:
        row = conn.execute(
            f"SELECT MAX({column}) FROM shop_items WHERE {column} > 0 AND role_id IN ({marks})", ids).fetchone()
    return int(row[0] or 0)


def win_bonus_for_roles(role_ids) -> int:
    return min(_best_bonus("win_bonus", role_ids), SHOP_CONFIG["max_win_bonus"])


def daily_bonus_for_roles(role_ids) -> int:
    return min(_best_bonus("daily_bonus", role_ids), SHOP_CONFIG["max_daily_bonus"])


def add_stock(item_id: int, amount: int) -> Optional[int]:
    with transaction() as conn:
        row = conn.execute("SELECT stock FROM shop_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise ShopError("That item no longer exists.")
        if row["stock"] is None:
            return None
        new = row["stock"] + amount
        conn.execute("UPDATE shop_items SET stock = ? WHERE id = ?", (new, item_id))
        return new


def delete_item(item_id: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))


def place_order(uid: int, item_id: int, quantity: int) -> Tuple[Dict[str, Any], int]:
    uid_s = str(uid)
    with transaction() as conn:
        item = conn.execute("SELECT * FROM shop_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            raise ShopError("That item is no longer in the shop.")
        if quantity < 1:
            raise ShopError("The quantity must be at least 1.")
        if quantity > SHOP_CONFIG["max_quantity"]:
            raise ShopError(f"The most you can buy at once is {SHOP_CONFIG['max_quantity']:,}.")
        if item["role_id"]:
            if quantity != 1:
                raise ShopError("Roles can only be bought one at a time.")
            held = conn.execute(
                "SELECT 1 FROM shop_orders WHERE discord_id = ? AND item_id = ? AND ("
                " status = 'pending' OR (status = 'fulfilled' AND role_removed = 0 AND "
                "   (expires_at IS NULL OR expires_at > ?))) LIMIT 1",
                (uid_s, item_id, _iso(_now()))).fetchone()
            if held:
                raise ShopError("You already have this role (or an order for it is waiting for HQ).")
        if item["stock"] is not None and item["stock"] < quantity:
            raise ShopError("Out of stock." if item["stock"] == 0 else f"Only {item['stock']:,} left in stock.")

        total = item["price"] * quantity
        new_balance = cdb._move(conn, uid_s, -total, "shop_purchase",
                                note=f"Shop: {quantity:,} x {item['name']}")
        if item["stock"] is not None:
            conn.execute("UPDATE shop_items SET stock = stock - ? WHERE id = ?", (quantity, item_id))
        cur = conn.execute(
            "INSERT INTO shop_orders (discord_id, item_id, item_name, category, quantity, unit_price, total, unit, "
            "role_id, duration_days, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uid_s, item_id, item["name"], item["category"], quantity, item["price"], total, item["unit"],
             item["role_id"], item["duration_days"], _iso(_now())))
        order = dict(conn.execute("SELECT * FROM shop_orders WHERE id = ?", (cur.lastrowid,)).fetchone())
    return order, new_balance


def get_order(order_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM shop_orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row else None


def set_order_message(order_id: int, channel_id: int, message_id: int) -> None:
    with transaction() as conn:
        conn.execute("UPDATE shop_orders SET channel_id = ?, message_id = ? WHERE id = ?",
                     (str(channel_id), str(message_id), order_id))


def resolve_order(order_id: int, confirm: bool, staff_id: int, note: Optional[str] = None) -> Dict[str, Any]:
    with transaction() as conn:
        o = conn.execute("SELECT * FROM shop_orders WHERE id = ?", (order_id,)).fetchone()
        if not o:
            raise ShopError("That order does not exist.")
        if o["status"] != "pending":
            raise ShopError(f"Order #{order_id} was already {o['status']}.")
        now = _now()
        if confirm:
            expires = None
            if o["role_id"] and o["duration_days"]:
                expires = _iso(now + timedelta(days=o["duration_days"]))
            conn.execute(
                "UPDATE shop_orders SET status = 'fulfilled', handled_by = ?, handled_at = ?, note = ?, expires_at = ? WHERE id = ?",
                (str(staff_id), _iso(now), note, expires, order_id))
        else:
            cdb._move(conn, o["discord_id"], o["total"], "shop_refund", actor=str(staff_id),
                      note=f"Shop order #{order_id} rejected" + (f": {note}" if note else ""))
            if o["item_id"]:
                conn.execute("UPDATE shop_items SET stock = stock + ? WHERE id = ? AND stock IS NOT NULL",
                             (o["quantity"], o["item_id"]))
            conn.execute(
                "UPDATE shop_orders SET status = 'rejected', handled_by = ?, handled_at = ?, note = ? WHERE id = ?",
                (str(staff_id), _iso(now), note, order_id))
        return dict(conn.execute("SELECT * FROM shop_orders WHERE id = ?", (order_id,)).fetchone())


def user_orders(uid: int, limit: int = 10) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM shop_orders WHERE discord_id = ? ORDER BY id DESC LIMIT ?",
                            (str(uid), limit)).fetchall()
    return [dict(r) for r in rows]


def pending_orders(limit: int = 20) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM shop_orders WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def due_role_expiries() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM shop_orders WHERE status = 'fulfilled' AND role_id IS NOT NULL AND role_removed = 0 "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (_iso(_now()),)).fetchall()
    return [dict(r) for r in rows]


def mark_role_removed(order_id: int) -> None:
    with transaction() as conn:
        conn.execute("UPDATE shop_orders SET role_removed = 1 WHERE id = ?", (order_id,))


