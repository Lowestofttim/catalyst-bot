import json
from unittest.mock import MagicMock, patch

import coin_prep_worker


def _worker():
    worker = coin_prep_worker.CoinPrepWorker.__new__(coin_prep_worker.CoinPrepWorker)
    worker.log = MagicMock()
    worker.update_status = MagicMock()
    return worker


def test_coin_prep_cancel_all_cancels_every_open_offer(tmp_path):
    worker = _worker()
    worker.get_all_open_offers_rpc = MagicMock(
        side_effect=[
            [{"id": "trade-a"}, {"id": "trade-b"}],
            [],
        ]
    )
    cancelled_file = tmp_path / "worker_cancelled_ids.json"

    with (
        patch("user_paths.worker_cancelled_ids_file", return_value=str(cancelled_file)),
        patch(
            "coin_prep_worker.cancel_offers_batch",
            return_value={
                "trade-a": {"success": True},
                "trade-b": {"success": True},
            },
        ) as batch,
        patch("coin_prep_worker.time.sleep"),
    ):
        assert worker.cancel_all_offers() is True

    batch.assert_called_once_with(["trade-a", "trade-b"], secure=True)
    payload = json.loads(cancelled_file.read_text(encoding="utf-8"))
    assert payload["cancelled_ids"] == ["trade-a", "trade-b"]


def test_coin_prep_cancel_all_fails_if_offers_remain_open(tmp_path):
    worker = _worker()
    worker.get_all_open_offers_rpc = MagicMock(return_value=[{"id": "trade-a"}])

    with (
        patch(
            "user_paths.worker_cancelled_ids_file",
            return_value=str(tmp_path / "worker_cancelled_ids.json"),
        ),
        patch(
            "coin_prep_worker.cancel_offers_batch",
            return_value={"trade-a": {"success": True}},
        ),
        patch("coin_prep_worker.time.sleep"),
    ):
        assert worker.cancel_all_offers() is False
