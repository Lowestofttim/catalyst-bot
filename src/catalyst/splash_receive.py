"""Pure helpers for classifying inbound Splash offer payloads

Given the various shapes Splash and the wallet return when viewing an
offer, these helpers normalize offered/requested sides and decide
whether an offer is relevant to the active CAT pair — and if so, on
which side (buy or sell). No network, wallet, or database calls; every
function is a stateless transformation suitable for unit testing.

Key responsibilities:
    - Normalize asset keys and maker/taker payload variants
    - Produce a consistent offered/requested dict from any input shape
    - Classify an offer against a target asset as buy, sell, or ignore

Called by the Splash incoming webhook handler in api_server.
"""

from typing import Any, Dict, Iterable


def _asset_key(value: Any) -> str:
    """Normalize offer summary asset keys."""
    if value is None:
        return "xch"
    text = str(value).strip().lower()
    return "xch" if not text or text == "xch" else text


def _normalize_side(entries: Dict[Any, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    if not isinstance(entries, dict):
        return normalized
    for raw_key, amount in entries.items():
        normalized[_asset_key(raw_key)] = amount
    return normalized


def _from_maker_taker(items: Iterable[dict]) -> Dict[str, Any]:
    side: Dict[str, Any] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        asset_info = item.get("asset") or {}
        asset_id = asset_info.get("asset_id") if isinstance(asset_info, dict) else None
        side[_asset_key(asset_id)] = item.get("amount", 0)
    return side


def normalize_offer_summary(view_result: Any) -> Dict[str, Dict[str, Any]]:
    """Convert various offer-view shapes into offered/requested dicts."""
    payloads = []
    if isinstance(view_result, dict):
        payloads.append(view_result.get("summary"))
        offer_obj = view_result.get("offer")
        if isinstance(offer_obj, dict):
            payloads.append(offer_obj.get("summary"))
        payloads.append(view_result)

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if "offered" in payload or "requested" in payload:
            return {
                "offered": _normalize_side(payload.get("offered") or {}),
                "requested": _normalize_side(payload.get("requested") or {}),
            }
        if "maker" in payload or "taker" in payload:
            return {
                "offered": _from_maker_taker(payload.get("maker") or []),
                "requested": _from_maker_taker(payload.get("taker") or []),
            }

    return {"offered": {}, "requested": {}}


def classify_offer_for_asset(view_result: Any, asset_id: str) -> Dict[str, Any]:
    """Classify whether an inbound offer is the active CAT/XCH pair."""
    target_asset = _asset_key(asset_id)
    summary = normalize_offer_summary(view_result)
    offered = summary.get("offered") or {}
    requested = summary.get("requested") or {}

    offered_keys = set(offered.keys())
    requested_keys = set(requested.keys())
    pair_assets = sorted((offered_keys | requested_keys) - {"xch"})

    side = ""
    pair_hint = ""
    if len(pair_assets) == 1 and len(offered_keys) == 1 and len(requested_keys) == 1:
        pair_hint = pair_assets[0]
        if offered_keys == {"xch"} and requested_keys == {pair_hint}:
            side = "buy"
        elif offered_keys == {pair_hint} and requested_keys == {"xch"}:
            side = "sell"

    relevant = bool(pair_hint and target_asset and pair_hint == target_asset and side)

    return {
        "relevant": relevant,
        "pair_hint": pair_hint,
        "side": side,
        "summary": summary,
    }

