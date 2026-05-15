"""Tests for the reserve-floor guard against failed balance reads.

Background: prior to this guard the bot interpreted a failed Sage RPC as
"balance == 0", which immediately tripped the reserve-floor breach and
mass-cancelled every open offer. A single Sage 401 (e.g. user changed
fingerprint in Sage) burned the entire ladder.

The guard now defers the reserve check whenever the balance read does not
return a usable wallet_balance payload — the next cycle will retry once
Sage recovers.
"""

import pytest

from bot_loop import _extract_wallet_balance_or_defer, _ReserveCheckDeferred


# ---- Failure cases: must defer (no coercion to zero) ----------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        [],
        42,
        {"success": False, "error": "Sage HTTP 401: Not logged in"},
        {"success": False, "error": "Sage HTTP 500: Wallet error"},
        {},  # missing wallet_balance entirely
        {"success": True},  # success flag but no payload
        {"wallet_balance": None},  # null payload
        {"wallet_balance": {}},  # empty payload
        {"success": True, "wallet_balance": {}},
    ],
)
def test_failed_read_defers(raw):
    with pytest.raises(_ReserveCheckDeferred):
        _extract_wallet_balance_or_defer(raw)


# ---- Success cases: must return the wallet_balance dict -------------------


def test_explicit_success_returns_payload():
    raw = {
        "success": True,
        "wallet_balance": {"confirmed_wallet_balance": 12345, "spendable_balance": 100},
    }
    bal = _extract_wallet_balance_or_defer(raw)
    assert bal == {"confirmed_wallet_balance": 12345, "spendable_balance": 100}


def test_payload_without_success_flag_still_passes():
    """Some legacy paths return {wallet_balance: {...}} without a success
    flag. Those must still be treated as a valid read — the absence of
    success=False is what matters."""
    raw = {"wallet_balance": {"confirmed_wallet_balance": 0}}
    bal = _extract_wallet_balance_or_defer(raw)
    assert bal == {"confirmed_wallet_balance": 0}


def test_zero_balance_is_a_real_zero_not_a_defer():
    """A successful read that returns 0 IS a real zero balance — the
    breach should fire downstream. The guard only defers on read failure,
    not on legitimate empty wallets."""
    raw = {"success": True, "wallet_balance": {"confirmed_wallet_balance": 0}}
    bal = _extract_wallet_balance_or_defer(raw)
    assert bal["confirmed_wallet_balance"] == 0
