import asyncio
import gzip
import json
import logging
import os
import queue
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymysql
from dotenv import load_dotenv
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.converters import conversions

from database.errors import Error, OperationalError, InterfaceError
from utils.settings import PERFORMANCE_CONFIG

load_dotenv()
logger = logging.getLogger(__name__)

DB_DIR = Path("data")
BACKUP_DIR = DB_DIR / "backups"

IDLE_PING_AFTER = 20.0
_LOST_CODES = {2006, 2013, 2014, 2027, 2055, 1927, 1317}


class Row:
    __slots__ = ("_idx", "_names", "_values")

    def __init__(self, idx: Dict[str, int], names: List[str], values: tuple):
        self._idx, self._names, self._values = idx, names, values

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        i = self._idx.get(key)
        if i is None and isinstance(key, str):
            i = self._idx.get(key.lower())
        if i is None:
            raise IndexError("No item with that key")
        return self._values[i]

    def get(self, key, default=None):
        try:
            return self[key]
        except IndexError:
            return default

    def keys(self) -> List[str]:
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        return isinstance(other, Row) and self._values == other._values and self._names == other._names

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


class Result:
    def __init__(self, cur, rows: Optional[List[Row]] = None):
        self.lastrowid = getattr(cur, "lastrowid", None)
        self.rowcount = getattr(cur, "rowcount", -1)
        self._rows = rows or []
        self._pos = 0

    def fetchone(self) -> Optional[Row]:
        if self._pos < len(self._rows):
            self._pos += 1
            return self._rows[self._pos - 1]
        return None

    def fetchall(self) -> List[Row]:
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _Cursor:
    def __init__(self, conn: "Connection"):
        self._conn = conn
        self._res: Optional[Result] = None
        self.lastrowid = None
        self.rowcount = -1

    def execute(self, sql, params=()):
        self._res = self._conn.execute(sql, params)
        self.lastrowid, self.rowcount = self._res.lastrowid, self._res.rowcount
        return self

    def fetchone(self):
        return self._res.fetchone() if self._res else None

    def fetchall(self):
        return self._res.fetchall() if self._res else []

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        pass


_QUOTE_RE = re.compile(r"""'(?:[^'\\]|''|\\.)*'|"(?:[^"\\]|""|\\.)*"|`[^`]*`|--[^\n]*""", re.S)
_NOW_RE = re.compile(r"datetime\(\s*'now'\s*\)", re.I)
_SELECT_LOCK_SKIP = re.compile(r"\b(GROUP\s+BY|DISTINCT|UNION|HAVING|COUNT|SUM|AVG|MAX|MIN|GROUP_CONCAT)\b", re.I)


def _fix_minmax(s: str) -> str:
    pat = re.compile(r"\b(MAX|MIN)\s*\(", re.I)
    out, pos = [], 0
    while True:
        m = pat.search(s, pos)
        if not m:
            out.append(s[pos:])
            return "".join(out)
        i, depth, commas = m.end(), 1, 0
        while i < len(s) and depth:
            ch = s[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 1:
                commas += 1
            i += 1
        name = m.group(1).upper()
        if commas:
            name = "GREATEST" if name == "MAX" else "LEAST"
        out.append(s[pos:m.start()])
        out.append(f"{name}({_fix_minmax(s[m.end():i - 1])})")
        pos = i


@lru_cache(maxsize=1024)
def _translate(sql: str) -> str:
    sql = _NOW_RE.sub("UTC_TIMESTAMP()", sql)

    stash: List[str] = []

    def hide(m):
        text = m.group(0)
        if text.startswith("--"):
            return " "
        stash.append(text)
        return f"\x00{len(stash) - 1}\x00"

    s = _QUOTE_RE.sub(hide, sql)

    s = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", s, flags=re.I)
    s = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "REPLACE INTO", s, flags=re.I)
    s = re.sub(r"\s+COLLATE\s+NOCASE\b", "", s, flags=re.I)
    s = re.sub(r"\bON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET\b", "ON DUPLICATE KEY UPDATE", s, flags=re.I)
    s = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", s, flags=re.I)
    s = s.replace("%", "%%")
    s = s.replace("?", "%s")
    s = re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", r"%(\1)s", s)
    s = _fix_minmax(s)
    s = re.sub(r"(?<!PRIMARY )(?<!FOREIGN )(?<!UNIQUE )(?<!DUPLICATE )\bkey\b", "`key`", s, flags=re.I)
    s = re.sub(r"\brank\b", "`rank`", s, flags=re.I)
    s = re.sub(r"\blead\b", "`lead`", s, flags=re.I)

    def restore(m):
        text = stash[int(m.group(1))]
        return text if text.startswith("`") else text.replace("%", "%%")

    return re.sub(r"\x00(\d+)\x00", restore, s)


