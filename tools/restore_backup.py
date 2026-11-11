import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import BACKUP_TABLES, close_database, get_db_connection, initialize_database


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    wipe = "--wipe" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 1
    data = json.loads(gzip.decompress(Path(args[0]).read_bytes()))
    tables = data["tables"]

    initialize_database()
    with get_db_connection() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM members").fetchone()["n"]
        if existing and not wipe:
            print(f"The database already has {existing} members. Use --wipe to replace everything.")
            return 1
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            if wipe:
                for table in reversed(BACKUP_TABLES):
                    conn.execute(f"DELETE FROM {table}")
            for table in BACKUP_TABLES:
                rows = tables.get(table, [])
                for row in rows:
                    cols = list(row.keys())
                    sql = (f"INSERT INTO {table} ({', '.join('`' + c + '`' for c in cols)}) "
                           f"VALUES ({', '.join('?' for _ in cols)})")
                    conn.execute(sql, [row[c] for c in cols])
                print(f"{table}: {len(rows)} rows")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    close_database()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
