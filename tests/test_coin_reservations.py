"""Tests for the coin reservation registry."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import threading
import time

from coin_reservations import ReservationRegistry


class TestBasicReservation:
    def test_reserve_new_coins_succeeds(self):
        reg = ReservationRegistry()
        result = reg.reserve(
            ["aaa", "bbb", "ccc"],
            owner="test_owner",
            purpose="unit_test",
        )
        assert set(result) == {"aaa", "bbb", "ccc"}

    def test_reserving_already_reserved_coin_skips(self):
        reg = ReservationRegistry()
        reg.reserve(["aaa"], owner="first", purpose="a")
        result = reg.reserve(["aaa", "bbb"], owner="second", purpose="b")
        # "aaa" was already reserved by "first" — skipped.
        # "bbb" was free — reserved.
        assert set(result) == {"bbb"}

    def test_same_owner_refreshes_ttl(self):
        reg = ReservationRegistry()
        reg.reserve(["aaa"], owner="first", purpose="a", ttl_seconds=5)
        # Same owner re-reserves — should succeed (refresh)
        result = reg.reserve(["aaa"], owner="first", purpose="a", ttl_seconds=10)
        assert "aaa" in result

    def test_normalises_0x_prefix_and_case(self):
        reg = ReservationRegistry()
        reg.reserve(["0xABC"], owner="o", purpose="p")
        assert reg.is_reserved("abc")
        assert reg.is_reserved("0xABC")
        assert reg.is_reserved_by("abc", "o")


class TestRelease:
    def test_release_own_reservations(self):
        reg = ReservationRegistry()
        reg.reserve(["a", "b"], owner="me", purpose="p")
        released = reg.release(["a", "b"], owner="me")
        assert released == 2
        assert not reg.is_reserved("a")
        assert not reg.is_reserved("b")

    def test_release_doesnt_touch_others(self):
        reg = ReservationRegistry()
        reg.reserve(["a"], owner="alice", purpose="p")
        reg.reserve(["b"], owner="bob", purpose="p")
        released = reg.release(["a", "b"], owner="alice")
        # Only "a" (alice's) released; "b" stays reserved by bob
        assert released == 1
        assert reg.is_reserved("b")
        assert not reg.is_reserved("a")

    def test_release_by_owner_clears_all(self):
        reg = ReservationRegistry()
        reg.reserve(["a", "b", "c"], owner="me", purpose="p")
        released = reg.release_by_owner("me")
        assert released == 3
        assert not reg.is_reserved("a")
        assert not reg.is_reserved("b")
        assert not reg.is_reserved("c")


class TestExpiration:
    def test_expired_reservation_auto_released(self):
        reg = ReservationRegistry()
        reg.reserve(["a"], owner="me", purpose="p", ttl_seconds=1)
        # Fast-forward beyond TTL
        time.sleep(1.1)
        # is_reserved should lazily expire
        assert not reg.is_reserved("a")
        # And now a new owner can reserve it
        result = reg.reserve(["a"], owner="other", purpose="p")
        assert "a" in result

    def test_gc_expired_returns_count(self):
        reg = ReservationRegistry()
        reg.reserve(["a", "b"], owner="me", purpose="p", ttl_seconds=1)
        reg.reserve(["c"], owner="long", purpose="p", ttl_seconds=60)
        time.sleep(1.1)
        count = reg.gc_expired()
        assert count == 2  # a, b expired
        # c still reserved
        assert reg.is_reserved("c")


class TestFilterUnreserved:
    def test_returns_only_free_coins(self):
        reg = ReservationRegistry()
        reg.reserve(["a", "c"], owner="me", purpose="p")
        result = reg.filter_unreserved(["a", "b", "c", "d"])
        assert set(result) == {"b", "d"}


class TestThreadSafety:
    """Basic contention test — many threads reserving same pool of coins."""

    def test_no_double_reservation_under_contention(self):
        reg = ReservationRegistry()
        coins = [f"coin_{i}" for i in range(100)]
        results = []
        lock = threading.Lock()

        def worker(name):
            acquired = reg.reserve(coins, owner=name, purpose="p", ttl_seconds=30)
            with lock:
                results.append((name, acquired))

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Sum of all acquired coins across workers must equal 100 exactly —
        # no coin was acquired by two workers.
        all_acquired = []
        for _, acq in results:
            all_acquired.extend(acq)
        assert len(all_acquired) == len(set(all_acquired))
        assert len(all_acquired) == 100


class TestStats:
    def test_stats_reflects_activity(self):
        reg = ReservationRegistry()
        reg.reserve(["a", "b"], owner="me", purpose="p")
        reg.reserve(["a"], owner="other", purpose="p")  # contested
        reg.release(["a"], owner="me")

        s = reg.stats()
        assert s["currently_reserved"] == 1  # b remains
        assert s["total_reserved"] == 2  # a, b
        assert s["total_released"] == 1  # a
        assert s["total_contested"] == 1  # other couldn't get a
