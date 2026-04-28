"""
Mathematical engine implementing the Wang Transform framework from:
Yang (2026) "Pricing Prediction Markets: Incomplete Markets, Selection Rules, and Risk Premia"

Core model (Proposition 2, Eq. 1):
    p_mkt = Φ(Φ⁻¹(p*) + λ)

where:
    p_mkt : observed market price
    p*    : physical (objective) event probability
    λ     : pricing-measure selection parameter (pricing wedge)
    Φ     : standard normal CDF
    Φ⁻¹   : inverse normal CDF (probit / norm.ppf)

The pricing wedge λ is positive on real-money platforms (Table 18, Yang 2026):
    Polymarket: λ̂ ≈ 0.176 (SE=0.027)
    Kalshi:     λ̂ ≈ 0.187 (SE=0.003)
    Manifold:   λ̂ ≈ -0.218 (play-money; overconfidence, no risk premium)
"""

import numpy as np
from scipy.stats import norm
from typing import Optional, Union

from lambda_harvester.config import LAMBDA_POLYMARKET, MIN_PRICE_HISTORY_HOURS

_EPSILON = 1e-6


# ── Probability <-> log-odds transformations ───────────────────────────────────

def logit(p: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """Log-odds transformation: x = ln(p / (1-p)).

    This is the natural state variable for prediction market prices (Section 3.2,
    Yang 2026). Just as ln(S) maps stock prices to R, logit(p) maps probabilities
    to R with constant diffusion coefficient under the martingale-consistent model.
    """
    p = np.clip(p, _EPSILON, 1 - _EPSILON)
    return np.log(p / (1 - p))


def sigmoid(x: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """Inverse logit: p = 1 / (1 + exp(-x))."""
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=float)))


# ── Wang Transform (Proposition 2, Yang 2026) ─────────────────────────────────

def wang_transform(p_star: float, lambda_val: float) -> float:
    """Forward Wang Transform: p_mkt = Φ(Φ⁻¹(p*) + λ).

    Under the latent Gaussian threshold model with one-parameter exponential tilt
    dQλ/dP = exp(λZ - ½λ²), the market price satisfies this exact relation.

    Args:
        p_star:     Physical event probability (objective measure P)
        lambda_val: Pricing wedge parameter λ

    Returns:
        Risk-adjusted market price p_mkt ∈ (0,1)
    """
    p_star = np.clip(p_star, _EPSILON, 1 - _EPSILON)
    return float(norm.cdf(norm.ppf(p_star) + lambda_val))


def extract_physical_prob(p_mkt: float, lambda_hat: float = LAMBDA_POLYMARKET) -> float:
    """Inverse Wang: strip pricing wedge to recover physical probability.

    p̂* = Φ(Φ⁻¹(p_mkt) - λ̂)

    Used to convert observed market prices into unbiased probability estimates.
    Under Yang (2026) calibration, this reduces ECE by 53.8% on Polymarket.

    Args:
        p_mkt:      Observed market mid-price
        lambda_hat: Estimated platform-level pricing wedge (default: Polymarket MLE)

    Returns:
        Estimated physical probability p̂* ∈ (0,1)
    """
    p_mkt = np.clip(p_mkt, _EPSILON, 1 - _EPSILON)
    return float(norm.cdf(norm.ppf(p_mkt) - lambda_hat))


def compute_lambda(p_mkt: float, p_star: float) -> float:
    """Compute contract-level pricing wedge: λ = Φ⁻¹(p_mkt) - Φ⁻¹(p*).

    This is the central quantity in the Yang (2026) framework. A large positive λ
    implies the market price embeds a significant risk premium above the physical
    probability — the "pricing wedge" or λ-harvesting opportunity.

    Args:
        p_mkt:  Current market mid-price
        p_star: Benchmark physical probability estimate

    Returns:
        Pricing wedge λ (positive = market overprices relative to p*)
    """
    p_mkt = np.clip(p_mkt, _EPSILON, 1 - _EPSILON)
    p_star = np.clip(p_star, _EPSILON, 1 - _EPSILON)
    return float(norm.ppf(p_mkt) - norm.ppf(p_star))


# ── Physical probability benchmarks ───────────────────────────────────────────

def benchmark_p_star_ema(prices: list[float], alpha: float = 0.08) -> Optional[float]:
    """Estimate p* as exponential moving average of log-odds (slow-moving reference).

    Rationale: The pricing wedge is largest at contract opening and decays monotonically
    (Section 5.3, Yang 2026). A slow EMA captures the "fundamental" price level while
    filtering out short-term pricing wedge inflation.

    Uses log-odds space (not raw prices) per Section 3.2: logit is the canonical
    state variable — analogous to log-price in equity option pricing.

    Args:
        prices: Time-ordered price history
        alpha:  EMA smoothing factor (smaller = slower-moving, more conservative p*)

    Returns:
        p* estimate as probability, or None if insufficient data
    """
    prices = [np.clip(p, _EPSILON, 1 - _EPSILON) for p in prices]
    if not prices:
        return None
    log_odds = [float(logit(p)) for p in prices]
    ema = log_odds[0]
    for x in log_odds[1:]:
        ema = alpha * x + (1 - alpha) * ema
    return float(sigmoid(ema))


