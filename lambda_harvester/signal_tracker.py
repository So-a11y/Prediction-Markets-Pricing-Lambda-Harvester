"""
Forward validation: saves live scanner signals to disk and checks outcomes when markets resolve.

Usage:
    python -m lambda_harvester.main --save-signals          # scan + save new signals
    python -m lambda_harvester.main --check-outcomes        # print resolution P&L for saved signals

File: signals_log.json in the project root (override with --signals-file).

Each signal is written once at scan time. outcome/pnl are filled in later by --check-outcomes.
Markets already in the log are never overwritten — first-seen entry price is preserved.
"""

import json
import datetime
from pathlib import Path
from typing import Optional

from lambda_harvester.scanner import ContractAnalysis
from lambda_harvester.config import MAX_BID_ASK_SPREAD

DEFAULT_LOG = Path("signals_log.json")


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: list[dict], path: Path):
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def save_signals(results: list[ContractAnalysis], client=None,
                 path: Path = DEFAULT_LOG) -> int:
    """Append new signals to the log. Skips markets already tracked.

    If client is provided, fetches the live CLOB order book to record the real
    bid/ask spread. entry_price is set to yes_bid (the realistic NO fill price:
    buying NO costs 1 - yes_bid, so yes_bid is what you're risking on the YES side).
    If the order book is unavailable, falls back to the Gamma mid-price with
    book_available=False flagged so you know the fill price isn't verified.
    """
    existing = _load(path)
    existing_ids = {r["market_id"] for r in existing}

    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    added = 0
    skipped_spread = 0

    for a in results:
        if a.market_id in existing_ids:
            continue

        yes_bid, yes_ask, spread, book_available = None, None, None, False

        if client is not None and a.token_id:
            yes_bid, yes_ask = client.get_order_book_top(a.token_id)
            if yes_bid is not None and yes_ask is not None:
                spread = round(yes_ask - yes_bid, 6)
                # bid≤0.02 / ask≥0.98 is the CLOB default when no one is quoting —
                # treat it as no_book rather than a real (untradeable) spread.
                if yes_bid <= 0.02 and yes_ask >= 0.98:
                    yes_bid, yes_ask, spread = None, None, None
                else:
                    book_available = True

        # Reject ghost markets: spread too wide to fill at any reasonable price.
        if book_available and spread > MAX_BID_ASK_SPREAD:
            skipped_spread += 1
            continue

        # entry_price = yes_bid when available (realistic NO fill equivalent).
        # Falls back to Gamma mid if CLOB is unavailable.
        entry_price = yes_bid if book_available else a.p_mkt

        if not book_available:
            liquidity = "no_book"
        elif spread <= 0.10:
            liquidity = "tight"
        else:
            liquidity = "wide"  # 10–25% spread — tradeable but less clean

        record = {
            "saved_at": now,
            "market_id": a.market_id,
            "question": a.question,
            "category": a.category,
            "token_id": a.token_id,
            "mid_price": round(a.p_mkt, 6),
            "yes_bid": round(yes_bid, 6) if yes_bid is not None else None,
            "yes_ask": round(yes_ask, 6) if yes_ask is not None else None,
            "spread": spread,
            "liquidity": liquidity,
            "book_available": book_available,
            "entry_price": round(entry_price, 6),
            "p_star": round(a.p_star, 6),
            "lambda_contract": round(a.lambda_contract, 6),
            "lambda_excess": round(a.lambda_excess, 6),
            "eiv": round(a.eiv, 6) if a.eiv is not None else None,
            "trade_score": round(a.trade_score, 4) if a.trade_score is not None else None,
            "days_at_signal": round(a.days_to_resolution, 1),
            "end_date": None,
            "outcome": None,
            "pnl": None,
            "checked_at": None,
        }
        existing.append(record)
        existing_ids.add(a.market_id)
        added += 1

    _save(existing, path)
    return added, skipped_spread


def check_outcomes(client, path: Path = DEFAULT_LOG) -> list[dict]:
    """
    Fetch resolution outcomes for all pending signals.
    Updates outcome/pnl in-place and persists. Returns full record list.
    """
    records = _load(path)
    if not records:
        return []

    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    pending = [r for r in records if r.get("outcome") is None]

    updated = 0
    for r in pending:
        market = client.get_market_by_id(r["market_id"])
        if market is None:
            continue

        end_dt = client.parse_end_date(market)
        if end_dt is not None:
            r["end_date"] = end_dt.isoformat(timespec="seconds")

        outcome = client.get_resolution_outcome(market)
        if outcome is not None:
            r["outcome"] = outcome
            r["pnl"] = round(r["entry_price"] - outcome, 6)
            r["checked_at"] = now
            updated += 1

    if updated:
        _save(records, path)

    return records


