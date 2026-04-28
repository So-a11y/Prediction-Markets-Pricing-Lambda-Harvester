"""
λ-Harvesting Bot — Yang (2026) Wang Transform Strategy for Polymarket.

Scans prediction markets for contracts where the market price (p_mkt)
significantly exceeds the physical probability (p*) in quiet markets
(low EIV), flagging them as SHORT YES opportunities.
"""

from lambda_harvester.scanner import LambdaScanner, ContractAnalysis
from lambda_harvester.math_engine import (
    wang_transform,
    compute_lambda,
    extract_physical_prob,
    compute_eiv,
    compute_trade_score,
    time_decay_lambda,
)
from lambda_harvester.config import LAMBDA_POLYMARKET

__all__ = [
    "LambdaScanner",
    "ContractAnalysis",
    "wang_transform",
    "compute_lambda",
    "extract_physical_prob",
    "compute_eiv",
    "compute_trade_score",
    "time_decay_lambda",
    "LAMBDA_POLYMARKET",
]
