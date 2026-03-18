"""
Trade execution — primary (T-5s) and secondary (gap trigger) entry strategies.
"""

import logging
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from src import config
from src.equity import get_total_equity

log = logging.getLogger("polybot")

# Minimum order size on Polymarket (in USDC)
MIN_ORDER_SIZE = 1.0


def _update_wallet_trade(state: dict, wallet_id: int, message: str) -> None:
    """Update per-wallet last trade message."""
    if state.get("wallets") and wallet_id < len(state["wallets"]):
        state["wallets"][wallet_id]["last_trade"] = message


def _get_token_prices(client: ClobClient, up_token: str, down_token: str) -> dict:
    """Fetch current UP and DOWN token prices from the CLOB."""
    try:
        up_price = client.get_price(up_token, side="BUY")
        down_price = client.get_price(down_token, side="BUY")

        # The API may return a dict with 'price' key or a raw float
        if isinstance(up_price, dict):
            up_price = float(up_price.get("price", 0))
        else:
            up_price = float(up_price or 0)

        if isinstance(down_price, dict):
            down_price = float(down_price.get("price", 0))
        else:
            down_price = float(down_price or 0)

        return {"up": up_price, "down": down_price}

    except Exception as e:
        log.error("Failed to fetch token prices: %s", e)
        return {"up": 0, "down": 0}





# Maximum retries for transient network errors
MAX_ORDER_RETRIES = 3
RETRY_DELAY_SECONDS = 0.5


def _is_transient_error(exc: Exception) -> bool:
    """Check if a PolyApiException is a transient network error (retriable)."""
    err_str = str(exc).lower()
    status_code = getattr(exc, "status_code", None)
    # status_code=None with 'request exception' → network/timeout issue
    if status_code is None and "request exception" in err_str:
        return True
    return False


def _is_balance_error(exc: Exception) -> bool:
    """Check if a PolyApiException is a balance/allowance error."""
    return "not enough balance" in str(exc).lower()


def _is_no_match_error(exc: Exception) -> bool:
    """Check if a PolyApiException is a 'no orders found to match' error."""
    return "no orders found to match" in str(exc).lower()


