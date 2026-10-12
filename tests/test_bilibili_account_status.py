import json

from core.parsers.bilibili.login import BilibiliLogin


def test_bilibili_cookie_status_reads_config_and_persisted_credential(tmp_path):
    login = BilibiliLogin.__new__(BilibiliLogin)
    login._credential = None
    login.raw_cookies = "buvid3=anonymous; SESSDATA=config-session"
    login.credential_file = tmp_path / "bilibili_credential.json"
    assert login.has_cookie() is True

    login.raw_cookies = ""
    login.credential_file.write_text(
        json.dumps({"sessdata": "persisted-session"}), encoding="utf-8"
    )
    assert login.has_cookie() is True

    login.credential_file.write_text("not-json", encoding="utf-8")
    assert login.has_cookie() is False
