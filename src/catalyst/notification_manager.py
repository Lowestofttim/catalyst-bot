"""Native OS notifications with per-category rate-limiting

Wraps `plyer` to deliver desktop notifications for meaningful bot events —
Windows toast, macOS Notification Center, Linux libnotify — with a
per-category cooldown so a burst of events never floods the user. If
`plyer` is not installed the manager degrades to a no-op so the rest of
the bot continues to run.

Key responsibilities:
    - Deliver notifications via the current OS's native mechanism
    - Enforce a per-category enable flag and minimum-gap cooldown
    - Expose categories `fill`, `error`, `circuit_breaker`, `sniper`,
      `coin_prep`, `price_alert`, and `info`
    - Fail silently when the `plyer` dependency is missing

Categories and cooldowns are configurable; defaults are chosen to favour
signal over noise (short cooldown on fills, longer on errors and breakers).
"""

import time
import threading


# Attempt import — graceful fail if not installed
try:
    from plyer import notification as plyer_notification
    PLYER_AVAILABLE = True
except ImportError:
    PLYER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Default category settings
# ---------------------------------------------------------------------------
DEFAULT_CATEGORIES = {
    "fill": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 5,       # Min gap between fill notifications
    },
    "error": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 30,      # Don't spam error notifications
    },
    "circuit_breaker": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 60,
    },
    "sniper": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 10,
    },
    "coin_prep": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 300,     # Coin prep is infrequent
    },
    "price_alert": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 120,
    },
    "info": {
        "enabled": True,
        "title_prefix": "",
        "cooldown_secs": 10,
    },
}


class NotificationManager:
    """
    Sends native OS notifications with rate limiting and category control.

    Usage:
        notifier = NotificationManager(app_name="CATalyst")

        # Send a notification
        notifier.notify(
            title="Offer Filled",
            message="Sold 500 MZ at 0.000345 XCH",
            category="fill"
        )

        # Disable a category
        notifier.set_category_enabled("info", False)

        # Disable ALL notifications
        notifier.enabled = False
    """

    def __init__(self, app_name: str = "CATalyst"):
        if not PLYER_AVAILABLE:
            raise ImportError("plyer is not installed")

        self.app_name = app_name
        self.enabled = True  # Master switch

        # Per-category settings (copy defaults so we don't mutate the module-level dict)
        self._categories = {}
        for key, val in DEFAULT_CATEGORIES.items():
            self._categories[key] = dict(val)

        # Rate limiting: last notification time per category
        self._last_sent = {}
        self._lock = threading.Lock()

    def notify(self, title: str, message: str, category: str = "info",
               timeout: int = 10):
        """
        Send a native notification.

        title:    Notification title
        message:  Notification body text
        category: Category key (fill, error, circuit_breaker, sniper, etc.)
        timeout:  Seconds before notification auto-dismisses (platform-dependent)
        """
        if not self.enabled:
            return False

        if not PLYER_AVAILABLE:
            return False

        # Check category settings
        cat_settings = self._categories.get(category, self._categories.get("info", {}))
        if not cat_settings.get("enabled", True):
            return False

        # Rate limiting — check cooldown
        cooldown = cat_settings.get("cooldown_secs", 10)
        now = time.time()

        with self._lock:
            last = self._last_sent.get(category, 0)
            if now - last < cooldown:
                return False  # Too soon, skip
            self._last_sent[category] = now

        # Build title with optional prefix
        prefix = cat_settings.get("title_prefix", "")
        full_title = f"{prefix}{title}" if prefix else title

        # Send notification in a background thread (plyer can block briefly)
        thread = threading.Thread(
            target=self._send,
            args=(full_title, message, timeout),
            daemon=True
        )
        thread.start()
        return True

    def set_category_enabled(self, category: str, enabled: bool):
        """Enable or disable notifications for a specific category."""
        if category in self._categories:
            self._categories[category]["enabled"] = enabled

    def get_categories(self) -> dict:
        """Return current category settings (for settings UI)."""
        return {k: dict(v) for k, v in self._categories.items()}

    def _send(self, title: str, message: str, timeout: int):
        """Actually send the notification (runs in background thread)."""
        try:
            plyer_notification.notify(
                title=title,
                message=message,
                app_name=self.app_name,
                timeout=timeout,
            )
        except Exception:
            # Notification failure is never critical — silently ignore
            pass

