"""
Simulation trader — paper trading with virtual balance using real market data.

Uses the same entry strategies as the real trader (primary T-5s, secondary gap trigger)
but simulates trades with a virtual balance. Waits for window resolution from Gamma API
to determine win/loss and update P&L.
"""

import logging
import asyncio
import time
import json
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from src import config

log = logging.getLogger("polybot")

# Minimum simulated order size
SIM_MIN_ORDER_SIZE = 0.10


class SimPortfolio:
    """Virtual portfolio tracker for simulation mode."""

    def __init__(self, starting_balance: float = 10.0):
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.trades: list = []
        self.wins = 0
        self.losses = 0
        self.pending_trades: dict = {}  # slug -> trade dict

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0

    @property
    def pnl(self) -> float:
        return self.balance - self.starting_balance

    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.starting_balance * 100) if self.starting_balance > 0 else 0

    def place_trade(self, side: str, price: float, size: float, slug: str) -> dict:
        """Simulate placing a trade. Returns the trade record."""
        tokens_bought = size / price if price > 0 else 0
        trade = {
            "slug": slug,
            "side": side,           # "UP" or "DOWN"
            "entry_price": price,   # odds at entry (e.g. 0.85)
            "size_usdc": size,      # USDC spent
            "tokens": tokens_bought,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "OPEN",
            "pnl": 0.0,
        }
        self.balance -= size
        self.pending_trades[slug] = trade
        self.trades.append(trade)
        return trade

    def resolve_trade(self, slug: str, winning_side: str) -> Optional[dict]:
        """Resolve a specific trade by slug based on the market outcome."""
        trade = self.pending_trades.pop(slug, None)
        if not trade:
            return None

        if trade["side"] == winning_side:
            # Winner — each token pays $1.00
            payout = trade["tokens"] * 1.0
            trade["pnl"] = payout - trade["size_usdc"]
            trade["status"] = "WON"
            self.balance += payout
            self.wins += 1
        else:
            # Loser — tokens worth $0
            trade["pnl"] = -trade["size_usdc"]
            trade["status"] = "LOST"
            self.losses += 1

        return trade

    def get_equity_dict(self) -> dict:
        """Return equity info compatible with the dashboard."""
        pending_value = sum(t["size_usdc"] for t in self.pending_trades.values())
        return {
            "usdc_balance": self.balance,
            "winning_value": pending_value,
            "total": self.balance + pending_value,
        }

    def get_positions_list(self) -> list:
        """Return positions compatible with the dashboard."""
        positions = []
        for t in self.pending_trades.values():
            positions.append({
                "market": t["slug"][-20:],
                "side": f"BUY {t['side']}",
                "size": t["size_usdc"],
                "status": "PENDING",
            })
        # Show last 4 resolved trades
        for t in reversed(self.trades):
            if t["status"] in ("WON", "LOST") and len(positions) < 5:
                pnl_str = f"+${t['pnl']:.2f}" if t["pnl"] > 0 else f"-${abs(t['pnl']):.2f}"
                positions.append({
                    "market": t["slug"][-20:],
                    "side": f"BUY {t['side']}",
                    "size": t["size_usdc"],
                    "status": f"{t['status']} ({pnl_str})",
                })
        return positions


