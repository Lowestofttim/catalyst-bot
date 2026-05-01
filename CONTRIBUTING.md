# Contributing to CATalyst

Thanks for taking an interest. CATalyst is beta software controlling live trading wallets, so contributions (reports, ideas, expert reviews, or code) are genuinely valued.

## Where different things go

| What you want to do | Where |
|---|---|
| Ask a question with a concrete answer | [Discussions → Q&A](https://github.com/Lowestofttim/catalyst-bot/discussions/categories/q-a) |
| Report a confirmed bug | [Issues](https://github.com/Lowestofttim/catalyst-bot/issues) |
| Propose a new feature or design change | [Discussions → Ideas](https://github.com/Lowestofttim/catalyst-bot/discussions/categories/ideas) |
| Share a config, dashboard, or result | [Discussions → Show and tell](https://github.com/Lowestofttim/catalyst-bot/discussions/categories/show-and-tell) |
| Offer expert review (Chialisp, wallet security, MM theory, etc.) | [Discussions → General](https://github.com/Lowestofttim/catalyst-bot/discussions/categories/general) |
| Submit a code change | Open a pull request (see below) |

**Rule of thumb:** Issues are for things we will close. Discussions are for things that stay open-ended. If you're unsure, start in Discussions; a maintainer will promote it to an Issue if it fits there.

## Reporting bugs

Include:
- **Version.** Run `Help → About` or check the `Catalyst-Setup-*.exe` filename.
- **OS.** Windows 10/11, macOS, or Linux.
- **Wallet.** Sage version.
- **CAT pair.** Asset ID or ticker.
- **What you expected vs. what happened.**
- **Logs.** Tail of `%APPDATA%\Catalyst\bot_superlog_*.log` (redact asset IDs if you prefer).

Never paste wallet certs, private keys, or the contents of `.env` or `user_secrets.json`.

## Suggesting features

Start an **Idea** discussion describing:
- The problem you're solving (not just the solution)
- Current behaviour you'd replace
- Rough sketch of how it would work

Maintainers will convert accepted Ideas into tracked Issues.

## Submitting code

1. Fork the repo and create a branch from `main`.
2. Use Python 3.12, which is what CI and the release builds use.
3. Follow the project conventions below.
4. Install developer dependencies: `python -m pip install -r requirements-dev.txt`.
5. Run the tests: `python -m pytest tests -q --ignore=tests/test_coin_prep.py --ignore=tests/test_coin_prep_v2.py --ignore=tests/test_offer_create.py`.
6. Run the static checks: `python -m ruff check . --select E9,F821`, `python -m bandit -r src --ini .bandit -ll`, `python scripts/check_env_example.py`, and `python scripts/check_tracked_secrets.py`.
7. Open a PR with a clear description of **why** the change is needed.

Core conventions:

- Use `Decimal` for prices and amounts; coin amounts are mojos as integers.
- Use `from config import cfg` for settings and `from super_log import slog` for logging.
- Keep database access inside `database.py`; do not add raw SQL in other modules.
- Use the `wallet.py` adapter for wallet calls instead of importing wallet backends directly.
- App bridge methods should return `{"success": True/False, ...}` dictionaries rather than raising into JavaScript.
- Escape server-sourced data before rendering it into HTML; prefer event delegation over inline handlers.

Small, focused PRs get reviewed faster. If you're unsure whether an approach will be accepted, open a Discussion first.

## Expert review welcome

If you know Chia deeply (coin-set model, Chialisp, offer mechanics), market microstructure, Python security hardening, or desktop app packaging, please look at whatever subset interests you and say what you'd do differently. Post in **General** or tag the maintainer in a relevant file.

## Ground rules

- Be specific. Vague observations are hard to act on.
- Be kind. Beta software, live money, honest mistakes.
- Redact secrets. Asset IDs and puzzle hashes are public; wallet cert paths and seeds are not.

## License

By contributing you agree your contributions are licensed under the MIT License (see [LICENSE](LICENSE)).
