"""
Configuration loader — reads .env and provides typed settings.
Validation is deferred to allow dry-run mode without credentials.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()


# ── Polymarket Auth (may be empty in dry-run) ────────────────────
POLY_WALLETS: str = os.getenv("POLY_WALLETS", "")
CHAIN_ID: int = 137  # Polygon Mainnet

# Fallback attributes to prevent AttributeError from legacy or external dependency usage
# These should generally be accessed via client.funder_address or client.signature_type_value
POLY_FUNDER_ADDRESS: str = ""
SIGNATURE_TYPE: int = 0

def parse_wallets() -> list[dict]:
    """Parse POLY_WALLETS env var into list of wallet configs."""
    if not POLY_WALLETS:
        return []
    wallets = []
    for entry in POLY_WALLETS.split(","):
        parts = entry.strip().split(":")
        if len(parts) == 3:
            wallets.append({
                "private_key": parts[0],
                "funder_address": parts[1],
                "signature_type": int(parts[2])
            })
    return wallets

# ── API Hosts ────────────────────────────────────────────────────
CLOB_HOST: str = "https://clob.polymarket.com"
GAMMA_API_HOST: str = "https://gamma-api.polymarket.com"
BINANCE_BTC_URL: str = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "")

# ── RPC Fallbacks ────────────────────────────────────────────────
POLYGON_RPC_FALLBACKS: list[str] = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://rpc-mainnet.maticvigil.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com"
]

# ── Quantitative Strategy Settings ───────────────────────────────
EDGE_THRESHOLD: float = float(os.getenv("EDGE_THRESHOLD", "0.07"))
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.5"))
BTC_VOLATILITY_PER_SEC: float = float(os.getenv("BTC_VOLATILITY_PER_SEC", "0.15"))
ENTRY_SECONDS_BEFORE_CLOSE: float = float(os.getenv("ENTRY_SECONDS_BEFORE_CLOSE", "3.0"))
PRE_CLOSE_SELL_SECONDS: float = float(os.getenv("PRE_CLOSE_SELL_SECONDS", "0.7"))
GAP_TRIGGER_USD: float = float(os.getenv("GAP_TRIGGER_USD", "2.0"))
GAP_TRIGGER_PERCENT: float = 0.05  # 5% fallback if needed, though USD is preferred
REDEEM_LOSSES: bool = os.getenv("REDEEM_LOSSES", "True").lower() == "true"

# ── Contract Addresses (Polygon) ────────────────────────────────
USDC_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CONDITIONAL_TOKENS_ADDRESS: str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_ADDRESS: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER_ADDRESS: str = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ── Timing ───────────────────────────────────────────────────────
DASHBOARD_REFRESH_PER_SECOND: int = 1  # 500ms
POSITION_POLL_INTERVAL: int = 5  # seconds
MARKET_RETRY_INTERVAL: int = 1  # seconds when idle
PRICE_FEED_INTERVAL: float = 1.0  # seconds

# ── Cooldown ─────────────────────────────────────────────────────
COOLDOWN_START_TIME: str = os.getenv("COOLDOWN_START_TIME", "10:00")
COOLDOWN_END_TIME: str = os.getenv("COOLDOWN_END_TIME", "11:59")
COOLDOWN_TIMEZONE: str = os.getenv("COOLDOWN_TIMEZONE", "US/Eastern")


def validate_trading_config() -> None:
    """
    Validate that required credentials are present.
    Call this before starting any trading operations (not during dry-run).
    """
    wallets = parse_wallets()
    if not wallets:
        print(f"[ERROR] Missing required env var: POLY_WALLETS")
        print(f"        Format: key1:address1:sigtype1,key2:address2:sigtype2")
        print(f"        Copy .env.example → .env and fill in your values.")
        sys.exit(1)
