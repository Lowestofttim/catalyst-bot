#!/usr/bin/env python3
"""
Test Runner — Runs all test suites in isolated batches.

Why batches? Some test files import heavy modules (api_server, wallet_sage)
with side effects (Flask app startup, TLS cert generation, emoji print
statements) that corrupt pytest's process-wide state when run together.
Running in batches avoids module-cache contamination.

Usage (original pytest runner):
    python run_tests.py           # Run all pytest batches
    python run_tests.py --quick   # Run only the fast pytest batches (skip slow ones)

Usage (simulation test suite — pass --sim flag to activate):
    python run_tests.py --sim                  Quick mode: 50 parametric + 42 API + 10 stress
    python run_tests.py --sim --full           All 5,600 parametric scenarios (~5 min)
    python run_tests.py --sim --api            API logic tests only
    python run_tests.py --sim --stress         Stress tests only
    python run_tests.py --sim --matrix         Parametric matrix only (50 quick scenarios)
    python run_tests.py --sim --list           Print all 5,600+ simulation test names
    python run_tests.py --sim --scenario NAME  Run one named simulation test
    python run_tests.py --sim --seed 123       Use specific seed for random subset
    python run_tests.py --sim --replay PATH    Replay a real bot log file
"""

import os
import subprocess
import sys
import time

# Each batch is isolated: tests within a batch share a process,
# but each batch runs in a fresh Python process.
BATCHES = [
    {
        "name": "New upgrade modules",
        "files": [
            "test_config_validator.py",
            "test_offer_lifecycle.py",
            "test_event_taxonomy.py",
            "test_reservation_manager.py",
            "test_doctor.py",
        ],
    },
    {
        "name": "Bot loop (status mapping)",
        "files": [
            "test_bot_loop_sage_status_mapping.py",
        ],
    },
    {
        "name": "Bot loop (recovery mode)",
        "files": [
            "test_bot_loop_recovery_mode.py",
        ],
    },
    {
        "name": "Bot loop (probe anchor)",
        "files": [
            "test_bot_loop_probe_anchor.py",
        ],
    },
    {
        "name": "Database + fills",
        "files": [
            "test_database_verified_fills.py",
            "test_database_reconcile_cat_tiers.py",
            "test_fill_tracker_verification.py",
            "test_hidden_coins.py",
        ],
    },
    {
        "name": "Coin manager",
        "files": [
            "test_coin_manager_exact_selectable.py",
            "test_coin_manager_fee_pool.py",
            "test_coin_manager_sage_snapshot.py",
            "test_coin_manager_topup_fail_closed.py",
        ],
    },
    {
        "name": "Offer tests",
        "files": [
            "test_offer_create.py",
            "test_offer_manager_coin_ids.py",
        ],
        "slow": True,
    },
    {
        "name": "Wallet sage",
        "files": [
            "test_wallet_sage_cancel_batch.py",
            "test_wallet_sage_login.py",
            "test_wallet_sage_signing_guard.py",
            "test_wallet_sage_spendable_views.py",
            "test_wallet_sage_startup_readiness.py",
            "test_wallet_sync_fail_closed.py",
        ],
    },
    {
        "name": "Coin prep",
        "files": [
            "test_coin_prep.py",
            "test_coin_prep_confirmed_views.py",
            "test_coin_prep_split_retry.py",
            "test_coin_prep_v2.py",
        ],
    },
    {
        "name": "API + security",
        "files": [
            "test_api_local_guard.py",
            "test_security_guardrails_source.py",
        ],
        "slow": True,
    },
    {
        "name": "Sniper",
        "files": [
            "test_sniper_coin_ids.py",
        ],
    },
    {
        "name": "Risk manager",
        "files": [
            "test_risk_manager_snapshot.py",
        ],
    },
    {
        "name": "Remaining",
        "files": [
            "test_runtime_monitor.py",
            "test_tier_group_counts.py",
            "test_market_intel_orderbook.py",
            "test_splash_receive.py",
            "test_tx_fees.py",
            "test_spacescan_verify_fill.py",
            "test_market_data_collector_spacescan.py",
        ],
    },
]