def benchmark_p_star_wang_prior(p_mkt: float, lambda_prior: float = LAMBDA_POLYMARKET) -> float:
    """Estimate p* using platform-level λ̂ as Bayesian prior.

    p̂* = Φ(Φ⁻¹(p_mkt) - λ̂_prior)

    This is the cleanest single-number approach when no historical price data is
    available. Uses the cross-sectional MLE from Yang (2026) as a structural prior.

    Args:
        p_mkt:        Current market price
        lambda_prior: Platform-level λ̂ (Yang 2026 MLE)

    Returns:
        Estimated physical probability
    """
    return extract_physical_prob(p_mkt, lambda_prior)


# ── Event Implied Volatility (Section 7.1, Yang 2026) ─────────────────────────

def compute_eiv(prices: list[float], annualize: bool = False) -> Optional[float]:
    """Compute realized Event Implied Volatility from log-odds increments.

    EIV = std(Δx_t)  where  x_t = logit(p_t)

    Conceptually equivalent to realized volatility in equity markets (Section 7.1,
    Yang 2026). Low EIV = "quiet market" where price wedge is structural risk premium
    rather than information-driven movement.

    From Yang (2026) empirical distribution (Table 20):
        Median annualized EIV ≈ 5.31 (IQR: [1.52, 16.73])
        Crypto contracts:   median ≈ 11.99 (highest)
        Sports contracts:   median ≈ 5.50

    Args:
        prices:     Time-ordered hourly prices
        annualize:  If True, annualize by sqrt(8760) for hourly data

    Returns:
        EIV (standard deviation of log-odds changes), or None if insufficient data
    """
    if len(prices) < MIN_PRICE_HISTORY_HOURS:
        return None

    arr = np.array([np.clip(p, _EPSILON, 1 - _EPSILON) for p in prices])
    log_odds = logit(arr)
    delta_x = np.diff(log_odds)

    if len(delta_x) < 2:
        return None

    eiv = float(np.std(delta_x, ddof=1))

    if annualize:
        eiv *= np.sqrt(8760)

    return eiv


def compute_vol_clustering(prices: list[float]) -> Optional[float]:
    """AC1 of squared log-odds increments — volatility clustering measure.

    From Table 3 (Yang 2026): median AC1(Δx²) = 0.16 across 2,036 contracts,
    confirming significant ARCH effects in prediction market prices.

    Returns:
        Lag-1 autocorrelation of squared increments (positive = clustering)
    """
    if len(prices) < 20:
        return None

    arr = np.array([np.clip(p, _EPSILON, 1 - _EPSILON) for p in prices])
    log_odds = logit(arr)
    delta_x = np.diff(log_odds)
    sq = delta_x ** 2

    if len(sq) < 4:
        return None

    corr = np.corrcoef(sq[:-1], sq[1:])[0, 1]
    return float(corr)


# ── Trade scoring ──────────────────────────────────────────────────────────────

def compute_trade_score(lambda_val: float, eiv: float, min_eiv: float = 1e-4) -> Optional[float]:
    """Trade Score = λ / EIV.

    The core ranking metric from the plan document. A high ratio means:
    - Large pricing wedge (market overprices event)
    - Low information arrival rate (wedge is structural, not news-driven)

    This is the signal to SHORT the YES side and harvest the risk premium.

    Args:
        lambda_val: Contract-level pricing wedge
        eiv:        Event Implied Volatility (raw, not annualized)
        min_eiv:    Floor to avoid division by near-zero EIV

    Returns:
        Trade score (higher = stronger SHORT signal on YES)
    """
    if eiv is None or np.isnan(eiv) or eiv < min_eiv:
        return None
    if lambda_val <= 0:
        return None
    return float(lambda_val / max(eiv, min_eiv))


def lambda_sensitivity(p_star: float, lambda_val: float) -> float:
    """∂p_mkt/∂λ = φ(Φ⁻¹(p*) + λ) — how much market price moves per unit λ.

    From Section 3.5 (Yang 2026). Maximum sensitivity at p* ≈ 0.5.
    """
    p_star = np.clip(p_star, _EPSILON, 1 - _EPSILON)
    return float(norm.pdf(norm.ppf(p_star) + lambda_val))


def overpricing_level(p_mkt: float, p_star: float) -> float:
    """Δp = p_mkt - p* — level overpricing in probability units.

    From Corollary 1 (Yang 2026): maximized at p* = Φ(-λ/2).
    For λ=0.176 (Polymarket MLE), maximum at p* ≈ Φ(-0.088) ≈ 0.465.
    """
    return float(p_mkt - p_star)


def time_decay_lambda(tau: float,
                      beta0: float = 0.170,
                      gamma1: float = -0.232,
                      gamma2: float = 0.151) -> float:
    """Expected λ at lifecycle fraction τ (stacked panel model, Table 6, Yang 2026).

    λ(τ) = β₀ + γ₁τ + γ₂τ²

    Empirical finding: pricing wedge falls from λ̂(0.05)=0.173 at opening to
    λ̂(0.80)=0.037 near resolution, with half-life at 33% of contract lifetime.

    Args:
        tau: Fraction of contract lifetime elapsed ∈ [0, 1]

    Returns:
        Expected pricing wedge at this lifecycle stage
    """
    tau = np.clip(tau, 0.0, 1.0)
    return float(beta0 + gamma1 * tau + gamma2 * tau ** 2)
