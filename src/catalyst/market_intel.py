"""Live competitive intelligence from the Dexie orderbook

`MarketIntel` polls `/v1/offers` on a 30-second cadence via
`refresh_orderbook()` and derives best bid / best ask, competitor spread,
per-side thin-book detection, and DBX reward eligibility. The bot's own
offers are excluded from competitor math by looking up the trade-id ->
dexie-id mapping from the database. Output flows into `risk_manager`
and the GUI so spread and placement decisions are reactive to the live
book rather than stale snapshots.

Key responsibilities:
    - Fetch and snapshot the live Dexie orderbook for the active pair
    - Separate bot-owned offers from competitor offers via dexie-id map
    - Derive best bid/ask, competitor spread, and thin-side signals
    - Flag whether current placement qualifies for DBX rewards
"""

import time
import requests
import threading
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from config import cfg
from database import get_trade_dexie_map, log_event


def _bps_to_pct(val):
    """Convert a BPS value to a formatted % string."""
    try:
        n = float(val) / 100
        if n < 1:
            return f"{n:.2f}%"
        return f"{n:.1f}%"
    except (ValueError, TypeError):
        return str(val)


class MarketIntel:
    """Live market intelligence from the Dexie orderbook and ecosystem.

    Core insights we extract:
    - Best competing bid/ask (tightest non-bot offers)
    - Total depth at each price level
    - Spread offered by competitors (are we tighter or wider?)
    - Whether our offers qualify for DBX rewards
    - Market activity metrics (new offers appearing, volume)
    """

    def __init__(self, price_engine=None):
        self._price_engine = price_engine
        self._known_dexie_ids: set[str] = set()

        # ---- Orderbook state ----
        self._orderbook: Dict = {
            "buy_offers": [],     # Sorted best (highest) to worst
            "sell_offers": [],    # Sorted best (lowest) to worst
            "last_refresh": 0,
            "refresh_count": 0,
            "errors": 0,
        }

        # ---- Competitor analysis ----
        self._competitors: Dict = {
            "best_bid": Decimal("0"),         # Highest competing buy price
            "best_ask": Decimal("0"),         # Lowest competing sell price
            "competitor_spread_bps": Decimal("0"),  # Their spread in BPS
            "our_spread_bps": Decimal("0"),   # Our spread for comparison
            "overall_best_bid": Decimal("0"),       # Highest buy price (anyone)
            "overall_best_ask": Decimal("0"),       # Lowest sell price (anyone)
            "overall_spread_bps": Decimal("0"),     # Full orderbook spread
            "buy_depth_xch": Decimal("0"),    # Total XCH depth on buy side
            "sell_depth_xch": Decimal("0"),   # Total XCH depth on sell side
            "num_buy_offers": 0,              # Total offers (including ours)
            "num_sell_offers": 0,             # Total offers (including ours)
            "num_competitor_buys": 0,         # Non-bot buy offers only
            "num_competitor_sells": 0,        # Non-bot sell offers only
            "whale_orders": [],               # Orders > 1 XCH
            "thin_side": "",                  # "buy", "sell", or "" (balanced)
        }

        # ---- DBX Rewards tracking ----
        self._dbx: Dict = {
            "eligible_offers": 0,       # How many of our offers qualify
            "max_eligible_spread": Decimal(str(getattr(cfg, "DBX_MAX_SPREAD_BPS", "500"))),
            "estimated_dbx_rate": Decimal("0"),   # Estimated DBX per hour
            "last_check": 0,
        }

        # ---- Thread safety ----
        self._lock = threading.Lock()

        # ---- Session for HTTP requests ----
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        # ---- Timing ----
        self._orderbook_refresh_interval = 30  # seconds between orderbook fetches
        self._dbx_check_interval = 300         # 5 min between DBX eligibility checks
        self._orderbook_page_size = max(
            50,
            int(getattr(cfg, "DEXIE_ORDERBOOK_PAGE_SIZE", 200) or 200),
        )

    # -------------------------------------------------------------------
    # Orderbook Monitoring (Dexie GET /v1/offers)
    # -------------------------------------------------------------------

    def refresh_orderbook(self, force: bool = False) -> Dict:
        """Fetch the current Dexie orderbook for our CAT pair.

        Uses GET /v1/offers endpoint which returns all active offers.
        We filter to our pair and separate into buy/sell sides.

        Returns summary of what was found.
        """
        now = time.time()
        if not force and (now - self._orderbook["last_refresh"]) < self._orderbook_refresh_interval:
            return self._competitors  # Use cached data

        if not cfg.CAT_ASSET_ID:
            return self._competitors

        try:
            try:
                trade_dexie_map = get_trade_dexie_map(cfg.CAT_ASSET_ID) or {}
                self._known_dexie_ids = {
                    str(dexie_id).strip()
                    for dexie_id in trade_dexie_map.values()
                    if str(dexie_id or "").strip()
                }
            except Exception:
                self._known_dexie_ids = set()

            # Dexie API: GET /v1/offers with asset filter
            # NOTE: Dexie v1 uses "offered" and "requested" (NOT "offered_asset_id")
            url = f"{cfg.DEXIE_API_BASE.rstrip('/')}/v1/offers"

            # Sell side: people offering CAT for XCH
            params = {
                "offered": cfg.CAT_ASSET_ID,
                "requested": "xch",
                "status": 0,  # 0 = active offers (4 = completed/taken)
                "page_size": self._orderbook_page_size,
                "sort": "price_asc",
            }

            resp = self._session.get(url, params=params, timeout=10)

            # Bail early on 429 — don't burn through the buy request too
            if resp.status_code == 429:
                log_event("warning", "dexie_rate_limited",
                          "Dexie orderbook API returned 429 — skipping refresh")
                self._orderbook["errors"] = self._orderbook.get("errors", 0) + 1
                return self._competitors

            # Buy side: people offering XCH for CAT (requesting our CAT)
            buy_params = {
                "offered": "xch",
                "requested": cfg.CAT_ASSET_ID,
                "status": 0,
                "page_size": self._orderbook_page_size,
                "sort": "price_desc",
            }

            buy_resp = self._session.get(url, params=buy_params, timeout=10)

            # Check buy side for 429 too
            if buy_resp.status_code == 429:
                log_event("warning", "dexie_rate_limited",
                          "Dexie orderbook buy API returned 429 — skipping refresh")
                self._orderbook["errors"] = self._orderbook.get("errors", 0) + 1
                return self._competitors

            sell_offers = []
            buy_offers = []

            # Parse sell side (others selling CAT for XCH)
            if resp.status_code == 200:
                data = resp.json()
                offers = data.get("offers", [])
                for offer in offers:
                    parsed = self._parse_dexie_offer(offer, "sell")
                    if parsed:
                        sell_offers.append(parsed)

            # Parse buy side (others buying CAT with XCH)
            if buy_resp.status_code == 200:
                data = buy_resp.json()
                offers = data.get("offers", [])
                for offer in offers:
                    parsed = self._parse_dexie_offer(offer, "buy")
                    if parsed:
                        buy_offers.append(parsed)

            # Sort: buys by price descending (best bid first),
            #        sells by price ascending (best ask first)
            buy_offers.sort(key=lambda x: x["price"], reverse=True)
            sell_offers.sort(key=lambda x: x["price"])

            with self._lock:
                self._orderbook["buy_offers"] = buy_offers
                self._orderbook["sell_offers"] = sell_offers
                self._orderbook["last_refresh"] = now
                self._orderbook["refresh_count"] += 1

            # Analyse the orderbook
            self._analyse_orderbook(buy_offers, sell_offers)

            if self._orderbook["refresh_count"] % 10 == 1:  # Log periodically
                log_event("info", "orderbook_refresh",
                          f"Dexie orderbook: {len(buy_offers)} bids, {len(sell_offers)} asks | "
                          f"Best bid: {self._competitors['best_bid']:.8f}, "
                          f"Best ask: {self._competitors['best_ask']:.8f}, "
                          f"Competitor spread: {_bps_to_pct(self._competitors['competitor_spread_bps'])}")

            return self._competitors

        except requests.RequestException as e:
            self._orderbook["errors"] += 1
            if self._orderbook["errors"] % 5 == 1:
                log_event("debug", "orderbook_error", f"Dexie orderbook fetch failed: {e}")
            return self._competitors

    def _parse_dexie_offer(self, offer: Dict, expected_side: str) -> Optional[Dict]:
        """Parse a Dexie offer into our internal format.

        Dexie offer format includes 'offered' and 'requested' arrays
        with asset details. We need to extract the price (XCH per CAT).
        """
        try:
            offer_id = offer.get("id", "")
            offered = offer.get("offered", [])
            requested = offer.get("requested", [])

            xch_amount = Decimal("0")
            cat_amount = Decimal("0")

            # Find XCH and CAT amounts in the offer
            for asset in offered + requested:
                code = str(asset.get("code", "")).upper()
                asset_id = str(asset.get("id", ""))

                amount_str = str(asset.get("amount", "0"))
                try:
                    amount = Decimal(amount_str)
                except (InvalidOperation, ValueError):
                    continue

                if code == "XCH" or asset_id == "" or asset_id == "xch":
                    xch_amount = amount
                elif asset_id.lower() == cfg.CAT_ASSET_ID.lower():
                    cat_amount = amount

            if xch_amount <= 0 or cat_amount <= 0:
                return None

            price = xch_amount / cat_amount

            # Determine if this is our own offer (by checking the bot tag)
            is_ours = False
            tags = offer.get("tags", [])
            bot_tag = str(getattr(cfg, "BOT_TAG", "") or "").strip()
            if isinstance(tags, list) and bot_tag and bot_tag in tags:
                is_ours = True
            elif offer_id and str(offer_id).strip() in self._known_dexie_ids:
                is_ours = True

            return {
                "offer_id": offer_id,
                "side": expected_side,
                "price": price,
                "xch_amount": xch_amount,
                "cat_amount": cat_amount,
                "is_ours": is_ours,
                "created_at": offer.get("date_found", ""),
            }

        except Exception:
            return None

    def _analyse_orderbook(self, buy_offers: List[Dict], sell_offers: List[Dict]):
        """Analyse the orderbook to extract competitive intelligence.

        Key metrics:
        - Best competing bid/ask (excluding our own offers)
        - Total depth on each side
        - Whale detection
        - Thin side detection
        """
        # Filter out our own offers for competitor analysis
        competitor_buys = [o for o in buy_offers if not o.get("is_ours")]
        competitor_sells = [o for o in sell_offers if not o.get("is_ours")]

        # Best bid/ask from competitors
        best_bid = competitor_buys[0]["price"] if competitor_buys else Decimal("0")
        best_ask = competitor_sells[0]["price"] if competitor_sells else Decimal("0")

        # Competitor spread
        competitor_spread_bps = Decimal("0")
        if best_bid > 0 and best_ask > 0 and best_bid < best_ask:
            mid = (best_bid + best_ask) / 2
            if mid > 0:
                competitor_spread_bps = (best_ask - best_bid) / mid * Decimal("10000")
        elif best_bid > 0 and best_ask > 0:
            # Ignore inverted competitor books — they are not a safe basis for
            # tightening/widening recommendations.
            best_bid = Decimal("0")
            best_ask = Decimal("0")

        # Total depth
        buy_depth = sum(o["xch_amount"] for o in buy_offers)
        sell_depth = sum(o["xch_amount"] for o in sell_offers)

        # Whale detection (orders > 1 XCH)
        whale_threshold = Decimal("1.0")
        whales = []
        for o in buy_offers + sell_offers:
            if o["xch_amount"] >= whale_threshold:
                whales.append({
                    "side": o["side"],
                    "price": str(o["price"]),
                    "xch_amount": str(o["xch_amount"]),
                    "is_ours": o["is_ours"],
                })

        # Thin side detection
        thin_side = ""
        if buy_depth > 0 and sell_depth > 0:
            ratio = buy_depth / sell_depth if sell_depth > 0 else Decimal("999")
            if ratio > Decimal("3"):
                thin_side = "sell"  # Sell side is thin relative to buy
            elif ratio < Decimal("0.33"):
                thin_side = "buy"   # Buy side is thin relative to sell

        # Overall orderbook spread (including our own offers)
        overall_best_bid = buy_offers[0]["price"] if buy_offers else Decimal("0")
        overall_best_ask = sell_offers[0]["price"] if sell_offers else Decimal("0")
        overall_spread_bps = Decimal("0")
        if overall_best_bid > 0 and overall_best_ask > 0 and overall_best_bid < overall_best_ask:
            overall_mid = (overall_best_bid + overall_best_ask) / 2
            if overall_mid > 0:
                overall_spread_bps = (overall_best_ask - overall_best_bid) / overall_mid * Decimal("10000")
        elif overall_best_bid > 0 and overall_best_ask > 0:
            overall_spread_bps = Decimal("0")

        # Our own spread (from our best bid/ask on the orderbook)
        our_buys = [o for o in buy_offers if o.get("is_ours")]
        our_sells = [o for o in sell_offers if o.get("is_ours")]
        our_best_bid = max((o["price"] for o in our_buys), default=Decimal("0"))
        our_best_ask = min((o["price"] for o in our_sells), default=Decimal("0"))
        our_spread_bps = Decimal("0")
        if our_best_bid > 0 and our_best_ask > 0 and our_best_bid < our_best_ask:
            our_mid = (our_best_bid + our_best_ask) / 2
            if our_mid > 0:
                our_spread_bps = (our_best_ask - our_best_bid) / our_mid * Decimal("10000")

        with self._lock:
            self._competitors["best_bid"] = best_bid
            self._competitors["best_ask"] = best_ask
            self._competitors["competitor_spread_bps"] = competitor_spread_bps
            self._competitors["our_spread_bps"] = our_spread_bps
            self._competitors["overall_best_bid"] = overall_best_bid
            self._competitors["overall_best_ask"] = overall_best_ask
            self._competitors["overall_spread_bps"] = overall_spread_bps
            self._competitors["buy_depth_xch"] = buy_depth
            self._competitors["sell_depth_xch"] = sell_depth
            self._competitors["num_buy_offers"] = len(buy_offers)
            self._competitors["num_sell_offers"] = len(sell_offers)
            self._competitors["num_competitor_buys"] = len(competitor_buys)
            self._competitors["num_competitor_sells"] = len(competitor_sells)
            self._competitors["whale_orders"] = whales[:5]  # Cap at 5
            self._competitors["thin_side"] = thin_side

    # -------------------------------------------------------------------
    # Competitor-Aware Spread Recommendations
    # -------------------------------------------------------------------

    def get_competitor_spread(self) -> Dict:
        """Get competitor spread data for the risk manager.

        Returns structured data that risk_manager can use to adjust our spreads.
        """
        with self._lock:
            return dict(self._competitors)

    def get_cached_data(self) -> Dict:
        """Return the cached competitor-analysis snapshot.

        Provides access to the same data as get_competitor_spread() under
        the name that bot_loop uses when feeding the Smart Advisor and the
        live dashboard.  Keys include: competitor_spread_bps, best_bid,
        best_ask, thin_side, buy_depth_xch, sell_depth_xch, etc.
        """
        return self.get_competitor_spread()

    def get_spread_recommendation(self, side: str, our_spread_bps: Decimal,
                                   mid_price: Decimal) -> Decimal:
        """Get a spread recommendation based on competitor analysis.

        Strategy:
        - If competitors are wider than us → we can widen slightly (more profit per fill)
        - If competitors are tighter → we might tighten to stay competitive
        - If the side is thin → tighten that side (more fills when liquidity is scarce)
        - Never go below MIN_EDGE_BPS regardless

        Returns recommended spread adjustment as BPS (positive = widen, negative = tighten).
        """
        with self._lock:
            comp_spread = self._competitors["competitor_spread_bps"]
            thin_side = self._competitors["thin_side"]
            best_bid = self._competitors["best_bid"]
            best_ask = self._competitors["best_ask"]

        def _dec(value, default="0"):
            if value is None:
                return Decimal(default)
            if isinstance(value, Decimal):
                return value
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                return Decimal(default)

        comp_spread = _dec(comp_spread)
        mid_price = _dec(mid_price)
        our_spread_bps = _dec(our_spread_bps)
        best_bid = _dec(best_bid)
        best_ask = _dec(best_ask)

        if comp_spread <= 0 or mid_price <= 0:
            return Decimal("0")  # No data, no adjustment

        # Calculate how our spread compares to competitors
        spread_diff = comp_spread - our_spread_bps

        adjustment = Decimal("0")

        # If competitors are much wider (>200 BPS wider), we can widen a bit
        if spread_diff > Decimal("200"):
            # Widen by up to 25% of the gap (conservative — don't match them fully)
            adjustment = spread_diff * Decimal("0.25")
            # Cap widening at 200 BPS
            adjustment = min(adjustment, Decimal("200"))

        # If competitors are tighter (we're wider), consider tightening
        elif spread_diff < Decimal("-100"):
            # Tighten by up to 30% of the gap
            adjustment = spread_diff * Decimal("0.30")
            # Cap tightening at -150 BPS
            adjustment = max(adjustment, Decimal("-150"))

        # Thin side bonus: if this side has less liquidity, tighten to attract fills
        if thin_side == side and thin_side != "":
            adjustment -= Decimal("50")  # Tighten by 50 BPS on thin side

        # Price undercutting: if a competitor is very close to us, be slightly better
        if side == "buy" and best_bid > 0 and mid_price > 0:
            our_bid = mid_price * (Decimal("1") - our_spread_bps / Decimal("10000"))
            if best_bid > 0 and abs(our_bid - best_bid) / mid_price * Decimal("10000") < Decimal("20"):
                # We're within 20 BPS of a competitor — nudge tighter
                adjustment -= Decimal("15")

        elif side == "sell" and best_ask > 0 and mid_price > 0:
            our_ask = mid_price * (Decimal("1") + our_spread_bps / Decimal("10000"))
            if best_ask > 0 and abs(our_ask - best_ask) / mid_price * Decimal("10000") < Decimal("20"):
                adjustment -= Decimal("15")

        return adjustment

    # -------------------------------------------------------------------
    # DBX Rewards Tracking
    # -------------------------------------------------------------------

    def check_dbx_eligibility(self, our_spread_bps: Decimal,
                                mid_price: Decimal) -> Dict:
        """Check if our offers qualify for Dexie's DBX liquidity rewards.

        Dexie's DBX program rewards market makers who provide liquidity
        within a certain spread of the mid price. Tighter spreads earn more.

        Returns eligibility info.
        """
        now = time.time()
        if now - self._dbx["last_check"] < self._dbx_check_interval:
            return dict(self._dbx)

        self._dbx["last_check"] = now

        # DBX eligibility rules (based on Dexie documentation):
        # - Offers must be within the eligible spread (typically 2-5% depending on pair)
        # - Both buy and sell offers needed (two-sided market making)
        # - Larger size = more rewards
        # - Tighter spread = more rewards

        # Estimate the max eligible spread (Dexie typically uses ~500 BPS for small caps)
        max_eligible = getattr(cfg, "DBX_MAX_SPREAD_BPS", Decimal("500"))
        self._dbx["max_eligible_spread"] = max_eligible

        if our_spread_bps <= max_eligible:
            self._dbx["eligible_offers"] = 1  # Simplified — we're eligible
            # Estimated reward rate scales inversely with spread
            if max_eligible > 0:
                efficiency = (max_eligible - our_spread_bps) / max_eligible
                self._dbx["estimated_dbx_rate"] = max(Decimal("0"), efficiency * Decimal("10"))
        else:
            self._dbx["eligible_offers"] = 0
            self._dbx["estimated_dbx_rate"] = Decimal("0")

        return dict(self._dbx)

    # -------------------------------------------------------------------
    # Market Summary (for GUI)
    # -------------------------------------------------------------------

    def get_market_summary(self) -> Dict:
        """Get a complete market intelligence summary for the GUI.

        Returns all the intelligence we've gathered in a single dict.
        """
        with self._lock:
            competitors = dict(self._competitors)

        # Serialize Decimals to strings for JSON
        serialized = {}
        for k, v in competitors.items():
            if isinstance(v, Decimal):
                serialized[k] = str(v)
            elif isinstance(v, list):
                serialized[k] = v
            else:
                serialized[k] = v

        serialized["orderbook_age_secs"] = round(
            time.time() - self._orderbook.get("last_refresh", 0), 1
        )
        serialized["orderbook_refreshes"] = self._orderbook.get("refresh_count", 0)
        serialized["orderbook_errors"] = self._orderbook.get("errors", 0)

        # Add DBX info
        serialized["dbx"] = {
            "eligible": self._dbx.get("eligible_offers", 0) > 0,
            "max_spread_bps": str(self._dbx.get("max_eligible_spread", "0")),
            "estimated_rate": str(self._dbx.get("estimated_dbx_rate", "0")),
        }

        return serialized

    def get_orderbook_snapshot(self) -> Dict:
        """Return a compact snapshot of the current cached Dexie orderbook."""
        with self._lock:
            buy_offers = list(self._orderbook.get("buy_offers", []))
            sell_offers = list(self._orderbook.get("sell_offers", []))

        our_buys = [offer for offer in buy_offers if offer.get("is_ours")]
        our_sells = [offer for offer in sell_offers if offer.get("is_ours")]

        our_best_bid = max((offer["price"] for offer in our_buys), default=Decimal("0"))
        our_best_ask = min((offer["price"] for offer in our_sells), default=Decimal("0"))

        return {
            "buy_count": len(buy_offers),
            "sell_count": len(sell_offers),
            "our_buy_count": len(our_buys),
            "our_sell_count": len(our_sells),
            "page_size": int(self._orderbook_page_size or 0),
            "buy_truncated": len(buy_offers) >= int(self._orderbook_page_size or 0),
            "sell_truncated": len(sell_offers) >= int(self._orderbook_page_size or 0),
            "our_best_bid": str(our_best_bid),
            "our_best_ask": str(our_best_ask),
        }

    def get_stats(self) -> Dict:
        """Get stats for the bot state endpoint."""
        return {
            "competitor_spread_bps": str(self._competitors.get("competitor_spread_bps", "0")),
            "best_bid": str(self._competitors.get("best_bid", "0")),
            "best_ask": str(self._competitors.get("best_ask", "0")),
            "buy_depth_xch": str(self._competitors.get("buy_depth_xch", "0")),
            "sell_depth_xch": str(self._competitors.get("sell_depth_xch", "0")),
            "thin_side": self._competitors.get("thin_side", ""),
        }

    def reset_session_stats(self):
        """Reset per-run market-intel counters for a fresh run."""
        with self._lock:
            self._orderbook["refresh_count"] = 0
            self._orderbook["errors"] = 0
            self._known_dexie_ids.clear()
            self._dbx["eligible_offers"] = 0
            self._dbx["estimated_dbx_rate"] = Decimal("0")
            self._dbx["last_check"] = 0

