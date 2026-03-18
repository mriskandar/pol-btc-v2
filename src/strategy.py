"""
Quantitative trading strategy logic.
Calculates win probability (p_true), edge, Expected Value (EV), and Kelly criterion size.
"""

import math
from dataclasses import dataclass
from typing import Optional

# Standard normal cumulative distribution function
def norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def sigmoid(x: float) -> float:
    """
    Robust sigmoid function to avoid OverflowError with math.exp.
    """
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


@dataclass
class TradeSignal:
    side: str
    price: float
    p_true: float
    edge: float
    ev: float
    kelly_size: float
    should_trade: bool
    reason: str


def estimate_p_true(gap: float, seconds_remaining: float, sigma_per_sec: float) -> float:
    """
    Estimate true probability of UP winning using sigmoid model.
    P(UP) = 1 / (1 + exp(-k * gap / sqrt(time)))
    """
    if seconds_remaining <= 0:
        return 1.0 if gap > 0 else 0.0 if gap < 0 else 0.5

    k = 182.0
    x = k * gap / math.sqrt(seconds_remaining)
    return sigmoid(x)


def calculate_edge(p_true: float, market_price: float) -> float:
    """Edge = our estimated probability - market implied probability"""
    return p_true - market_price


def calculate_ev(p_true: float, market_price: float) -> float:
    """
    Expected Value per $1 wagered.
    EV = (p_true * profit) - (q_true * loss)
    Profit on $1 is (1/market_price - 1). Loss is $1.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
        
    profit_if_win = (1.0 / market_price) - 1.0
    q_true = 1.0 - p_true
    
    return (p_true * profit_if_win) - (q_true * 1.0)


def kelly_size(p_true: float, market_price: float, balance: float, fraction: float = 0.5) -> float:
    """
    Calculate optimal bet size using Kelly Criterion.
    f* = (p * b - q) / b
    Where b is the net decimal odds received (profit on $1 bet).
    """
    if market_price <= 0 or market_price >= 1 or balance <= 0:
        return 0.0
        
    b = (1.0 / market_price) - 1.0
    q_true = 1.0 - p_true
    
    # Kelly fraction
    f_star = (p_true * b - q_true) / b
    
    # If edge is negative, Kelly says don't bet (f* <= 0)
    if f_star <= 0:
        return 0.0
        
    # Scale by desired fraction (e.g. 0.5 for half-Kelly)
    # Cap at 100% of balance
    bet_fraction = min(f_star * fraction, 1.0)

    # Capping size
    raw_size = balance * bet_fraction
    
    # Enforce Polymarket minimum order size if an edge exists
    if 0 < raw_size < 1.0:
        raw_size = 1.0
        
    return float(round(raw_size, 2))


def evaluate_market(
    btc_price: float,
    price_to_beat: float,
    seconds_remaining: float,
    up_odds: float,
    down_odds: float,
    balance: float,
    sigma_per_sec: float,
    edge_threshold: float,
    kelly_fraction: float,
    entry_seconds: float = 3.0,
    gap_trigger_usd: float = 2.0
) -> Optional[TradeSignal]:
    """
    Evaluate the market to generate a trade signal.
    Determines which side to bet on based on highest edge.
    """
    if seconds_remaining < 0 or btc_price <= 0 or price_to_beat <= 0:
        return None

    if up_odds <= 0 and down_odds <= 0:
        return None

    gap = btc_price - price_to_beat
    p_true_up = estimate_p_true(gap, seconds_remaining, sigma_per_sec)
    p_true_down = 1.0 - p_true_up

    # Gap-based directional filter: only consider the side aligned with gap direction
    if gap > 0:
        side = "UP"
        price = up_odds
        p_true = p_true_up
        edge = calculate_edge(p_true_up, up_odds) if up_odds > 0 else -1
    else:
        side = "DOWN"
        price = down_odds
        p_true = p_true_down
        edge = calculate_edge(p_true_down, down_odds) if down_odds > 0 else -1

    if price <= 0:
        return None

    # Calculate EV and Kelly size
    ev = calculate_ev(p_true, price)
    k_size = kelly_size(p_true, price, balance, kelly_fraction)

    # Trading rules
    should_trade = False
    reason = ""

    if seconds_remaining > entry_seconds:
        reason = f"Wait: {seconds_remaining:.1f}s > {entry_seconds}s"
    elif abs(gap) < gap_trigger_usd:
        reason = f"Wait: Gap ${abs(gap):.2f} < ${gap_trigger_usd:.2f}"
    elif edge < edge_threshold:
        reason = f"Edge {edge:.4f} < {edge_threshold}"
    elif ev <= 0:
        reason = f"Negative EV ({ev:.4f})"
    elif k_size <= 0:
        reason = "Kelly size is zero"
    else:
        should_trade = True
        reason = "Trade criteria met"

    return TradeSignal(
        side=side,
        price=price,
        p_true=p_true,
        edge=edge,
        ev=ev,
        kelly_size=k_size,
        should_trade=should_trade,
        reason=reason
    )
