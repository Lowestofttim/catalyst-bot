"""Verify .env.example defaults match config.py defaults.

Prevents the two files from drifting apart. Run from the repo root:

    python scripts/check_env_example.py

Exits 0 if every key present in .env.example matches the default the
loader in config.py uses when the env var is absent. Exits 1 and prints
a table of drift otherwise. Wired into CI so drift is caught in PR.
"""

from __future__ import annotations

# --- src-layout bootstrap (auto-inserted) ---
import os as _os
import sys as _sys
_sys.path.insert(
    0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src", "catalyst")
)
# --- end bootstrap ---

import os
import re
import sys
from decimal import Decimal


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "src", "catalyst", "config.py")
ENV_EXAMPLE_PATH = os.path.join(REPO_ROOT, ".env.example")


# Keys where .env.example intentionally biases toward a friendlier starter
# value than config.py's zero/false default. Keep the reason with the entry
# so future maintainers know whether the override is still load-bearing.
_KNOWN_TEMPLATE_OVERRIDES: dict[str, str] = {
    "SAGE_RPC_URL": "template uses 127.0.0.1 explicitly; Sage binds IPv4-only on Windows",
    "SAGE_SET_CHANGE_ADDRESS": "template recommends ON for cleaner coin management",
    "BASE_SPREAD_BPS": "template uses 800; loader default 0 would disable the dynamic spread engine",
    "MIN_EDGE_BPS": "template uses 200; slightly tighter than loader's 300 for first-run responsiveness",
    "MAX_ACTIVE_BUY": "template sets 25; loader has no default (driven by legacy MAX_ACTIVE_BUY_OFFERS)",
    "MAX_ACTIVE_SELL": "template sets 25; loader has no default (driven by legacy MAX_ACTIVE_SELL_OFFERS)",
    "BUY_LADDER_REVERSED": "template enables the v2 ladder layout; loader defaults to legacy OFF",
    "TRANSACTION_FEE_XCH": "template uses 0.000001; matches historical fee floor",
    "FEE_PREP_COUNT": "template suggests 50 fee coins; loader default 20 is low for active traders",
    "FEE_COIN_SIZE_XCH": "template uses 0.001; loader default 0.0001 is too small to cover fee bumps",
    "SNIPER_SIZE_XCH": "template uses 0.01; loader default 0.001 is a probing-only size",
    "SNIPER_PREP_COUNT": "template suggests 25 probes; loader default 20",
    "ENABLE_RUNTIME_COIN_HEALTH": "template enables runtime coin health for better observability",
    "CAT_NAME": "blank in template; loader default 'MZ' is dev-only",
    "CAT_WALLET_ID": "blank in template; loader default 2 is Sage-only and comes from get_wallets()",
    "CAT_RESERVE": "blank in template; loader has no default",
}


# Matches any _str / _int / _decimal / _bool / _safe_url call with a literal
# default. Captures (helper, key, default_literal).
_LOADER_RE = re.compile(
    r'_(str|int|decimal|bool|safe_url)\(\s*"([A-Z0-9_]+)"\s*(?:,\s*([^)]+?))?\s*\)'
)


def _clean_default(helper: str, raw: str | None) -> str:
    """Normalise a config.py default literal to the string it'd appear as in .env.example."""
    if raw is None or raw == "":
        return ""
    val = raw.strip().strip(",").strip()
    if val.startswith('"') and val.endswith('"'):
        val = val[1:-1]
    elif val.startswith("'") and val.endswith("'"):
        val = val[1:-1]
    if helper == "bool":
        return "true" if val.lower() in ("true", "1", "yes", "on") else "false"
    if helper == "decimal":
        try:
            return str(Decimal(val))
        except Exception:
            return val
    return val


def parse_config_defaults() -> dict[str, str]:
    """Return {KEY: default_string} for every typed loader call in config.py."""
    out: dict[str, str] = {}
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        source = fh.read()
    for match in _LOADER_RE.finditer(source):
        helper, key, raw = match.group(1), match.group(2), match.group(3)
        default = _clean_default(helper, raw)
        out.setdefault(key, default)
    return out


def parse_env_example() -> dict[str, str]:
    """Return {KEY: value_string} for every KEY=value line in .env.example."""
    out: dict[str, str] = {}
    with open(ENV_EXAMPLE_PATH, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, val = stripped.partition("=")
            key = key.strip()
            val = val.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            out[key] = val
    return out


def main() -> int:
    config_defaults = parse_config_defaults()
    env_values = parse_env_example()

    drift: list[tuple[str, str, str]] = []
    for key, example_val in env_values.items():
        if key not in config_defaults:
            continue
        if key in _KNOWN_TEMPLATE_OVERRIDES:
            continue
        config_default = config_defaults[key]
        # Empty string in either side is a valid "no default" signal on both.
        if example_val == "" and config_default == "":
            continue
        # Decimal comparison so "0.10" == "0.1" passes.
        try:
            if Decimal(example_val) == Decimal(config_default):
                continue
        except Exception:
            if example_val == config_default:
                continue
        drift.append((key, example_val, config_default))

    if not drift:
        print(f"OK: .env.example matches config.py defaults for {len(env_values)} keys")
        return 0

    print(f"DRIFT: {len(drift)} key(s) disagree between .env.example and config.py:\n")
    print(f"  {'KEY':<40} {'.env.example':<20} {'config.py':<20}")
    print(f"  {'-' * 40} {'-' * 20} {'-' * 20}")
    for key, ex, cfg_val in drift:
        print(f"  {key:<40} {ex:<20} {cfg_val:<20}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
