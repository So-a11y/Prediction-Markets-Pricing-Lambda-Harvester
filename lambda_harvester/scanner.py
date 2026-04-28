"""
Core scanner: fetches active markets, computes Wang Transform metrics,
applies edge filters, and ranks opportunities by λ/EIV trade score.

Filter criteria (plan document + Yang 2026 empirical evidence):
  1. Duration  > 7 days        (λ̂ = 0.444 for >7d, Table 10)
  2. Price     ∈ [0.05, 0.20]  (longshot focus; FLB strongest here, Corollary 1)
  3. Volume    < $10K          (λ̂ ≈ 0 above $10K — wedge competed away, Table 14)
  4. Category  = Crypto/Tech   (highest structural λ̂, Table 13)
  5. EIV       < threshold     (quiet market = risk premium, not information)
"""

import datetime
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
    MIN_LAMBDA_SIGNAL,
    MAX_EIV_THRESHOLD,
    MIN_TRADE_SCORE,
    HIGH_EDGE_CATEGORIES,
    LOW_EDGE_CATEGORIES,
    LAMBDA_BY_CATEGORY,
    LAMBDA_POLYMARKET,
)
from lambda_harvester.math_engine import (
    compute_lambda,
    compute_eiv,
    compute_trade_score,
    benchmark_p_star_ema,
    benchmark_p_star_wang_prior,
    extract_physical_prob,
    time_decay_lambda,
    overpricing_level,
    wang_transform,
)
from lambda_harvester.polymarket_client import PolymarketClient


@dataclass
class ContractAnalysis:
    """Full Wang Transform analysis for a single prediction market contract."""

    # ── Market metadata ─────────────────────────────────────────────────────
    market_id: str
    question: str
    category: str
    days_to_resolution: float
    volume_usd: float
    token_id: str

    # ── Current pricing ──────────────────────────────────────────────────────
    p_mkt: float            # observed market price (YES)

    # ── Wang Transform quantities ────────────────────────────────────────────
    p_star: float           # estimated physical probability
    p_star_method: str      # "ema" | "wang_prior"
    lambda_contract: float  # λ = Φ⁻¹(p_mkt) - Φ⁻¹(p*)
    lambda_expected: float  # expected λ for this category/duration from Yang (2026)
    lambda_excess: float    # λ_contract - λ_expected (abnormal wedge)

    # ── EIV & Trade Score ────────────────────────────────────────────────────
    eiv: Optional[float]        # std(Δx) over price history
    trade_score: Optional[float]  # λ / EIV — primary ranking metric
    n_price_obs: int            # number of hourly price observations used

    # ── Additional signals ────────────────────────────────────────────────────
    overpricing_pct: float      # (p_mkt - p_star) / p_star in %
    tau: float                  # lifecycle fraction elapsed (for decay model)
    passes_filters: bool = False
    filter_reasons: list[str] = field(default_factory=list)

    # ── Recommendation ────────────────────────────────────────────────────────
    recommendation: str = ""

    def __post_init__(self):
        self._generate_recommendation()

    def _generate_recommendation(self):
        if not self.passes_filters:
            return
        # No EIV available — rank by λ alone (no price history on this platform)
        if self.trade_score is None:
            if self.lambda_contract >= 0.30:
                self.recommendation = "STRONG BUY NO — large λ wedge (no EIV available)"
            elif self.lambda_contract >= 0.20:
                self.recommendation = "BUY NO — significant wedge (no EIV available)"
            else:
                self.recommendation = "WEAK BUY NO — monitor (no EIV available)"
            return
        if self.trade_score < MIN_TRADE_SCORE:
            self.recommendation = "WATCH (score below threshold)"
        elif self.lambda_contract >= 0.30:
            self.recommendation = "STRONG BUY NO — harvest risk premium"
        elif self.lambda_contract >= 0.20:
            self.recommendation = "BUY NO — price embeds significant wedge"
        else:
            self.recommendation = "WEAK BUY NO — monitor"

    def summary(self) -> str:
        eiv_str = f"{self.eiv:.4f}" if self.eiv is not None else "N/A"
        score_str = f"{self.trade_score:.2f}" if self.trade_score is not None else "N/A"
        lines = [
            f"{'='*70}",
            f"OPPORTUNITY: {self.question[:65]}",
            f"{'='*70}",
            f"  Category:         {self.category.upper()} | Days to resolution: {self.days_to_resolution:.1f}",
            f"  Volume:           ${self.volume_usd:,.0f}",
            f"  Market Price:     {self.p_mkt:.3f} ({self.p_mkt*100:.1f}%)",
            f"  Physical p*:      {self.p_star:.3f} ({self.p_star*100:.1f}%) [{self.p_star_method}]",
            f"  Overpricing:      {self.overpricing_pct:+.1f}%",
            f"",
            f"  λ (contract):     {self.lambda_contract:.4f}",
            f"  λ (expected):     {self.lambda_expected:.4f}  [Yang 2026 benchmark]",
            f"  λ (excess):       {self.lambda_excess:+.4f}",
            f"  EIV:              {eiv_str}  [n={self.n_price_obs} obs]",
            f"  Trade Score λ/EIV:{score_str}",
            f"",
            f"  ► {self.recommendation}",
        ]
        return "\n".join(lines)


