import os
import sys
import json
import time
from dotenv import load_dotenv
from web3 import Web3

sys.path.append(os.getcwd())

from src import config, auth

# ABI for ConditionalTokens.balanceOf and redeemPositions
CT_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
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
    }
]

def get_w3():
    rpcs = [config.POLYGON_RPC_URL] + config.POLYGON_RPC_FALLBACKS
    for url in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(url))
            if w3.is_connected():
                return w3
        except:
            continue
    return None

async def cleanup():
    load_dotenv()
    wallets = config.parse_wallets()
    if not wallets:
        print("No wallets found.")
        return
    
    client = auth.create_client(wallets[0])
    w3 = get_w3()
    if not w3:
        print("Could not connect to Web3.")
        return

    user_addr = Web3.to_checksum_address(client.funder_address)
    account = w3.eth.account.from_key(wallets[0]["private_key"])
    ct = w3.eth.contract(address=Web3.to_checksum_address(config.CONDITIONAL_TOKENS_ADDRESS), abi=CT_ABI)
    usdc = Web3.to_checksum_address(config.USDC_ADDRESS)
    parent_collection = bytes(32)

    print(f"Starting Deep Cleanup for {user_addr}...")
    
    seen_markets = set()
    redeemable = []
    
    try:
        # Fetching trades
        trades = client.get_trades()
        print(f"Fetched {len(trades)} trades.")
        
        for trade in trades:
            market_id = trade.get("condition_id") or trade.get("market")
            asset_id = trade.get("asset_id")
            
            if not market_id or not asset_id: continue
            if f"{market_id}_{asset_id}" in seen_markets: continue
            seen_markets.add(f"{market_id}_{asset_id}")
            
            try:
                bal = ct.functions.balanceOf(user_addr, int(asset_id)).call()
                if bal > 0:
                    market_info = client.get_market(market_id)
                    if market_info and market_info.get("closed"):
                        print(f"  - Found {bal/1e6:.2f} tokens in {market_id[:16]}... (RESOLVED)")
                        redeemable.append((market_id, asset_id))
                    else:
                        print(f"  - Found {bal/1e6:.2f} tokens in {market_id[:16]}... (ACTIVE - skipping)")
            except Exception as e:
                pass

        if not redeemable:
            print("No redeemable positions found.")
            return

        print(f"\nProcessing {len(redeemable)} redemptions with 2s delay between each...")
        
        for cid, aid in redeemable:
            # Refresh nonce for each tx to avoid in-flight limits and ensure order
            nonce = w3.eth.get_transaction_count(user_addr)
            print(f"Redeeming {cid[:16]}... (nonce: {nonce})")
            
            cid_bytes = bytes.fromhex(cid[2:]) if cid.startswith("0x") else bytes.fromhex(cid)
            
            # Simple check for balance again to avoid redundant calls if it was caught in previous batch
            if ct.functions.balanceOf(user_addr, int(aid)).call() == 0:
                print(f"  Already redeemed, skipping.")
                continue

            tx = ct.functions.redeemPositions(
                usdc, parent_collection, cid_bytes, [1, 2]
            ).build_transaction({
                "from": user_addr,
                "nonce": nonce,
                "gas": 200_000,
                "gasPrice": int(w3.eth.gas_price * 1.5),
                "chainId": 137,
            })
            
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  Sent: {tx_hash.hex()}. Waiting for confirmation...")
            
            # Wait for confirmation to clear in-flight limit
            try:
                w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                print("  Confirmed!")
            except Exception as e:
                print(f"  Wait error: {e}")
            
            time.sleep(2) # Extra breathing room
            
        print("\nCleanup cycle complete.")

    except Exception as e:
        print(f"Cleanup failed: {e}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(cleanup())
