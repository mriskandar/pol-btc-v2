"""
Position management — poll positions, detect resolved+winning, auto-redeem via web3.
"""

import logging
import asyncio
from typing import List

from py_clob_client.client import ClobClient
from web3 import Web3

from src import config

log = logging.getLogger("polybot")

# Minimal ConditionalTokens ABI
CONDITIONAL_TOKENS_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]


def get_w3() -> Web3:
    rpcs = [config.POLYGON_RPC_URL] if config.POLYGON_RPC_URL else []
    rpcs.extend(config.POLYGON_RPC_FALLBACKS)
    for url in rpcs:
        try:
            temp_w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if temp_w3.is_connected():
                return temp_w3
        except:
            continue
    return None


def fetch_positions(client: ClobClient) -> List[dict]:
    """Fetch unique markets traded recently to extract potential unredeemed tokens."""
    try:
        # Increase limit to 100 to catch more historical/unredeemed tokens
        result = client.get_trades()
        if isinstance(result, list):
            return result
        return []
    except Exception as e:
        log.error("Failed to fetch positions: %s", e)
        return []


def find_redeemable(client: ClobClient, w3: Web3, trades: List[dict]) -> List[dict]:
    """
    Find positions that are actually winning, resolved, and currently owned.
    """
    if not trades or not w3:
        return []

    # Get funder address from client
    user_addr = Web3.to_checksum_address(client.funder_address)
    ct = w3.eth.contract(
        address=Web3.to_checksum_address(config.CONDITIONAL_TOKENS_ADDRESS),
        abi=CONDITIONAL_TOKENS_ABI,
    )

    checked_markets = {}
    redeemable = []
    
    for trade in trades:
        market_id = trade.get("market")
        asset_id = trade.get("asset_id")
        
        if not market_id or not asset_id:
            continue
            
        # Avoid checking the same market/asset repeatedly
        cache_key = f"{market_id}_{asset_id}"
        if cache_key in checked_markets:
            continue
        
        checked_markets[cache_key] = True

        try:
            # Check balance first (it's fast and free RPC call)
            # If balance is 0, we've either already redeemed it or sold it
            bal = ct.functions.balanceOf(user_addr, int(asset_id)).call()
            if bal == 0:
                continue

            # If we own it, check if the market is closed and this token won
            market_info = client.get_market(market_id)
            if not market_info or not market_info.get("closed"):
                # Market still open or couldn't fetch
                # We record it as a live position for equity calculation
                redeemable.append({
                    "conditionId": market_id,
                    "asset_id": asset_id,
                    "size": bal / 1e6, # Rough scaling for equity purposes
                    "resolved": False
                })
                continue
                
            # Market is closed, check if our specific asset_id is the winner
            tokens = market_info.get("tokens", [])
            is_winner = False
            for t in tokens:
                if str(t.get("token_id")) == str(asset_id) and t.get("winner"):
                    is_winner = True
                    break
                    
            if is_winner:
                redeemable.append({
                    "conditionId": market_id,
                    "asset_id": asset_id,
                    "resolved": True,
                    "outcome": "Winning",  # Triggers equity calculation logic
                    "size": bal / 1e6      # Winning tokens redeem 1:1 for USDC (6 decimals)
                })
            elif config.REDEEM_LOSSES:
                # Still own tokens in a closed market but they didn't win
                redeemable.append({
                    "conditionId": market_id,
                    "asset_id": asset_id,
                    "resolved": True,
                    "outcome": "Lost",
                    "size": 0
                })

        except Exception as e:
            if "429" in str(e) or "too many requests" in str(e).lower():
                log.warning("Rate limited checking market %s: %s", market_id, e)
            else:
                log.debug("Error checking market %s: %s", market_id, e)

    return redeemable