def print_outcomes_report(records: list[dict]):
    """Print a formatted P&L report for all tracked signals."""
    if not records:
        print("  No signals on file. Run with --save-signals first.")
        return

    resolved = [r for r in records if r.get("outcome") is not None]
    pending  = [r for r in records if r.get("outcome") is None]

    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║              FORWARD VALIDATION — BUY NO SIGNAL TRACKER             ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()

    # ── Resolved trades ────────────────────────────────────────────────────────
    if resolved:
        wins   = [r for r in resolved if r["outcome"] == 0]
        losses = [r for r in resolved if r["outcome"] == 1]
        total_pnl = sum(r["pnl"] for r in resolved)
        avg_pnl   = total_pnl / len(resolved)
        win_rate  = len(wins) / len(resolved)

        print(f"── RESOLVED TRADES ({len(resolved)}) ──────────────────────────────────────────────")
        print(f"  Win rate  : {win_rate:.1%}  ({len(wins)} NO resolutions / {len(resolved)} trades)")
        print(f"  Avg P&L   : {avg_pnl:+.4f}  per $1 invested")
        print(f"  Total P&L : {total_pnl:+.4f}")
        print()

        print(f"  {'#':>3}  {'P&L':>7}  {'Fill':>6}  {'Mid':>6}  {'Sprd':>5}  {'Liq':>6}  "
              f"{'λ':>6}  {'Score':>6}  {'Res':>4}  {'Saved':>10}  Question")
        print(f"  {'─'*3}  {'─'*7}  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*6}  "
              f"{'─'*6}  {'─'*6}  {'─'*4}  {'─'*10}  {'─'*40}")

        for i, r in enumerate(sorted(resolved, key=lambda x: -x["pnl"]), 1):
            res_str    = "NO ✓" if r["outcome"] == 0 else "YES ✗"
            score_str  = f"{r['trade_score']:.2f}" if r["trade_score"] is not None else "  N/A"
            saved_dt   = r["saved_at"][:10]
            mid_str    = f"{r.get('mid_price', r['entry_price']):.3f}"
            sprd_str   = f"{r['spread']:.3f}" if r.get("spread") is not None else "  N/A"
            liq        = r.get("liquidity", "no_book")
            liq_str    = {"tight": "tight ", "wide": "WIDE  ", "no_book": "no_bk "}.get(liq, liq)
            print(f"  {i:>3}  {r['pnl']:>+7.4f}  {r['entry_price']:>6.3f}  {mid_str}  "
                  f"{sprd_str:>5}  {liq_str}  {r['lambda_contract']:>6.3f}  {score_str:>6}  "
                  f"{res_str:<6}  {saved_dt}  {r['question'][:40]}")
        print()

        # Category breakdown
        cats: dict[str, dict] = {}
        for r in resolved:
            c = cats.setdefault(r["category"], {"n": 0, "wins": 0, "pnl": 0.0})
            c["n"] += 1
            c["wins"] += int(r["outcome"] == 0)
            c["pnl"] += r["pnl"]
        for c in cats.values():
            c["win_rate"] = c["wins"] / c["n"]
            c["avg_pnl"]  = c["pnl"] / c["n"]

        print(f"  {'Cat':<10}  {'N':>4}  {'Win%':>6}  {'Avg PnL':>8}  {'Total PnL':>10}")
        print(f"  {'─'*10}  {'─'*4}  {'─'*6}  {'─'*8}  {'─'*10}")
        for cat, m in sorted(cats.items(), key=lambda x: -x[1]["avg_pnl"]):
            print(f"  {cat:<10}  {m['n']:>4}  {m['win_rate']:>6.1%}  "
                  f"{m['avg_pnl']:>+8.4f}  {m['pnl']:>+10.4f}")
        print()

        # Liquidity breakdown — tight vs wide vs no_book
        liq_groups: dict[str, dict] = {}
        for r in resolved:
            liq = r.get("liquidity", "no_book")
            g = liq_groups.setdefault(liq, {"n": 0, "wins": 0, "pnl": 0.0})
            g["n"] += 1
            g["wins"] += int(r["outcome"] == 0)
            g["pnl"] += r["pnl"]
        if len(liq_groups) > 1:
            print(f"  {'Liquidity':<10}  {'N':>4}  {'Win%':>6}  {'Avg PnL':>8}  {'Total PnL':>10}")
            print(f"  {'─'*10}  {'─'*4}  {'─'*6}  {'─'*8}  {'─'*10}")
            for liq in ["tight", "wide", "no_book"]:
                if liq not in liq_groups:
                    continue
                g = liq_groups[liq]
                wr = g["wins"] / g["n"]
                print(f"  {liq:<10}  {g['n']:>4}  {wr:>6.1%}  "
                      f"{g['pnl']/g['n']:>+8.4f}  {g['pnl']:>+10.4f}")
        print()

    # ── Pending trades ─────────────────────────────────────────────────────────
    if pending:
        print(f"── PENDING ({len(pending)} awaiting resolution) ─────────────────────────────────────")
        print(f"  {'#':>3}  {'Fill':>6}  {'Mid':>6}  {'Sprd':>5}  {'Liq':>6}  "
              f"{'λ':>6}  {'Score':>6}  {'Days':>5}  {'Saved':>10}  Question")
        print(f"  {'─'*3}  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*6}  "
              f"{'─'*6}  {'─'*6}  {'─'*5}  {'─'*10}  {'─'*40}")
        for i, r in enumerate(pending, 1):
            score_str = f"{r['trade_score']:.2f}" if r["trade_score"] is not None else "  N/A"
            saved_dt  = r["saved_at"][:10]
            mid_str   = f"{r.get('mid_price', r['entry_price']):.3f}"
            sprd_str  = f"{r['spread']:.3f}" if r.get("spread") is not None else "  N/A"
            liq       = r.get("liquidity", "no_book")
            liq_str   = {"tight": "tight ", "wide": "WIDE  ", "no_book": "no_bk "}.get(liq, liq)
            print(f"  {i:>3}  {r['entry_price']:>6.3f}  {mid_str}  "
                  f"{sprd_str:>5}  {liq_str}  {r['lambda_contract']:>6.3f}  "
                  f"{score_str:>6}  {r['days_at_signal']:>5.1f}  "
                  f"{saved_dt}  {r['question'][:40]}")
        print()

    print(f"  Log file: signals_log.json  ({len(records)} total signals tracked)")
    print()