# Not included (standalone scripts, not pytest suites):
#   test_parallel_offers.py   — live Sage integration test
#   test_spacescan.py         — live Spacescan diagnostic
#   test_api_data_sources.py  — live API diagnostic
#   test_all_apis.py          — live API diagnostic


def _run_simulation_tests() -> int:
    """Delegate to the simulation test runner when --sim flag is present.

    Strips '--sim' from sys.argv and passes the rest to the simulation runner,
    which is implemented inline here to avoid a separate file dependency.

    Returns:
        Exit code from the simulation test runner.
    """
    # Remove --sim from argv so the simulation runner doesn't see it
    sim_argv = [a for a in sys.argv[1:] if a != "--sim"]

    _ROOT = os.path.dirname(os.path.abspath(__file__))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    # Lazy imports — only needed when --sim flag is used
    import argparse

    parser = argparse.ArgumentParser(
        description="CATalyst — Simulation Test Suite",
        add_help=True,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full", action="store_true", help="Run all 5,600 parametric scenarios."
    )
    mode.add_argument(
        "--quick",
        action="store_true",
        help="Run 50 most important parametric scenarios only.",
    )
    mode.add_argument("--api", action="store_true", help="Run API tests only.")
    mode.add_argument("--stress", action="store_true", help="Run stress tests only.")
    mode.add_argument(
        "--matrix", action="store_true", help="Run parametric matrix only (50 quick)."
    )
    mode.add_argument(
        "--list", action="store_true", help="Print all test names (1000+) and exit."
    )
    mode.add_argument(
        "--scenario", metavar="NAME", help="Run a single named simulation test."
    )
    mode.add_argument("--replay", metavar="PATH", help="Replay a real bot log file.")
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for subset selection (default: 42).",
    )

    args = parser.parse_args(sim_argv)

    # --list
    if args.list:
        _sim_list_all_names()
        return 0

    # --replay
    if args.replay:
        try:
            import run_simulation

            run_simulation._run_replay(args.replay)
        except Exception as exc:
            print(f"ERROR running replay: {exc}")
        return 0

    # --scenario
    if args.scenario:
        return _sim_run_single(args.scenario)

    # Determine modes
    run_parametric = not (args.api or args.stress)
    run_api = not (args.matrix or args.stress or args.quick)
    run_stress_flag = not (args.matrix or args.api or args.quick)
    full_matrix = args.full

    # Count plan
    n_mat = 5600 if full_matrix else 50
    n_api = 42 if run_api else 0
    n_str = 10 if run_stress_flag else 0
    n_plan = (n_mat if run_parametric else 0) + n_api + n_str

    print()
    print("CATalyst — Simulation Test Suite")
    print("=" * 42)
    parts = []
    if run_parametric:
        parts.append(f"{'all 5,600' if full_matrix else '50'} parametric")
    if run_api:
        parts.append("42 API")
    if run_stress_flag:
        parts.append("10 stress")
    print(f"  {' + '.join(parts)} = {n_plan} tests")
    print()

    t0 = time.time()
    mx_p = mx_f = None
    mx_fail_list = []
    ap_p = ap_f = None
    ap_fail_list = []
    st_p = st_f = None
    st_fail_list = []

    if run_parametric:
        print("Running parametric matrix...", flush=True)
        mx_p, mx_f, mx_fail_list = _sim_run_matrix(full=full_matrix, seed=args.seed)
        print(f"  Parametric: {mx_p}/{mx_p + mx_f} passed")

    if run_api:
        print("Running API logic tests...", flush=True)
        ap_p, ap_f, ap_fail_list = _sim_run_api()
        print(f"  API tests: {ap_p}/{ap_p + ap_f} passed")

    if run_stress_flag:
        st_p, st_f, st_fail_list = _sim_run_stress()
        print(f"  Stress: {st_p}/{st_p + st_f} passed")

    elapsed = time.time() - t0
    return _sim_print_summary(
        elapsed,
        mx_p,
        mx_f,
        mx_fail_list,
        ap_p,
        ap_f,
        ap_fail_list,
        st_p,
        st_f,
        st_fail_list,
    )


