"""Dexie liquidity-reward claim flow.

Implements the protocol used by the official ``dexie-rewards`` Python CLI
(https://github.com/dexie-space/dexie-rewards):

    1. List our offers from the wallet.
    2. For each offer, the dexie offer-id is base58(sha256(offer_bech32)).
    3. POST those ids to ``/v1/rewards/check`` — Dexie returns the subset
       that has unclaimed rewards plus each offer's maker_puzzle_hash.
    4. For every offer with rewards, sign the message
       ``"Claim dexie liquidity rewards for offer <id> (<timestamp>)"``
       (or the variant with ``" to <target_puzzle_hash>"``) using the
       maker address — Sage exposes ``sign_message_by_address`` for this.
    5. POST ``{claims: [{offer_id, signature, public_key, timestamp}]}``
       to ``/v1/rewards/claim``. Dexie pays out to the maker address in
       batches roughly every 15 minutes. No XCH leaves the wallet.

This module deliberately avoids extra dependencies — base58 is implemented
inline (Bitcoin alphabet, the default Dexie expects) and bech32 conversion
uses ``chia.util.bech32m`` which is already installed.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

import requests

try:
    from config import cfg
except Exception:
    cfg = None  # type: ignore

try:
    from chia.util.bech32m import encode_puzzle_hash
except Exception:
    encode_puzzle_hash = None  # type: ignore


_DEFAULT_BASE = "https://api.dexie.space"
_API_VERSION = "/v1"
_BS58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _api_base() -> str:
    base = getattr(cfg, "DEXIE_API_BASE", _DEFAULT_BASE) if cfg else _DEFAULT_BASE
    return (base or _DEFAULT_BASE).rstrip("/")


# ────────────────────────── helpers ──────────────────────────


def _b58encode(data: bytes) -> str:
    """Base58 encode using the Bitcoin alphabet (matches Dexie's `based58`)."""
    if not data:
        return ""
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_BS58_ALPHABET[rem])
    # Preserve leading zero bytes as leading "1"s (base58 convention).
    for byte in data:
        if byte == 0:
            out.append(_BS58_ALPHABET[0])
        else:
            break
    return out[::-1].decode("ascii")


def compute_offer_hash(offer_bech32: str) -> str:
    """Return the dexie offer id for an offer's bech32 string.

    Mirrors ``get_dexie_bs58_offer_hash`` in dexie-rewards: sha256 of the
    bech32 bytes, then base58-encode the digest.
    """
    if not offer_bech32:
        return ""
    digest = hashlib.sha256(offer_bech32.strip().encode("utf-8")).digest()
    return _b58encode(digest)


def _network_prefix() -> str:
    """Return the bech32m prefix matching the active wallet network."""
    try:
        from wallet import get_next_address

        info = get_next_address(new_address=False)
        if info and info.get("success"):
            addr = str(info.get("address", "")).strip()
            if addr.startswith("txch1"):
                return "txch"
    except Exception:
        pass
    return "xch"


def puzzle_hash_to_address(maker_puzzle_hash_hex: str) -> str:
    """Bech32m-encode a puzzle hash hex string to an xch1/txch1 address."""
    if not maker_puzzle_hash_hex or encode_puzzle_hash is None:
        return ""
    h = maker_puzzle_hash_hex.lower()
    if h.startswith("0x"):
        h = h[2:]
    try:
        return encode_puzzle_hash(bytes.fromhex(h), _network_prefix())
    except Exception:
        return ""


# ────────────────────────── HTTP ──────────────────────────


def _post(path: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    url = f"{_api_base()}{_API_VERSION}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"HTTP {resp.status_code}",
                "body": resp.text[:200],
            }
        return resp.json() or {}
    except Exception as e:
        return {"success": False, "error": str(e)}


def check_rewards(offer_ids: List[str]) -> Dict[str, Any]:
    """POST ``/v1/rewards/check`` with a list of dexie offer ids.

    Returns ``{success, offers: [{id, status, maker_puzzle_hash, rewards: [...]}]}``
    """
    if not offer_ids:
        return {"success": True, "offers": []}
    return _post("/rewards/check", {"ids": offer_ids})


def submit_claims(
    claims: List[Dict[str, Any]], target_puzzle_hash_hex: Optional[str] = None
) -> Dict[str, Any]:
    """POST signed claims to ``/v1/rewards/claim``."""
    if not claims:
        return {"success": False, "error": "no_claims"}
    body: Dict[str, Any] = {"claims": claims}
    if target_puzzle_hash_hex:
        h = target_puzzle_hash_hex.lower()
        if h.startswith("0x"):
            h = h[2:]
        body["target_puzzle_hash"] = h
    return _post("/rewards/claim", body)


# ────────────────────────── orchestration ──────────────────────────


def _gather_our_offer_bech32s(limit: int = 500) -> List[str]:
    """Pull bech32 strings for offers we've created from the wallet.

    Sage's ``get_all_offers`` returns offer rows with the bech32 field; we
    keep duplicates de-duped so a re-posted offer isn't checked twice.
    """
    try:
        from wallet import get_all_offers
    except Exception:
        return []
    seen: set = set()
    out: List[str] = []
    try:
        rows = get_all_offers(end=limit) or []
    except Exception:
        return []
    for row in rows:
        bech = row.get("offer") if isinstance(row, dict) else None
        if not bech or not isinstance(bech, str):
            continue
        bech = bech.strip()
        if bech in seen:
            continue
        seen.add(bech)
        out.append(bech)
    return out