async def _execute_market_order(
    client: ClobClient,
    token_id: str,
    token_label: str,
    trade_size: float,
    state: dict,
    wallet_id: int = 0,
) -> bool:
    """
    Execute a Fill-and-Kill (FAK) market order.
    Returns True if order was placed (regardless of fill), False on error.

    Error handling:
      - Balance/allowance errors: pre-checked, capped to real USDC balance.
      - Transient network errors: retried up to MAX_ORDER_RETRIES times.
      - No match (empty orderbook): returns False without locking.
    """
    # ── Pre-validate USDC balance ────────────────────────────────────
    from src.equity import get_usdc_balance

    usdc_balance = get_usdc_balance(client)
    
    # Polymarket subtracts fees (up to 2%) ON TOP of the Maker Amount for Market BUYs.
    # Therefore, the maximum trade size we can safely submit without hitting an
    # "insufficient balance" error is exactly `(usdc_balance / 1.02)`.
    # We use 1.025 to leave a safe slippage/rounding buffer.
    usdc_cap = usdc_balance / 1.025

    if trade_size > usdc_cap:
        if usdc_cap < MIN_ORDER_SIZE:
            log.warning(
                "USDC balance $%.2f too low to place minimum order — skipping",
                usdc_balance,
            )
            state["last_trade"] = f"SKIPPED — USDC balance ${usdc_balance:.2f} too low"
            _update_wallet_trade(state, wallet_id, "SKIPPED — low balance")
            return False
        log.warning(
            "Capping trade size from $%.2f to MAX $%.2f (to account for 2%% fees on $%.2f balance)",
            trade_size, usdc_cap, usdc_balance,
        )
        trade_size = round(usdc_cap, 2)

    if trade_size < MIN_ORDER_SIZE:
        log.warning(
            "Trade size $%.2f below minimum $%.2f — skipping this window",
            trade_size, MIN_ORDER_SIZE,
        )
        state["last_trade"] = f"SKIPPED — size ${trade_size:.2f} below minimum"
        _update_wallet_trade(state, wallet_id, "SKIPPED — size too small")
        return False

    # ── Place order with retry logic ─────────────────────────────────
    last_exc = None
    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        try:
            log.info(
                "Placing FAK BUY %s | size=$%.2f | token=%s... (attempt %d/%d)",
                token_label, trade_size, token_id[:16], attempt, MAX_ORDER_RETRIES,
            )

            # Create market order — amount is in USDC
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=trade_size,
                side=BUY,
                order_type=OrderType.FAK
            )

            signed = client.create_market_order(order_args)
            resp = client.post_order(signed, orderType=OrderType.FAK)

            # Parse response
            if isinstance(resp, dict):
                status = resp.get("status", resp.get("orderStatus", "UNKNOWN"))
                order_id = resp.get("orderID", resp.get("id", ""))
            else:
                status = str(resp)
                order_id = ""

            log.info(
                "FAK order result: status=%s | order=%s",
                status, order_id[:16] if order_id else "N/A",
            )

            if "reject" in str(status).lower() or "fail" in str(status).lower():
                reason = resp.get("message", "") if isinstance(resp, dict) else str(resp)
                log.warning("FAK REJECTED: %s — skipping window", reason)
                state["last_trade"] = f"REJECTED — {reason}"
                _update_wallet_trade(state, wallet_id, "REJECTED")
            else:
                state["last_trade"] = f"BUY {token_label} ${trade_size:.2f} | {status}"
                _update_wallet_trade(state, wallet_id, f"BUY {token_label} ${trade_size:.2f}")

            return True

        except Exception as e:
            last_exc = e

            # Transient network error → retry
            if _is_transient_error(e) and attempt < MAX_ORDER_RETRIES:
                log.warning(
                    "FAK order attempt %d/%d failed (transient): %s — retrying in %.1fs",
                    attempt, MAX_ORDER_RETRIES, e, RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

            # Balance/allowance error
            if _is_balance_error(e):
                log.error("FAK order failed: %s — balance issue, will retry next window", e)
                state["last_trade"] = f"BALANCE ERROR — {e}"
                return True

            # No match (empty orderbook)
            if _is_no_match_error(e):
                log.warning("FAK order: no matching orders in book — skipping window")
                state["last_trade"] = "NO MATCH — empty orderbook"
                return True

            # Unknown/permanent error
            log.error("FAK order failed: %s — skipping window", e)
            state["last_trade"] = f"ERROR — {e}"
            _update_wallet_trade(state, wallet_id, "ERROR")
            return True

    # All retries exhausted (only transient errors reach here)
    log.error(
        "FAK order failed after %d attempts: %s — skipping window",
        MAX_ORDER_RETRIES, last_exc,
    )
    state["last_trade"] = f"NETWORK ERROR — {last_exc} (retries exhausted)"
    return True


async def _execute_sell_order(
    client: ClobClient,
    token_id: str,
    token_label: str,
    num_shares: float,
    state: dict,
) -> bool:
    """
    Execute a FAK market SELL order to immediately dump owned tokens.
    Returns True if order was placed, False on error.
    """
    from py_clob_client.order_builder.constants import SELL
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    try:
        # 1. Fetch exact token balance to avoid Builder orderbook failure
        exact_shares = num_shares
        try:
            resp = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            )
            # Response is typically a list or dict
            if isinstance(resp, list) and len(resp) > 0:
                exact_shares = float(resp[0].get("balance", "0"))
            elif isinstance(resp, dict):
                exact_shares = float(resp.get("balance", "0"))
        except Exception as e:
            log.warning("Could not fetch exact token balance: %s. Using estimate.", e)

        if exact_shares <= 0:
            log.warning("Pre-Close Auto-Sell aborted: We do not own any shares of %s", token_id[:16])
            state["last_redeem"] = "Pre-Close FAK Blocked: Zero Balance"
            return False

        log.info(
            "Placing FAK SELL %s | exact_shares=%.2f | token=%s...",
            token_label, exact_shares, token_id[:16],
        )

        # Create market SELL order — amount is in Shares
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=exact_shares,
            side=SELL,
            order_type=OrderType.FAK
        )

        signed = client.create_market_order(order_args)
        resp = client.post_order(signed, orderType=OrderType.FAK)

        if isinstance(resp, dict):
            status = resp.get("status", resp.get("orderStatus", "UNKNOWN"))
            order_id = resp.get("orderID", resp.get("id", ""))
        else:
            status = str(resp)
            order_id = ""

        log.info(
            "SELL order result: status=%s | order=%s",
            status, order_id[:16] if order_id else "N/A",
        )

        if "reject" in str(status).lower() or "fail" in str(status).lower():
            reason = resp.get("message", "") if isinstance(resp, dict) else str(resp)
            log.warning("SELL REJECTED: %s", reason)
            state["last_trade"] = f"SELL REJECTED — {reason}"
            return False
            
        return True

    except Exception as e:
        log.error("SELL order failed: %s", e)
        return False


