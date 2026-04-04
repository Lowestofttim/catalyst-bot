"""
V2 Config Module — Typed Configuration Loading

Replaces scattered os.getenv() calls throughout V1's api_server.py.
All configuration lives in .env and is loaded once here with proper
types, defaults, and validation.

Usage:
    from config import cfg
    spread = cfg.SPREAD_BPS
    cfg.reload()  # Hot-reload from .env

Settings are updated from the GUI via cfg.update("KEY", value) which
writes back to .env and updates the in-memory value.
"""

import os
import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from dotenv import load_dotenv, set_key
from typing import Optional


# ---------------------------------------------------------------------------
# Load .env from the project directory
# ---------------------------------------------------------------------------
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)


def _strip_quotes(val: str) -> str:
    """Strip surrounding single or double quotes from .env values."""
    val = val.strip()
    if len(val) >= 2:
        if (val.startswith("'") and val.endswith("'")) or \
           (val.startswith('"') and val.endswith('"')):
            val = val[1:-1]
    return val


def _str(key: str, default: str = "") -> str:
    return _strip_quotes(os.getenv(key, default))


def _int(key: str, default: int = 0) -> int:
    val = _strip_quotes(os.getenv(key, ""))
    try:
        return int(val) if val else default
    except (ValueError, TypeError):
        return default


def _decimal(key: str, default: str = "0") -> Decimal:
    val = _strip_quotes(os.getenv(key, ""))
    try:
        return Decimal(val) if val else Decimal(default)
    except InvalidOperation:
        return Decimal(default)


def _safe_url(key: str, default: str) -> str:
    """Load a URL from env and validate its scheme (http/https only)."""
    val = _str(key, default)
    if val:
        parsed = urlparse(val)
        if parsed.scheme not in ("http", "https"):
            print(f"[CONFIG] WARNING: {key} has invalid scheme '{parsed.scheme}' — using default")
            return default
    return val


def _bool(key: str, default: bool = False) -> bool:
    val = _strip_quotes(os.getenv(key, "")).lower()
    if not val:
        return default
    return val in ("true", "1", "yes", "on")


