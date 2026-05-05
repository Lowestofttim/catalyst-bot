from pathlib import Path


def test_windows_notification_identity_is_user_facing_app_name():
    text = Path("desktop_app.py").read_text(encoding="utf-8")

    assert 'WINDOWS_APP_USER_MODEL_ID = APP_NAME' in text
    assert '"com.monkeyzoo.catalyst"' not in text