def _prep_param(v: Any) -> Any:
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 10 ** 15:
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc)
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return v


def _prep_params(params):
    if params is None:
        return ()
    if isinstance(params, dict):
        return {k: _prep_param(v) for k, v in params.items()}
    return tuple(_prep_param(v) for v in params)


def _is_lost(e: Exception) -> bool:
    if isinstance(e, InterfaceError):
        return True
    code = e.args[0] if e.args and isinstance(e.args[0], int) else None
    return code in _LOST_CODES


def _num(text: str):
    if "." not in text:
        return int(text)
    f = float(text)
    return int(f) if f.is_integer() else f


def _converters() -> dict:
    conv = conversions.copy()
    for t in (FIELD_TYPE.DATETIME, FIELD_TYPE.TIMESTAMP, FIELD_TYPE.DATE, FIELD_TYPE.TIME):
        conv[t] = lambda v: v
    conv[FIELD_TYPE.DECIMAL] = conv[FIELD_TYPE.NEWDECIMAL] = _num
    return conv


_CONV = _converters()


def _settings() -> dict:
    missing = [k for k in ("TIDB_HOST", "TIDB_USER", "TIDB_PASSWORD") if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            "Missing TiDB settings: " + ", ".join(missing) +
            ". Add them to .env (or the host's environment variables), see .env.example.")
    cfg = dict(
        host=os.environ["TIDB_HOST"].strip(),
        port=int(os.getenv("TIDB_PORT", "4000")),
        user=os.environ["TIDB_USER"].strip(),
        password=os.environ["TIDB_PASSWORD"],
        database=os.getenv("TIDB_DATABASE", "test").strip(),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        client_flag=CLIENT.FOUND_ROWS,
        conv=_CONV,
        ssl_verify_cert=True,
        ssl_verify_identity=True,
    )
    ca = os.getenv("TIDB_SSL_CA", "").strip()
    if not ca:
        try:
            import certifi
            ca = certifi.where()
        except ImportError:
            ca = ""
    if ca:
        cfg["ssl_ca"] = ca
    return cfg


def _new_raw():
    raw = pymysql.connect(**_settings())
    with raw.cursor() as cur:
        cur.execute("SET time_zone = '+00:00', group_concat_max_len = 1000000, "
                    "sql_mode = REPLACE(@@sql_mode, 'ONLY_FULL_GROUP_BY', '')")
    return raw