class Config:
    """Typed configuration with hot-reload support.

    All settings are loaded from .env on init. Call reload() to
    re-read from disk (e.g., after GUI changes a setting).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reload()

    def reload(self):
        """Re-read all settings from .env file (thread-safe)."""
        lock = getattr(self, '_lock', None)
        if lock:
            lock.acquire()
        try:
            self._reload_inner()
        finally:
            if lock:
                lock.release()
        # Re-apply user-local secrets after every reload so they aren't wiped
        # by .env values (SPACESCAN_API_KEY is blank in .env by design).
        try:
            import user_secrets as _user_secrets
            _user_secrets.apply_to_config(self)
        except Exception:
            pass
        # Run config validation and cache the result so the API and GUI can
        # surface errors/warnings immediately after any reload.
        try:
            from config_validator import validate_config
            self._validation_report = validate_config(self)
        except Exception:
            self._validation_report = None

    def _reload_inner(self):
        """Internal reload — called under lock."""
        # Force re-read of .env
        load_dotenv(_ENV_PATH, override=True)

        # ----- Wallet Backend Selection -----
        # "sage" = Sage light wallet (port 9257, no full node needed) — default
        # "chia" = official Chia wallet (port 9256, requires full node) — legacy
        self.WALLET_TYPE = _str("WALLET_TYPE", "sage")

        # ----- Chia Wallet & Blockchain -----
        self.CHIA_WALLET_RPC_URL = _str("CHIA_WALLET_RPC_URL", "https://localhost:9256")
        self.CHIA_WALLET_CERT = _str("CHIA_WALLET_CERT")
        self.CHIA_WALLET_KEY = _str("CHIA_WALLET_KEY")
        self.CHIA_FULL_NODE_RPC_URL = _str("CHIA_FULL_NODE_RPC_URL", "https://localhost:8555")
        self.WALLET_ID_XCH = _int("CHIA_WALLET_ID_XCH", 1)
        self.WALLET_FINGERPRINT = _str("WALLET_FINGERPRINT")
        self.WALLET_DEBUG = _bool("WALLET_DEBUG", False)

        # ----- Sage Wallet (alternative backend) -----
        self.SAGE_RPC_URL = _str("SAGE_RPC_URL", "https://localhost:9257")
        self.SAGE_CERT_PATH = _str("SAGE_CERT_PATH")
        self.SAGE_KEY_PATH = _str("SAGE_KEY_PATH")
        self.SAGE_EXE_PATH = _str("SAGE_EXE_PATH")           # Auto-detected if empty
        self.SAGE_FINGERPRINT = _str("SAGE_FINGERPRINT")      # Auto-login fingerprint
        self.SAGE_DATA_DIR = _str("SAGE_DATA_DIR")            # Sage data dir (auto-detected)
        self.SAGE_SET_CHANGE_ADDRESS = _bool("SAGE_SET_CHANGE_ADDRESS", False)

        # ----- Wallet Address (for Spacescan self-spend detection) -----
        # Populated dynamically at startup from wallet RPC (get_next_address).
        # No need to set in .env — auto-detects from the connected wallet.
        self.WALLET_ADDRESS = ""

        # ----- CAT Token -----
        self.CAT_ASSET_ID = _str("CAT_ASSET_ID")
        self.CAT_TICKER_ID = _str("CAT_TICKER_ID")
        self.CAT_NAME = _str("CAT_NAME", "MZ")
        self.CAT_DECIMALS = _int("CAT_DECIMALS", 3)
        # CAT wallet_id — assigned dynamically by get_wallets() based on
        # which CAT matches CAT_ASSET_ID. Do NOT set in .env for Sage.
        # Default 2 is the standard Sage dynamic ID. get_wallets() will
        # confirm or override this on startup.
        self.CAT_WALLET_ID = _int("CAT_WALLET_ID", 2)

        # ----- Trading Core -----
        self.DRY_RUN = _bool("DRY_RUN", False)
        self.ENABLE_BUY = _bool("ENABLE_BUY", True)
        self.ENABLE_SELL = _bool("ENABLE_SELL", True)
        self.LOOP_SECONDS = _int("LOOP_SECONDS", 90)
        self.MIN_TRADE_XCH = _decimal("MIN_TRADE_XCH", "0.005")
        self.MAX_TRADE_XCH = _decimal("MAX_TRADE_XCH", "0.050")
        self.DEFAULT_TRADE_XCH = _decimal("DEFAULT_TRADE_XCH", "0.0275")

        # ----- Spread & Pricing -----
        self.SPREAD_BPS = _decimal("SPREAD_BPS", "800")
        self.MIN_EDGE_BPS = _decimal("MIN_EDGE_BPS", "300")
        self.PRICE_STRATEGY = _str("PRICE_STRATEGY", "weighted")
        self.TIBET_WEIGHT = _decimal("TIBET_WEIGHT", "0.85")
        self.ARB_ALERT_THRESHOLD_BPS = _decimal("ARB_ALERT_THRESHOLD_BPS", "200")

        # ----- Price Safety Guards -----
        # Dynamic limits: auto-calculated as reference_price ± DYNAMIC_LIMIT_PCT%.
        # The reference price is set from the first live price on startup,
        # then slowly tracks the market via EMA (1% weight per update).
        # Set to 0 to disable dynamic limits and use only hard limits.
        self.DYNAMIC_LIMIT_PCT = _decimal("DYNAMIC_LIMIT_PCT", "50")  # ±50% default

        # Hard limits: absolute backstop from .env. Set very wide (e.g. ±200%
        # of expected price) or to 0 to disable.  These only fire if the price
        # is WAY outside normal range (oracle failure, bug, etc.).
        # The GUI writes HARD_MIN/HARD_MAX_PRICE_XCH. Legacy .env files may use
        # MIN_MID / MAX_MID instead — fall back to those so old configs stay protected.
        _hard_min = _decimal("HARD_MIN_PRICE_XCH", "0")
        _hard_max = _decimal("HARD_MAX_PRICE_XCH", "0")
        _legacy_min = _decimal("MIN_MID", "0") if _str("MIN_MID") else Decimal("0")
        _legacy_max = _decimal("MAX_MID", "0") if _str("MAX_MID") else Decimal("0")
        self.HARD_MIN_PRICE_XCH = _hard_min if _hard_min > 0 else _legacy_min
        self.HARD_MAX_PRICE_XCH = _hard_max if _hard_max > 0 else _legacy_max
        self.MAX_STEP_CHANGE_FRACTION = _decimal("MAX_STEP_CHANGE_FRACTION", "0")
        self.MIN_MID = _decimal("MIN_MID", "0") if _str("MIN_MID") else None
        self.MAX_MID = _decimal("MAX_MID", "0") if _str("MAX_MID") else None
        self.MAX_MID_MOVE_BPS = _decimal("MAX_MID_MOVE_BPS", "500")

        # ----- Offer Management -----
        # GUI writes MAX_ACTIVE_BUY; old .env may have MAX_ACTIVE_BUY_OFFERS.
        # GUI key takes priority (it's what the user actually sets).
        self.MAX_ACTIVE_BUY_OFFERS = _int("MAX_ACTIVE_BUY") or _int("MAX_ACTIVE_BUY_OFFERS", 0)
        self.MAX_ACTIVE_SELL_OFFERS = _int("MAX_ACTIVE_SELL") or _int("MAX_ACTIVE_SELL_OFFERS", 0)
        self.OFFER_EXPIRY_SECS = _int("OFFER_EXPIRY_SECS", 86400)  # 24 hours — safety net only
        self.OFFER_STAGGER_SECS = _int("OFFER_STAGGER_SECS", 10)  # Stagger to avoid mass-expiry
        self.OFFER_REFRESH_BEFORE = _int("OFFER_REFRESH_BEFORE", 1800)  # Refresh 30 min before expiry (settle time)
        self.FILL_PROTECT_SECS = _int("FILL_PROTECT_SECS", 180)

        # ----- Requoting -----
        self.AUTO_REQUOTE = _bool("AUTO_REQUOTE", True)
        self.REQUOTE_BPS = _decimal("REQUOTE_BPS", "0")
        self.REQUOTE_COOLDOWN_SECS = _int("REQUOTE_COOLDOWN_SECS", 60)
        self.REQUOTE_BATCH_SIZE = _int("REQUOTE_BATCH_SIZE", 5)
        self.REQUOTE_COIN_FREE_WAIT = _int("REQUOTE_COIN_FREE_WAIT", 5)

        # ----- Reserves -----
        self.XCH_RESERVE = _decimal("XCH_RESERVE", "0.03")
        # Support both new and legacy names
        self.CAT_RESERVE = _decimal("CAT_RESERVE") or _decimal("MZ_RESERVE", "0")

        # ----- Coin Preparation -----
        self.ENABLE_COIN_PREP = _bool("ENABLE_COIN_PREP", False)
        self.COIN_PREP_COOLDOWN_SECS = _int("COIN_PREP_COOLDOWN_SECS", 3600)
        self.XCH_TARGET_COINS = _int("XCH_TARGET_COINS", 50)
        self.XCH_COIN_SIZE = _decimal("XCH_COIN_SIZE", "0.25")
        self.CAT_TARGET_COINS = _int("CAT_TARGET_COINS", 50)
        self.CAT_COIN_SIZE = _decimal("CAT_COIN_SIZE", "4000")

        # ----- Dexie Integration -----
        self.DEXIE_API_BASE = _safe_url("DEXIE_API_BASE", "https://api.dexie.space")
        self.DEXIE_AUTO_POST = _bool("DEXIE_AUTO_POST", True)
        self.DEXIE_POST_ENABLED = _bool("DEXIE_POST_ENABLED", True)
        self.DEXIE_POST_TIMEOUT = _int("DEXIE_POST_TIMEOUT", 15)
        self.DEXIE_POST_RETRIES = _int("DEXIE_POST_RETRIES", 2)
        self.DEXIE_POST_RETRY_SLEEP = float(_str("DEXIE_POST_RETRY_SLEEP", "1.5"))
        self.MAX_POSTS_PER_LOOP = _int("MAX_POSTS_PER_LOOP", 30)
        self.BOT_TAG = _str("BOT_TAG", "CAT_MM_BOT")

        # ----- TibetSwap -----
        self.TIBET_API_BASE = _safe_url("TIBET_API_BASE", "https://api.v2.tibetswap.io")
        self.TIBET_TIMEOUT = _int("TIBET_TIMEOUT", 10)

        # ----- AMM Monitor (Fill Intelligence Tier 1) -----
        # Set TIBET_PAIR_ID to the pair ID for your token on TibetSwap v2.
        # Find it at https://v2.tibetswap.io — the pair ID is in the URL.
        self.TIBET_PAIR_ID = _str("TIBET_PAIR_ID", "")
        self.AMM_POLL_INTERVAL_SECS = _int("AMM_POLL_INTERVAL_SECS", 30)
        # AMM_DRIFT_REQUOTE_BPS: if AMM price moves this far from our last
        # quoted mid-price, invalidate the Tibet cache to force a fresh requote.
        self.AMM_DRIFT_REQUOTE_BPS = _decimal("AMM_DRIFT_REQUOTE_BPS", "40")
        # ENABLE_AMM_BUFFER: when True, offers within AMM_BUFFER_BPS of the
        # AMM price are skipped — they would be instantly arbed by TibetSwap.
        self.ENABLE_AMM_BUFFER = _bool("ENABLE_AMM_BUFFER", False)
        self.AMM_BUFFER_BPS = _decimal("AMM_BUFFER_BPS", "30")
        # Mempool watching: poll Coinset for pending spends on our offer coins.
        # Gives ~30-50s earlier fill detection before block confirmation.
        self.ENABLE_MEMPOOL_WATCH = _bool("ENABLE_MEMPOOL_WATCH", False)
        self.MEMPOOL_POLL_INTERVAL_SECS = _int("MEMPOOL_POLL_INTERVAL_SECS", 10)
        # Known arb bot puzzle hashes (hex, no 0x prefix, comma-separated).
        # Used to classify fills as arb sweeps vs retail/combined.
        self.KNOWN_ARB_PUZZLE_HASHES = [
            h.strip().lower().lstrip("0x")
            for h in _str("KNOWN_ARB_PUZZLE_HASHES", "").split(",")
            if h.strip()
        ]

        # ----- Runtime Features -----
        self.ENABLE_RUNTIME_COIN_HEALTH = _bool("ENABLE_RUNTIME_COIN_HEALTH", False)

        # ===== V2 NEW SETTINGS =====

        # ----- Inventory Management (V2) -----
        self.INVENTORY_ENABLED = _bool("INVENTORY_ENABLED", True)
        self.SKEW_INTENSITY = _decimal("SKEW_INTENSITY", "0.3")
        self.MAX_POSITION_XCH = _decimal("MAX_POSITION_XCH", "5.0")

        # ----- Dynamic Spreads (V2) -----
        self.DYNAMIC_SPREAD_ENABLED = _bool("DYNAMIC_SPREAD_ENABLED", False)
        self.BASE_SPREAD_BPS = _decimal("BASE_SPREAD_BPS", "0")
        self.MIN_SPREAD_BPS = _decimal("MIN_SPREAD_BPS", "300")
        self.MAX_SPREAD_BPS = _decimal("MAX_SPREAD_BPS", "3000")
        self.VOLATILITY_WINDOW_HOURS = _decimal("VOLATILITY_WINDOW_HOURS", "4")
        self.DYNAMIC_FILL_RATE_START_PER_HOUR = _decimal(
            "DYNAMIC_FILL_RATE_START_PER_HOUR", "4"
        )
        self.DYNAMIC_FILL_RATE_FULL_PER_HOUR = _decimal(
            "DYNAMIC_FILL_RATE_FULL_PER_HOUR", "12"
        )
        self.DYNAMIC_FILL_RATE_MAX_BPS = _decimal(
            "DYNAMIC_FILL_RATE_MAX_BPS", "100"
        )

        # ----- Tiered Orders (V2) -----
        self.TIER_ENABLED = _bool("TIER_ENABLED", False)
        # When True, buy-side tier sizes are reversed: smallest offer closest to
        # price (less XCH committed when sellers come in), largest offer furthest
        # from price (big commitment only on real support-level drops).
        # Sell side is unaffected — large sells stay closest to price.
        self.BUY_LADDER_REVERSED = _bool("BUY_LADDER_REVERSED", False)
        self.INNER_SIZE_XCH = _decimal("INNER_SIZE_XCH", "0")
        self.MID_SIZE_XCH = _decimal("MID_SIZE_XCH", "0")
        self.OUTER_SIZE_XCH = _decimal("OUTER_SIZE_XCH", "0")
        self.EXTREME_SIZE_XCH = _decimal("EXTREME_SIZE_XCH", "0")
        self.INNER_TIER_COUNT = _int("INNER_TIER_COUNT", 0)
        self.MID_TIER_COUNT = _int("MID_TIER_COUNT", 0)
        self.OUTER_TIER_COUNT = _int("OUTER_TIER_COUNT", 0)
        self.EXTREME_TIER_COUNT = _int("EXTREME_TIER_COUNT", 0)
        self.INNER_TIER_SPARE_COUNT = _int("INNER_TIER_SPARE_COUNT", 0)
        self.MID_TIER_SPARE_COUNT = _int("MID_TIER_SPARE_COUNT", 0)
        self.OUTER_TIER_SPARE_COUNT = _int("OUTER_TIER_SPARE_COUNT", 0)
        self.EXTREME_TIER_SPARE_COUNT = _int("EXTREME_TIER_SPARE_COUNT", 0)

        # ----- Coin Prep -----
        self.COIN_PREP_MULTIPLIER = _decimal("COIN_PREP_MULTIPLIER", "1.0")
        self.COIN_PREP_HEADROOM_PCT = _decimal("COIN_PREP_HEADROOM_PCT", "10")
        default_fee_mode = "manual" if self.WALLET_TYPE == "sage" else "auto"
        self.TRANSACTION_FEE_MODE = _str("TRANSACTION_FEE_MODE", default_fee_mode)
        self.TRANSACTION_FEE_XCH = _decimal("TRANSACTION_FEE_XCH", "0")
        self.TRANSACTION_FEE_TARGET_SECS = _int("TRANSACTION_FEE_TARGET_SECS", 300)
        self.TRANSACTION_FEE_ESTIMATE_COST = _int("TRANSACTION_FEE_ESTIMATE_COST", 20_000_000)
        self.FEE_PREP_COUNT = _int("FEE_PREP_COUNT", 20)
        self.FEE_COIN_SIZE_XCH = _decimal("FEE_COIN_SIZE_XCH", "0.0001")
        self.LADDER_CREATE_PARALLELISM = _int("LADDER_CREATE_PARALLELISM", 5)
        self.LADDER_CREATE_DELAY_MS = _int("LADDER_CREATE_DELAY_MS", 0)
        self.LADDER_CREATE_GLOBAL_SERIAL = _bool("LADDER_CREATE_GLOBAL_SERIAL", False)

        # ----- Adaptive Coin Management (V3 — designation-based pools) -----
        # Topup triggers: % of spares remaining before replenishment starts
        self.TOPUP_SLOW_PCT = _int("TOPUP_SLOW_PCT", 20)        # Trigger at 20% spares on slow days
        self.TOPUP_NORMAL_PCT = _int("TOPUP_NORMAL_PCT", 30)     # Trigger at 30% spares normally
        self.TOPUP_BUSY_PCT = _int("TOPUP_BUSY_PCT", 50)         # Trigger at 50% spares on busy days
        # Trading pace thresholds (fills per hour)
        self.FILLS_PER_HOUR_BUSY = _int("FILLS_PER_HOUR_BUSY", 10)   # >10 fills/hr = busy
        self.FILLS_PER_HOUR_SLOW = _int("FILLS_PER_HOUR_SLOW", 2)    # <2 fills/hr = slow
        # Wallet reconciliation frequency
        self.RECONCILE_EVERY_N_LOOPS = _int("RECONCILE_EVERY_N_LOOPS", 2)

        # ----- Sniper (V2) -----
        self.SNIPER_ENABLED = _bool("SNIPER_ENABLED", True)
        self.SNIPER_SIZE_XCH = _decimal("SNIPER_SIZE_XCH", "0.001")
        self.SNIPER_PREP_COUNT = _int("SNIPER_PREP_COUNT", 20)
        self.SNIPER_EXPIRY_SECS = _int("SNIPER_EXPIRY_SECS", 600)  # 10 min — edge probes should expire quickly
        self.SNIPER_COOLDOWN_SECS = _int("SNIPER_COOLDOWN_SECS", 30)
        self.SNIPER_CONFIRM_SECS = _int("SNIPER_CONFIRM_SECS", 54)  # ~3 Chia blocks (18s each)
        self.SNIPER_LINGER_SECS = _int("SNIPER_LINGER_SECS", 600)  # 10 min — keep edge only briefly
        self.SNIPER_POLL_SECS = _int("SNIPER_POLL_SECS", 5)  # Fast wallet poll cadence while probes are active
        self.SNIPER_BUFFER_BPS = _decimal("SNIPER_BUFFER_BPS", "50")  # How far past TibetSwap to place sniper (0.5%)
        self.SNIPER_TOP_BOOK_BPS = _decimal("SNIPER_TOP_BOOK_BPS", "1")  # Improve the live best bid/ask by 1 BPS when probing
        self.SNIPER_RETRY_BACKOFF_BPS = _decimal("SNIPER_RETRY_BACKOFF_BPS", "50")  # How far to step back from the last sniper after it gets arbed
        self.SNIPER_MAIN_BOOK_GUARD_BPS = _decimal("SNIPER_MAIN_BOOK_GUARD_BPS", "1")  # Keep the main book 1 BPS behind the probe
        self.SNIPER_MIN_GAP_BPS = _decimal("SNIPER_MIN_GAP_BPS", "400")  # Only snipe on big moves (4%+)
        self.SNIPER_REARM_PRICE_MOVE_BPS = _decimal("SNIPER_REARM_PRICE_MOVE_BPS", "100")  # Re-discover only after ~1% price move
        self.SNIPER_REARM_GAP_MOVE_BPS = _decimal("SNIPER_REARM_GAP_MOVE_BPS", "100")  # Or after ~1% arb-gap shift

        # ----- Close the Gap (Dexie ranking improvement) -----
        # Offer settings
        self.BOOST_SIZE_XCH = _decimal("BOOST_SIZE_XCH", "0.2")  # Small offers to minimise arb risk
        self.BOOST_EXPIRY_SECS = _int("BOOST_EXPIRY_SECS", 1800)  # 30 min — auto-cleanup
        self.BOOST_SPREAD_BPS = _int("BOOST_SPREAD_BPS", 200)  # Fallback spread if no main book
        # Adaptive gap-closing strategy
        self.GAP_CLOSE_START_PCT = _int("GAP_CLOSE_START_PCT", 75)  # Start at 75% of main spread
        self.GAP_CLOSE_STEP_PCT = _int("GAP_CLOSE_STEP_PCT", 10)  # Tighten 10% per stable period
        self.GAP_CLOSE_SAFETY_BUFFER_BPS = _int("GAP_CLOSE_SAFETY_BUFFER_BPS", 20)  # Buffer above arb gap
        self.GAP_CLOSE_STEP_COOLDOWN_SECS = _int("GAP_CLOSE_STEP_COOLDOWN_SECS", 60)  # 1 min between steps
        self.GAP_CLOSE_CONVERGENCE_SECS = _int("GAP_CLOSE_CONVERGENCE_SECS", 120)  # 2 min between main book convergence steps
        self.GAP_CLOSE_CONVERGENCE_STEP_PCT = _int("GAP_CLOSE_CONVERGENCE_STEP_PCT", 20)  # Main book tightens 20% per step
        self.GAP_CLOSE_CASCADE_WAIT_SECS = _int("GAP_CLOSE_CASCADE_WAIT_SECS", 60)  # Wait 60s before cascading main book behind probe
        self.GAP_CLOSE_CASCADE_BATCH_SIZE = _int("GAP_CLOSE_CASCADE_BATCH_SIZE", 5)  # How many offers to replace per cascade batch

        # ----- Market Intelligence (V2 — ecosystem upgrades) -----
        self.COMPETITOR_AWARE_ENABLED = _bool("COMPETITOR_AWARE_ENABLED", False)
        self.OFFERPOOL_ENABLED = _bool("OFFERPOOL_ENABLED", False)
        self.OFFERPOOL_API_URL = _safe_url("OFFERPOOL_API_URL", "https://offerpool.io/api/v1/offers")
        self.DBX_MAX_SPREAD_BPS = _decimal("DBX_MAX_SPREAD_BPS", "500")

        # ----- Splash Network (V3 — decentralized offer broadcasting) -----
        self.SPLASH_ENABLED = _bool("SPLASH_ENABLED", False)
        self.SPLASH_SUBMIT_URL = _str("SPLASH_SUBMIT_URL", "http://localhost:4000")
        self.SPLASH_POST_RETRIES = _int("SPLASH_POST_RETRIES", 2)
        self.SPLASH_POST_TIMEOUT = _int("SPLASH_POST_TIMEOUT", 15)
        self.SPLASH_POST_RETRY_SLEEP = float(_str("SPLASH_POST_RETRY_SLEEP", "1.5"))
        self.SPLASH_RECEIVE_ENABLED = _bool("SPLASH_RECEIVE_ENABLED", True)
        self.SPLASH_RECEIVE_POLL_SECS = _int("SPLASH_RECEIVE_POLL_SECS", 5)
        self.SPLASH_RECEIVE_BATCH_SIZE = _int("SPLASH_RECEIVE_BATCH_SIZE", 10)
        self.SPLASH_BINARY_PATH = _str("SPLASH_BINARY_PATH", "")
        self.SPLASH_P2P_PORT = _int("SPLASH_P2P_PORT", 11511)
        self.SPLASH_AUTO_START = _bool("SPLASH_AUTO_START", True)
        self.SPLASH_TESTNET = _bool("SPLASH_TESTNET", False)

        # ----- Coin Selection (V3 — deterministic coin locking via Sage PR#761) -----
        # When enabled, the bot pre-selects which coin to use for each offer
        # and passes it via coin_ids to make_offer. Eliminates before/after
        # snapshot polling (~45x faster batch creation). Requires Sage wallet
        # with coin_ids support. Falls back to polling if selection fails.
        # Chia wallet ignores coin_ids silently (always uses polling).
        self.COIN_IDS_ENABLED = _bool("COIN_IDS_ENABLED", True)  # Default ON — 4x faster offer creation

        # ----- Coinset API (V3 — fast cloud coin queries) -----
        self.COINSET_ENABLED = _bool("COINSET_ENABLED", True)
        self.COINSET_API_URL = _safe_url("COINSET_API_URL", "https://api.coinset.org")
        self.COINSET_TIMEOUT = _int("COINSET_TIMEOUT", 5)
        self.COINSET_FALLBACK_WALLET = _bool("COINSET_FALLBACK_WALLET", True)

        # ----- Spacescan (V4 — on-chain verification, golden source of truth) -----
        self.SPACESCAN_ENABLED = _bool("SPACESCAN_ENABLED", True)
        self.SPACESCAN_API_KEY = _str("SPACESCAN_API_KEY", "")
        self.SPACESCAN_PRO_URL = _safe_url("SPACESCAN_PRO_URL", "https://pro-api.spacescan.io")
        self.SPACESCAN_FREE_URL = _safe_url("SPACESCAN_FREE_URL", "https://api.spacescan.io")
        self.SPACESCAN_TIMEOUT = _int("SPACESCAN_TIMEOUT", 10)
        self.SPACESCAN_BALANCE_CHECK_EVERY_N = _int("SPACESCAN_BALANCE_CHECK_EVERY_N", 10)  # Check balance every N loops
        self.SPACESCAN_BALANCE_THRESHOLD_XCH = _decimal("SPACESCAN_BALANCE_THRESHOLD_XCH", "0.1")  # Alert if diff > this
        self.RUNTIME_MONITOR_ENABLED = _bool("RUNTIME_MONITOR_ENABLED", True)
        self.RUNTIME_MONITOR_POLL_SECS = _int("RUNTIME_MONITOR_POLL_SECS", 20)
        self.RUNTIME_MONITOR_DEXIE_GRACE_SECS = _int("RUNTIME_MONITOR_DEXIE_GRACE_SECS", 120)
        self.RUNTIME_MONITOR_TOPUP_WARN_SECS = _int("RUNTIME_MONITOR_TOPUP_WARN_SECS", 900)
        self.RUNTIME_MONITOR_STALE_POLLS = _int("RUNTIME_MONITOR_STALE_POLLS", 2)
        # Stale wallet data guard: block new offer creation after this many
        # consecutive stale sync cycles. Default 3 (~15s at 5s/loop).
        self.WALLET_STALE_CREATE_LIMIT = _int("WALLET_STALE_CREATE_LIMIT", 3)

    # Settings that are safe to modify via the GUI/API.
    # Credentials, paths, and wallet URLs are excluded to prevent
    # an attacker with API access from redirecting wallet RPC.
    _UPDATABLE_KEYS = {
        # Trading core
        "DRY_RUN", "ENABLE_BUY", "ENABLE_SELL", "LOOP_SECONDS",
        "MIN_TRADE_XCH", "MAX_TRADE_XCH", "DEFAULT_TRADE_XCH",
        # Spread & pricing
        "SPREAD_BPS", "MIN_EDGE_BPS", "PRICE_STRATEGY", "TIBET_WEIGHT",
        "ARB_ALERT_THRESHOLD_BPS",
        # Price safety
        "DYNAMIC_LIMIT_PCT", "HARD_MIN_PRICE_XCH", "HARD_MAX_PRICE_XCH",
        "MAX_STEP_CHANGE_FRACTION", "MIN_MID", "MAX_MID", "MAX_MID_MOVE_BPS",
        # Offer management
        "MAX_ACTIVE_BUY", "MAX_ACTIVE_SELL",
        "MAX_ACTIVE_BUY_OFFERS", "MAX_ACTIVE_SELL_OFFERS",
        "OFFER_EXPIRY_SECS", "OFFER_STAGGER_SECS",
        "OFFER_REFRESH_BEFORE", "FILL_PROTECT_SECS",
        # Requoting
        "AUTO_REQUOTE", "REQUOTE_BPS", "REQUOTE_COOLDOWN_SECS",
        "REQUOTE_BATCH_SIZE", "REQUOTE_COIN_FREE_WAIT",
        # Reserves
        "XCH_RESERVE", "CAT_RESERVE", "MZ_RESERVE",
        # Coin prep
        "ENABLE_COIN_PREP", "COIN_PREP_COOLDOWN_SECS",
        "XCH_TARGET_COINS", "XCH_COIN_SIZE",
        "CAT_TARGET_COINS", "CAT_COIN_SIZE",
        "COIN_PREP_MULTIPLIER", "COIN_PREP_HEADROOM_PCT",
        "TRANSACTION_FEE_MODE", "TRANSACTION_FEE_XCH",
        "TRANSACTION_FEE_TARGET_SECS", "TRANSACTION_FEE_ESTIMATE_COST",
        "FEE_PREP_COUNT", "FEE_COIN_SIZE_XCH",
        "LADDER_CREATE_PARALLELISM", "LADDER_CREATE_DELAY_MS",
        "LADDER_CREATE_GLOBAL_SERIAL",
        # Dexie
        "DEXIE_AUTO_POST", "DEXIE_POST_ENABLED",
        "DEXIE_POST_TIMEOUT", "DEXIE_POST_RETRIES",
        "DEXIE_POST_RETRY_SLEEP", "MAX_POSTS_PER_LOOP", "BOT_TAG",
        # TibetSwap
        "TIBET_TIMEOUT",
        # Runtime features
        "ENABLE_RUNTIME_COIN_HEALTH",
        "SAGE_SET_CHANGE_ADDRESS",
        # Inventory
        "INVENTORY_ENABLED", "SKEW_INTENSITY", "MAX_POSITION_XCH",
        # Dynamic spreads
        "DYNAMIC_SPREAD_ENABLED", "BASE_SPREAD_BPS",
        "MIN_SPREAD_BPS", "MAX_SPREAD_BPS",
        "VOLATILITY_WINDOW_HOURS",
        "DYNAMIC_FILL_RATE_START_PER_HOUR",
        "DYNAMIC_FILL_RATE_FULL_PER_HOUR",
        "DYNAMIC_FILL_RATE_MAX_BPS",
        # Tiered orders
        "TIER_ENABLED", "BUY_LADDER_REVERSED", "INNER_SIZE_XCH", "MID_SIZE_XCH",
        "OUTER_SIZE_XCH", "EXTREME_SIZE_XCH",
        "INNER_TIER_COUNT", "MID_TIER_COUNT",
        "OUTER_TIER_COUNT", "EXTREME_TIER_COUNT",
        "INNER_TIER_SPARE_COUNT", "MID_TIER_SPARE_COUNT",
        "OUTER_TIER_SPARE_COUNT", "EXTREME_TIER_SPARE_COUNT",
        # Adaptive coin management
        "TOPUP_SLOW_PCT", "TOPUP_NORMAL_PCT", "TOPUP_BUSY_PCT",
        "FILLS_PER_HOUR_BUSY", "FILLS_PER_HOUR_SLOW",
        "RECONCILE_EVERY_N_LOOPS",
        # Sniper
        "SNIPER_ENABLED", "SNIPER_SIZE_XCH", "SNIPER_PREP_COUNT",
        "SNIPER_EXPIRY_SECS", "SNIPER_COOLDOWN_SECS",
        "SNIPER_CONFIRM_SECS", "SNIPER_LINGER_SECS",
        "SNIPER_POLL_SECS", "SNIPER_BUFFER_BPS",
        "SNIPER_TOP_BOOK_BPS", "SNIPER_RETRY_BACKOFF_BPS",
        "SNIPER_MAIN_BOOK_GUARD_BPS", "SNIPER_MIN_GAP_BPS",
        "SNIPER_REARM_PRICE_MOVE_BPS", "SNIPER_REARM_GAP_MOVE_BPS",
        # Close the Gap / Boost
        "BOOST_SIZE_XCH", "BOOST_EXPIRY_SECS", "BOOST_SPREAD_BPS",
        "GAP_CLOSE_START_PCT", "GAP_CLOSE_STEP_PCT",
        "GAP_CLOSE_SAFETY_BUFFER_BPS", "GAP_CLOSE_STEP_COOLDOWN_SECS",
        "GAP_CLOSE_CONVERGENCE_SECS", "GAP_CLOSE_CONVERGENCE_STEP_PCT",
        "GAP_CLOSE_CASCADE_WAIT_SECS", "GAP_CLOSE_CASCADE_BATCH_SIZE",
        # Market intel
        "COMPETITOR_AWARE_ENABLED", "OFFERPOOL_ENABLED",
        "DBX_MAX_SPREAD_BPS",
        # Splash
        "SPLASH_ENABLED", "SPLASH_POST_RETRIES",
        "SPLASH_POST_TIMEOUT", "SPLASH_POST_RETRY_SLEEP",
        "SPLASH_RECEIVE_ENABLED", "SPLASH_RECEIVE_POLL_SECS",
        "SPLASH_RECEIVE_BATCH_SIZE", "SPLASH_P2P_PORT",
        "SPLASH_AUTO_START", "SPLASH_TESTNET",
        # Coin IDs
        "COIN_IDS_ENABLED",
        # Coinset
        "COINSET_ENABLED", "COINSET_TIMEOUT", "COINSET_FALLBACK_WALLET",
        # Spacescan
        "SPACESCAN_ENABLED", "SPACESCAN_TIMEOUT",
        "SPACESCAN_BALANCE_CHECK_EVERY_N",
        "SPACESCAN_BALANCE_THRESHOLD_XCH",
        "RUNTIME_MONITOR_ENABLED", "RUNTIME_MONITOR_POLL_SECS",
        "RUNTIME_MONITOR_DEXIE_GRACE_SECS",
        "RUNTIME_MONITOR_TOPUP_WARN_SECS",
        "RUNTIME_MONITOR_STALE_POLLS",
        "WALLET_STALE_CREATE_LIMIT",
        # CAT identity (safe — does not control wallet access)
        "CAT_ASSET_ID", "CAT_TICKER_ID", "CAT_NAME", "CAT_DECIMALS",
        # TibetSwap pair (auto-resolved by cat_resolver at startup)
        "TIBET_PAIR_ID",
    }

    def update(self, key: str, value: str) -> bool:
        """Update a setting: writes to .env and refreshes in-memory value.

        Only keys in _UPDATABLE_KEYS can be modified via the API.
        Credentials, wallet URLs, and cert paths are blocked.

        Args:
            key: The setting name (e.g., 'SPREAD_BPS')
            value: The new value as a string

        Returns True if successful.
        """
        if key not in self._UPDATABLE_KEYS:
            print(f"[CONFIG] Blocked update of non-updatable key: {key}")
            return False

        # Reject control characters that could inject extra .env lines
        if any(c in str(value) for c in ("\n", "\r", "\x00")):
            print(f"[CONFIG] Blocked update of {key}: value contains control characters")
            return False

        try:
            # Record old value for change tracking
            old_value = str(getattr(self, key, ""))

            # Write to .env file
            set_key(_ENV_PATH, key, value)

            # Reload all settings from disk
            self.reload()

            # Record the change (import here to avoid circular import)
            try:
                from database import record_config_change
                record_config_change(key, old_value, value)
            except ImportError:
                pass  # Database not available yet during early startup

            return True
        except Exception as e:
            print(f"[CONFIG] Failed to update {key}: {e}")
            try:
                from database import log_event as _log_cfg
                _log_cfg("error", "config_error", f"Failed to update {key}: {e}")
            except Exception:
                pass
            return False

    def validate(self) -> dict:
        """Validate that critical numeric settings are in sane ranges.

        Returns a dict:
            {
                "warnings": [str, ...],   # out-of-range but bot can continue
                "errors":   [str, ...],   # dangerous values — bot should not start
            }

        Call this at startup and log/display every entry.  An "error" entry
        does NOT raise — the caller decides whether to abort.
        """
        warnings = []
        errors = []

        # SPREAD_BPS: must be positive; warn if outside 10-5000
        spread = self.SPREAD_BPS
        if spread <= Decimal("0"):
            errors.append(
                f"SPREAD_BPS={spread} is zero or negative — spread is invalid, "
                f"bot will create mis-priced offers"
            )
        elif spread < Decimal("10"):
            warnings.append(
                f"SPREAD_BPS={spread} is very low (<10 bps) — nearly zero spread, "
                f"risk of loss on every round-trip"
            )
        elif spread > Decimal("5000"):
            warnings.append(
                f"SPREAD_BPS={spread} is very high (>5000 bps) — offers will be "
                f"far from market price and unlikely to fill"
            )

        # LOOP_SECONDS: must be positive; warn outside 5-3600
        loop = self.LOOP_SECONDS
        if loop <= 0:
            errors.append(
                f"LOOP_SECONDS={loop} is zero or negative — main loop will spin continuously"
            )
        elif loop < 5:
            warnings.append(
                f"LOOP_SECONDS={loop} is very short (<5s) — may overwhelm wallet RPC"
            )
        elif loop > 3600:
            warnings.append(
                f"LOOP_SECONDS={loop} is very long (>3600s) — book will be stale for >1 hour"
            )

        # MAX_ACTIVE_BUY_OFFERS / MAX_ACTIVE_SELL_OFFERS: 1-50
        for attr, label in [
            ("MAX_ACTIVE_BUY_OFFERS", "MAX_ACTIVE_BUY_OFFERS"),
            ("MAX_ACTIVE_SELL_OFFERS", "MAX_ACTIVE_SELL_OFFERS"),
        ]:
            val = getattr(self, attr, 0)
            if val <= 0:
                warnings.append(
                    f"{label}={val} — that side is disabled (no offers will be placed)"
                )
            elif val > 50:
                warnings.append(
                    f"{label}={val} is very high (>50) — large coin pools required, "
                    f"wallet RPC load will be high"
                )

        # REQUOTE_BPS: warn if zero or very high; must be positive
        requote = self.REQUOTE_BPS
        if requote <= Decimal("0"):
            warnings.append(
                f"REQUOTE_BPS={requote} is zero or negative — requoting is effectively "
                f"disabled (offers will never refresh based on price drift)"
            )
        elif requote > Decimal("2000"):
            warnings.append(
                f"REQUOTE_BPS={requote} is very high (>2000 bps) — requotes will only "
                f"fire after a 20%+ price move; stale offers may fill at bad prices"
            )

        # XCH_RESERVE: warn if > 10 XCH (likely a misconfiguration)
        reserve = self.XCH_RESERVE
        if reserve < Decimal("0"):
            errors.append(
                f"XCH_RESERVE={reserve} is negative — invalid reserve value"
            )
        elif reserve > Decimal("10"):
            warnings.append(
                f"XCH_RESERVE={reserve} XCH is large (>10) — verify this is intentional; "
                f"most bots use 0.01-0.1 XCH as reserve"
            )

        # CAT_ASSET_ID: must be set and look like a valid 64-char hex string
        asset_id = (self.CAT_ASSET_ID or "").strip()
        if not asset_id:
            errors.append(
                "CAT_ASSET_ID is not set — bot cannot trade without a CAT token configured"
            )
        elif len(asset_id) != 64:
            errors.append(
                f"CAT_ASSET_ID='{asset_id[:20]}...' is not 64 hex characters — "
                f"likely a misconfigured or truncated asset ID"
            )

        return {"warnings": warnings, "errors": errors}

    def get_spread_fraction(self) -> Decimal:
        """Convert SPREAD_BPS to a fraction (e.g., 800 BPS → 0.08)."""
        return self.SPREAD_BPS / Decimal("10000")

    def get_requote_fraction(self) -> Decimal:
        """Convert REQUOTE_BPS to a fraction."""
        return self.REQUOTE_BPS / Decimal("10000")

    def to_dict(self) -> dict:
        """Export all settings as a dictionary (for API responses).

        Excludes sensitive wallet credentials.
        """
        excluded = {"CHIA_WALLET_CERT", "CHIA_WALLET_KEY", "WALLET_FINGERPRINT",
                    "SPACESCAN_API_KEY",
                    "SAGE_CERT_PATH", "SAGE_KEY_PATH", "SAGE_FINGERPRINT",
                    "SAGE_EXE_PATH", "SAGE_DATA_DIR"}
        result = {}
        for key, value in self.__dict__.items():
            if key.startswith("_") or key in excluded:
                continue
            # Convert Decimal to string for JSON serialization
            if isinstance(value, Decimal):
                result[key] = str(value)
            else:
                result[key] = value
        return result


# ---------------------------------------------------------------------------
# Global config instance — import this everywhere
# ---------------------------------------------------------------------------
cfg = Config()