def _sim_list_all_names() -> None:
    """Print every simulation test name."""
    from simulation.test_matrix import get_test_names
    from simulation.stress_tests import ALL_STRESS_TESTS
    from simulation.api_tests import ALL_API_TESTS

    for n in get_test_names():
        print(n)
    for fn in ALL_API_TESTS:
        print(fn.__name__)
    for st in ALL_STRESS_TESTS:
        print(st.name)
    try:
        from simulation.scenarios import ALL_SCENARIOS

        for sc in ALL_SCENARIOS:
            print(f"scenario_{sc.name}")
    except Exception:
        pass


def _sim_run_single(name: str) -> int:
    """Run one named simulation test."""
    from simulation.stress_tests import ALL_STRESS_TESTS

    for st in ALL_STRESS_TESTS:
        if st.name == name:
            result = st.run()
            print(
                f"{'PASS' if result.passed else 'FAIL'} {result.name}: {result.reason or 'ok'}"
            )
            for k, v in result.metrics.items():
                print(f"  {k}: {v}")
            return 0 if result.passed else 1

    if name.startswith("matrix_"):
        from simulation.test_matrix import generate_matrix, run_matrix_scenario

        for ms in generate_matrix():
            if ms.name == name:
                result = run_matrix_scenario(ms)
                status = "PASS" if result["passed"] else "FAIL"
                print(f"{status} {name}: {result.get('fail_reason', 'ok')}")
                return 0 if result["passed"] else 1
        print(f"ERROR: '{name}' not found. Use --sim --list.")
        return 1

    try:
        from simulation.scenarios import SCENARIO_MAP
        from simulation.runner import run_scenario

        if name in SCENARIO_MAP:
            result = run_scenario(SCENARIO_MAP[name], verbose=True)
            print(f"P&L: {result.pnl_xch:+.6f} XCH  Fills: {result.total_fills}")
            return 0
    except Exception:
        pass

    print(f"ERROR: Unknown scenario '{name}'. Use --sim --list.")
    return 1


def _sim_run_matrix(full: bool = False, seed: int = 42):
    """Run parametric matrix. Returns (passed, failed, failures)."""
    from simulation.test_matrix import (
        generate_matrix,
        generate_quick,
        run_matrix_scenario,
    )

    scenarios = generate_matrix() if full else generate_quick(n=50)
    total = len(scenarios)
    passed = failed = 0
    failures = []
    for i, ms in enumerate(scenarios):
        _sim_progress(i, total, ms.name[:30])
        result = run_matrix_scenario(ms)
        if result["passed"]:
            passed += 1
        else:
            failed += 1
            failures.append(result)
    _sim_clear()
    return passed, failed, failures


def _sim_run_api():
    """Run API tests. Returns (passed, failed, failures)."""
    from simulation.api_tests import run_all_api_tests

    results = run_all_api_tests()
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    failures = [
        {"name": r.name, "fail_reason": r.reason} for r in results if not r.passed
    ]
    return passed, failed, failures


def _sim_run_stress():
    """Run stress tests. Returns (passed, failed, failures)."""
    from simulation.stress_tests import run_all_stress_tests

    print("  Running stress tests (may take up to 60s)...", flush=True)
    results = run_all_stress_tests()
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    failures = [
        {"name": r.name, "fail_reason": r.reason} for r in results if not r.passed
    ]
    return passed, failed, failures


def _sim_progress(done: int, total: int, label: str = "") -> None:
    filled = int(20 * done / max(total, 1))
    bar = "#" * filled + "." * (20 - filled)
    line = f"[{bar}] {done}/{total} {label}"
    print(f"\r{line:<72}", end="", flush=True)


def _sim_clear() -> None:
    print(f"\r{' ' * 72}\r", end="", flush=True)


