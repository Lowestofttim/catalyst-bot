import os
import time


def test_splash_install_path_lives_under_user_data():
    import splash_setup
    from user_paths import data_dir

    info = splash_setup.detect_platform()
    install_path = os.path.abspath(info["install_path"])

    assert install_path.startswith(os.path.abspath(data_dir()) + os.sep)
    assert os.path.dirname(os.path.abspath(splash_setup.__file__)) not in install_path


def test_splash_node_prefers_user_data_binary(monkeypatch):
    import splash_node
    from config import cfg
    from user_paths import data_dir

    binary_name = "splash.exe" if os.name == "nt" else "splash"
    splash_dir = os.path.join(data_dir(), "splash")
    os.makedirs(splash_dir, exist_ok=True)
    binary_path = os.path.join(splash_dir, binary_name)
    with open(binary_path, "wb") as fh:
        fh.write(b"fake splash binary")

    monkeypatch.setattr(cfg, "SPLASH_BINARY_PATH", "", raising=False)
    node = splash_node.SplashNode()

    assert os.path.abspath(node.find_binary()) == os.path.abspath(binary_path)


def test_splash_download_refuses_release_without_checksum(monkeypatch):
    import splash_setup

    info = splash_setup.detect_platform()
    requested_urls = []

    monkeypatch.delenv("CATALYST_ALLOW_UNVERIFIED_SPLASH_DOWNLOAD", raising=False)
    monkeypatch.setattr(
        splash_setup,
        "get_latest_release",
        lambda: {
            "tag": "v-test",
            "assets": [
                {
                    "name": info["asset_name"],
                    "size": 12,
                    "url": "https://example.invalid/splash",
                }
            ],
        },
    )

    def fake_get(url, *args, **kwargs):
        requested_urls.append(url)
        raise AssertionError("binary download should not start without checksum")

    monkeypatch.setattr(splash_setup.requests, "get", fake_get)

    result = splash_setup.download_splash()

    assert result["success"] is False
    assert "sha256" in result["message"].lower()
    assert requested_urls == []


def test_splash_node_offer_hook_uses_ipv4_loopback(monkeypatch):
    import splash_node
    from config import cfg

    captured = {}

    class FakeProcess:
        pid = 12345
        stdout = []

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return FakeProcess()

    monkeypatch.setattr(cfg, "SPLASH_RECEIVE_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "PORT", 5000, raising=False)
    monkeypatch.setattr(
        cfg, "SPLASH_SUBMIT_URL", "http://localhost:4000", raising=False
    )
    monkeypatch.setattr(cfg, "SPLASH_P2P_PORT", 11511, raising=False)
    monkeypatch.setattr(cfg, "SPLASH_METRICS_PORT", 4001, raising=False)
    monkeypatch.setattr(splash_node.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(splash_node.threading, "Thread", FakeThread)

    node = splash_node.SplashNode()
    node._binary_path = "splash.exe"
    node._kill_stale_process = lambda port: None

    node._launch_process()

    hook_index = captured["cmd"].index("--offer-hook") + 1
    assert captured["cmd"][hook_index] == "http://127.0.0.1:5000/api/splash/incoming"
    assert "http://localhost:5000/api/splash/incoming" not in captured["cmd"]


def test_splash_output_reader_keeps_reading_lines(monkeypatch):
    import splash_node

    class FakeProcess:
        stdout = iter(
            [
                "connected to peer one\n",
                "connected to peer two\n",
            ]
        )

    monkeypatch.setattr(splash_node, "log_event", lambda *args, **kwargs: None)
    node = splash_node.SplashNode()
    node._process = FakeProcess()
    node._last_start_time = time.time()

    node._read_output()

    assert node.get_recent_output(10) == [
        "connected to peer one",
        "connected to peer two",
    ]
