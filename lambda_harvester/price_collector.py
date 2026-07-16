"""
Hourly Polymarket price collector.

Snapshots current YES prices for all active markets into a local SQLite
database (price_history.db). Run once per hour via Task Scheduler.

After enough observations accumulate (>=10 hours per market), the scanner
can read price history from the local DB instead of the CLOB API — which
means resolved markets retain their history even after Polymarket deletes
the token from the order book.

Usage:
    python -m lambda_harvester.price_collector
    python -m lambda_harvester.price_collector --quiet
    python -m lambda_harvester.price_collector --stats
"""

import time
import datetime
import argparse
from pathlib import Path

from lambda_harvester.polymarket_client import PolymarketClient
from lambda_harvester.price_db import PriceDB, DEFAULT_DB
from lambda_harvester.config import MIN_VOLUME_USD


def collect_once(db: PriceDB, client: PolymarketClient,
                 max_markets: int = 2000, quiet: bool = False) -> int:
    if not quiet:
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        print(f"[{ts} UTC] Fetching active markets...")

    markets = client.get_all_active_markets(max_markets=max_markets)

    if not quiet:
        print(f"  {len(markets)} markets from Gamma API")

    now_ts = int(time.time())
    saved = 0

    for m in markets:
        price = client.parse_market_price(m)
        if price is None or not (0 < price < 1):
            continue

        volume = client.parse_volume(m)
        if volume < MIN_VOLUME_USD:
            continue

        market_id = m.get("id") or m.get("conditionId")
        if not market_id:
            continue

        tokens = client.get_clob_token_ids(m)
        token_id = tokens[0] if tokens else None
        category = client.categorize_market(m)

        db.upsert_market(
            market_id=market_id,
            question=m.get("question", ""),
            category=category,
            token_id=token_id,
            end_date=m.get("endDate"),
        )
        db.insert_price(market_id, now_ts, price, volume)
        saved += 1

    if not quiet:
        stats = db.get_db_stats()
        print(f"  Saved {saved} snapshots  |  "
              f"DB: {stats['markets']} markets, {stats['price_rows']} rows")

    return saved


def print_stats(db: PriceDB):
    stats = db.get_db_stats()
    print(f"Database: {db.path}")
    print(f"  Markets tracked : {stats['markets']}")
    print(f"  Price rows      : {stats['price_rows']}")
    if stats["oldest_ts"]:
        oldest = datetime.datetime.utcfromtimestamp(stats["oldest_ts"])
        newest = datetime.datetime.utcfromtimestamp(stats["newest_ts"])
        age_days = (stats["newest_ts"] - stats["oldest_ts"]) / 86400
        print(f"  Oldest snapshot : {oldest.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"  Newest snapshot : {newest.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"  History span    : {age_days:.1f} days")
    else:
        print("  No price data yet.")


def main():
    parser = argparse.ArgumentParser(description="Polymarket hourly price collector")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--max-markets", type=int, default=2000)
    parser.add_argument("--quiet", action="store_true", help="Suppress output")
    parser.add_argument("--stats", action="store_true", help="Print DB stats and exit")
    args = parser.parse_args()

    db = PriceDB(Path(args.db))

    if args.stats:
        print_stats(db)
        return

    client = PolymarketClient(request_delay=0.15)
    collect_once(db, client, max_markets=args.max_markets, quiet=args.quiet)


if __name__ == "__main__":
    main()
