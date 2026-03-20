"""
Token odds feed — continuously polls UP/DOWN midpoint prices from CLOB API.
"""

import logging
import asyncio

from src import config

log = logging.getLogger("polybot")


async def odds_feed_loop(state: dict) -> None:
    """
    Continuous loop that keeps state["up_odds"] and state["down_odds"]
    updated with live midpoint prices from the CLOB orderbook.
    Uses py_clob_client's get_price() method which is more reliable.
    """
    from src.auth import create_client

    # Create a client for price fetching (uses first wallet config)
    wallets = config.parse_wallets()
    if not wallets:
        log.error("No wallets configured - odds feed disabled")
        return

    client = create_client(wallets[0])

    while True:
        window = state.get("window")
        if window and window.up_token_id and window.down_token_id:
            try:
                loop = asyncio.get_event_loop()
                # Use CLOB client's get_price method (non-blocking)
                from functools import partial
                up_price = await loop.run_in_executor(None, partial(client.get_price, window.up_token_id, side="BUY"))
                down_price = await loop.run_in_executor(None, partial(client.get_price, window.down_token_id, side="BUY"))

                # Parse response (can be dict or float)
                if isinstance(up_price, dict):
                    up_price = float(up_price.get("price", 0))
                else:
                    up_price = float(up_price or 0)

                if isinstance(down_price, dict):
                    down_price = float(down_price.get("price", 0))
                else:
                    down_price = float(down_price or 0)

                if up_price > 0:
                    state["up_odds"] = up_price
                if down_price > 0:
                    state["down_odds"] = down_price

            except Exception as e:
                log.warning("Odds fetch error: %s", e)

        await asyncio.sleep(0.5)

