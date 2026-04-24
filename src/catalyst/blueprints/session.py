"""Session lifecycle routes: fresh-start and resume-chosen.

Two small routes that mediate between the resume-modal on the GUI and
the run-history cutoff bookkeeping in api_server. Delegates to the
session helpers (`_reset_fresh_run_session`, `_fresh_start_set/clear`)
that still live in api_server since they touch lots of module state.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import api_server
from database import log_event


bp = Blueprint("session", __name__)


@bp.route("/api/session/fresh-start", methods=["POST"])
def api_session_fresh_start():
    """Begin a brand new run without carrying forward old session state."""
    try:
        payload = api_server._reset_fresh_run_session(
            clear_coins=False,
            clear_price_history=False,
            clear_inventory=False,
            cancel_open_offers=False,
            reason="session_fresh_start",
        )
        # Persist the choice so check-resume returns can_resume=False on the
        # next page load, even though the old live offers are still in Sage.
        # Cleared automatically when the bot starts a new run.
        api_server._fresh_start_set()
        return jsonify({
            "success": True,
            "message": "Fresh run session started",
            **api_server._serialize_dict(payload),
        })
    except Exception as e:
        log_event("warning", "session_fresh_start_failed",
                  f"Failed to reset fresh run session: {e}")
        return api_server._api_error(e, request.path)


@bp.route("/api/session/resume-chosen", methods=["POST"])
def api_session_resume_chosen():
    """User explicitly chose 'Load Previous Session' — clear the fresh-start flag."""
    api_server._fresh_start_clear()
    return jsonify({"success": True})