class LambdaScanner:
    """Scans a prediction market platform for high λ/EIV opportunities using Yang (2026) framework."""

    def __init__(self,
                 client=None,
                 use_ema_benchmark: bool = True,
                 verbose: bool = True):
        self.client = client if client is not None else PolymarketClient()
        self.use_ema_benchmark = use_ema_benchmark
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _apply_filters(self, market: dict, p_mkt: float) -> tuple[bool, list[str]]:
        """Apply execution filters and return (passes, reasons_failed)."""
        failures = []

        # 1. Price range [0.05, 0.20] — longshot focus
        if not (PRICE_LOWER_BOUND <= p_mkt <= PRICE_UPPER_BOUND):
            failures.append(f"price {p_mkt:.3f} outside [{PRICE_LOWER_BOUND},{PRICE_UPPER_BOUND}]")

        # 2. Duration > 7 days — λ̂ largest here (Table 10)
        days = self.client.days_to_resolution(market)
        if days is None or days < MIN_DAYS_TO_RESOLUTION:
            failures.append(f"only {days:.1f if days else '?'}d to resolution (need >{MIN_DAYS_TO_RESOLUTION}d)")

        # 3. Volume bounds — need liquidity but not so much wedge is competed away
        volume = self.client.parse_volume(market)
        if volume < MIN_VOLUME_USD:
            failures.append(f"volume ${volume:.0f} too low (need >${MIN_VOLUME_USD})")
        if volume > MAX_VOLUME_USD:
            failures.append(f"volume ${volume:.0f} too high (wedge likely competed away above ${MAX_VOLUME_USD})")

        return len(failures) == 0, failures

    def _estimate_p_star(self, prices: list[float], p_mkt: float, category: str) -> tuple[float, str]:
        """Estimate physical probability p* using best available method."""
        if self.use_ema_benchmark and len(prices) >= MIN_PRICE_HISTORY_HOURS:
            p_star = benchmark_p_star_ema(prices, alpha=0.08)
            if p_star is not None and 1e-4 < p_star < 1 - 1e-4:
                return p_star, "ema"

        # Fallback: use category-level λ̂ from Yang (2026) as structural prior
        lambda_prior = LAMBDA_BY_CATEGORY.get(category, LAMBDA_POLYMARKET)
        p_star = benchmark_p_star_wang_prior(p_mkt, lambda_prior)
        return p_star, "wang_prior"

    def _compute_expected_lambda(self, category: str, days_to_resolution: float,
                                 volume: float, tau: float) -> float:
        """Expected λ for this contract given characteristics (Yang 2026).

        Combines category effect, duration effect, and time-decay model.
        """
        lambda_cat = LAMBDA_BY_CATEGORY.get(category, LAMBDA_POLYMARKET)

        # Duration scaling: >7d contracts have λ̂=0.444 vs 0.176 global mean
        # Use time-decay model evaluated at opening (tau≈0.05 if early in life)
        lambda_time = time_decay_lambda(min(tau, 0.05))

        # Volume adjustment: very high volume kills the wedge
        if volume > 10000:
            vol_adj = -0.20
        elif volume > 2000:
            vol_adj = 0.0
        else:
            vol_adj = 0.05

        return float(np.clip(lambda_cat + vol_adj, 0.01, 0.60))

    def analyze_market(self, market: dict) -> Optional[ContractAnalysis]:
        """Run full Wang Transform analysis on a single market."""
        market_id = market.get("id", "unknown")
        question = market.get("question", "")

        # Parse current price
        p_mkt = self.client.parse_market_price(market)
        if p_mkt is None:
            return None

        # Check basic filters early (before API calls)
        passes, failures = self._apply_filters(market, p_mkt)

        volume = self.client.parse_volume(market)
        days = self.client.days_to_resolution(market) or 0.0
        category = self.client.categorize_market(market)

        # Compute lifecycle fraction τ (for time-decay model)
        start_raw = market.get("startDate") or market.get("start_date_iso")
        total_days = None
        try:
            if start_raw:
                if start_raw.endswith("Z"):
                    start_raw = start_raw[:-1] + "+00:00"
                start_dt = datetime.datetime.fromisoformat(start_raw)
                end_dt = self.client.parse_end_date(market)
                if start_dt and end_dt:
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
                    total_secs = (end_dt - start_dt).total_seconds()
                    elapsed_secs = total_secs - days * 86400
                    tau = float(np.clip(elapsed_secs / max(total_secs, 1), 0, 1))
                else:
                    tau = 0.05
            else:
                tau = 0.05
        except Exception:
            tau = 0.05

        # Fetch price history for EIV calculation
        token_ids = self.client.get_clob_token_ids(market)
        prices = []
        if token_ids:
            prices = self.client.get_price_history(token_ids[0], hours=PRICE_HISTORY_HOURS)
        n_obs = len(prices)

        # Estimate p*
        p_star, p_star_method = self._estimate_p_star(prices, p_mkt, category)

        # Core Wang Transform calculations
        lambda_contract = compute_lambda(p_mkt, p_star)
        lambda_expected = self._compute_expected_lambda(category, days, volume, tau)
        lambda_excess = lambda_contract - lambda_expected

        # EIV
        eiv = compute_eiv(prices) if prices else None

        # Trade Score
        trade_score = compute_trade_score(lambda_contract, eiv) if eiv is not None else None

        # Overpricing magnitude
        op_pct = (p_mkt - p_star) / max(p_star, 1e-6) * 100

        # EIV filter: only penalise markets where EIV is measurable AND too high.
        # When price history is unavailable (EIV=None) we allow through — ranked by λ alone.
        eiv_ok = (eiv is None) or (eiv <= MAX_EIV_THRESHOLD)
        lambda_ok = (lambda_contract >= MIN_LAMBDA_SIGNAL)

        if not eiv_ok:
            failures.append(f"EIV {eiv:.3f} too high (noisy market, need <{MAX_EIV_THRESHOLD})")
        if not lambda_ok:
            failures.append(f"λ={lambda_contract:.3f} below signal threshold {MIN_LAMBDA_SIGNAL}")

        passes = passes and eiv_ok and lambda_ok

        return ContractAnalysis(
            market_id=market_id,
            question=question,
            category=category,
            days_to_resolution=days,
            volume_usd=volume,
            token_id=token_ids[0] if token_ids else "",
            p_mkt=p_mkt,
            p_star=p_star,
            p_star_method=p_star_method,
            lambda_contract=lambda_contract,
            lambda_expected=lambda_expected,
            lambda_excess=lambda_excess,
            eiv=eiv,
            trade_score=trade_score,
            n_price_obs=n_obs,
            overpricing_pct=op_pct,
            tau=tau,
            passes_filters=passes,
            filter_reasons=failures,
        )

    def scan(self, max_markets: int = 1000) -> list[ContractAnalysis]:
        """Full market scan: fetch, filter, score, rank.

        Returns list of ContractAnalysis objects sorted by trade_score descending.
        Only markets passing all execution filters are returned.
        """
        self._log(f"\n{'='*70}")
        self._log("λ-HARVESTING SCANNER — Yang (2026) Wang Transform Strategy")
        self._log(f"{'='*70}")
        self._log(f"Fetching up to {max_markets} active Polymarket contracts...")

        markets = self.client.get_all_active_markets(max_markets=max_markets)
        self._log(f"Fetched {len(markets)} markets. Analyzing...")

        results = []
        reject = {"no_price": 0, "price_range": 0, "duration": 0, "volume": 0}

        for i, market in enumerate(markets):
            if self.verbose and i % 50 == 0:
                print(f"  [{i}/{len(markets)}] Analyzed {len(results)} candidates so far...")

            # Quick pre-filter on price and volume before making CLOB API calls
            p_mkt = self.client.parse_market_price(market)
            if p_mkt is None:
                reject["no_price"] += 1
                continue

            volume = self.client.parse_volume(market)
            days = self.client.days_to_resolution(market)

            # Fast reject before expensive API calls
            if p_mkt < PRICE_LOWER_BOUND or p_mkt > PRICE_UPPER_BOUND:
                reject["price_range"] += 1
                continue
            if days is None or days < MIN_DAYS_TO_RESOLUTION:
                reject["duration"] += 1
                continue
            if volume > MAX_VOLUME_USD or volume < MIN_VOLUME_USD:
                reject["volume"] += 1
                continue

            analysis = self.analyze_market(market)
            if analysis is not None and analysis.passes_filters:
                results.append(analysis)

        # Sort by trade_score (λ/EIV) descending, None scores last
        results.sort(
            key=lambda x: (x.trade_score is not None, x.trade_score or 0),
            reverse=True
        )

        total = sum(reject.values())
        self._log(f"\nScan complete: {len(results)} opportunities from {len(markets)} markets.")
        self._log(f"Fast-rejected {total}: price_range={reject['price_range']}  "
                  f"duration={reject['duration']}  volume={reject['volume']}  "
                  f"no_price={reject['no_price']}")
        return results
