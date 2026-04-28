"""
λ-Harvesting Bot — CLI entry point (platform-agnostic version for publication).

Usage:
    python -m lambda_harvester.main_publish
    python -m lambda_harvester.main_publish --max-markets 500
    python -m lambda_harvester.main_publish --top 10 --no-ema
    python -m lambda_harvester.main_publish --demo
"""

import argparse
import datetime
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from lambda_harvester.scanner import LambdaScanner, ContractAnalysis
from lambda_harvester.backtest import LambdaBacktester, BacktestResults
from lambda_harvester.polymarket_client import PolymarketClient
from lambda_harvester.kalshi_client import KalshiClient
from lambda_harvester.kalshi_matcher import KalshiMatcher
from lambda_harvester.config import (
    MIN_DAYS_TO_RESOLUTION,
    PRICE_LOWER_BOUND,
    PRICE_UPPER_BOUND,
    MAX_VOLUME_USD,
    MIN_VOLUME_USD,
    MAX_EIV_THRESHOLD,
    MIN_LAMBDA_SIGNAL,
    MIN_TRADE_SCORE,
    LAMBDA_POLYMARKET,
)


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _bar(label: str, width: int = 70) -> str:
    pad = width - len(label) - 4
    return f"{'─' * 2} {label} {'─' * pad}"


