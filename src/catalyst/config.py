"""Centralised typed configuration for the bot with hot-reload support

The `Config` class loads settings from the user's `.env` file (resolved via
`user_paths.env_file()`) once at import time and exposes them as typed
attributes. The module-level singleton `cfg` is the canonical access point
used everywhere in the codebase — `from config import cfg; cfg.SPREAD_BPS` —
so no other module should read environment variables directly.

Key responsibilities:
    - Parse `.env` into typed fields (Decimal for prices, int/bool/str)
    - Provide `cfg.update(key, value)` to write back to `.env` and reload
    - Expose `cfg.reload()` for hot-reload after external edits
    - Seed `.env` from `.env.example` on first launch

All mutations are guarded by an internal lock so concurrent GUI and
trading-loop writes stay consistent.
"""

import os
import sys
import threading
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from dotenv import load_dotenv, set_key


def _find_env_example_path(install_dir: str) -> str:
    """Find the bundled `.env.example` across source and frozen layouts."""
    roots = []

    def add_root(path: str) -> None:
        if not path:
            return
        root = os.path.abspath(path)
        if root not in roots:
            roots.append(root)

    add_root(install_dir)
    # Source layout: src/catalyst -> src -> repo root.
    add_root(os.path.dirname(install_dir))
    add_root(os.path.dirname(os.path.dirname(install_dir)))
    # PyInstaller onedir layout: modules live in _internal, while build.py
    # copies .env.example beside Catalyst.exe.
    if getattr(sys, "frozen", False):
        add_root(os.path.dirname(sys.executable))
        add_root(getattr(sys, "_MEIPASS", ""))
    # Dev fallback when launched from the repo root.
    add_root(os.getcwd())

    for root in roots:
        candidate = os.path.join(root, ".env.example")
        if os.path.isfile(candidate):
            return candidate
    return ""


# ---------------------------------------------------------------------------
# Load .env from the user data directory (writable across install types).
#
# The user data dir lives under %APPDATA% (Windows), ~/Library/Application
# Support (macOS), or ~/.local/share (Linux).  user_paths.py handles
# first-launch migration from the legacy install-dir location, so
# existing dev installs keep working transparently.
# ---------------------------------------------------------------------------
try:
    from user_paths import env_file as _env_file, install_dir as _install_dir
    _ENV_PATH = _env_file()
    # If the user data .env doesn't exist yet but a template does in the
    # install dir, seed the data-dir .env from .env.example so first-run
    # users start with sensible defaults.
    if not os.path.exists(_ENV_PATH):
        _example = _find_env_example_path(_install_dir())
        if _example:
            try:
                import shutil as _shutil
                _tmp_env = f"{_ENV_PATH}.{os.getpid()}.tmp"
                _shutil.copy2(_example, _tmp_env)
                try:
                    os.replace(_tmp_env, _ENV_PATH)
                except OSError:
                    # Another process may have won the first-run seed race.
                    if not os.path.exists(_ENV_PATH):
                        raise
                    try:
                        os.unlink(_tmp_env)
                    except OSError:
                        pass
            except Exception as _copy_err:
                print(f"[config] Could not seed .env from .env.example: {_copy_err}", flush=True)
except Exception as _e:
    # Fallback: legacy behaviour if user_paths import fails during an
    # unusual dev setup.  Should never happen in a packaged build.
    print(f"[config] user_paths unavailable ({_e}); falling back to install dir", flush=True)
    _ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

load_dotenv(_ENV_PATH)


_PRESERVE_PROCESS_ENV_FLAG = "_CATALYST_PRESERVE_PROCESS_ENV"
_PRESERVED_PROCESS_ENV_KEYS = {
    "WALLET_TYPE",
    "SAGE_RPC_URL",
    "SAGE_CERT_PATH",
    "SAGE_KEY_PATH",
    "SAGE_DATA_DIR",
}


def _capture_preserved_process_env() -> dict:
    if os.getenv(_PRESERVE_PROCESS_ENV_FLAG) != "1":
        return {}
    return {
        key: os.environ[key]
        for key in _PRESERVED_PROCESS_ENV_KEYS
        if key in os.environ
    }


