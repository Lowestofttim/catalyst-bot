@app.route("/api/coins")
def api_coins():
    """Get coin status.

    F62 (2026-04-09): refresh inventory on-demand so the dashboard
    reflects the current wallet state even when the bot isn't running.
    Without this, the in-memory inventory dict stays at whatever the
    last loop tick captured — typically all-zero on a fresh session,
    or stale post-coin-prep until the user starts the bot. The refresh
    is guarded against running during coin prep / topup so it doesn't
    race with the worker.
    """
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # On-demand refresh when the bot isn't running (so the dashboard
    # shows accurate numbers after coin prep finishes). When the bot IS
    # running, its loop refreshes every tick, so skip the extra RPC.
    #
    # Also reap the coin_prep subprocess here — only the bot loop normally
    # calls check_coin_prep_status(), so a manual prep while the bot is
    # stopped leaves ``_prep_running`` pinned True until the next bot
    # start. That blocks the on-demand refresh below and any second prep
    # attempt. Reaping it here lets the dashboard recover without a
    # bot restart.
    try:
        if not bot.is_running():
            bot.coin_manager.check_coin_prep_status()
            bot.coin_manager.update_coin_counts()
    except Exception as _refresh_err:
        # Don't fail the endpoint if the refresh glitches; the cached
        # status is still better than a 500.
        log_event("debug", "api_coins_refresh_failed",
                  f"On-demand coin refresh failed: {_refresh_err}")

    return jsonify(bot.coin_manager.get_status())


@app.route("/api/coins/topup", methods=["POST"])
def api_coin_topup():
    """Manually trigger coin topup."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Block if bot is live — topup splits coins and races with offer creation
    if bot.is_running():
        return jsonify({
            "error": "Stop the bot before manual top-up. "
                     "The bot handles top-up automatically while running.",
            "requires_stop": True,
        }), 409

    open_buys = bot.offer_manager.get_open_offer_count("buy")
    open_sells = bot.offer_manager.get_open_offer_count("sell")

    started = bot.coin_manager.start_topup(open_buys, open_sells)
    return jsonify({"status": "started" if started else "already_running"})


@app.route("/api/coins/prep", methods=["POST"])