async def redeem_positions(client: ClobClient, w3: Web3, redeemable: List[dict]) -> int:
    """
    Batch redeem all eligible winning positions via ConditionalTokens contract.
    """
    if not redeemable or not w3:
        return 0

    # Skip redemption for proxy wallets (signature_type=2)
    signature_type = getattr(client, 'signature_type_value', 0)
    if signature_type == 2:
        return 0

    try:
        # Get private key from client
        private_key = client.private_key
        account = w3.eth.account.from_key(private_key)
        ct = w3.eth.contract(
            address=Web3.to_checksum_address(config.CONDITIONAL_TOKENS_ADDRESS),
            abi=CONDITIONAL_TOKENS_ABI,
        )

        redeemed = 0
        parent_collection = bytes(32)  # 0x0...0
        usdc = Web3.to_checksum_address(config.USDC_ADDRESS)
        
        base_nonce = w3.eth.get_transaction_count(account.address)
        tx_index = 0

        # Filter to only the ones marked resolved
        to_redeem = [pos for pos in redeemable if pos.get("resolved")]

        if not to_redeem:
            return 0

        for pos in to_redeem:
            condition_id = pos.get("conditionId")
            if not condition_id:
                continue

            if isinstance(condition_id, str):
                if condition_id.startswith("0x"):
                    cid_bytes = bytes.fromhex(condition_id[2:])
                else:
                    cid_bytes = bytes.fromhex(condition_id)
            else:
                cid_bytes = condition_id

            # [1, 2] redeems both slots since we only buy UP/DOWN
            index_sets = [1, 2]

            try:
                # Double-check balance before redeeming (may have been redeemed already)
                current_bal = ct.functions.balanceOf(account.address, int(pos.get("asset_id"))).call()
                if current_bal == 0:
                    log.debug("Position %s already redeemed (balance=0), skipping", condition_id[:16])
                    continue

                tx = ct.functions.redeemPositions(
                    usdc, parent_collection, cid_bytes, index_sets
                ).build_transaction(
                    {
                        "from": account.address,
                        "nonce": base_nonce + tx_index,
                        "gas": 200_000,
                        "gasPrice": w3.eth.gas_price,
                        "chainId": config.CHAIN_ID,
                    }
                )
                signed = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                tx_index += 1

                log.info("Sent Redeem tx for %s: %s", condition_id[:16], tx_hash.hex())
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

                if receipt["status"] == 1:
                    redeemed += 1
                    log.info("OK Successfully redeemed %s", condition_id[:16])
                else:
                    log.warning("ERR Redeem tx failed for %s", condition_id[:16])

            except Exception as e:
                log.error("Redeem tx error for %s: %s", condition_id[:16], e)
                # Always increment tx_index to avoid nonce reuse
                tx_index += 1

        return redeemed

    except Exception as e:
        log.error("Redeem setup error: %s", e)
        return 0


async def position_loop(client: ClobClient, state: dict, wallet_id: int = 0) -> None:
    """
    Background loop: poll positions every 5s, auto-redeem winning positions.
    """
    w3 = get_w3()
    if not w3:
        log.error("Position loop: Failed to connect to Web3 RPC")
        return

    redeemed_cache = set()  # Track already redeemed condition IDs

    while True:
        try:
            trades = fetch_positions(client)

            # Find both active unredeemed positions AND closed winning positions
            positions = find_redeemable(client, w3, trades)
            
            # Store in per-wallet state
            if state.get("wallets") and wallet_id < len(state["wallets"]):
                state["wallets"][wallet_id]["positions"] = positions
            else:
                state["positions"] = positions

            # Filter out already redeemed positions
            to_redeem = [p for p in positions if p.get("resolved") and p.get("conditionId") not in redeemed_cache]

            if to_redeem:
                count = await redeem_positions(client, w3, to_redeem)
                if count:
                    state["last_redeem"] = f"Redeemed {count} position(s)"
                    # Mark as redeemed
                    for p in to_redeem:
                        redeemed_cache.add(p.get("conditionId"))

        except Exception as e:
            log.error("Position loop error: %s", e)

        await asyncio.sleep(config.POSITION_POLL_INTERVAL)