async def _resolve_window_outcome(slug: str) -> Optional[str]:
    """
    Poll Gamma API until the window resolves. Returns "UP" or "DOWN".

    NOTE: Gamma API can take several minutes to set `closed: True`.
    Instead we check if outcomePrices are settled (near 0 or 1) which
    happens much faster.
    """
    url = f"{config.GAMMA_API_HOST}/events"
    async with aiohttp.ClientSession() as session:
        for attempt in range(150):  # try for up to 5 minutes (150 × 2s)
            try:
                async with session.get(url, params={"slug": slug}) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(2)
                        continue
                    events = await resp.json()

                if not events:
                    await asyncio.sleep(2)
                    continue

                event = events[0]
                markets = event.get("markets", [])
                if not markets:
                    await asyncio.sleep(2)
                    continue

                mkt = markets[0]
                outcome_prices = mkt.get("outcomePrices", "")
                if isinstance(outcome_prices, str):
                    if not outcome_prices:
                        await asyncio.sleep(2)
                        continue
                    outcome_prices = json.loads(outcome_prices)

                if not outcome_prices or len(outcome_prices) < 2:
                    await asyncio.sleep(2)
                    continue

                outcomes = mkt.get("outcomes", [])
                
                # Find which outcome has settled to >= 0.95
                winning_outcome = None
                for idx, price_str in enumerate(outcome_prices):
                    try:
                        price = float(price_str)
                        if price >= 0.95:
                            # Usually "Up" or "Down" -> uppercase it to match "UP"/"DOWN" side
                            if idx < len(outcomes):
                                winning_outcome = str(outcomes[idx]).upper()
                            break
                    except ValueError:
                        continue
                        
                if winning_outcome:
                    log.info(
                        "SIM RESOLVE %s: outcomes=%s prices=%s → %s won",
                        slug, outcomes, outcome_prices, winning_outcome
                    )
                    return winning_outcome

                # Prices not yet settled — keep polling
                if attempt % 15 == 14:
                    log.debug(
                        "SIM: Still waiting for %s to settle (attempt %d, prices=%s)",
                        slug, attempt + 1, outcome_prices,
                    )

            except Exception as e:
                log.debug("Resolve poll error for %s: %s", slug, e)

            await asyncio.sleep(2)

    log.warning("Timed out waiting for %s to resolve after 5 minutes", slug)
    return None


async def sim_trade_loop(portfolio: SimPortfolio, state: dict) -> None:
    """
    Simulation trade loop — uses Edge-EV-Kelly strategy with virtual balance.
    After each trade, waits for window resolution to determine win/loss.
    """
    from src.strategy import evaluate_market
    
    current_window_slug = None

    while True:
        window = state.get("window")
        if not window:
            await asyncio.sleep(0.5)
            continue

        now = datetime.now(timezone.utc)
        seconds_to_close = (window.end_date - now).total_seconds()
        state["seconds_to_close"] = seconds_to_close

        state["equity"] = portfolio.get_equity_dict()
        state["positions"] = portfolio.get_positions_list()

        # ── Window Change Reset ──────────────────────────────────────────
        if current_window_slug != window.slug:
            log.info("--- SIM New Window Detected: %s ---", window.slug)
            current_window_slug = window.slug
            state["window_locked"] = False
            state["last_trade"] = "No trades yet"
            state["position_shares"] = 0
            state["sell_locked"] = False

        # Already traded this window?
        if state.get("window_locked", False):
            # Check if we should simulate a pre-close sell (configurable limit before resolution)
            if not state.get("sell_locked", False) and state.get("position_shares", 0) > 0:
                if 0.0 < seconds_to_close <= config.PRE_CLOSE_SELL_SECONDS:
                    log.info("SIM PRE-CLOSE AUTO-SELL TRIGGERED")
                    sell_label = state.get("signal_side", "UNKNOWN")
                    # In simulation, we just immediately resolve the trade at the current active price
                    # To mimic FAK sell, we simulate the 'win' based on current odds or simply close the position.
                    # A proper simulate sell would resolve the PnL right now instead of waiting for outcome.
                    # For simplicity of the virtual portfolio, we will just log it and let the oracle handle the absolute win/loss 
                    # since the FAK sell is mathematically equivalent to the oracle payout if the token is at 0.99 anyway.
                    state["sell_locked"] = True
                    state["last_redeem"] = "SIM Pre-Close Auto-Sold"
                    log.info("SIM window successfully fake-sold to USDC.")

            # Continue high-frequency ticking if window is locked to wait for sell
            await asyncio.sleep(0.05)
            continue

        # Evaluate strategy continuously using current cached state
        btc_price = state.get("btc_price", 0)
        up_odds = state.get("up_odds", 0)
        down_odds = state.get("down_odds", 0)
        
        # Give the state dictionary time to populate missing data
        if btc_price <= 0 or window.price_to_beat <= 0 or up_odds <= 0 or down_odds <= 0:
            await asyncio.sleep(0.5)
            continue

        # Run quantitative model 
        signal = evaluate_market(
            btc_price=btc_price,
            price_to_beat=window.price_to_beat,
            seconds_remaining=seconds_to_close,
            up_odds=up_odds,
            down_odds=down_odds,
            balance=portfolio.balance,
            sigma_per_sec=config.BTC_VOLATILITY_PER_SEC,
            edge_threshold=config.EDGE_THRESHOLD,
            kelly_fraction=config.KELLY_FRACTION,
            entry_seconds=config.ENTRY_SECONDS_BEFORE_CLOSE,
            gap_trigger_usd=config.GAP_TRIGGER_USD
        )
        
        if signal:
            # Update state for dashboard
            state["p_true"] = signal.p_true
            state["edge"] = signal.edge
            state["ev"] = signal.ev
            state["kelly_size"] = signal.kelly_size
            state["signal_side"] = signal.side
            state["signal_reason"] = signal.reason

            if signal.should_trade:
                trade_size = signal.kelly_size
                if trade_size < SIM_MIN_ORDER_SIZE:
                    trade_size = SIM_MIN_ORDER_SIZE
                    state["last_trade"] = f"SIM SKIP — bet size ${trade_size:.2f} too low"
                    state["window_locked"] = True
                    continue

                trade = portfolio.place_trade(signal.side, signal.price, trade_size, window.slug)
                log.info(
                    "SIM TRADE: BUY %s @ %.4f | $%.2f | edge=%.2f%% ev=%.3f",
                    signal.side, signal.price, trade_size, signal.edge*100, signal.ev
                )
                state["last_trade"] = f"SIM BUY {signal.side} @ {signal.price:.4f} | ${trade_size:.2f}"
                state["equity"] = portfolio.get_equity_dict()
                state["positions"] = portfolio.get_positions_list()
                
                # Mock the shares owned so the sell trigger knows we are in a position
                state["position_shares"] = trade_size / signal.price
                state["position_token_id"] = "sim_token" 
                
                state["window_locked"] = True

                # Resolve asynchronously
                asyncio.create_task(_resolve_and_update(portfolio, state, window.slug))
            else:
                log.info("SIM SKIP: %s", signal.reason)
                state["last_trade"] = f"SIM SKIP — {signal.reason}"
        else:
            log.warning("SIM: Not enough data for strategy eval")
            state["last_trade"] = "SIM SKIP — missing data"
            
        await asyncio.sleep(0.5)


