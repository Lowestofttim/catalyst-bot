"""Structured validation of the loaded Config singleton

Runs a suite of checks against a populated `Config` object and returns a
`ValidationReport` containing `ConfigIssue` entries split into errors
(severity that should block startup or a reload) and warnings (log-only).
Consumed by `config` itself on reload, by `api_server` through the validate
endpoint, and by the `doctor` preflight to surface bad configuration before
the trading loop starts.

Key responsibilities:
    - Validate types, ranges, and URL formats for individual settings
    - Catch contradictory or mutually-exclusive combinations
    - Return a structured report rather than raising
    - Keep validation logic independent of I/O and framework code

The validator is pure: it reads from the passed-in config object and
produces a report — no logging, no file access, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """A single validation finding."""
    key: str
    message: str
    severity: str  # "error" or "warning"


@dataclass
class ValidationReport:
    """Result of config validation."""
    errors: List[ConfigIssue] = field(default_factory=list)
    warnings: List[ConfigIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "errors": [{"key": i.key, "message": i.message, "severity": i.severity} for i in self.errors],
            "warnings": [{"key": i.key, "message": i.message, "severity": i.severity} for i in self.warnings],
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
        }


def _is_valid_url(url: str) -> bool:
    """Check if a string is a valid http/https URL."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def validate_config(cfg) -> ValidationReport:
    """Validate the current config for dangerous or broken settings.

    Args:
        cfg: The Config instance (from config.py)

    Returns:
        ValidationReport with errors (block start) and warnings (log but allow).
    """
    report = ValidationReport()

    def err(key, msg):
        report.errors.append(ConfigIssue(key=key, message=msg, severity="error"))

    def warn(key, msg):
        report.warnings.append(ConfigIssue(key=key, message=msg, severity="warning"))

    # ---- CAT Identity ----
    cat_id = getattr(cfg, "CAT_ASSET_ID", "")
    if not cat_id:
        err("CAT_ASSET_ID", "CAT asset ID is empty — bot cannot identify which token to trade")

    cat_dec = getattr(cfg, "CAT_DECIMALS", 3)
    if cat_dec < 0 or cat_dec > 12:
        err("CAT_DECIMALS", f"CAT_DECIMALS={cat_dec} is outside valid range 0-12")

    # ---- Trading Direction ----
    enable_buy = getattr(cfg, "ENABLE_BUY", True)
    enable_sell = getattr(cfg, "ENABLE_SELL", True)
    if not enable_buy and not enable_sell:
        err("ENABLE_BUY/ENABLE_SELL", "Both buy and sell are disabled — bot has nothing to do")

    # ---- Trade Size Ranges ----
    min_trade = getattr(cfg, "MIN_TRADE_XCH", Decimal("0"))
    max_trade = getattr(cfg, "MAX_TRADE_XCH", Decimal("0"))
    default_trade = getattr(cfg, "DEFAULT_TRADE_XCH", Decimal("0"))

    if min_trade <= Decimal("0"):
        err("MIN_TRADE_XCH", f"MIN_TRADE_XCH={min_trade} must be positive")
    if max_trade <= Decimal("0"):
        err("MAX_TRADE_XCH", f"MAX_TRADE_XCH={max_trade} must be positive")
    if min_trade > Decimal("0") and max_trade > Decimal("0") and min_trade > max_trade:
        err("MIN_TRADE_XCH/MAX_TRADE_XCH", f"MIN_TRADE_XCH ({min_trade}) > MAX_TRADE_XCH ({max_trade})")
    if default_trade > Decimal("0") and max_trade > Decimal("0") and default_trade > max_trade:
        warn("DEFAULT_TRADE_XCH", f"DEFAULT_TRADE_XCH ({default_trade}) exceeds MAX_TRADE_XCH ({max_trade})")

    # ---- Tier sizes vs MAX_TRADE_XCH ----
    # When tier mode is active, offer sizes come from the tier config, not
    # DEFAULT_TRADE_XCH. A misconfigured tier can create offers much larger
    # than the user intended without any other warning.
    if max_trade > Decimal("0") and getattr(cfg, "TIER_ENABLED", False):
        tier_size_keys = [
            ("INNER_SIZE_XCH", getattr(cfg, "INNER_SIZE_XCH", Decimal("0"))),
            ("MID_SIZE_XCH",   getattr(cfg, "MID_SIZE_XCH",   Decimal("0"))),
            ("OUTER_SIZE_XCH", getattr(cfg, "OUTER_SIZE_XCH", Decimal("0"))),
            ("EXTREME_SIZE_XCH", getattr(cfg, "EXTREME_SIZE_XCH", Decimal("0"))),
        ]
        for tier_key, tier_size in tier_size_keys:
            if tier_size > Decimal("0") and tier_size > max_trade:
                warn(tier_key,
                     f"{tier_key} ({tier_size} XCH) exceeds MAX_TRADE_XCH ({max_trade} XCH) — "
                     f"offers will be larger than your configured maximum trade size")

    # ---- Spread & Pricing ----
    spread = getattr(cfg, "SPREAD_BPS", Decimal("0"))
    min_edge = getattr(cfg, "MIN_EDGE_BPS", Decimal("0"))

    if spread <= Decimal("0"):
        err("SPREAD_BPS", f"SPREAD_BPS={spread} must be positive")
    if min_edge >= spread and spread > Decimal("0"):
        warn("MIN_EDGE_BPS", f"MIN_EDGE_BPS ({min_edge}) >= SPREAD_BPS ({spread}) — edge cannot exceed spread")

    # Dynamic spreads cross-check
    if getattr(cfg, "DYNAMIC_SPREAD_ENABLED", False):
        min_sp = getattr(cfg, "MIN_SPREAD_BPS", Decimal("0"))
        max_sp = getattr(cfg, "MAX_SPREAD_BPS", Decimal("0"))
        if min_sp > Decimal("0") and max_sp > Decimal("0") and min_sp > max_sp:
            err("MIN_SPREAD_BPS/MAX_SPREAD_BPS", f"MIN_SPREAD_BPS ({min_sp}) > MAX_SPREAD_BPS ({max_sp})")

    # ---- Hard Price Limits ----
    hard_min = getattr(cfg, "HARD_MIN_PRICE_XCH", Decimal("0"))
    hard_max = getattr(cfg, "HARD_MAX_PRICE_XCH", Decimal("0"))
    if hard_min > Decimal("0") and hard_max > Decimal("0") and hard_min > hard_max:
        err("HARD_MIN_PRICE_XCH/HARD_MAX_PRICE_XCH",
            f"HARD_MIN ({hard_min}) > HARD_MAX ({hard_max})")

    # ---- Price Strategy ----
    try:
        raw_strategy = getattr(cfg, "PRICE_STRATEGY", None)
        strategy = str(raw_strategy).strip().lower() if raw_strategy and isinstance(raw_strategy, str) else ""
        allowed_strategies = {"dexie_only", "tibet_only", "average", "weighted"}
        if strategy and strategy not in allowed_strategies:
            err("PRICE_STRATEGY",
                f"PRICE_STRATEGY='{strategy}' is invalid; expected one of {sorted(allowed_strategies)}")
    except Exception:
        pass  # Attribute missing or not a string — use default

    # ---- Tibet Weight ----
    try:
        raw_weight = getattr(cfg, "TIBET_WEIGHT", None)
        if raw_weight is not None and isinstance(raw_weight, (int, float, str, Decimal)):
            weight = Decimal(str(raw_weight))
            if weight < Decimal("0") or weight > Decimal("1"):
                err("TIBET_WEIGHT",
                    f"TIBET_WEIGHT={weight} must be between 0 and 1 inclusive")
    except (InvalidOperation, TypeError, ValueError):
        err("TIBET_WEIGHT", "TIBET_WEIGHT is not a valid decimal number")

    # ---- Offer Management ----
    expiry = getattr(cfg, "OFFER_EXPIRY_SECS", 86400)
    if expiry < 300:
        err("OFFER_EXPIRY_SECS", f"OFFER_EXPIRY_SECS={expiry} is dangerously short (< 5 min)")

    refresh_before = getattr(cfg, "OFFER_REFRESH_BEFORE", 1800)
    if refresh_before >= expiry and expiry > 0:
        warn("OFFER_REFRESH_BEFORE", f"OFFER_REFRESH_BEFORE ({refresh_before}) >= OFFER_EXPIRY_SECS ({expiry})")

    max_buy = getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25)
    max_sell = getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25)
    total_offers = max_buy + max_sell
    # Smart Defaults spreads the ladder widely (up to ~60 per side on busy
    # markets, ~120 total) — only warn well above that.
    if total_offers > 300:
        warn("MAX_ACTIVE_*_OFFERS", f"Total max offers ({total_offers}) > 300 — wallet may struggle")

    # ---- Requoting ----
    requote_cooldown = getattr(cfg, "REQUOTE_COOLDOWN_SECS", 60)
    if requote_cooldown < 10:
        warn("REQUOTE_COOLDOWN_SECS", f"REQUOTE_COOLDOWN_SECS={requote_cooldown} is very aggressive (< 10s)")

    requote_batch = getattr(cfg, "REQUOTE_BATCH_SIZE", 5)
    if requote_batch > max(max_buy, max_sell) and max(max_buy, max_sell) > 0:
        warn("REQUOTE_BATCH_SIZE",
             f"REQUOTE_BATCH_SIZE ({requote_batch}) exceeds max offers per side ({max(max_buy, max_sell)})")

    # ---- Loop Timing ----
    loop_secs = getattr(cfg, "LOOP_SECONDS", 90)
    if loop_secs < 30:
        warn("LOOP_SECONDS", f"LOOP_SECONDS={loop_secs} is very fast (< 30s) — may cause wallet contention")

    # ---- Reserves ----
    xch_reserve = getattr(cfg, "XCH_RESERVE", Decimal("0"))
    if xch_reserve < Decimal("0"):
        err("XCH_RESERVE", f"XCH_RESERVE={xch_reserve} cannot be negative")

    cat_reserve = getattr(cfg, "CAT_RESERVE", Decimal("0"))
    if cat_reserve < Decimal("0"):
        err("CAT_RESERVE", f"CAT_RESERVE={cat_reserve} cannot be negative")

    # ---- Tiered Orders ----
    tier_enabled = getattr(cfg, "TIER_ENABLED", False)
    if tier_enabled:
        tier_counts = (
            getattr(cfg, "INNER_TIER_COUNT", 0) +
            getattr(cfg, "MID_TIER_COUNT", 0) +
            getattr(cfg, "OUTER_TIER_COUNT", 0) +
            getattr(cfg, "EXTREME_TIER_COUNT", 0)
        )
        if tier_counts == 0:
            warn("TIER_*_COUNT", "TIER_ENABLED=True but all tier counts are 0 — no tiered offers will be created")

        # Check tier sizes are positive
        for tier_name in ("INNER", "MID", "OUTER", "EXTREME"):
            count = getattr(cfg, f"{tier_name}_TIER_COUNT", 0)
            size = getattr(cfg, f"{tier_name}_SIZE_XCH", Decimal("0"))
            if count > 0 and size <= Decimal("0"):
                err(f"{tier_name}_SIZE_XCH",
                    f"{tier_name}_TIER_COUNT={count} but {tier_name}_SIZE_XCH={size} — tier has no size")

    # ---- Wallet URLs ----
    wallet_type = getattr(cfg, "WALLET_TYPE", "sage")
    if wallet_type == "sage":
        sage_url = getattr(cfg, "SAGE_RPC_URL", "")
        if sage_url and not _is_valid_url(sage_url):
            err("SAGE_RPC_URL", f"SAGE_RPC_URL is not a valid URL: {sage_url}")
    elif wallet_type == "chia":
        chia_url = getattr(cfg, "CHIA_WALLET_RPC_URL", "")
        if chia_url and not _is_valid_url(chia_url):
            err("CHIA_WALLET_RPC_URL", f"CHIA_WALLET_RPC_URL is not a valid URL: {chia_url}")

    # ---- Dexie URL ----
    dexie_url = getattr(cfg, "DEXIE_API_BASE", "")
    if dexie_url and not _is_valid_url(dexie_url):
        warn("DEXIE_API_BASE", f"DEXIE_API_BASE is not a valid URL: {dexie_url}")

    # ---- Tibet URL ----
    tibet_url = getattr(cfg, "TIBET_API_BASE", "")
    if tibet_url and not _is_valid_url(tibet_url):
        warn("TIBET_API_BASE", f"TIBET_API_BASE is not a valid URL: {tibet_url}")

    # ---- Sniper sanity ----
    if getattr(cfg, "SNIPER_ENABLED", False):
        sniper_size = getattr(cfg, "SNIPER_SIZE_XCH", Decimal("0"))
        if sniper_size <= Decimal("0"):
            warn("SNIPER_SIZE_XCH", f"SNIPER_ENABLED=True but SNIPER_SIZE_XCH={sniper_size}")

    # ---- Fee coin must be smaller than sniper coin ----
    # Sage auto-picks the smallest available coin for fees.  If fee coins
    # are the same size or bigger than sniper coins, Sage may grab a sniper
    # coin for a fee — burning a more expensive coin and leaving fees short.
    fee_coin_size = getattr(cfg, "FEE_COIN_SIZE_XCH", Decimal("0"))
    sniper_size_check = getattr(cfg, "SNIPER_SIZE_XCH", Decimal("0"))
    if (fee_coin_size > Decimal("0") and sniper_size_check > Decimal("0")
            and fee_coin_size >= sniper_size_check):
        warn("FEE_COIN_SIZE_XCH",
             f"FEE_COIN_SIZE_XCH ({fee_coin_size}) must be smaller than "
             f"SNIPER_SIZE_XCH ({sniper_size_check}) — Sage auto-picks the "
             f"smallest coin for fees, so fee coins must be the smallest tier")

    # ---- Ladder parallelism ----
    parallelism = getattr(cfg, "LADDER_CREATE_PARALLELISM", 5)
    if parallelism > 20:
        warn("LADDER_CREATE_PARALLELISM",
             f"LADDER_CREATE_PARALLELISM={parallelism} is very high — may overwhelm wallet RPC")

    # ---- Wallet type ----
    wallet_type = getattr(cfg, "WALLET_TYPE", "sage")
    if wallet_type not in ("sage", "chia"):
        warn("WALLET_TYPE",
             f"WALLET_TYPE='{wallet_type}' is not recognized (expected 'sage' or 'chia')")

    # ---- DEFAULT_TRADE_XCH vs MIN_TRADE_XCH ----
    if (default_trade > Decimal("0") and min_trade > Decimal("0")
            and default_trade < min_trade):
        warn("DEFAULT_TRADE_XCH",
             f"DEFAULT_TRADE_XCH ({default_trade}) is below MIN_TRADE_XCH ({min_trade})")

    # ---- Sniper expiry vs cooldown ----
    if getattr(cfg, "SNIPER_ENABLED", False):
        sniper_expiry = getattr(cfg, "SNIPER_EXPIRY_SECS", 600)
        sniper_cooldown = getattr(cfg, "SNIPER_COOLDOWN_SECS", 30)
        if sniper_cooldown >= sniper_expiry and sniper_expiry > 0:
            warn("SNIPER_COOLDOWN_SECS",
                 f"SNIPER_COOLDOWN_SECS ({sniper_cooldown}) >= SNIPER_EXPIRY_SECS ({sniper_expiry})")

    # ---- Boost size sanity ----
    boost_size = getattr(cfg, "BOOST_SIZE_XCH", Decimal("0"))
    if boost_size > Decimal("0") and max_trade > Decimal("0") and boost_size > max_trade:
        warn("BOOST_SIZE_XCH",
             f"BOOST_SIZE_XCH ({boost_size}) exceeds MAX_TRADE_XCH ({max_trade})")

    # ---- Coin prep without tier ----
    if getattr(cfg, "ENABLE_COIN_PREP", False) and not tier_enabled:
        xch_target = getattr(cfg, "XCH_TARGET_COINS", 0)
        cat_target = getattr(cfg, "CAT_TARGET_COINS", 0)
        if xch_target <= 0 and cat_target <= 0:
            warn("ENABLE_COIN_PREP",
                 "ENABLE_COIN_PREP=True but no target coin counts set and TIER_ENABLED=False")

    return report

