"""
Historical backtester for the λ-harvesting (SHORT YES) strategy.

Methodology:
  1. Fetch recently resolved Polymarket markets from Gamma API.
  2. For each market, fetch its full price history (CLOB API) with Unix timestamps.
  3. Walk the price series forward; at each hourly snapshot, check whether all
     execution filters pass (price range, days-to-resolution, EIV, λ threshold).
  4. On the first passing snapshot, record the "entry" and compute P&L at resolution.
  5. Aggregate results into summary statistics.

P&L model (SHORT YES, unit position):
    P&L = entry_price - resolution   (resolution = 1 if YES, 0 if NO)
    → profit when market resolves NO and entry_price > 0
    → loss  when market resolves YES and entry_price < 1

Known limitations:
  - Volume filter uses final cumulative volume (intra-market volume not available).
  - CLOB history may not extend to market open for older contracts.
  - No bid/ask spread, slippage, or position-sizing modeled.
"""

import datetime
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from lambda_harvester.config import (
    MIN_DAYS_TO_RESOLUTION,
    PRICE_LOWER_BOUND,
    PRICE_UPPER_BOUND,
    MAX_VOLUME_USD,
    MIN_VOLUME_USD,
    MIN_PRICE_HISTORY_HOURS,
    PRICE_HISTORY_HOURS,
    MAX_EIV_THRESHOLD,
    MIN_LAMBDA_SIGNAL,
    MIN_TRADE_SCORE,
    LAMBDA_BY_CATEGORY,
    LAMBDA_POLYMARKET,
)
from lambda_harvester.math_engine import (
    compute_lambda,
    compute_eiv,
    compute_trade_score,
    benchmark_p_star_ema,
    benchmark_p_star_wang_prior,
)
from lambda_harvester.polymarket_client import PolymarketClient


@dataclass
class BacktestTrade:
    """One matched historical trade."""
    market_id: str
    question: str
    category: str

    entry_price: float           # p_mkt at signal time (BUY NO entry)
    p_star: float                # estimated physical probability at entry
    lambda_contract: float       # λ at entry
    eiv: Optional[float]         # EIV at entry; None when CLOB history unavailable
    trade_score: Optional[float] # λ/EIV at entry; None when EIV unavailable
    days_at_entry: float         # days to resolution when signal triggered
    volume_usd: float            # cumulative volume (proxy for liquidity)

    resolution: int          # 1 = YES resolved, 0 = NO resolved
    pnl: float               # entry_price - resolution

    @property
    def win(self) -> bool:
        return self.resolution == 0


@dataclass
class BacktestResults:
    trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.win) / len(self.trades)

    @property
    def avg_pnl(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.pnl for t in self.trades]))

    @property
    def total_pnl(self) -> float:
        return float(sum(t.pnl for t in self.trades))

    @property
    def sharpe(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        pnls = np.array([t.pnl for t in self.trades])
        return float(pnls.mean() / (pnls.std(ddof=1) + 1e-9))

    @property
    def avg_lambda(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.lambda_contract for t in self.trades]))

    @property
    def avg_entry_price(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([t.entry_price for t in self.trades]))

    def by_category(self) -> dict[str, dict]:
        cats: dict[str, dict] = {}
        for t in self.trades:
            c = cats.setdefault(t.category, {"n": 0, "wins": 0, "pnl": 0.0})
            c["n"] += 1
            c["wins"] += int(t.win)
            c["pnl"] += t.pnl
        for c in cats.values():
            c["win_rate"] = c["wins"] / c["n"]
            c["avg_pnl"] = c["pnl"] / c["n"]
        return cats


