"""SuperLog management routes: stats, runtime level change, archive, download.

Four read-only (plus one level-change POST) routes that expose the
super_log module to the GUI. No shared-state dependencies — each route
calls a super_log helper directly.
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request, send_file

import api_server


bp = Blueprint("superlog", __name__)


@bp.route("/api/superlog/stats")
def api_superlog_stats():
    """Get superlog statistics — file size, level, error dump count."""
    try:
        from super_log import get_log_stats
        return jsonify(get_log_stats())
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/superlog/level", methods=["POST"])
def api_superlog_level():
    """Change superlog file/terminal level at runtime.

    POST {"file_level": "trace"} to enable verbose logging for debugging.
    POST {"file_level": "info"} to go back to quiet mode.
    """
    try:
        data = request.get_json(force=True) or {}
        from super_log import set_file_level, set_terminal_level, get_log_stats
        if "file_level" in data:
            set_file_level(data["file_level"])
        if "terminal_level" in data:
            set_terminal_level(data["terminal_level"])
        return jsonify({"ok": True, **get_log_stats()})
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/superlog/archive")
def api_superlog_archive():
    """Get archived digests from past log sessions.

    Shows error history, fill counts, and cycle stats from rotated logs.
    Useful for seeing what happened days/weeks ago without keeping full logs.
    Query param: ?last=20 (default 10)
    """
    try:
        from super_log import get_archive_summary
        last_n = request.args.get("last", 10, type=int)
        return jsonify(get_archive_summary(last_n=min(last_n, 100)))
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/superlog/download")
def api_superlog_download():
    """Download the current superlog file directly."""
    try:
        from super_log import get_log_path
        log_path = get_log_path()
        if log_path and os.path.exists(log_path):
            return send_file(log_path, mimetype="text/plain",
                             as_attachment=True,
                             download_name=os.path.basename(log_path))
        return jsonify({"error": "No superlog file found"}), 404
    except Exception as e:
        return api_server._api_error(e, request.path)