class Connection:
    def __init__(self):
        self._raw = _new_raw()
        self._in_txn = False
        self.broken = False
        self.last_used = time.monotonic()

    def ping(self) -> bool:
        try:
            self._raw.ping(reconnect=True)
            self._in_txn = False
            return True
        except Exception:
            self.broken = True
            return False

    def _reconnect(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass
        self._raw = _new_raw()
        self._in_txn = False
        logger.warning("Database connection was lost, reconnected")

    def execute(self, sql: str, params=()) -> Result:
        stripped = sql.strip()
        up = stripped.upper()
        if up.startswith("PRAGMA"):
            m = re.match(r"PRAGMA\s+foreign_keys\s*=\s*(ON|OFF|1|0)", up)
            if m:
                on = m.group(1) in ("ON", "1")
                return self._run(f"SET FOREIGN_KEY_CHECKS = {1 if on else 0}", ())
            return Result(None)
        query = _translate(stripped.rstrip(";"))
        if (self._in_txn and up.startswith("SELECT") and not _SELECT_LOCK_SKIP.search(query)
                and "FOR UPDATE" not in query.upper()):
            query += " FOR UPDATE"
        return self._run(query, _prep_params(params))

    def _run(self, query: str, args) -> Result:
        for attempt in (0, 1):
            try:
                with self._raw.cursor() as cur:
                    cur.execute(query, args)
                    rows = None
                    if cur.description:
                        names = [d[0] for d in cur.description]
                        idx: Dict[str, int] = {}
                        for i, n in enumerate(names):
                            idx.setdefault(n, i)
                            idx.setdefault(n.lower(), i)
                        rows = [Row(idx, names, tuple(r)) for r in cur.fetchall()]
                    self.last_used = time.monotonic()
                    return Result(cur, rows)
            except (OperationalError, InterfaceError) as e:
                if attempt == 0 and _is_lost(e) and not self._in_txn:
                    self._reconnect()
                    continue
                if _is_lost(e):
                    self.broken = True
                raise
        raise RuntimeError("unreachable")

    def execute_raw(self, sql: str) -> Result:
        return self._run(sql, ())

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def begin(self) -> None:
        try:
            self._raw.begin()
        except (OperationalError, InterfaceError) as e:
            if not _is_lost(e):
                raise
            self._reconnect()
            self._raw.begin()
        self._in_txn = True

    def commit(self) -> None:
        try:
            self._raw.commit()
        finally:
            self._in_txn = False

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except Exception:
            self.broken = True
        finally:
            self._in_txn = False

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass


class ConnectionPool:
    def __init__(self, maxsize: int, timeout: float):
        self._maxsize, self._timeout = maxsize, timeout
        self._idle: queue.Queue = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False

    def _make(self) -> Connection:
        try:
            return Connection()
        except Exception:
            with self._lock:
                self._created -= 1
            raise

    def acquire(self) -> Connection:
        if self._closed:
            raise RuntimeError("Connection pool is closed")
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            if conn.broken or (time.monotonic() - conn.last_used > IDLE_PING_AFTER and not conn.ping()):
                self._discard(conn)
                continue
            return conn
        with self._lock:
            if self._created < self._maxsize:
                self._created += 1
                make = True
            else:
                make = False
        if make:
            return self._make()
        try:
            conn = self._idle.get(timeout=self._timeout)
        except queue.Empty:
            raise TimeoutError(f"Could not get a database connection within {self._timeout}s")
        if conn.broken or not conn.ping():
            self._discard(conn)
            with self._lock:
                self._created += 1
            return self._make()
        return conn

    def _discard(self, conn: Connection) -> None:
        conn.close()
        with self._lock:
            self._created -= 1

    def release(self, conn: Connection) -> None:
        if conn._in_txn:
            conn.rollback()
        if conn.broken or self._closed:
            self._discard(conn)
        else:
            self._idle.put(conn)

    def ping_all(self) -> int:
        idle = []
        while True:
            try:
                idle.append(self._idle.get_nowait())
            except queue.Empty:
                break
        alive = 0
        for conn in idle:
            if not conn.broken and conn.ping():
                try:
                    conn.execute("SELECT 1").fetchone()
                    conn.last_used = time.monotonic()
                    alive += 1
                    self._idle.put(conn)
                    continue
                except Exception:
                    conn.broken = True
            self._discard(conn)
        if alive == 0:
            with get_db_connection() as conn:
                conn.execute("SELECT 1").fetchone()
            alive = 1
        return alive

    def close_all(self) -> None:
        self._closed = True
        closed = 0
        while True:
            try:
                self._idle.get_nowait().close()
                closed += 1
            except queue.Empty:
                break
        logger.info(f"Closed {closed} database connections")


_POOL = ConnectionPool(
    maxsize=min(PERFORMANCE_CONFIG["db_pool_maxsize"], 6),
    timeout=PERFORMANCE_CONFIG["db_pool_timeout_seconds"],
)


@contextmanager
def get_db_connection():
    conn = _POOL.acquire()
    try:
        yield conn
    except Exception as e:
        if not getattr(e, "expected", False):
            logger.error(f"Database operation error: {e}")
        raise
    finally:
        _POOL.release(conn)


@contextmanager
def transaction():
    with get_db_connection() as conn:
        conn.begin()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


class AsyncConnection:
    def __init__(self, conn: Connection):
        self._c = conn

    async def execute(self, sql, params=()):
        return await asyncio.to_thread(self._c.execute, sql, params)

    async def execute_raw(self, sql):
        return await asyncio.to_thread(self._c.execute_raw, sql)

    async def commit(self):
        await asyncio.to_thread(self._c.commit)

    async def rollback(self):
        await asyncio.to_thread(self._c.rollback)

    async def begin(self):
        await asyncio.to_thread(self._c.begin)

    def cursor(self) -> _Cursor:
        return _Cursor(self._c)

    @property
    def sync(self) -> Connection:
        return self._c


@asynccontextmanager
async def aget_db_connection():
    conn = await asyncio.to_thread(_POOL.acquire)
    try:
        yield AsyncConnection(conn)
    except Exception as e:
        if not getattr(e, "expected", False):
            logger.error(f"Database operation error: {e}")
        raise
    finally:
        await asyncio.to_thread(_POOL.release, conn)


@asynccontextmanager
async def atransaction():
    async with aget_db_connection() as aconn:
        await aconn.begin()
        try:
            yield aconn
            await aconn.commit()
        except BaseException:
            await asyncio.shield(aconn.rollback())
            raise


async def arun(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def ping_database() -> bool:
    try:
        _POOL.ping_all()
        return True
    except Exception as e:
        logger.warning(f"Database heartbeat failed: {e}")
        return False


def close_database() -> None:
    _POOL.close_all()


def get_channel_ids(channel_key: str) -> Optional[List[int]]:
    try:
        with get_db_connection() as conn:
            result = conn.execute("SELECT value FROM config WHERE key = ?", (channel_key,)).fetchone()
            if not result or not result['value']:
                return None
            channel_ids = []
            for part in result['value'].split(','):
                part = part.strip()
                if not part:
                    continue
                if part.startswith("<#") and part.endswith(">"):
                    part = part[2:-1]
                try:
                    channel_ids.append(int(part))
                except (ValueError, TypeError):
                    logger.warning(f"Invalid channel ID in '{channel_key}': {part}")
            return channel_ids or None
    except Error as e:
        logger.error(f"Database error fetching channel IDs for '{channel_key}': {e}")
        return None


def get_next_operation_report_id(report_type: str) -> int:
    try:
        with transaction() as conn:
            result = conn.execute(
                "SELECT next_id FROM operation_counters WHERE report_type = ?", (report_type,)).fetchone()
            if result:
                current_id = result['next_id']
                conn.execute("UPDATE operation_counters SET next_id = next_id + 1 WHERE report_type = ?", (report_type,))
            else:
                current_id = 1
                conn.execute("INSERT INTO operation_counters (report_type, next_id) VALUES (?, 2)", (report_type,))
            return current_id
    except Error as e:
        logger.error(f"Error getting operation report ID for '{report_type}': {e}")
        return 0


def get_config_value(key: str, default: Any = None) -> Any:
    try:
        with get_db_connection() as conn:
            result = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
            return result['value'] if result else default
    except Error as e:
        logger.error(f"Error fetching config '{key}': {e}")
        return default


def set_config_value(key: str, value: str) -> bool:
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
                (key, value))
        return True
    except Error as e:
        logger.error(f"Error setting config '{key}': {e}")
        return False


_TAIL = "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
_AUTO = " AUTO_ID_CACHE=1"

_SCHEMA: List[Tuple[str, bool]] = []


def _table(sql: str) -> None:
    _SCHEMA.append((sql, "AUTO_INCREMENT" in sql))


_table("""CREATE TABLE IF NOT EXISTS members (
    discord_id       VARCHAR(32)  NOT NULL PRIMARY KEY,
    discord_name     VARCHAR(128) NOT NULL,
    ig_name          VARCHAR(64)  NOT NULL,
    account_name     VARCHAR(64)  NOT NULL,
    `rank`           VARCHAR(64)  NOT NULL DEFAULT 'Cadet',
    country          VARCHAR(64),
    status           VARCHAR(16)  NOT NULL DEFAULT 'Active',
    is_af            TINYINT      NOT NULL DEFAULT 0,
    af_rank          VARCHAR(64)  NOT NULL DEFAULT 'None',
    has_slot         TINYINT      NOT NULL DEFAULT 0,
    playtime_minutes BIGINT       NOT NULL DEFAULT 0,
    last_online      VARCHAR(40),
    date_added       DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_updated     DATETIME DEFAULT CURRENT_TIMESTAMP,
    CHECK (CHAR_LENGTH(ig_name) BETWEEN 3 AND 32),
    CHECK (CHAR_LENGTH(account_name) BETWEEN 3 AND 32),
    CHECK (status IN ('Active', 'Reserve')),
    CHECK (is_af IN (0, 1)),
    CHECK (has_slot IN (0, 1)),
    KEY idx_members_rank (`rank`), KEY idx_members_status (status), KEY idx_members_ig_name (ig_name)
)""")

_table("""CREATE TABLE IF NOT EXISTS points (
    discord_id        VARCHAR(32) NOT NULL PRIMARY KEY,
    promo_points      INT DEFAULT 0,
    leadership_points INT DEFAULT 0,
    af_points         INT DEFAULT 0
)""")

_table("""CREATE TABLE IF NOT EXISTS rank_stats (
    discord_id      VARCHAR(32) NOT NULL,
    `rank`          VARCHAR(64) NOT NULL,
    warzone INT DEFAULT 0, doomsday INT DEFAULT 0, ammo_transport INT DEFAULT 0, bank_ops INT DEFAULT 0,
    casino_ops INT DEFAULT 0, securicar_ops INT DEFAULT 0, andromada_ops INT DEFAULT 0,
    andromada_terrors_ops INT DEFAULT 0, assault_ops INT DEFAULT 0, training INT DEFAULT 0, patrol INT DEFAULT 0,
    extra_points INT DEFAULT 0, promo_points INT DEFAULT 0, leadership_points INT DEFAULT 0, af_points INT DEFAULT 0,
    af_warzone INT DEFAULT 0, af_counter INT DEFAULT 0, af_training INT DEFAULT 0,
    PRIMARY KEY (discord_id, `rank`)
)""")

_table("""CREATE TABLE IF NOT EXISTS career_stats (
    discord_id VARCHAR(32) NOT NULL PRIMARY KEY,
    total_warzone INT DEFAULT 0, total_doomsday INT DEFAULT 0, total_ammo_transport INT DEFAULT 0,
    total_bank_ops INT DEFAULT 0, total_casino_ops INT DEFAULT 0, total_securicar_ops INT DEFAULT 0,
    total_andromada_ops INT DEFAULT 0, total_andromada_terrors_ops INT DEFAULT 0, total_assault_ops INT DEFAULT 0,
    total_training INT DEFAULT 0, total_patrol INT DEFAULT 0, total_extra_points INT DEFAULT 0,
    total_promo_points INT DEFAULT 0, total_af_warzone INT DEFAULT 0, total_af_counter INT DEFAULT 0,
    total_af_training INT DEFAULT 0
)""")

_table("""CREATE TABLE IF NOT EXISTS reports (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    operation_report_id INT,
    report_type         VARCHAR(32) NOT NULL,
    reporter_id         VARCHAR(32),
    message_id          VARCHAR(32),
    channel_id          VARCHAR(32) NOT NULL,
    date_time           VARCHAR(40),
    duration            VARCHAR(64),
    result              VARCHAR(16),
    at_level            TINYINT,
    activity            TEXT,
    reason              TEXT,
    notes               TEXT,
    screenshot          TEXT,
    af_pilot            VARCHAR(255),
    spawn_type          VARCHAR(16),
    against_name        VARCHAR(255),
    attempts            INT,
    arrests             INT,
    enemies             TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_reports_op (operation_report_id, report_type),
    KEY idx_reports_type_date (report_type, created_at), KEY idx_reports_created_at (created_at),
    KEY idx_reports_reporter (reporter_id),
    CHECK (report_type IN ('bank_ops','casino_ops','securicar_ops','andromada_ops','ammo_transport',
        'andromada_terrors_ops','assault_ops','warzone','doomsday','training','patrol','extra_points',
        'af_warzone','af_counter','af_training')),
    CHECK (result IN ('Success','Failure','4-0','3-1','2-2','1-3','0-4')),
    CHECK (at_level IN (1, 2, 3)),
    CHECK (spawn_type IN ('Hydra', 'Hunter'))
)""")

_table("""CREATE TABLE IF NOT EXISTS operation_counters (
    report_type VARCHAR(32) NOT NULL PRIMARY KEY,
    next_id     INT NOT NULL DEFAULT 1
)""")

_table("""CREATE TABLE IF NOT EXISTS leadership_history (
    id                       BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id               VARCHAR(32) NOT NULL,
    report_id                BIGINT NOT NULL,
    operation_type           VARCHAR(32) NOT NULL,
    was_reporter             TINYINT DEFAULT 0,
    leadership_points_gained INT DEFAULT 0,
    promo_points_gained      INT DEFAULT 0,
    created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_leadership (discord_id, report_id),
    KEY idx_leadership_report (report_id)
)""")

_table("""CREATE TABLE IF NOT EXISTS operation_participants (
    id                       BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id               VARCHAR(32) NOT NULL,
    report_id                BIGINT NOT NULL,
    was_reporter             TINYINT DEFAULT 0,
    was_leader               TINYINT DEFAULT 0,
    was_airforce             TINYINT DEFAULT 0,
    promo_points_gained      INT DEFAULT 0,
    leadership_points_gained INT DEFAULT 0,
    af_points_gained         INT DEFAULT 0,
    participant_rank         VARCHAR(64),
    created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_participants (discord_id, report_id),
    KEY idx_op_participants_report (report_id),
    KEY idx_op_participants_member_date (discord_id, created_at)
)""")

_table("""CREATE TABLE IF NOT EXISTS extra_points_requests (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    report_type  VARCHAR(32) NOT NULL DEFAULT 'extra_points',
    submitted_by VARCHAR(32) NOT NULL,
    message_id   VARCHAR(32),
    channel_id   VARCHAR(32),
    date_time    VARCHAR(40),
    participants TEXT,
    reason       TEXT NOT NULL,
    action       TEXT,
    screenshot   TEXT,
    status       VARCHAR(16) NOT NULL DEFAULT 'pending',
    approved_by  VARCHAR(32),
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    approved_at  DATETIME,
    KEY idx_extra_status (status, created_at), KEY idx_extra_submitted (submitted_by), KEY idx_extra_approved_by (approved_by),
    CHECK (status IN ('pending', 'approved', 'declined'))
)""")

_table("""CREATE TABLE IF NOT EXISTS tickets (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    channel_id  VARCHAR(64) NOT NULL,
    message_id  VARCHAR(32),
    creator_id  VARCHAR(32) NOT NULL,
    ticket_type VARCHAR(64) NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at   DATETIME,
    closed_by   VARCHAR(32),
    UNIQUE KEY uq_tickets_channel (channel_id),
    KEY idx_tickets_creator (creator_id), KEY idx_tickets_status (status), KEY idx_tickets_created_at (created_at),
    CHECK (status IN ('open', 'closed'))
)""")

_table("""CREATE TABLE IF NOT EXISTS manual_points_awards (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id VARCHAR(32) NOT NULL,
    point_type VARCHAR(16) NOT NULL,
    amount     INT NOT NULL,
    awarded_by VARCHAR(32),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_manual_member (discord_id), KEY idx_manual_date (created_at), KEY idx_manual_by (awarded_by),
    CHECK (point_type IN ('promo', 'leadership', 'af'))
)""")

_table("""CREATE TABLE IF NOT EXISTS config (
    `key`      VARCHAR(100) NOT NULL PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

_table("""CREATE TABLE IF NOT EXISTS promotion_boosts (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id     VARCHAR(32) NOT NULL,
    days_decreased INT NOT NULL DEFAULT 0,
    applied_by     VARCHAR(32),
    applied_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_boost_member (discord_id), KEY idx_boost_by (applied_by)
)""")

_table("""CREATE TABLE IF NOT EXISTS promotion_delays (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id     VARCHAR(32) NOT NULL,
    days_increased INT NOT NULL DEFAULT 0,
    applied_by     VARCHAR(32),
    applied_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_delay_member (discord_id), KEY idx_delay_by (applied_by)
)""")

_table("""CREATE TABLE IF NOT EXISTS playtime_log (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id VARCHAR(32) NOT NULL,
    log_date   VARCHAR(10) NOT NULL,
    minutes    INT NOT NULL DEFAULT 0,
    UNIQUE KEY uq_playtime (discord_id, log_date),
    KEY idx_playtime_date (log_date)
)""")

_table("""CREATE TABLE IF NOT EXISTS casino_accounts (
    discord_id    VARCHAR(32) NOT NULL PRIMARY KEY,
    balance       BIGINT NOT NULL DEFAULT 0,
    last_daily    VARCHAR(10),
    daily_streak  INT NOT NULL DEFAULT 0,
    games_played  INT NOT NULL DEFAULT 0,
    games_won     INT NOT NULL DEFAULT 0,
    games_lost    INT NOT NULL DEFAULT 0,
    total_wagered BIGINT NOT NULL DEFAULT 0,
    total_won     BIGINT NOT NULL DEFAULT 0,
    biggest_win   BIGINT NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_casino_accounts_bal (balance),
    CHECK (balance >= 0)
)""")

_table("""CREATE TABLE IF NOT EXISTS casino_games (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id VARCHAR(32) NOT NULL,
    game       VARCHAR(16) NOT NULL,
    bet        BIGINT NOT NULL DEFAULT 0,
    payout     BIGINT NOT NULL DEFAULT 0,
    profit     BIGINT NOT NULL DEFAULT 0,
    outcome    VARCHAR(32),
    details    TEXT,
    status     VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    settled_at DATETIME,
    KEY idx_casino_games_player (discord_id, created_at), KEY idx_casino_games_status (status),
    CHECK (game IN ('slots','roll','wheel','poker','blackjack')),
    CHECK (status IN ('open','settled','refunded'))
)""")

_table("""CREATE TABLE IF NOT EXISTS casino_transactions (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id    VARCHAR(32) NOT NULL,
    kind          VARCHAR(24) NOT NULL,
    amount        BIGINT NOT NULL,
    balance_after BIGINT NOT NULL,
    game_id       BIGINT,
    actor_id      VARCHAR(32),
    note          TEXT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_casino_tx_player (discord_id, created_at), KEY idx_casino_tx_game (game_id),
    CHECK (kind IN ('start_balance','daily','bet','payout','refund','wheel_spin','admin_add','admin_remove',
                    'admin_set','shop_purchase','shop_refund','trivia'))
)""")

_table("""CREATE TABLE IF NOT EXISTS casino_wheel_spins (
    discord_id VARCHAR(32) NOT NULL,
    spin_day   VARCHAR(10) NOT NULL,
    spins      INT NOT NULL DEFAULT 0,
    PRIMARY KEY (discord_id, spin_day)
)""")

_table("""CREATE TABLE IF NOT EXISTS shop_items (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    category      VARCHAR(100) COLLATE utf8mb4_general_ci NOT NULL,
    name          VARCHAR(100) COLLATE utf8mb4_general_ci NOT NULL,
    description   TEXT,
    price         BIGINT NOT NULL,
    unit          VARCHAR(64) NOT NULL DEFAULT 'per one',
    stock         BIGINT,
    role_id       VARCHAR(32),
    duration_days INT,
    win_bonus     INT NOT NULL DEFAULT 0,
    daily_bonus   INT NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_shop_items_name (name), KEY idx_shop_items_category (category),
    CHECK (price >= 0)
)""")

_table("""CREATE TABLE IF NOT EXISTS shop_orders (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id    VARCHAR(32) NOT NULL,
    item_id       BIGINT,
    item_name     VARCHAR(100) NOT NULL,
    category      VARCHAR(100) NOT NULL,
    quantity      INT NOT NULL,
    unit_price    BIGINT NOT NULL,
    total         BIGINT NOT NULL,
    unit          VARCHAR(64),
    status        VARCHAR(16) NOT NULL DEFAULT 'pending',
    role_id       VARCHAR(32),
    duration_days INT,
    expires_at    VARCHAR(24),
    role_removed  TINYINT NOT NULL DEFAULT 0,
    handled_by    VARCHAR(32),
    handled_at    VARCHAR(24),
    note          TEXT,
    channel_id    VARCHAR(32),
    message_id    VARCHAR(32),
    created_at    VARCHAR(24) NOT NULL,
    KEY idx_shop_orders_player (discord_id, id), KEY idx_shop_orders_status (status), KEY idx_shop_orders_item (item_id),
    CHECK (status IN ('pending','fulfilled','rejected'))
)""")

_table("""CREATE TABLE IF NOT EXISTS trivia_stats (
    discord_id     VARCHAR(32) NOT NULL PRIMARY KEY,
    answered       INT NOT NULL DEFAULT 0,
    correct        INT NOT NULL DEFAULT 0,
    streak         INT NOT NULL DEFAULT 0,
    best_streak    INT NOT NULL DEFAULT 0,
    best_streak_at VARCHAR(24),
    total_earned   BIGINT NOT NULL DEFAULT 0,
    last_played    VARCHAR(24),
    KEY idx_trivia_stats_best (best_streak)
)""")

_table("""CREATE TABLE IF NOT EXISTS trivia_log (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    discord_id   VARCHAR(32) NOT NULL,
    result       VARCHAR(16) NOT NULL,
    picked       VARCHAR(255),
    streak_after INT NOT NULL DEFAULT 0,
    reward       BIGINT NOT NULL DEFAULT 0,
    bonus_pct    INT NOT NULL DEFAULT 0,
    bonus        BIGINT NOT NULL DEFAULT 0,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_trivia_log_player (discord_id, id),
    CHECK (result IN ('correct','wrong','timeout'))
)""")

_FOREIGN_KEYS = [
    ("fk_points_member", "points", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_rank_stats_member", "rank_stats", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_career_member", "career_stats", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_reports_reporter", "reports", "reporter_id", "members", "discord_id", "SET NULL"),
    ("fk_leadership_member", "leadership_history", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_leadership_report", "leadership_history", "report_id", "reports", "id", "CASCADE"),
    ("fk_participants_member", "operation_participants", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_participants_report", "operation_participants", "report_id", "reports", "id", "CASCADE"),
    ("fk_extra_submitted", "extra_points_requests", "submitted_by", "members", "discord_id", "CASCADE"),
    ("fk_extra_approved", "extra_points_requests", "approved_by", "members", "discord_id", "SET NULL"),
    ("fk_manual_member", "manual_points_awards", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_manual_by", "manual_points_awards", "awarded_by", "members", "discord_id", "SET NULL"),
    ("fk_boost_member", "promotion_boosts", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_boost_by", "promotion_boosts", "applied_by", "members", "discord_id", "SET NULL"),
    ("fk_delay_member", "promotion_delays", "discord_id", "members", "discord_id", "CASCADE"),
    ("fk_delay_by", "promotion_delays", "applied_by", "members", "discord_id", "SET NULL"),
    ("fk_casino_tx_game", "casino_transactions", "game_id", "casino_games", "id", "SET NULL"),
    ("fk_shop_orders_item", "shop_orders", "item_id", "shop_items", "id", "SET NULL"),
]

BACKUP_TABLES = [
    "members", "points", "rank_stats", "career_stats", "reports", "operation_counters", "leadership_history",
    "operation_participants", "extra_points_requests", "tickets", "manual_points_awards", "config",
    "promotion_boosts", "promotion_delays", "playtime_log", "casino_accounts", "casino_games",
    "casino_transactions", "casino_wheel_spins", "shop_items", "shop_orders", "trivia_stats", "trivia_log",
]


def initialize_database() -> None:
    try:
        with get_db_connection() as conn:
            version = conn.execute("SELECT VERSION() AS v").fetchone()["v"]
            logger.info(f"Connected to TiDB/MySQL {version} at {os.getenv('TIDB_HOST')}")

            for sql, auto in _SCHEMA:
                stmt = f"{sql} {_TAIL}"
                try:
                    conn.execute_raw(stmt + (_AUTO if auto else ""))
                except Error as e:
                    if not auto:
                        raise
                    logger.warning(f"AUTO_ID_CACHE not accepted ({e}), creating the table without it")
                    conn.execute_raw(stmt)

            try:
                existing = {r[0].lower() for r in conn.execute(
                    "SELECT constraint_name FROM information_schema.referential_constraints "
                    "WHERE constraint_schema = DATABASE()").fetchall()}
            except Error:
                existing = set()
            for name, table, col, parent, pcol, action in _FOREIGN_KEYS:
                if name in existing:
                    continue
                try:
                    conn.execute_raw(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (`{col}`) "
                                 f"REFERENCES {parent}(`{pcol}`) ON DELETE {action}")
                except Error as e:
                    logger.warning(f"Could not add foreign key {name}: {e}")
        logger.info("Database initialized")
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}")
        raise


def export_backup_bytes() -> Tuple[str, bytes]:
    data: Dict[str, Any] = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "tables": {}}
    with get_db_connection() as conn:
        for table in BACKUP_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            data["tables"][table] = [dict(r) for r in rows]
    raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    name = f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json.gz"
    return name, gzip.compress(raw)


def backup_database(backup_dir: Optional[Path] = None) -> Optional[Path]:
    backup_dir = backup_dir or BACKUP_DIR
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        name, payload = export_backup_bytes()
        path = backup_dir / name
        path.write_bytes(payload)
        logger.info(f"Backup created: {path.name} ({len(payload) / 1024:.1f} KB)")
        return path
    except Exception as e:
        logger.error(f"Failed to create backup: {e}", exc_info=True)
        return None


def cleanup_old_backups(backup_dir: Optional[Path] = None, keep_count: int = 4) -> int:
    backup_dir = backup_dir or BACKUP_DIR
    if not backup_dir.exists():
        return 0
    backups = sorted(backup_dir.glob("backup_*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    deleted = 0
    for old in backups[keep_count:]:
        try:
            old.unlink()
            deleted += 1
        except Exception as e:
            logger.warning(f"Failed to delete backup {old.name}: {e}")
    return deleted


def auto_backup_database(keep_count: int = 4) -> Optional[Path]:
    path = backup_database()
    if path:
        cleanup_old_backups(keep_count=keep_count)
    return path