def _sim_print_summary(
    elapsed, mx_p, mx_f, mx_fl, ap_p, ap_f, ap_fl, st_p, st_f, st_fl
):
    """Print summary table. Returns exit code."""
    total_p = total_f = 0
    all_fl = []
    print()
    print("CATalyst — Simulation Test Suite")
    print("=" * 42)
    rows = []
    if mx_p is not None:
        rows.append(("PARAMETRIC", mx_p, mx_f))
        total_p += mx_p
        total_f += mx_f
        all_fl.extend(mx_fl)
    if ap_p is not None:
        rows.append(("API TESTS", ap_p, ap_f))
        total_p += ap_p
        total_f += ap_f
        all_fl.extend(ap_fl)
    if st_p is not None:
        rows.append(("STRESS", st_p, st_f))
        total_p += st_p
        total_f += st_f
        all_fl.extend(st_fl)
    for label, p, f in rows:
        t = p + f
        tag = "ok" if f == 0 else f"{f} fail"
        print(f"  {label:<12}: {p}/{t} pass  ({tag})")
    print(f"  {'-' * 28}")
    grand = total_p + total_f
    pct = 100 * total_p / max(grand, 1)
    print(f"  {'TOTAL':<12}: {total_p}/{grand} ({pct:.1f}%)")
    print(f"  Elapsed     : {elapsed:.1f}s")
    print()
    if all_fl:
        print("FAILURES:")
        for f_dict in all_fl[:20]:
            name = f_dict.get("name", "?")
            reason = f_dict.get("fail_reason", "")
            print(f"  x {name}")
            if reason:
                print(f"      {reason}")
        if len(all_fl) > 20:
            print(f"  ... and {len(all_fl) - 20} more")
        print()
    return 0 if total_f == 0 else 1


def main():
    # If --sim flag present, delegate entirely to the simulation runner
    if "--sim" in sys.argv:
        sys.exit(_run_simulation_tests())

    quick = "--quick" in sys.argv
    total_passed = 0
    total_failed = 0
    total_errors = 0
    failed_batches = []

    start = time.time()

    for batch in BATCHES:
        if quick and batch.get("slow"):
            print(f"  SKIP  {batch['name']} (slow, use without --quick)")
            continue

        cmd = (
            [sys.executable, "-m", "pytest"]
            + batch["files"]
            + [
                "--tb=line",
                "-q",
                "--no-header",
            ]
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )

        # Parse last line for counts
        last_line = (result.stdout.strip().split("\n") or [""])[-1]
        passed = 0
        failed = 0
        errors = 0
        for part in last_line.split(","):
            part = part.strip()
            if "passed" in part:
                try:
                    passed = int(part.split()[0])
                except (ValueError, IndexError):
                    pass
            elif "failed" in part:
                try:
                    failed = int(part.split()[0])
                except (ValueError, IndexError):
                    pass
            elif "error" in part:
                try:
                    errors = int(part.split()[0])
                except (ValueError, IndexError):
                    pass

        total_passed += passed
        total_failed += failed
        total_errors += errors

        status = "PASS" if failed == 0 and errors == 0 else "FAIL"
        icon = "OK" if status == "PASS" else "!!"
        print(
            f"  [{icon}] {batch['name']}: {passed} passed"
            + (f", {failed} failed" if failed else "")
            + (f", {errors} errors" if errors else "")
        )

        if failed > 0 or errors > 0:
            failed_batches.append(batch["name"])
            # Show failure details
            for line in result.stdout.split("\n"):
                if "FAILED" in line or "ERROR" in line:
                    print(f"       {line.strip()}")

    elapsed = time.time() - start
    print()
    print(f"{'=' * 50}")
    print(
        f"  Total: {total_passed} passed, {total_failed} failed, "
        f"{total_errors} errors ({elapsed:.1f}s)"
    )
    if failed_batches:
        print(f"  Failed batches: {', '.join(failed_batches)}")
    else:
        print("  All batches passed!")
    print(f"{'=' * 50}")

    sys.exit(1 if (total_failed + total_errors) > 0 else 0)


if __name__ == "__main__":
    main()