def list_pending_rewards() -> Dict[str, Any]:
    """Return all of our offers with currently-claimable rewards.

    Returns:
        {
            "success": bool,
            "offers": [
                {
                    "offer_id": <base58 hash>,
                    "maker_puzzle_hash": <hex>,
                    "rewards": [{"amount": float, "code": str, ...}, ...],
                    "is_active": bool,
                },
                ...
            ],
            "totals": {"DBX": float, ...},
        }
    """
    bechs = _gather_our_offer_bech32s()
    if not bechs:
        return {"success": True, "offers": [], "totals": {}}

    ids = [compute_offer_hash(b) for b in bechs]
    # Reverse-lookup so we can match an offer back to its bech32 if needed.
    response = check_rewards(ids)
    if not response.get("success"):
        return {
            "success": False,
            "error": response.get("error", "rewards_check_failed"),
            "offers": [],
            "totals": {},
        }

    offers_out: List[Dict[str, Any]] = []
    totals: Dict[str, float] = {}
    for entry in response.get("offers", []) or []:
        rewards = []
        for r in entry.get("rewards", []) or []:
            try:
                amt = float(r.get("amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0.0
            code = str(r.get("code") or "DBX")
            rewards.append(
                {
                    "amount": round(amt, 4),
                    "code": code,
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                }
            )
            totals[code] = totals.get(code, 0.0) + amt
        offers_out.append(
            {
                "offer_id": entry.get("id", ""),
                "maker_puzzle_hash": entry.get("maker_puzzle_hash", ""),
                "rewards": rewards,
                "is_active": entry.get("status", 0) == 0,
                "date_found": entry.get("date_found", ""),
            }
        )
    totals = {k: round(v, 4) for k, v in totals.items()}
    return {"success": True, "offers": offers_out, "totals": totals}


def claim_all(target_address: Optional[str] = None) -> Dict[str, Any]:
    """Sign and submit claims for every offer with claimable rewards.

    ``target_address`` is optional — when provided rewards are sent there
    instead of the maker address. Both branches sign a different message
    (Dexie verifies the address suffix matches the payload).
    """
    pending = list_pending_rewards()
    if not pending.get("success"):
        return pending
    offers = pending.get("offers", [])
    if not offers:
        return {
            "success": True,
            "claims_submitted": 0,
            "message": "no rewards to claim",
        }

    try:
        from wallet import sign_message_by_address
    except Exception:
        return {"success": False, "error": "wallet_signing_unavailable"}

    target_puzzle_hash_hex: Optional[str] = None
    target_address_clean: Optional[str] = None
    if target_address:
        target_address_clean = target_address.strip()
        try:
            from chia.util.bech32m import decode_puzzle_hash

            target_puzzle_hash_hex = decode_puzzle_hash(target_address_clean).hex()
        except Exception:
            return {"success": False, "error": "invalid_target_address"}

    timestamp = int(time.time())
    claims: List[Dict[str, Any]] = []
    sign_failures: List[Dict[str, Any]] = []
    for offer in offers:
        offer_id = offer.get("offer_id")
        maker_hex = offer.get("maker_puzzle_hash")
        if not offer_id or not maker_hex:
            continue
        maker_address = puzzle_hash_to_address(maker_hex)
        if not maker_address:
            sign_failures.append(
                {"offer_id": offer_id, "error": "address_encode_failed"}
            )
            continue
        if target_puzzle_hash_hex:
            message = (
                f"Claim dexie liquidity rewards for offer {offer_id} "
                f"to {target_puzzle_hash_hex} ({timestamp})"
            )
        else:
            message = (
                f"Claim dexie liquidity rewards for offer {offer_id} ({timestamp})"
            )
        sig_result = sign_message_by_address(maker_address, message)
        if not sig_result or not sig_result.get("success"):
            sign_failures.append(
                {
                    "offer_id": offer_id,
                    "error": (sig_result or {}).get("error", "sign_failed"),
                }
            )
            continue
        claims.append(
            {
                "offer_id": offer_id,
                "signature": sig_result.get("signature", ""),
                "public_key": sig_result.get("public_key", ""),
                "timestamp": timestamp,
            }
        )

    if not claims:
        # If every offer hit the same root cause (Sage's RPC doesn't expose
        # message signing), surface that as the top-level error so the GUI
        # can render the right guidance instead of just "no_signed_claims".
        unique_errors = {f.get("error") for f in sign_failures}
        if unique_errors == {"signing_not_supported_by_sage_rpc"}:
            return {
                "success": False,
                "error": "signing_not_supported_by_sage_rpc",
                "user_message": (
                    "DBX claim signing isn't available on Sage's standalone "
                    "RPC. Use Dexie's official CLI (`pip install dexie-rewards`) "
                    "or claim from Sage's WalletConnect-enabled UI for now."
                ),
                "sign_failures": sign_failures,
            }
        return {
            "success": False,
            "error": "no_signed_claims",
            "sign_failures": sign_failures,
        }

    result = submit_claims(claims, target_puzzle_hash_hex=target_puzzle_hash_hex)
    return {
        "success": bool(result.get("success", False)),
        "claims_submitted": len(claims),
        "sign_failures": sign_failures,
        "dexie_response": result,
    }