def _restore_preserved_process_env(values: dict) -> None:
    for key, value in values.items():
        os.environ[key] = value


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
        # RLock so update() can hold the lock across set_key() + reload(),
        # and reload() can still re-acquire from the same thread.
        self._lock = threading.RLock()
        self.reload()

    def reload(self):
        """Re-read all settings from .env file (thread-safe)."""
        lock = getattr(self, '_lock', None)
        if lock:
            lock.acquire()
        try:
            self._reload_inner()
            # Re-apply user-local secrets under the same lock so other threads
            # never see partially-applied config (reloaded from .env but secrets
            # not yet applied).
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
        finally:
            if lock:
                lock.release()

    def _reload_inner(self):
        """Internal reload — called under lock."""
        # Force re-read of .env
        _preserved_env = _capture_preserved_process_env()
        load_dotenv(_ENV_PATH, override=True)
        _restore_preserved_process_env(_preserved_env)

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
        self.SAGE_DATA_DIR = _str("SAGE_DATA_DIR")           # Optional custom Sage data root
        # SAGE_FINGERPRINT is read directly via os.getenv() in sage_node.py
        # (line 416). Kept in cfg for to_dict() exclusion list completeness.
        self.SAGE_FINGERPRINT = _str("SAGE_FINGERPRINT")      # Auto-login fingerprint
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
        # LIQUIDITY_MODE is the source of truth for single vs two-sided mode.
        # Valid values: "two_sided", "buy_only", "sell_only". ENABLE_BUY /
        # ENABLE_SELL below are derived from it for backward compatibility —
        # older code paths read the ENABLE_* flags directly and we don't
        # want to chase every call site. New code should prefer
        # ``cfg.LIQUIDITY_MODE`` / ``cfg.is_buy_enabled()`` /
        # ``cfg.is_sell_enabled()`` so the semantics are explicit.
        _liquidity_mode_raw = _str("LIQUIDITY_MODE", "two_sided").strip().lower()
        if _liquidity_mode_raw not in ("two_sided", "buy_only", "sell_only"):
            _liquidity_mode_raw = "two_sided"
        self.LIQUIDITY_MODE = _liquidity_mode_raw
        # Read the raw ENABLE flags, then override if LIQUIDITY_MODE pins
        # a single side. A misconfigured env (e.g. mode=buy_only with
        # ENABLE_BUY=False) always resolves in favour of LIQUIDITY_MODE
        # — the user picked the mode explicitly in the GUI.
        _raw_enable_buy = _bool("ENABLE_BUY", True)
        _raw_enable_sell = _bool("ENABLE_SELL", True)
        if self.LIQUIDITY_MODE == "buy_only":
            self.ENABLE_BUY = True
            self.ENABLE_SELL = False
        elif self.LIQUIDITY_MODE == "sell_only":
            self.ENABLE_BUY = False
            self.ENABLE_SELL = True
        else:  # two_sided
            self.ENABLE_BUY = _raw_enable_buy
            self.ENABLE_SELL = _raw_enable_sell
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

        # ----- Oracle Staleness Policy -----
        # Soft threshold: how long the Dexie/Tibet pair can be unavailable
        # before we alert the operator. Offers stay live until this point
        # on the last-known mid — beyond it, the operator is told.
        self.PRICE_STALE_ALERT_SECS = _int("PRICE_STALE_ALERT_SECS", 60)
        # Hard threshold: once the outage reaches this age, the bot trips
        # the price circuit breaker, cancels every live offer, and stops
        # quoting until a price comes back. Prevents the book from sitting
        # exposed on a stale mid while both oracles are down.
        self.PRICE_HARD_PAUSE_SECS = _int("PRICE_HARD_PAUSE_SECS", 120)

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

        # ----- Sweep Protection -----
        # Minimum fills in the same block to classify as a sweep (default 3).
        # On liquid pairs with many active offers (40+), two fills in the same
        # ~18-second Chia block are very often just two independent retail buyers
        # who happened to transact simultaneously — NOT an arb sweep.  Setting
        # this too low (2) causes false-positive protection events that pause
        # offer creation for 90 s.  Raise to 4+ for very active pairs; lower to
        # 2 for thin/illiquid pairs where any multi-fill is suspicious.
        self.SWEEP_MIN_FILLS = _int("SWEEP_MIN_FILLS", 3)
        # Seconds to pause requoting the swept side (direction known), default 90.
        self.SWEEP_PROTECTION_SECS = _int("SWEEP_PROTECTION_SECS", 90)
        # Seconds to pause both sides when sweep direction is unknown, default 30.
        self.SWEEP_PROTECTION_UNKNOWN_SECS = _int("SWEEP_PROTECTION_UNKNOWN_SECS", 30)
        # MAX_STEP_CHANGE_FRACTION: reject single-fetch jumps larger than
        # this fraction (e.g. 0.10 = 10%). Set to 0 to disable. Default
        # raised from 0 → 0.10 on 2026-04-08: with the dynamic band at ±50%
        # the step guard was the only line of defence against a corrupted
        # Tibet response inside the band, and leaving it disabled meant a
        # fat-finger price could trigger emergency requoting at the bad
        # value. 10% is wide enough to allow normal market gaps but blocks
        # garbage data; reduce in .env if your market is more volatile.
        self.MAX_STEP_CHANGE_FRACTION = _decimal("MAX_STEP_CHANGE_FRACTION", "0.10")

        # ----- Offer Management -----
        # GUI writes MAX_ACTIVE_BUY; old .env may have MAX_ACTIVE_BUY_OFFERS.
        # GUI key takes priority (it's what the user actually sets).
        # GUI writes MAX_ACTIVE_BUY; old .env may have MAX_ACTIVE_BUY_OFFERS.
        # Use _str() to distinguish "key not set" from "key set to 0".
        self.MAX_ACTIVE_BUY_OFFERS = _int("MAX_ACTIVE_BUY") if _str("MAX_ACTIVE_BUY") else _int("MAX_ACTIVE_BUY_OFFERS", 0)
        self.MAX_ACTIVE_SELL_OFFERS = _int("MAX_ACTIVE_SELL") if _str("MAX_ACTIVE_SELL") else _int("MAX_ACTIVE_SELL_OFFERS", 0)
        self.OFFER_EXPIRY_SECS = _int("OFFER_EXPIRY_SECS", 86400)  # 24 hours — safety net only
        self.OFFER_STAGGER_SECS = _int("OFFER_STAGGER_SECS", 10)  # Stagger to avoid mass-expiry
        self.OFFER_REFRESH_BEFORE = _int("OFFER_REFRESH_BEFORE", 1800)  # Refresh 30 min before expiry (settle time)
        self.FILL_PROTECT_SECS = _int("FILL_PROTECT_SECS", 180)

        # ----- Requoting -----
        self.AUTO_REQUOTE = _bool("AUTO_REQUOTE", True)
        # REQUOTE_BPS: minimum price drift (in bps) before a side gets
        # requoted. Acts as hysteresis on the offer_manager.should_requote
        # gate to prevent every-cycle churn from sub-bp Tibet ticks.
        # Default raised from 0 → 30 on 2026-04-08: 0 meant any drift at
        # all triggered a requote, which combined with the 90s loop and
        # 60s cooldown produced repeated requotes during stable markets.
        # 30 bps (~0.3%) leaves room for slow drift, while AMM_DRIFT_REQUOTE_BPS
        # (default 80) catches faster moves that need an emergency requote.
        self.REQUOTE_BPS = _decimal("REQUOTE_BPS", "30")
        self.REQUOTE_COOLDOWN_SECS = _int("REQUOTE_COOLDOWN_SECS", 60)
        self.REQUOTE_BATCH_SIZE = _int("REQUOTE_BATCH_SIZE", 5)
        self.REQUOTE_COIN_FREE_WAIT = _int("REQUOTE_COIN_FREE_WAIT", 5)

        # ----- Incremental Reaction Strategy (cycle budget) -----
        # Max on-chain actions per cycle. The bot processes the most
        # urgent offers first (inner tier, highest staleness) and defers
        # the rest to subsequent cycles. This prevents costly full-ladder
        # rebuilds from a single price move.
        self.CYCLE_MAX_CANCELS = _int("CYCLE_MAX_CANCELS", 6)
        # CYCLE_MAX_CREATES removed 2026-04-17 (F77) — was never read by any
        # trading code. The equivalent clamping happens via MAX_POSTS_PER_LOOP
        # and the per-side slot limits (MAX_ACTIVE_BUY/SELL_OFFERS).
        # Max expiry refreshes per cycle (prevents mass-refresh when many
        # offers approach expiry at the same time).
        self.CYCLE_MAX_EXPIRY_REFRESH = _int("CYCLE_MAX_EXPIRY_REFRESH", 4)
        # Per-tier drift thresholds (fraction, not bps) for graduated
        # requoting.  Only tiers whose drift exceeds their threshold are
        # requoted.  Uses REQUOTE_BPS and AMM_DRIFT_REQUOTE_BPS as the
        # first two defaults to stay consistent with existing behaviour.
        self.REQUOTE_DRIFT_INNER = _decimal("REQUOTE_DRIFT_INNER", "0.003")   # 30 bps
        self.REQUOTE_DRIFT_MID   = _decimal("REQUOTE_DRIFT_MID",   "0.008")   # 80 bps
        self.REQUOTE_DRIFT_FULL  = _decimal("REQUOTE_DRIFT_FULL",  "0.02")    # 200 bps
        self.REQUOTE_DRIFT_EMERGENCY = _decimal("REQUOTE_DRIFT_EMERGENCY", "0.05")  # 500 bps
        # TibetSwap confirmed-reserve shock protection. Trigger is a percent
        # move, not bps. 0 preserves auto mode: half MIN_EDGE_BPS, with a
        # 0.50% floor. Positive values give operators a direct threshold.
        self.TIBET_SHOCK_CANCEL_TRIGGER_PCT = _decimal("TIBET_SHOCK_CANCEL_TRIGGER_PCT", "0")
        self.TIBET_SHOCK_CANCEL_MID_PCT = _decimal("TIBET_SHOCK_CANCEL_MID_PCT", "5")
        self.TIBET_SHOCK_CANCEL_OUTER_PCT = _decimal("TIBET_SHOCK_CANCEL_OUTER_PCT", "10")

        # ----- Reserves -----
        #
        # Two-tier reserve system (F49, 2026-04-09):
        #
        # (1) USER RESERVE — XCH_RESERVE / CAT_RESERVE
        #     The "do not touch" floor set by the user in step 1 of
        #     settings. The bot will refuse any coin split that would
        #     drop the wallet below this amount. Acts as a hard safety
        #     margin the user keeps no matter what.
        #
        # (2) TOPUP POOL — TOPUP_POOL_XCH / TOPUP_POOL_CAT
        #     Working budget set by Smart Settings as a fraction of the
        #     balance *after* the user reserve is subtracted. Default
        #     10% of _avail_xch. This is what the topup worker is
        #     allowed to split into new tier coins across a session.
        #     Once the session spends more than this budget, topup
        #     refuses further splits until Smart Settings re-allocates.
        #
        # Historical note: the coin classifier in coin_manager.py also
        # has a bucket called "reserve" meaning "any coin large enough
        # to split" — that is NOT a reserve in the user-facing sense,
        # it's topup fuel. The user-facing "reserve" is always
        # XCH_RESERVE / CAT_RESERVE. See coin_manager._classify_coins.
        self.XCH_RESERVE = _decimal("XCH_RESERVE", "0.03")
        # Support both new and legacy names
        self.CAT_RESERVE = _decimal("CAT_RESERVE") if _str("CAT_RESERVE") else _decimal("MZ_RESERVE", "0")

        # Topup pool — percentage of (spendable - reserve) that Smart
        # Settings allocates to the coin-splitting budget. Operators can
        # override via .env; Smart Settings respects the current value.
        # F60 (2026-04-09): raised default from 0.10 → 0.15. F57c tier
        # sizes are ~38% larger, so a 10% pool only lasted ~1 day of
        # fills before Smart Settings needed a manual refresh. 15% gives
        # ~2 days of autonomous runtime while still leaving ~80% of
        # avail capital in the trading ladder.
        self.TOPUP_POOL_PCT = _decimal("TOPUP_POOL_PCT", "0.15")
        # Absolute topup budgets — written by Smart Settings when it
        # allocates the pool. A value of 0 means "no explicit budget,
        # fall back to hard-reserve guard only."
        self.TOPUP_POOL_XCH = _decimal("TOPUP_POOL_XCH", "0")
        self.TOPUP_POOL_CAT = _decimal("TOPUP_POOL_CAT", "0")

        # F51 (2026-04-09): cancel-all poll & retry tuning.
        # The verification loop after cancel_offers_batch polls Sage for
        # on-chain confirmation of submitted cancels.
        # CANCEL_POLL_INTERVAL_SECS + CANCEL_MAX_WAIT_SECS cover the first
        # polling window. CANCEL_RETRY_WAIT_SECS was removed 2026-04-17
        # (F77) — never read by any verification code.
        self.CANCEL_POLL_INTERVAL_SECS = _int("CANCEL_POLL_INTERVAL_SECS", 10)
        self.CANCEL_MAX_WAIT_SECS = _int("CANCEL_MAX_WAIT_SECS", 90)

        # ----- Coin Preparation -----
        self.ENABLE_COIN_PREP = _bool("ENABLE_COIN_PREP", False)
        # COIN_PREP_COOLDOWN_SECS removed 2026-04-17 (F77) — never read by
        # the coin-prep worker. The drip-topup worker has its own cooldown
        # via TOPUP_POOL_PCT rate limiting.
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
        # Auto-claim DBX liquidity rewards: set the `claim_rewards` flag
        # when posting an offer so Dexie pays out rewards to our maker
        # address automatically — batched once daily to keep the spend
        # bundle from spawning excessive coin dust (per dexie's API docs).
        # The manual /v1/rewards/claim path pays every ~15 min, but the
        # auto-claim flag is a different code path with a different cadence.
        # Flag is a no-op for non-incentivized pairs, so always-on is safe.
        self.DEXIE_AUTO_CLAIM_REWARDS = _bool("DEXIE_AUTO_CLAIM_REWARDS", True)
        try:
            self.DEXIE_POST_RETRY_SLEEP = float(_str("DEXIE_POST_RETRY_SLEEP", "1.5"))
        except (ValueError, TypeError):
            self.DEXIE_POST_RETRY_SLEEP = 1.5
        self.MAX_POSTS_PER_LOOP = _int("MAX_POSTS_PER_LOOP", 30)
        self.BOT_TAG = _str("BOT_TAG", "CAT_MM_BOT")

        # ----- TibetSwap -----
        self.TIBET_API_BASE = _safe_url("TIBET_API_BASE", "https://api.v2.tibetswap.io")
        self.TIBET_TIMEOUT = _int("TIBET_TIMEOUT", 10)

        # ----- AMM Monitor (Fill Intelligence Tier 1) -----
        # Set TIBET_PAIR_ID to the pair ID for your token on TibetSwap v2.
        # Find it at https://v2.tibetswap.io — the pair ID is in the URL.
        self.TIBET_PAIR_ID = _str("TIBET_PAIR_ID", "")
        # F80 (2026-04-18): default lowered from 30 → 10. At 30s a TibetSwap
        # AMM trade could move the pool meaningfully and our exposed offers
        # got arbed for ~30s before the bot noticed (verified live on
        # 2026-04-18 user-test sell — bot took ~60s to detect a 9.8% pool
        # move and 2 large extreme-position buys filled in the meantime).
        # 10s reduces the worst-case lag to one Chia block window.
        self.AMM_POLL_INTERVAL_SECS = _int("AMM_POLL_INTERVAL_SECS", 10)
        # AMM_DRIFT_REQUOTE_BPS: if AMM price moves this far from our last
        # quoted mid-price, invalidate the Tibet cache to force a fresh requote.
        # Default raised from 40 → 80 on 2026-04-07: 40 was too tight for
        # stable markets and triggered ~22 forced requotes in 12 minutes
        # during the cascade. 80 bps (0.8%) lets minor AMM ticks ride out
        # without invoking the requote machinery. The cooldown gate
        # (REQUOTE_COOLDOWN_SECS) also rate-limits this trigger.
        self.AMM_DRIFT_REQUOTE_BPS = _decimal("AMM_DRIFT_REQUOTE_BPS", "80")
        # ENABLE_AMM_BUFFER: when True, offers within AMM_BUFFER_BPS of the
        # AMM price are skipped — they would be instantly arbed by TibetSwap.
        self.ENABLE_AMM_BUFFER = _bool("ENABLE_AMM_BUFFER", False)
        self.AMM_BUFFER_BPS = _decimal("AMM_BUFFER_BPS", "30")
        # Mempool watching: poll Coinset for pending spends on our offer coins.
        # Gives ~30-50s earlier fill detection before block confirmation.
        self.ENABLE_MEMPOOL_WATCH = _bool("ENABLE_MEMPOOL_WATCH", False)
        # MEMPOOL_POLL_INTERVAL_SECS removed 2026-04-17 (F77) — mempool_watcher
        # uses a hardcoded 5-second interval (see mempool_watcher.py:94).
        # If tuning is ever needed, restore here and read via cfg.
        # Known arb bot puzzle hashes (hex, no 0x prefix, comma-separated).
        # Used to classify fills as arb sweeps vs retail/combined.
        self.KNOWN_ARB_PUZZLE_HASHES = [
            h.strip().lower().removeprefix("0x")
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
        # Default flipped from False → True on 2026-04-08: with this off,
        # volatility scaling and pool-depth adjustment of spreads are inert
        # and the bot quotes a flat BASE_SPREAD_BPS regardless of market
        # conditions. The dashboard markets the bot as having "adaptive"
        # spreads — that is only true when this is on.
        self.DYNAMIC_SPREAD_ENABLED = _bool("DYNAMIC_SPREAD_ENABLED", True)
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
        # F62 (2026-04-09): PER-SIDE tier sizes. These are POSITION-semantic
        # (BUY_INNER_SIZE_XCH is the XCH committed per offer at the buy side's
        # position inner, i.e. tightest to mid). Prior to F62 all size fields
        # were shared and the bot used `BUY_LADDER_REVERSED` to flip the
        # lookup at read time — which kept buy and sell symmetric in XCH
        # terms and left huge amounts of capital idle under reverse-buy.
        # With per-side sizes, Smart Settings produces two independent
        # capital plans and writes them directly, so each side can fully
        # consume its own balance minus reserve. Fall back to the shared
        # legacy keys when per-side values are zero (upgrade path).
        #
        # ⚠ F77 audit note (2026-04-17): the per-side size values are
        # WRITTEN by Smart Settings and LOADED by the GUI but NOT actually
        # consumed by the trading code path — the ladder planner and offer
        # manager still read INNER_SIZE_XCH etc. (unified). This means
        # editing a per-side value in the GUI has no runtime effect.
        # F62 is incomplete; completing it requires threading the
        # per-side resolution (see get_buy_tier_size_xch /
        # get_sell_tier_size_xch helpers below) through ladder_planner
        # and offer_manager's coin-selection path. Left in config for
        # forward compatibility; Smart Settings still writes them so a
        # future wire-up can light them up without schema migration.
        self.BUY_INNER_SIZE_XCH = _decimal("BUY_INNER_SIZE_XCH", "0")
        self.BUY_MID_SIZE_XCH = _decimal("BUY_MID_SIZE_XCH", "0")
        self.BUY_OUTER_SIZE_XCH = _decimal("BUY_OUTER_SIZE_XCH", "0")
        self.BUY_EXTREME_SIZE_XCH = _decimal("BUY_EXTREME_SIZE_XCH", "0")
        self.SELL_INNER_SIZE_XCH = _decimal("SELL_INNER_SIZE_XCH", "0")
        self.SELL_MID_SIZE_XCH = _decimal("SELL_MID_SIZE_XCH", "0")
        self.SELL_OUTER_SIZE_XCH = _decimal("SELL_OUTER_SIZE_XCH", "0")
        self.SELL_EXTREME_SIZE_XCH = _decimal("SELL_EXTREME_SIZE_XCH", "0")
        self.INNER_TIER_COUNT = _int("INNER_TIER_COUNT", 0)
        self.MID_TIER_COUNT = _int("MID_TIER_COUNT", 0)
        self.OUTER_TIER_COUNT = _int("OUTER_TIER_COUNT", 0)
        self.EXTREME_TIER_COUNT = _int("EXTREME_TIER_COUNT", 0)
        # Per-side live tier counts (V4): BUY_* shapes the buy ladder (XCH-funded),
        # SELL_* shapes the sell ladder (CAT-funded). Fall back to the legacy
        # single-shared keys above when per-side keys are not set, so existing
        # configs keep working unchanged.
        self.BUY_INNER_TIER_COUNT = _int("BUY_INNER_TIER_COUNT", str(self.INNER_TIER_COUNT))
        self.BUY_MID_TIER_COUNT = _int("BUY_MID_TIER_COUNT", str(self.MID_TIER_COUNT))
        self.BUY_OUTER_TIER_COUNT = _int("BUY_OUTER_TIER_COUNT", str(self.OUTER_TIER_COUNT))
        self.BUY_EXTREME_TIER_COUNT = _int("BUY_EXTREME_TIER_COUNT", str(self.EXTREME_TIER_COUNT))
        self.SELL_INNER_TIER_COUNT = _int("SELL_INNER_TIER_COUNT", str(self.INNER_TIER_COUNT))
        self.SELL_MID_TIER_COUNT = _int("SELL_MID_TIER_COUNT", str(self.MID_TIER_COUNT))
        self.SELL_OUTER_TIER_COUNT = _int("SELL_OUTER_TIER_COUNT", str(self.OUTER_TIER_COUNT))
        self.SELL_EXTREME_TIER_COUNT = _int("SELL_EXTREME_TIER_COUNT", str(self.EXTREME_TIER_COUNT))
        self.INNER_TIER_SPARE_COUNT = _int("INNER_TIER_SPARE_COUNT", 0)
        self.MID_TIER_SPARE_COUNT = _int("MID_TIER_SPARE_COUNT", 0)
        self.OUTER_TIER_SPARE_COUNT = _int("OUTER_TIER_SPARE_COUNT", 0)
        self.EXTREME_TIER_SPARE_COUNT = _int("EXTREME_TIER_SPARE_COUNT", 0)
        # Per-side spare counts (V4): BUY_* governs XCH (used to fund buy offers),
        # SELL_* governs CAT (used to fund sell offers). When the legacy single-shared
        # keys above are non-zero and per-side keys are not set, fall back to legacy.
        self.BUY_INNER_TIER_SPARE_COUNT = _int("BUY_INNER_TIER_SPARE_COUNT", str(self.INNER_TIER_SPARE_COUNT))
        self.BUY_MID_TIER_SPARE_COUNT = _int("BUY_MID_TIER_SPARE_COUNT", str(self.MID_TIER_SPARE_COUNT))
        self.BUY_OUTER_TIER_SPARE_COUNT = _int("BUY_OUTER_TIER_SPARE_COUNT", str(self.OUTER_TIER_SPARE_COUNT))
        self.BUY_EXTREME_TIER_SPARE_COUNT = _int("BUY_EXTREME_TIER_SPARE_COUNT", str(self.EXTREME_TIER_SPARE_COUNT))
        self.SELL_INNER_TIER_SPARE_COUNT = _int("SELL_INNER_TIER_SPARE_COUNT", str(self.INNER_TIER_SPARE_COUNT))
        self.SELL_MID_TIER_SPARE_COUNT = _int("SELL_MID_TIER_SPARE_COUNT", str(self.MID_TIER_SPARE_COUNT))
        self.SELL_OUTER_TIER_SPARE_COUNT = _int("SELL_OUTER_TIER_SPARE_COUNT", str(self.OUTER_TIER_SPARE_COUNT))
        self.SELL_EXTREME_TIER_SPARE_COUNT = _int("SELL_EXTREME_TIER_SPARE_COUNT", str(self.EXTREME_TIER_SPARE_COUNT))

        # ----- Coin Prep -----
        self.COIN_PREP_MULTIPLIER = _decimal("COIN_PREP_MULTIPLIER", "1.0")
        self.COIN_PREP_HEADROOM_PCT = _decimal("COIN_PREP_HEADROOM_PCT", "10")
        # Maximum ratio of coin size to offer size in exact_tier_spend_mode.
        # Prevents a 5 XCH coin being used for a 0.634 XCH offer (87% locked
        # as change, causing cascading wrong-size cycles). When no coin fits
        # within [offer_size, offer_size × ratio], the slot fails cleanly and
        # triggers topup to split the reserve into right-sized coins.
        # Default 1.5 = coin may be at most 50% larger than the offer amount.
        # Set to 0 to disable (restores old fallback behaviour).
        self.COIN_MAX_SIZE_RATIO = float(os.getenv("COIN_MAX_SIZE_RATIO", "1.5"))
        # Same-tier emergency slack: if no exact-tier coin fits the normal cap,
        # the selector may use a moderately oversized coin up to this ratio.
        # The reclaim pass uses the same ceiling so it does not immediately
        # cancel offers the selector intentionally allowed.
        self.COIN_OVERSIZE_FALLBACK_RATIO = float(os.getenv("COIN_OVERSIZE_FALLBACK_RATIO", "2.0"))
        default_fee_mode = "manual" if self.WALLET_TYPE == "sage" else "auto"
        self.TRANSACTION_FEE_MODE = _str("TRANSACTION_FEE_MODE", default_fee_mode)
        self.TRANSACTION_FEE_XCH = _decimal("TRANSACTION_FEE_XCH", "0")
        self.TRANSACTION_FEE_TARGET_SECS = _int("TRANSACTION_FEE_TARGET_SECS", 300)
        self.TRANSACTION_FEE_ESTIMATE_COST = _int("TRANSACTION_FEE_ESTIMATE_COST", 20_000_000)
        self.FEE_PREP_COUNT = _int("FEE_PREP_COUNT", 20)
        self.FEE_COIN_SIZE_XCH = _decimal("FEE_COIN_SIZE_XCH", "0.0001")
        self.LADDER_CREATE_PARALLELISM = _int("LADDER_CREATE_PARALLELISM", 5)
        self.LADDER_CREATE_DELAY_MS = _int("LADDER_CREATE_DELAY_MS", 200)
        self.LADDER_CREATE_GLOBAL_SERIAL = _bool("LADDER_CREATE_GLOBAL_SERIAL", False)

        # ----- Adaptive Coin Management (V3 — designation-based pools) -----
        # Topup triggers: % of spares remaining before replenishment starts
        self.TOPUP_SLOW_PCT = _int("TOPUP_SLOW_PCT", 20)        # Trigger at 20% spares on slow days
        self.TOPUP_NORMAL_PCT = _int("TOPUP_NORMAL_PCT", 30)     # Trigger at 30% spares normally
        self.TOPUP_BUSY_PCT = _int("TOPUP_BUSY_PCT", 50)         # Trigger at 50% spares on busy days
        # Per-tier topup trigger percentages (V4). Overrides the global pace
        # threshold above on a per-tier basis. The "starting pool size" for
        # each tier comes from the live spare-count settings (source of truth),
        # and the trigger fires when that tier's free spares drop below
        # (pool_size * pct / 100). Inner tier has the biggest pool and gets
        # hit most often, so it triggers earliest. Extreme tier has the
        # smallest pool and is hit least often, so it tolerates the deepest
        # drawdown before triggering prep. Percentages can still be scaled
        # by the pace factor (busy/normal/slow) via TIER_TRIGGER_PACE_SCALE.
        self.TIER_TRIGGER_PCT_INNER = _int("TIER_TRIGGER_PCT_INNER", 50)
        self.TIER_TRIGGER_PCT_MID = _int("TIER_TRIGGER_PCT_MID", 40)
        self.TIER_TRIGGER_PCT_OUTER = _int("TIER_TRIGGER_PCT_OUTER", 25)
        self.TIER_TRIGGER_PCT_EXTREME = _int("TIER_TRIGGER_PCT_EXTREME", 15)
        self.TIER_TRIGGER_PCT_SNIPER = _int("TIER_TRIGGER_PCT_SNIPER", 40)
        self.TIER_TRIGGER_PCT_FEES = _int("TIER_TRIGGER_PCT_FEES", 30)
        # If True, tier percentages are scaled by the pace factor
        # (busy=1.4x, normal=1.0x, slow=0.7x). If False, pace is ignored and
        # the per-tier base percentages are used exactly as configured.
        self.TIER_TRIGGER_PACE_SCALE = _bool("TIER_TRIGGER_PACE_SCALE", True)
        # Proactive refill target used by the drip topup path. 100 means any
        # visible SPARE_BUFFER_LOW tier is eligible for gradual replenishment;
        # the harder TIER_TRIGGER_PCT_* values still govern emergency topups.
        self.TIER_DRIP_PCT = _int("TIER_DRIP_PCT", 100)
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
        # Iterative floor tightening: after a probe is confirmed, fire tighter
        # probes each cycle until one gets taken. That locates the true AMM-arb
        # floor empirically instead of stopping at the first comfortable buffer.
        self.SNIPER_FLOOR_TIGHTEN_ENABLED = _bool("SNIPER_FLOOR_TIGHTEN_ENABLED", True)
        self.SNIPER_FLOOR_TIGHTEN_STEP_BPS = _int("SNIPER_FLOOR_TIGHTEN_STEP_BPS", 15)  # Buffer reduction per successful round
        self.SNIPER_FLOOR_TIGHTEN_COOLDOWN_SECS = _int("SNIPER_FLOOR_TIGHTEN_COOLDOWN_SECS", 60)  # Wait between tightening rounds
        self.SNIPER_FLOOR_SAFETY_BPS = _int("SNIPER_FLOOR_SAFETY_BPS", 5)  # Stop tightening when within this many bps of TIBETSWAP_FEE_BPS

        # ----- Close the Gap (Dexie ranking improvement) -----
        # Probe size uses SNIPER_SIZE_XCH (same coin pool).
        # Probe expiry uses SNIPER_EXPIRY_SECS (same lifecycle).
        self.BOOST_SPREAD_BPS = _int("BOOST_SPREAD_BPS", 200)  # Fallback spread if no main book
        # Adaptive gap-closing strategy
        self.GAP_CLOSE_START_PCT = _int("GAP_CLOSE_START_PCT", 75)  # Start at 75% of main spread
        self.GAP_CLOSE_STEP_PCT = _int("GAP_CLOSE_STEP_PCT", 30)  # Tighten 30% per stable period (bigger jumps = faster floor discovery)
        self.GAP_CLOSE_SAFETY_BUFFER_BPS = _int("GAP_CLOSE_SAFETY_BUFFER_BPS", 5)  # Buffer above arb gap (tight — empirical test showed Dexie watchers take any +EV offer, so sub-probe needs room to actually push past the real arb threshold)

        # ----- Inverted-probe floor discovery (2026-04-25) -----
        # Empirical evidence: symmetric tight quotes (positive half-spread on
        # both sides) are NEVER arbed via TibetSwap because the math doesn't
        # work — taking a SELL at mid+X and dumping to TibetSwap (which pays
        # mid-fee) loses (X+fee) bps every time. To actually find the arb
        # floor, probes must INVERT past mid+/-tibet_fee where TibetSwap-
        # routed arbs become profitable.
        self.TIBETSWAP_FEE_BPS = _int("TIBETSWAP_FEE_BPS", 70)
        # Initial inverted probe offset, measured as bps PAST tibet_fee
        # (e.g. tibet_fee=70, initial=10 → start probe at mid+/-80bps).
        self.GAP_PROBE_INITIAL_PAST_FEE_BPS = _int("GAP_PROBE_INITIAL_PAST_FEE_BPS", 10)
        # How much deeper the probe pushes per cooldown cycle when surviving.
        self.GAP_PROBE_STEP_BPS = _int("GAP_PROBE_STEP_BPS", 30)
        # Hard cap on probe depth to prevent runaway losses if every probe
        # gets arbed (in busy markets). 500bps = 5% inversion is plenty.
        self.GAP_PROBE_MAX_PAST_FEE_BPS = _int("GAP_PROBE_MAX_PAST_FEE_BPS", 500)
        # Safety buffer subtracted from the proven floor when handing off.
        # Proven floor is the deepest survival point; ladder plants at
        # (proven_floor - safety_buffer) — i.e. one step safer than where
        # we got arbed.
        self.GAP_PROBE_HANDOFF_BUFFER_BPS = _int("GAP_PROBE_HANDOFF_BUFFER_BPS", 20)
        # Inverted-mode cascade after both sides settle: plant N new tight
        # inner-tier offers per side at half-spread Y, cancel N furthest.
        self.GAP_PROBE_CASCADE_COUNT_PER_SIDE = _int("GAP_PROBE_CASCADE_COUNT_PER_SIDE", 2)
        self.GAP_PROBE_CASCADE_HALF_SPREAD_BPS = _int("GAP_PROBE_CASCADE_HALF_SPREAD_BPS", 50)
        self.GAP_CLOSE_STEP_COOLDOWN_SECS = _int("GAP_CLOSE_STEP_COOLDOWN_SECS", 60)  # 1 min between steps
        self.GAP_CLOSE_CONVERGENCE_SECS = _int("GAP_CLOSE_CONVERGENCE_SECS", 120)  # 2 min between main book convergence steps
        self.GAP_CLOSE_CONVERGENCE_STEP_PCT = _int("GAP_CLOSE_CONVERGENCE_STEP_PCT", 20)  # Main book tightens 20% per step
        self.GAP_CLOSE_CASCADE_WAIT_SECS = _int("GAP_CLOSE_CASCADE_WAIT_SECS", 60)  # Wait 60s before cascading main book behind probe
        self.GAP_CLOSE_CASCADE_BATCH_SIZE = _int("GAP_CLOSE_CASCADE_BATCH_SIZE", 5)  # How many offers to replace per cascade batch

        # ----- Market Intelligence (V2 — ecosystem upgrades) -----
        self.COMPETITOR_AWARE_ENABLED = _bool("COMPETITOR_AWARE_ENABLED", False)
        self.DBX_MAX_SPREAD_BPS = _decimal("DBX_MAX_SPREAD_BPS", "500")

        # ----- Splash Network (V3 — decentralized offer broadcasting) -----
        self.SPLASH_ENABLED = _bool("SPLASH_ENABLED", False)
        self.SPLASH_SUBMIT_URL = _str("SPLASH_SUBMIT_URL", "http://localhost:4000")
        self.SPLASH_POST_RETRIES = _int("SPLASH_POST_RETRIES", 2)
        self.SPLASH_POST_TIMEOUT = _int("SPLASH_POST_TIMEOUT", 15)
        try:
            self.SPLASH_POST_RETRY_SLEEP = float(_str("SPLASH_POST_RETRY_SLEEP", "1.5"))
        except (ValueError, TypeError):
            self.SPLASH_POST_RETRY_SLEEP = 1.5
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

        # ----- Local Chia full-node RPC (optional, 2026-04-22) -----
        # When a local full-node is configured, the mempool watcher polls it
        # directly instead of Coinset's indexed snapshot. Eliminates the
        # third-party indexer lag that caused 0/13 mempool hits on same-block
        # sweeps during testing. Leave blank to keep the Coinset path active.
        # Typical values:
        #   FULL_NODE_RPC_URL=https://127.0.0.1:8555
        #   FULL_NODE_CERT_PATH=~/.chia/mainnet/config/ssl/full_node/private_full_node.crt
        #   FULL_NODE_KEY_PATH=~/.chia/mainnet/config/ssl/full_node/private_full_node.key
        self.FULL_NODE_RPC_URL = _safe_url("FULL_NODE_RPC_URL", "")
        self.FULL_NODE_CERT_PATH = _str("FULL_NODE_CERT_PATH", "")
        self.FULL_NODE_KEY_PATH = _str("FULL_NODE_KEY_PATH", "")
        self.FULL_NODE_TIMEOUT = _int("FULL_NODE_TIMEOUT", 5)
        self.FULL_NODE_ENABLED = _bool(
            "FULL_NODE_ENABLED",
            "True" if self.FULL_NODE_RPC_URL else "False",
        )

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
        "DRY_RUN", "ENABLE_BUY", "ENABLE_SELL", "LIQUIDITY_MODE", "LOOP_SECONDS",
        "MIN_TRADE_XCH", "MAX_TRADE_XCH", "DEFAULT_TRADE_XCH",
        # Spread & pricing
        "SPREAD_BPS", "MIN_EDGE_BPS", "PRICE_STRATEGY", "TIBET_WEIGHT",
        "ARB_ALERT_THRESHOLD_BPS",
        # Price safety
        "DYNAMIC_LIMIT_PCT", "HARD_MIN_PRICE_XCH", "HARD_MAX_PRICE_XCH",
        # MIN_MID / MAX_MID kept for Smart Settings clear-only path —
        # they're legacy fallbacks for HARD_MIN/MAX_PRICE_XCH (see lines
        # 217-220) and Smart Settings explicitly nulls them so the new
        # rails take precedence. MAX_MID_MOVE_BPS removed 2026-04-08:
        # the trading code never consumed it.
        "MAX_STEP_CHANGE_FRACTION", "MIN_MID", "MAX_MID",
        # Offer management
        "MAX_ACTIVE_BUY", "MAX_ACTIVE_SELL",
        "MAX_ACTIVE_BUY_OFFERS", "MAX_ACTIVE_SELL_OFFERS",
        "OFFER_EXPIRY_SECS", "OFFER_STAGGER_SECS",
        "OFFER_REFRESH_BEFORE", "FILL_PROTECT_SECS",
        # Requoting
        "AUTO_REQUOTE", "REQUOTE_BPS", "REQUOTE_COOLDOWN_SECS",
        "REQUOTE_BATCH_SIZE", "REQUOTE_COIN_FREE_WAIT",
        "TIBET_SHOCK_CANCEL_TRIGGER_PCT",
        "TIBET_SHOCK_CANCEL_MID_PCT", "TIBET_SHOCK_CANCEL_OUTER_PCT",
        # Reserves + topup pool (F49)
        "XCH_RESERVE", "CAT_RESERVE", "MZ_RESERVE",
        "TOPUP_POOL_PCT", "TOPUP_POOL_XCH", "TOPUP_POOL_CAT",
        # Cancel poll tuning (F51). CANCEL_RETRY_WAIT_SECS removed F77.
        "CANCEL_POLL_INTERVAL_SECS", "CANCEL_MAX_WAIT_SECS",
        # Coin prep. COIN_PREP_COOLDOWN_SECS removed F77 (never consumed).
        "ENABLE_COIN_PREP",
        "XCH_TARGET_COINS", "XCH_COIN_SIZE",
        "CAT_TARGET_COINS", "CAT_COIN_SIZE",
        "COIN_PREP_MULTIPLIER", "COIN_PREP_HEADROOM_PCT", "COIN_MAX_SIZE_RATIO",
        "COIN_OVERSIZE_FALLBACK_RATIO",
        "TRANSACTION_FEE_MODE", "TRANSACTION_FEE_XCH",
        "TRANSACTION_FEE_TARGET_SECS", "TRANSACTION_FEE_ESTIMATE_COST",
        "FEE_PREP_COUNT", "FEE_COIN_SIZE_XCH",
        "LADDER_CREATE_PARALLELISM", "LADDER_CREATE_DELAY_MS",
        "LADDER_CREATE_GLOBAL_SERIAL",
        # Dexie
        "DEXIE_AUTO_POST", "DEXIE_POST_ENABLED",
        "DEXIE_POST_TIMEOUT", "DEXIE_POST_RETRIES",
        "DEXIE_AUTO_CLAIM_REWARDS",
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
        # F62 (2026-04-09): per-side tier sizes so buy and sell ladders
        # can be sized independently from their own balances.
        "BUY_INNER_SIZE_XCH", "BUY_MID_SIZE_XCH",
        "BUY_OUTER_SIZE_XCH", "BUY_EXTREME_SIZE_XCH",
        "SELL_INNER_SIZE_XCH", "SELL_MID_SIZE_XCH",
        "SELL_OUTER_SIZE_XCH", "SELL_EXTREME_SIZE_XCH",
        "INNER_TIER_COUNT", "MID_TIER_COUNT",
        "OUTER_TIER_COUNT", "EXTREME_TIER_COUNT",
        "BUY_INNER_TIER_COUNT", "BUY_MID_TIER_COUNT",
        "BUY_OUTER_TIER_COUNT", "BUY_EXTREME_TIER_COUNT",
        "SELL_INNER_TIER_COUNT", "SELL_MID_TIER_COUNT",
        "SELL_OUTER_TIER_COUNT", "SELL_EXTREME_TIER_COUNT",
        "INNER_TIER_SPARE_COUNT", "MID_TIER_SPARE_COUNT",
        "OUTER_TIER_SPARE_COUNT", "EXTREME_TIER_SPARE_COUNT",
        "BUY_INNER_TIER_SPARE_COUNT", "BUY_MID_TIER_SPARE_COUNT",
        "BUY_OUTER_TIER_SPARE_COUNT", "BUY_EXTREME_TIER_SPARE_COUNT",
        "SELL_INNER_TIER_SPARE_COUNT", "SELL_MID_TIER_SPARE_COUNT",
        "SELL_OUTER_TIER_SPARE_COUNT", "SELL_EXTREME_TIER_SPARE_COUNT",
        # Adaptive coin management
        "TOPUP_SLOW_PCT", "TOPUP_NORMAL_PCT", "TOPUP_BUSY_PCT",
        "TIER_TRIGGER_PCT_INNER", "TIER_TRIGGER_PCT_MID",
        "TIER_TRIGGER_PCT_OUTER", "TIER_TRIGGER_PCT_EXTREME",
        "TIER_TRIGGER_PCT_SNIPER", "TIER_TRIGGER_PCT_FEES",
        "TIER_TRIGGER_PACE_SCALE", "TIER_DRIP_PCT",
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
        "SNIPER_FLOOR_TIGHTEN_ENABLED", "SNIPER_FLOOR_TIGHTEN_STEP_BPS",
        "SNIPER_FLOOR_TIGHTEN_COOLDOWN_SECS", "SNIPER_FLOOR_SAFETY_BPS",
        # Close the Gap / Boost
        "BOOST_SIZE_XCH", "BOOST_EXPIRY_SECS", "BOOST_SPREAD_BPS",
        "GAP_CLOSE_START_PCT", "GAP_CLOSE_STEP_PCT",
        "GAP_CLOSE_SAFETY_BUFFER_BPS", "GAP_CLOSE_STEP_COOLDOWN_SECS",
        "GAP_CLOSE_CONVERGENCE_SECS", "GAP_CLOSE_CONVERGENCE_STEP_PCT",
        "GAP_CLOSE_CASCADE_WAIT_SECS", "GAP_CLOSE_CASCADE_BATCH_SIZE",
        # Market intel
        "COMPETITOR_AWARE_ENABLED",
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
        # Local Chia full-node RPC (optional zero-lag mempool source).
        # URL + cert/key paths are updatable from the GUI so the operator
        # can point the watcher at their own node without editing .env.
        "FULL_NODE_ENABLED", "FULL_NODE_RPC_URL",
        "FULL_NODE_CERT_PATH", "FULL_NODE_KEY_PATH",
        "FULL_NODE_TIMEOUT",
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

    def update(self, key: str, value: str,
               source: str = "api", note: str = "") -> bool:
        """Update a setting: writes to .env and refreshes in-memory value.

        Only keys in _UPDATABLE_KEYS can be modified via the API.
        Credentials, wallet URLs, and cert paths are blocked.

        F26 (2026-04-08): added `source` and `note` parameters that
        propagate to the config_history audit table. Callers should
        pass meaningful sources like "gui_live_control",
        "smart_settings", "api_settings_save", "smart_defaults_apply".

        Args:
            key: The setting name (e.g., 'SPREAD_BPS')
            value: The new value as a string
            source: Where the change came from (audit trail)
            note: Optional human-readable note

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
            # Hold the lock across the set_key → reload pair so two
            # concurrent update() calls can't race each other through
            # python-dotenv's non-atomic read-modify-write of .env.
            with self._lock:
                # Record old value for change tracking
                old_value = str(getattr(self, key, ""))

                # Write to .env file
                set_key(_ENV_PATH, key, value)

                # Reload all settings from disk (RLock re-entry is fine)
                self.reload()

                # Record the change (import here to avoid circular import)
                try:
                    from database import record_config_change
                    record_config_change(key, old_value, value, source=source, note=note)
                except ImportError:
                    pass  # Database not available yet during early startup

                # F49 (2026-04-09): when Smart Settings writes new
                # TOPUP_POOL_* values, reset the session spend counter
                # so the fresh allocation gets its full budget. Without
                # this, a new Smart Settings run would inherit stale
                # spend from the previous session and the topup worker
                # would refuse splits immediately.
                try:
                    if key in ("TOPUP_POOL_XCH", "TOPUP_POOL_CAT"):
                        from database import set_setting as _set_setting
                        spend_key = (
                            "topup_pool_cat_spent_mojos"
                            if key == "TOPUP_POOL_CAT"
                            else "topup_pool_xch_spent_mojos"
                        )
                        _set_setting(spend_key, "0")
                except Exception:
                    pass  # DB may not be ready yet

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

        # MAX_ACTIVE_BUY_OFFERS / MAX_ACTIVE_SELL_OFFERS
        # Smart Defaults spreads the ladder widely (up to ~60 per side on
        # busy markets), so the "very high" warning only fires above 150.
        for attr, label in [
            ("MAX_ACTIVE_BUY_OFFERS", "MAX_ACTIVE_BUY_OFFERS"),
            ("MAX_ACTIVE_SELL_OFFERS", "MAX_ACTIVE_SELL_OFFERS"),
        ]:
            val = getattr(self, attr, 0)
            if val <= 0:
                warnings.append(
                    f"{label}={val} — that side is disabled (no offers will be placed)"
                )
            elif val > 150:
                warnings.append(
                    f"{label}={val} is very high (>150) — large coin pools required, "
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

        shock_trigger = getattr(self, "TIBET_SHOCK_CANCEL_TRIGGER_PCT", Decimal("0"))
        if shock_trigger < Decimal("0"):
            errors.append(
                "TIBET_SHOCK_CANCEL_TRIGGER_PCT cannot be negative"
            )
        elif shock_trigger > Decimal("20"):
            warnings.append(
                "TIBET_SHOCK_CANCEL_TRIGGER_PCT is above 20% — defensive "
                "TibetSwap shock cancels may react too late"
            )

        # XCH_RESERVE: warn only on truly implausible values (typos). The old
        # >10 XCH threshold was pure noise for medium wallets — Smart Settings
        # routinely picks 15-25 XCH reserves when the user asks for a 20-25%
        # hard floor on a 50-100 XCH wallet.
        reserve = self.XCH_RESERVE
        if reserve < Decimal("0"):
            errors.append(
                f"XCH_RESERVE={reserve} is negative — invalid reserve value"
            )
        elif reserve > Decimal("100"):
            warnings.append(
                f"XCH_RESERVE={reserve} XCH is very large (>100) — verify this is intentional"
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
        else:
            # Reject anything that isn't pure hex — stray characters would
            # silently fail at RPC time with confusing errors.
            asset_id_lower = asset_id.lower()
            if any(c not in "0123456789abcdef" for c in asset_id_lower):
                errors.append(
                    f"CAT_ASSET_ID='{asset_id[:20]}...' contains non-hex characters — "
                    f"must be 64 hex digits only"
                )

        # HARD_MIN_PRICE_XCH vs HARD_MAX_PRICE_XCH cross-check
        try:
            hmin = Decimal(str(getattr(self, "HARD_MIN_PRICE_XCH", 0) or 0))
            hmax = Decimal(str(getattr(self, "HARD_MAX_PRICE_XCH", 0) or 0))
            if hmin > 0 and hmax > 0 and hmin >= hmax:
                errors.append(
                    f"HARD_MIN_PRICE_XCH ({hmin}) is >= HARD_MAX_PRICE_XCH ({hmax}) — "
                    f"all prices would be rejected; swap or adjust these values"
                )
        except (InvalidOperation, ValueError, TypeError):
            pass

        return {"warnings": warnings, "errors": errors}

    def get_spread_fraction(self) -> Decimal:
        """Convert SPREAD_BPS to a fraction (e.g., 800 BPS → 0.08)."""
        return self.SPREAD_BPS / Decimal("10000")

    def get_requote_fraction(self) -> Decimal:
        """Convert REQUOTE_BPS to a fraction."""
        return self.REQUOTE_BPS / Decimal("10000")

    def is_two_sided(self) -> bool:
        """True when the bot should quote BOTH buy and sell ladders."""
        return (getattr(self, "LIQUIDITY_MODE", "two_sided") == "two_sided"
                and bool(self.ENABLE_BUY) and bool(self.ENABLE_SELL))

    def is_single_sided(self) -> bool:
        """True when LIQUIDITY_MODE pins the bot to one side."""
        return getattr(self, "LIQUIDITY_MODE", "two_sided") in ("buy_only", "sell_only")

    def active_side(self) -> str:
        """Return ``"buy"``, ``"sell"``, or ``"both"`` based on the mode.

        New callers should prefer this over reading ENABLE_BUY / ENABLE_SELL
        directly — the naming is explicit about the single-sided invariant
        (exactly one side active) and the helper keeps the tri-state logic
        in one place.
        """
        mode = getattr(self, "LIQUIDITY_MODE", "two_sided")
        if mode == "buy_only":
            return "buy"
        if mode == "sell_only":
            return "sell"
        return "both"

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


# ---------------------------------------------------------------------------
# Per-side tier size resolution (F62 — 2026-04-09)
# ---------------------------------------------------------------------------
# Callers should go through these helpers instead of reading INNER_SIZE_XCH
# etc. directly. They encapsulate:
#   1. Prefer the per-side field (BUY_*_SIZE_XCH / SELL_*_SIZE_XCH) when set
#   2. Fall back to the shared legacy field (INNER_SIZE_XCH etc.)
#   3. When falling back on the BUY side under BUY_LADDER_REVERSED, apply
#      the reverse-buy flip so upgrading configs behave identically to
#      pre-F62 until Smart Settings re-runs and writes the new fields.
#
# These are module-level functions so helpers in risk_manager / coin_manager /
# offer_manager / api_server can share a single source of truth.

_TIER_NAMES = ("inner", "mid", "outer", "extreme")
_REVERSE_BUY_MAP = {
    "inner":   "extreme",
    "mid":     "outer",
    "outer":   "mid",
    "extreme": "inner",
}


def _legacy_tier_size(tier_name: str) -> Decimal:
    """Read the legacy single-shared tier size (INNER_SIZE_XCH etc.)."""
    attr = f"{tier_name.upper()}_SIZE_XCH"
    return Decimal(str(getattr(cfg, attr, 0) or 0))


def get_buy_tier_size_xch(tier_name: str) -> Decimal:
    """Return the XCH committed per buy offer at the given POSITION tier.

    POSITION-semantic. ``BUY_INNER_SIZE_XCH`` stores the size for the
    inner POSITION (the slot closest to mid). Under BUY_LADDER_REVERSED,
    Smart Defaults writes a SMALL value to ``BUY_INNER_SIZE_XCH`` because
    the inner position offers a small amount close to mid; the UI swap
    in ``handleReverseLadderToggle`` keeps that convention consistent.

    F79 (2026-04-18) added a flip that read ``BUY_EXTREME_SIZE_XCH`` when
    asked about the inner POSITION under reverse-buy, inverting Smart
    Defaults' write convention. That produced a ~2× inflation of the
    computed buy-side prep budget: 10 inner POSITION slots got paired
    with the EXTREME size (6.49 XCH) instead of the INNER size
    (0.902 XCH), which is why coin_prep_worker then rejected plans that
    Smart Defaults said fit the wallet. Revert the flip — storage is
    position-indexed end-to-end, so a direct field read is correct.

    Legacy fallback: when ``BUY_*_SIZE_XCH`` is unset (pre-F62 configs),
    fall back to the shared legacy ``<TIER>_SIZE_XCH`` field with the
    reverse-buy flip applied. Legacy config only stored sell-side-like
    (inner=biggest) values, so under reverse-buy the buy ladder needs
    the flip to get the inverted shape.
    """
    tier = (tier_name or "").strip().lower()
    if tier not in _TIER_NAMES:
        return Decimal("0")
    # Modern configs: BUY_*_SIZE_XCH is position-indexed — return directly.
    attr = f"BUY_{tier.upper()}_SIZE_XCH"
    val = Decimal(str(getattr(cfg, attr, 0) or 0))
    if val > 0:
        return val
    # Legacy fallback: shared tier field with the reverse-buy flip.
    if getattr(cfg, "BUY_LADDER_REVERSED", False):
        legacy_tier = _REVERSE_BUY_MAP[tier]
    else:
        legacy_tier = tier
    return _legacy_tier_size(legacy_tier)


def get_sell_tier_size_xch(tier_name: str) -> Decimal:
    """Return the XCH-equivalent committed per sell offer at the given position tier.

    Prefers SELL_<tier>_SIZE_XCH; falls back to the shared legacy size.
    Sell side is never flipped — reverse-buy only affects the buy ladder.
    """
    tier = (tier_name or "").strip().lower()
    if tier not in _TIER_NAMES:
        return Decimal("0")
    attr = f"SELL_{tier.upper()}_SIZE_XCH"
    val = Decimal(str(getattr(cfg, attr, 0) or 0))
    if val > 0:
        return val
    return _legacy_tier_size(tier)


def get_tier_sizes_for_side(side: str) -> dict:
    """Return {tier: size_xch} for the given side ("buy" or "sell")."""
    s = (side or "").strip().lower()
    if s == "buy":
        return {t: get_buy_tier_size_xch(t) for t in _TIER_NAMES}
    return {t: get_sell_tier_size_xch(t) for t in _TIER_NAMES}


def has_per_side_tier_sizes() -> bool:
    """True if any per-side size field is set (i.e. F62 layout in use)."""
    for t in _TIER_NAMES:
        if Decimal(str(getattr(cfg, f"BUY_{t.upper()}_SIZE_XCH", 0) or 0)) > 0:
            return True
        if Decimal(str(getattr(cfg, f"SELL_{t.upper()}_SIZE_XCH", 0) or 0)) > 0:
            return True
    return False
