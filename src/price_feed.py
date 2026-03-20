"""
Live BTC price feed — reads from Binance WebSocket stream.
Replaces the delayed Polygon Chainlink oracle for millisecond accuracy
for the 5m options strategy.
"""

import logging
import asyncio
import aiohttp
import json
from src import config

log = logging.getLogger("polybot")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"

async def price_feed_loop(state: dict) -> None:
    """
    Continuous loop that keeps state["btc_price"] updated with the latest
    real-time BTC/USDT price from Binance WebSocket.
    """
    log.info("Starting real-time Binance BTC/USDT WebSocket feed...")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_WS_URL, heartbeat=15.0) as ws:
                    log.info("Connected to Binance WebSocket")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            price = float(data.get("p", 0))
                            if price > 0:
                                state["btc_price"] = price
                                import time
                                state["btc_price_timestamp"] = time.time()
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.warning("WebSocket error")
                            break
        except Exception as e:
            log.warning("Binance WebSocket error: %s, reconnecting in 5s...", e)
            await asyncio.sleep(5)