def print_header():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║          λ-HARVESTING BOT  —  Yang (2026) Wang Transform            ║")
    print("║          Polymarket Structural Risk Premium Scanner                  ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print(f"  Run timestamp : {now}")
    print(f"  Strategy      : BUY NO on p_mkt >> p* in quiet (low-EIV) markets")
    print(f"  Platform λ̂    : {LAMBDA_POLYMARKET:.3f}  (MLE, Yang 2026 Table 18, N=2,460)")
    print()


def print_filter_summary():
    print(_bar("EXECUTION FILTERS (Yang 2026 + Plan Document)"))
    print(f"  Price range     : [{PRICE_LOWER_BOUND:.2f}, {PRICE_UPPER_BOUND:.2f}]"
          f"  — longshot focus; FLB strongest here (Corollary 1)")
    print(f"  Duration        : > {MIN_DAYS_TO_RESOLUTION}d"
          f"              — λ̂ = 0.444 for >7d contracts (Table 10)")
    print(f"  Volume          : ${MIN_VOLUME_USD:,} – ${MAX_VOLUME_USD:,}"
          f"   — wedge competed away above $10K (Table 14)")
    print(f"  EIV threshold   : ≤ {MAX_EIV_THRESHOLD:.2f}"
          f"             — quiet market = structural premium, not information")
    print(f"  Min λ signal    : ≥ {MIN_LAMBDA_SIGNAL:.2f}"
          f"             — below global Polymarket mean = noise")
    print(f"  Min trade score : λ/EIV ≥ {MIN_TRADE_SCORE:.1f}")
    print()


def print_opportunity(rank: int, a: ContractAnalysis):
    eiv_str = f"{a.eiv:.4f}" if a.eiv is not None else "N/A"
    score_str = f"{a.trade_score:.2f}" if a.trade_score is not None else "N/A"
    excess_sign = "+" if a.lambda_excess >= 0 else ""

    tier = ""
    if a.lambda_contract >= 0.30:
        tier = "🔴 STRONG"
    elif a.lambda_contract >= 0.20:
        tier = "🟡 MEDIUM"
    else:
        tier = "🟢 WEAK"

    print(f"{'═'*70}")
    print(f"  #{rank}  {tier}  |  Trade Score (λ/EIV): {score_str}")
    print(f"{'═'*70}")
    print(f"  Market   : {a.question[:65]}")
    print(f"  Category : {a.category.upper():<12}  Days to resolution: {a.days_to_resolution:.1f}d")
    print(f"  Volume   : ${a.volume_usd:>10,.0f}")
    print()
    print(f"  ┌─ Pricing ──────────────────────────────────────────────────────┐")
    print(f"  │  Market Price  (p_mkt) : {a.p_mkt:.4f}  ({a.p_mkt*100:.1f}%)                    │")
    print(f"  │  Physical p*           : {a.p_star:.4f}  ({a.p_star*100:.1f}%)  [{a.p_star_method}]           │")
    print(f"  │  Overpricing           : {a.overpricing_pct:+.1f}%                              │")
    print(f"  └────────────────────────────────────────────────────────────────┘")
    print()
    print(f"  ┌─ Wang Transform (λ) ───────────────────────────────────────────┐")
    print(f"  │  λ (contract)  : {a.lambda_contract:+.4f}                                   │")
    print(f"  │  λ (expected)  : {a.lambda_expected:+.4f}  [Yang 2026 benchmark]            │")
    print(f"  │  λ (excess)    : {excess_sign}{a.lambda_excess:.4f}  [abnormal wedge]                  │")
    print(f"  │  EIV           :  {eiv_str}  [n={a.n_price_obs} hourly obs]              │")
    print(f"  │  Trade Score   :  {score_str}  [λ/EIV — primary rank metric]      │")
    print(f"  └────────────────────────────────────────────────────────────────┘")
    print()
    print(f"  ► RECOMMENDATION: {a.recommendation}")
    print(f"    Token ID: {a.token_id}")
    print()


def print_opportunity_compact(rank: int, a: ContractAnalysis):
    score_str = f"{a.trade_score:.2f}" if a.trade_score is not None else " N/A"
    eiv_str = f"{a.eiv:.3f}" if a.eiv is not None else "N/A "
    q = a.question[:48].ljust(48)
    print(f"  {rank:>3}. [{score_str:>5}] {q} "
          f"p={a.p_mkt:.2f} λ={a.lambda_contract:.3f} EIV={eiv_str} "
          f"{a.category[:6].upper()}")


def print_summary_table(results: list[ContractAnalysis]):
    print(_bar("OPPORTUNITY SUMMARY TABLE"))
    print(f"  {'#':>3}  {'Score':>6}  {'Question':<48}  {'p_mkt':>5}  "
          f"{'λ':>6}  {'EIV':>6}  {'Cat':>6}  {'Days':>5}")
    print(f"  {'─'*3}  {'─'*6}  {'─'*48}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*5}")
    for i, a in enumerate(results, 1):
        score_str = f"{a.trade_score:.2f}" if a.trade_score is not None else "  N/A"
        eiv_str = f"{a.eiv:.3f}" if a.eiv is not None else "  N/A"
        q = a.question[:48].ljust(48)
        print(f"  {i:>3}  {score_str:>6}  {q}  {a.p_mkt:>5.3f}  "
              f"{a.lambda_contract:>6.3f}  {eiv_str:>6}  {a.category[:6].upper():>6}  "
              f"{a.days_to_resolution:>5.1f}")
    print()


def print_stats(results: list[ContractAnalysis], total_scanned: int):
    print(_bar("SCAN STATISTICS"))
    if not results:
        print("  No opportunities found matching all execution filters.")
        return

    import numpy as np
    lambdas = [r.lambda_contract for r in results]
    scores = [r.trade_score for r in results if r.trade_score is not None]
    eivs = [r.eiv for r in results if r.eiv is not None]

    print(f"  Opportunities found : {len(results)} / {total_scanned} scanned")
    print(f"  λ  — mean: {np.mean(lambdas):.3f}  max: {max(lambdas):.3f}  min: {min(lambdas):.3f}")
    if scores:
        print(f"  Score — mean: {np.mean(scores):.2f}  max: {max(scores):.2f}  min: {min(scores):.2f}")
    if eivs:
        print(f"  EIV  — mean: {np.mean(eivs):.4f}  max: {max(eivs):.4f}  min: {min(eivs):.4f}")

    cats = {}
    for r in results:
        cats[r.category] = cats.get(r.category, 0) + 1
    print(f"  Categories : {dict(sorted(cats.items(), key=lambda x: -x[1]))}")
    print()


def print_backtest_results(results: BacktestResults):
    cats = results.by_category()
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║                 λ-BACKTEST RESULTS — BUY NO                         ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()
    if results.n == 0:
        print("  No trades matched. Try --backtest-markets N (larger) or widen config.py filters.")
        return
    print(_bar("AGGREGATE PERFORMANCE"))
    print(f"  Trades             : {results.n}")
    print(f"  Win rate (NO res.) : {results.win_rate:.1%}")
    print(f"  Avg P&L / trade    : {results.avg_pnl:+.4f}  (per $1 invested)")
    print(f"  Total P&L          : {results.total_pnl:+.4f}")
    print(f"  Sharpe (P&L/σ)     : {results.sharpe:+.3f}")
    print(f"  Avg λ at entry     : {results.avg_lambda:.4f}")
    print(f"  Avg entry price    : {results.avg_entry_price:.4f}")
    print()
    print(_bar("BREAKDOWN BY CATEGORY"))
    print(f"  {'Cat':<10}  {'N':>4}  {'Win%':>6}  {'Avg PnL':>8}  {'Total PnL':>10}")
    print(f"  {'─'*10}  {'─'*4}  {'─'*6}  {'─'*8}  {'─'*10}")
    for cat, m in sorted(cats.items(), key=lambda x: -x[1]["avg_pnl"]):
        print(f"  {cat:<10}  {m['n']:>4}  {m['win_rate']:>6.1%}  "
              f"{m['avg_pnl']:>+8.4f}  {m['pnl']:>+10.4f}")
    print()


def _build_client(platform: str):
    if platform == "kalshi":
        return None
    return PolymarketClient()


def run_backtest(client, max_markets: int, history_days: int, no_ema: bool, quiet: bool):
    print_header()
    print(f"  [BACKTEST MODE — {history_days}d price history window, up to {max_markets} resolved markets]\n")
    print_filter_summary()
    bt = LambdaBacktester(
        client=client,
        history_days=history_days,
        use_ema_benchmark=not no_ema,
        verbose=not quiet,
    )
    results = bt.run(max_markets=max_markets)
    print_backtest_results(results)


def run_demo():
    from lambda_harvester.scanner import ContractAnalysis
    demo = ContractAnalysis(
        market_id="demo-001",
        question="Will Bitcoin exceed $100,000 before May 31 2026?",
        category="crypto",
        days_to_resolution=18.3,
        volume_usd=2450.0,
        token_id="71321083...",
        p_mkt=0.150,
        p_star=0.080,
        p_star_method="ema",
        lambda_contract=0.2412,
        lambda_expected=0.2530,
        lambda_excess=-0.0118,
        eiv=0.0823,
        trade_score=2.93,
        n_price_obs=42,
        overpricing_pct=87.5,
        tau=0.04,
        passes_filters=True,
        filter_reasons=[],
    )
    print_header()
    print("  [DEMO MODE — synthetic data, no API calls]\n")
    print_filter_summary()
    print(_bar("TOP OPPORTUNITIES  (ranked by Trade Score λ/EIV)"))
    print()
    print_opportunity(1, demo)


def main():
    parser = argparse.ArgumentParser(
        description="λ-Harvesting Bot — platform-agnostic version for publication."
    )
    parser.add_argument("--platform", choices=["polymarket", "kalshi"], default="polymarket")
    parser.add_argument("--max-markets", type=int, default=3000, metavar="N")
    parser.add_argument("--top", type=int, default=5, metavar="N")
    parser.add_argument("--no-ema", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--backtest-markets", type=int, default=500, metavar="N")
    parser.add_argument("--history-days", type=int, default=60, metavar="N")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return

    client = _build_client(args.platform)
    kalshi_matcher = KalshiMatcher(verbose=not args.quiet) if args.platform == "kalshi" else None

    if args.backtest:
        run_backtest(client=client, max_markets=args.backtest_markets,
                     history_days=args.history_days, no_ema=args.no_ema, quiet=args.quiet)
        return

    print_header()
    if kalshi_matcher:
        print("  [CROSS-PLATFORM MODE: signals from Polymarket → trades on Kalshi]\n")
    print_filter_summary()

    scanner = LambdaScanner(client=client, use_ema_benchmark=not args.no_ema, verbose=not args.quiet)
    results = scanner.scan(max_markets=args.max_markets)

    if not results:
        print("\n  No opportunities passed all execution filters.")
        sys.exit(0)

    print()
    print(_bar(f"TOP OPPORTUNITIES  (ranked by Trade Score λ/EIV)  [{len(results)} found]"))
    print()

    top_n = results[:args.top]

    if args.compact:
        print(f"  {'#':>3}  {'Score':>6}  {'Question':<48}  p_mkt  λ      EIV    Cat")
        print(f"  {'─'*3}  {'─'*6}  {'─'*48}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*4}")
        for i, a in enumerate(top_n, 1):
            print_opportunity_compact(i, a)
    else:
        for i, a in enumerate(top_n, 1):
            print_opportunity(i, a)
            if kalshi_matcher:
                match = kalshi_matcher.find_equivalent(a.question)
                if match:
                    km, kp, ks = match
                    print(kalshi_matcher.format_trade(km, kp, ks))
                else:
                    print("  KALSHI EQUIVALENT: None found\n")

    if len(results) > args.top:
        print(_bar(f"REMAINING {len(results) - args.top} OPPORTUNITIES (compact)"))
        print()
        for i, a in enumerate(results[args.top:], args.top + 1):
            print_opportunity_compact(i, a)
        print()

    print_summary_table(top_n)
    print_stats(results, args.max_markets)

    print(_bar("ALERTS"))
    print()
    for a in top_n:
        eiv_str = f"{a.eiv:.4f}" if a.eiv is not None else "near-zero"
        print(f"  Opportunity Found: {a.question[:65]}")
        print(f"  Market Price is ${a.p_mkt:.2f}, Statistical Model says ${a.p_star:.2f}.")
        print(f"  λ is {a.lambda_contract:.2f} with EIV={eiv_str}.")
        print(f"  Recommendation: {a.recommendation}")
        print()

    print("═" * 70)
    print("  Scan complete. Review recommendations before trading.")
    print("═" * 70)
    print()


if __name__ == "__main__":
    main()