async def trade_loop(client: ClobClient, state: dict, wallet_id: int = 0) -> None:
    """
    Main trade execution loop using quantitative Edge-EV-Kelly strategy.
      - Wait for active market window
      - Apply strategy at gap trigger and primary entry timings
      - Lock window after any trade
    """
    from src.strategy import evaluate_market
    from src.equity import get_total_equity
    from src.utils import is_in_cooldown

    current_window_slug = None

    while True:
        # ── Cooldown Check ───────────────────────────────────────────
        if is_in_cooldown():
            state["cooldown_active"] = True
            log.info("Trading Cooldown Active (%s - %s %s)", 
                     config.COOLDOWN_START_TIME, 
                     config.COOLDOWN_END_TIME, 
                     config.COOLDOWN_TIMEZONE)
            state["last_trade"] = f"COOLDOWN — {config.COOLDOWN_START_TIME}-{config.COOLDOWN_END_TIME}"
            _update_wallet_trade(state, wallet_id, "COOLDOWN")
            await asyncio.sleep(60) # Sleep longer during cooldown
            continue
        
        state["cooldown_active"] = False
        window = state.get("window")
        if not window:
            await asyncio.sleep(0.5)
            continue

        now = datetime.now(timezone.utc)
        seconds_to_close = (window.end_date - now).total_seconds()
        state["seconds_to_close"] = seconds_to_close

        # ── Per-Wallet State Shortcut ────────────────────────────────────
        wallet_state = state["wallets"][wallet_id] if state.get("wallets") and wallet_id < len(state["wallets"]) else {}
        if not wallet_state:
            await asyncio.sleep(1)
            continue

        # Check if we should execute a pre-close sell (configurable limit before resolution)
        if wallet_state.get("window_locked", False):
            if not wallet_state.get("sell_locked", False) and wallet_state.get("position_shares", 0) > 0:
                if 0.0 < seconds_to_close <= config.PRE_CLOSE_SELL_SECONDS:
                    log.info("PRE-CLOSE AUTO-SELL TRIGGERED: selling %.2f shares", wallet_state["position_shares"])
                    sell_token = wallet_state["position_token_id"]
                    sell_label = state.get("signal_side", "UNKNOWN")
                    
                    sell_success = await _execute_sell_order(
                        client, 
                        sell_token, 
                        sell_label, 
                        wallet_state["position_shares"], 
                        state
                    )
                    
                    wallet_state["sell_locked"] = True
                    if sell_success:
                        log.info("Pre-close auto-sell fired successfully.")
                        state["last_redeem"] = "Pre-Close FAK Auto-Sold"
                    else:
                        log.error("Pre-close auto-sell failed.")

            # Continue high-frequency ticking if window is locked to wait for sell
            await asyncio.sleep(0.05)
            continue

        btc_price = state.get("btc_price", 0)
        up_odds = state.get("up_odds", 0)
        down_odds = state.get("down_odds", 0)
        positions = state.get("positions", [])

        # ── Window Change Reset ──────────────────────────────────────────
        if current_window_slug != window.slug:
            log.info("--- Wallet %d New Window Detected: %s ---", wallet_id, window.slug)
            current_window_slug = window.slug
            wallet_state["window_locked"] = False
            wallet_state["position_shares"] = 0
            wallet_state["sell_locked"] = False
            wallet_state["last_trade"] = "No trades yet"
            # Optional: update global state if this is the primary wallet or for general visibility
            if wallet_id == 0:
                state["last_trade"] = "No trades yet"

        # Skip trading logic if data not ready, but continue to show what we have
        if btc_price <= 0 or window.price_to_beat <= 0 or up_odds <= 0 or down_odds <= 0:
            await asyncio.sleep(0.5)
            continue

        # Get balance for Kelly sizing
        wallet_positions = wallet_state.get("positions", [])
        equity = get_total_equity(client, wallet_positions)
        total_balance = equity["usdc_balance"]

        # Run quantitative model
        signal = evaluate_market(
            btc_price=btc_price,
            price_to_beat=window.price_to_beat,
            seconds_remaining=seconds_to_close,
            up_odds=up_odds,
            down_odds=down_odds,
            balance=total_balance,
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
                # ── LIVE TRADING: Fetch exact orderbook prices right before execution
                # The continuous loop uses the 1-second cached background odds.
                # Once the math says YES, we must verify with the live API to prevent slippage.
                prices = _get_token_prices(client, window.up_token_id, window.down_token_id)
                exact_up_odds = prices["up"]
                exact_down_odds = prices["down"]
                
                # Update state
                state["up_odds"] = exact_up_odds
                state["down_odds"] = exact_down_odds

                # Re-evaluate the math with the exact, lowest-latency prices
                exact_signal = evaluate_market(
                    btc_price=btc_price,
                    price_to_beat=window.price_to_beat,
                    seconds_remaining=seconds_to_close,
                    up_odds=exact_up_odds,
                    down_odds=exact_down_odds,
                    balance=total_balance,
                    sigma_per_sec=config.BTC_VOLATILITY_PER_SEC,
                    edge_threshold=config.EDGE_THRESHOLD,
                    kelly_fraction=config.KELLY_FRACTION,
                    entry_seconds=config.ENTRY_SECONDS_BEFORE_CLOSE,
                    gap_trigger_usd=config.GAP_TRIGGER_USD
                )
                
                if not exact_signal.should_trade:
                    log.warning("Trade aborted: Exact live prices erased the mathematical edge.")
                    state["last_trade"] = "ABORTED — Edge lost on live price check"
                    await asyncio.sleep(0.5)
                    continue
                
                # Proceed with exact signal
                token_id = window.up_token_id if exact_signal.side == "UP" else window.down_token_id
                
                log.info(
                    "TRADE SIGNAL: BUY %s @ %.4f | Edge: %.2f%% | EV: %.3f | Kelly Size: $%.2f",
                    exact_signal.side, exact_signal.price, exact_signal.edge * 100, exact_signal.ev, exact_signal.kelly_size
                )
                
                success = await _execute_market_order(client, token_id, exact_signal.side, exact_signal.kelly_size, state, wallet_id)
                
                # ALWAYS lock after any trade attempt.
                # Reverting to safety-first approach to prevent any possibility of buy-cascades.
                wallet_state["window_locked"] = True
                log.info("Window locked for wallet %d — no further buys this window", wallet_id)
                
                if success:
                    wallet_state["position_token_id"] = token_id
                    wallet_state["position_shares"] = 999999.0  # FAK sell-all trick
            else:
                log.info("SIGNAL SKIP: %s", signal.reason)
        else:
            log.warning("Not enough data for strategy eval — skipping")
            
        await asyncio.sleep(0.5)

        # Not yet in trade zone — sleep briefly
        await asyncio.sleep(0.1)
