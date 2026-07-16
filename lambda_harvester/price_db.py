"""SQLite price history database for the hourly Polymarket price collector."""

import sqlite3
import datetime
import time
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path(__file__).parent.parent / "price_history.db"


class PriceDB:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS markets (
                    market_id  TEXT PRIMARY KEY,
                    question   TEXT,
                    category   TEXT,
                    token_id   TEXT,
                    end_date   TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prices (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    ts        INTEGER NOT NULL,
                    price     REAL NOT NULL,
                    volume    REAL,
                    UNIQUE(market_id, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_prices_market_ts
                    ON prices(market_id, ts);
            """)

    def upsert_market(self, market_id: str, question: str, category: str,
                      token_id: Optional[str], end_date: Optional[str]):
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO markets(market_id, question, category, token_id,
                                    end_date, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    question  = excluded.question,
                    category  = excluded.category,
                    token_id  = excluded.token_id,
                    end_date  = excluded.end_date,
                    last_seen = excluded.last_seen
            """, (market_id, question, category, token_id, end_date, now, now))

    def insert_price(self, market_id: str, ts: int, price: float,
                     volume: Optional[float] = None):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO prices(market_id, ts, price, volume)
                VALUES (?, ?, ?, ?)
            """, (market_id, ts, price, volume))

    def get_price_history(self, market_id: str, hours: int = 48) -> list[float]:
        """Prices in chronological order for the last N hours."""
        cutoff = int(time.time()) - hours * 3600
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT price FROM prices
                WHERE market_id = ? AND ts >= ?
                ORDER BY ts ASC
            """, (market_id, cutoff)).fetchall()
        return [r["price"] for r in rows]

    def get_tracked_market_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]

    def get_price_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]

    def get_db_stats(self) -> dict:
        with self._conn() as conn:
            markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
            prices = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            oldest = conn.execute("SELECT MIN(ts) FROM prices").fetchone()[0]
            newest = conn.execute("SELECT MAX(ts) FROM prices").fetchone()[0]
        return {
            "markets": markets,
            "price_rows": prices,
            "oldest_ts": oldest,
            "newest_ts": newest,
        }