class LambdaBacktester:
    """Replays resolved prediction market contracts to backtest the λ-harvesting signal."""

    def __init__(
        self,
        client=None,
        history_days: int = 60,
        use_ema_benchmark: bool = True,
        verbose: bool = True,
    ):
        self.client = client if client is not None else PolymarketClient()
        self.history_days = history_days
        self.use_ema_benchmark = use_ema_benchmark
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _estimate_p_star(
        self, prior_prices: list[float], p_mkt: float, category: str
    ) -> tuple[float, str]:
        if self.use_ema_benchmark and len(prior_prices) >= MIN_PRICE_HISTORY_HOURS:
            p_star = benchmark_p_star_ema(prior_prices, alpha=0.08)
            if p_star is not None and 1e-4 < p_star < 1 - 1e-4:
                return p_star, "ema"
        lambda_prior = LAMBDA_BY_CATEGORY.get(category, LAMBDA_POLYMARKET)
        return benchmark_p_star_wang_prior(p_mkt, lambda_prior), "wang_prior"

    def _find_entry(
        self,
        prices_ts: list[tuple[int, float]],
        end_dt: datetime.datetime,
        category: str,
        volume: float,
    ) -> Optional[BacktestTrade]:
        """Walk the price series forward; return first snapshot passing all filters."""

        for i in range(MIN_PRICE_HISTORY_HOURS, len(prices_ts)):
            ts, p_mkt = prices_ts[i]
            snapshot_dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)

            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)

            days_remaining = (end_dt - snapshot_dt).total_seconds() / 86400

            # Must have enough time left for the signal to matter
            if days_remaining < MIN_DAYS_TO_RESOLUTION:
                break

            # Price filter
            if not (PRICE_LOWER_BOUND <= p_mkt <= PRICE_UPPER_BOUND):
                continue

            # Volume filter (uses cumulative volume as proxy)
            if volume < MIN_VOLUME_USD or volume > MAX_VOLUME_USD:
                break

            # Gather prior PRICE_HISTORY_HOURS prices for EIV + EMA
            prior_start = max(0, i - PRICE_HISTORY_HOURS)
            prior_prices = [p for _, p in prices_ts[prior_start:i]]

            eiv = compute_eiv(prior_prices) if prior_prices else None
            # Allow None EIV through (CLOB history unavailable for resolved tokens);
            # only reject when EIV is measurable AND too high (noisy market).
            if eiv is not None and eiv > MAX_EIV_THRESHOLD:
                continue

            p_star, _ = self._estimate_p_star(prior_prices, p_mkt, category)
            lambda_contract = compute_lambda(p_mkt, p_star)

            if lambda_contract < MIN_LAMBDA_SIGNAL:
                continue

            trade_score = compute_trade_score(lambda_contract, eiv)
            if trade_score is not None and trade_score < MIN_TRADE_SCORE:
                continue

            # Valid entry found — return a partial trade (outcome filled by caller)
            return _PartialEntry(
                p_mkt=p_mkt,
                p_star=p_star,
                lambda_contract=lambda_contract,
                eiv=eiv,
                trade_score=trade_score,
                days_at_entry=days_remaining,
            )

        return None

    def simulate_market(self, market: dict) -> Optional[BacktestTrade]:
        """Run backtest for a single resolved market."""
        market_id = market.get("id", "unknown")
        question = market.get("question", "")

        outcome = self.client.get_resolution_outcome(market)
        if outcome is None:
            return None

        end_dt = self.client.parse_end_date(market)
        if end_dt is None:
            return None

        token_ids = self.client.get_clob_token_ids(market)
        if not token_ids:
            return None

        volume = self.client.parse_volume(market)
        category = self.client.categorize_market(market)

        # Fetch price history starting history_days ago (or from market start)
        now_ts = int(time.time())
        start_ts = now_ts - self.history_days * 86400

        prices_ts = self.client.get_price_history_with_timestamps(
            token_ids[0], start_ts=start_ts, fidelity=60
        )

        if len(prices_ts) < MIN_PRICE_HISTORY_HOURS:
            return None

        entry = self._find_entry(prices_ts, end_dt, category, volume)
        if entry is None:
            return None

        pnl = entry.p_mkt - outcome  # SHORT YES P&L

        return BacktestTrade(
            market_id=market_id,
            question=question,
            category=category,
            entry_price=entry.p_mkt,
            p_star=entry.p_star,
            lambda_contract=entry.lambda_contract,
            eiv=entry.eiv,
            trade_score=entry.trade_score,
            days_at_entry=entry.days_at_entry,
            volume_usd=volume,
            resolution=outcome,
            pnl=pnl,
        )

    def run(self, max_markets: int = 500) -> BacktestResults:
        """Full backtest: fetch resolved markets, simulate each, return results."""
        self._log(f"\n{'='*70}")
        self._log("λ-BACKTEST — Yang (2026) Wang Transform Strategy")
        self._log(f"{'='*70}")
        self._log(f"Fetching up to {max_markets} resolved Polymarket markets...")

        markets = self.client.get_all_resolved_markets(max_markets=max_markets)
        self._log(f"Fetched {len(markets)} resolved markets. Simulating entries...")

        results = BacktestResults()
        skipped = 0

        for i, market in enumerate(markets):
            if self.verbose and i % 50 == 0:
                self._log(f"  [{i}/{len(markets)}] Matched {results.n} trades so far...")

            trade = self.simulate_market(market)
            if trade is not None:
                results.trades.append(trade)
            else:
                skipped += 1

        self._log(
            f"\nBacktest complete: {results.n} trades from {len(markets)} resolved markets "
            f"({skipped} no valid entry found)."
        )
        return results


# ── Internal helper ────────────────────────────────────────────────────────────

@dataclass
class _PartialEntry:
    p_mkt: float
    p_star: float
    lambda_contract: float
    eiv: Optional[float]
    trade_score: Optional[float]
    days_at_entry: float