async def _resolve_and_update(portfolio: SimPortfolio, state: dict, slug: str) -> None:
    """Wait for window resolution, then update portfolio and state."""
    log.info("SIM: Waiting for %s to resolve...", slug)
    winner = await _resolve_window_outcome(slug)

    if winner:
        trade = portfolio.resolve_trade(slug, winner)
        if trade:
            pnl_str = f"+${trade['pnl']:.2f}" if trade["pnl"] >= 0 else f"-${abs(trade['pnl']):.2f}"
            result = "✅ WON" if trade["status"] == "WON" else "❌ LOST"
            log.info(
                "SIM RESULT: %s | bought %s, %s won | PnL: %s | Balance: $%.2f | W/L: %d/%d (%.0f%%)",
                result, trade["side"], winner, pnl_str, portfolio.balance,
                portfolio.wins, portfolio.losses, portfolio.win_rate,
            )
            state["last_trade"] = (
                f"{result} {trade['side']} @ {trade['entry_price']:.4f} | "
                f"PnL: {pnl_str} | Bal: ${portfolio.balance:.2f}"
            )
        else:
            log.warning("SIM: Trade for %s not found in pending — may have been resolved already", slug)
    else:
        log.warning("SIM: Could not resolve %s — treating as loss", slug)
        portfolio.resolve_trade(slug, "UNKNOWN")
        state["last_trade"] = f"SIM: Resolution timeout for {slug}"

    state["equity"] = portfolio.get_equity_dict()
    state["positions"] = portfolio.get_positions_list()

