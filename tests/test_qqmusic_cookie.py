import os
from types import SimpleNamespace

import pytest

from core.parsers.qqmusic_cookie import QQMusicCookieJar


def _jar(tmp_path, configured="uin=123; qqmusic_key=original"):
    return QQMusicCookieJar(
        SimpleNamespace(cookie_dir=tmp_path),
        SimpleNamespace(name="qqmusic", cookies=configured),
    )


def test_unchanged_config_does_not_revert_renewed_cookie(tmp_path):
    jar = _jar(tmp_path)
    jar.replace_from_cookies_str("uin=123; qqmusic_key=renewed")

    restored = _jar(tmp_path)

    assert restored.get()["qqmusic_key"] == "renewed"


def test_changed_config_imports_new_account(tmp_path):
    jar = _jar(tmp_path)
    jar.replace_from_cookies_str("uin=123; qqmusic_key=renewed")

    restored = _jar(tmp_path, "uin=456; qqmusic_key=manually-changed")

    assert restored.get() == {"uin": "456", "qqmusic_key": "manually-changed"}


def test_empty_config_preserves_scan_login_on_restart(tmp_path):
    jar = _jar(tmp_path, "")
    jar.replace_from_cookies_str("uin=123; qqmusic_key=scanned")

    assert _jar(tmp_path, "").get()["qqmusic_key"] == "scanned"


def test_manual_cookie_file_edit_is_not_mistaken_for_saved_refresh(tmp_path):
    jar = _jar(tmp_path)
    jar.cookie_file.write_text("damaged", encoding="utf-8")

    assert _jar(tmp_path).get()["qqmusic_key"] == "original"


def test_retired_jar_cannot_update_import_record(tmp_path):
    jar = _jar(tmp_path)
    record = jar._import_record.read_bytes()
    jar.persistence_enabled = False
    jar.replace_from_cookies_str("uin=456; qqmusic_key=stale")

    assert jar._import_record.read_bytes() == record
    assert _jar(tmp_path).get()["qqmusic_key"] == "original"


def test_config_cookie_removal_does_not_resurrect_old_config_on_restart(tmp_path):
    jar = _jar(tmp_path)
    jar.replace_from_cookies_str("")

    assert not _jar(tmp_path).get()


def test_import_record_failure_happens_before_cookie_commit(tmp_path, monkeypatch):
    jar = _jar(tmp_path)
    before = jar.cookie_file.read_bytes()
    replace = os.replace

    def fail_record(source, target):
        if target == jar._import_record:
            raise OSError("record write failed")
        return replace(source, target)

    monkeypatch.setattr("core.parsers.qqmusic_cookie.os.replace", fail_record)
    with pytest.raises(OSError):
        jar.replace_from_cookies_str("uin=123; qqmusic_key=renewed")
    assert jar.cookie_file.read_bytes() == before
    assert _jar(tmp_path).get()["qqmusic_key"] == "original"


def test_prepared_record_recovers_old_file_after_cookie_replace_failure(
    tmp_path, monkeypatch
):
    jar = _jar(tmp_path)
    jar.replace_from_cookies_str("uin=123; qqmusic_key=first-renewal")
    replace = os.replace

    def fail_cookie(source, target):
        if target == jar.cookie_file:
            raise OSError("cookie replace failed")
        return replace(source, target)

    monkeypatch.setattr("core.parsers.qqmusic_cookie.os.replace", fail_cookie)
    with pytest.raises(OSError):
        jar.replace_from_cookies_str("uin=123; qqmusic_key=second-renewal")
    assert _jar(tmp_path).get()["qqmusic_key"] == "first-renewal"


def test_no_fallible_fingerprint_read_after_cookie_commit(tmp_path, monkeypatch):
    jar = _jar(tmp_path)
    fingerprint = jar._file_fingerprint
    replace = os.replace
    committed = False

    def commit(source, target):
        nonlocal committed
        result = replace(source, target)
        if target == jar.cookie_file:
            committed = True
        return result

    def read_before_only():
        if committed:
            raise OSError("read failed after commit")
        return fingerprint()

    monkeypatch.setattr("core.parsers.qqmusic_cookie.os.replace", commit)
    monkeypatch.setattr(jar, "_file_fingerprint", read_before_only)
    jar.replace_from_cookies_str("uin=123; qqmusic_key=renewed")
    assert _jar(tmp_path).get()["qqmusic_key"] == "renewed"


def test_legacy_cookie_without_record_still_imports_config(tmp_path):
    jar = _jar(tmp_path)
    jar.replace_from_cookies_str("uin=123; qqmusic_key=legacy")
    jar._import_record.unlink()

    assert _jar(tmp_path).get()["qqmusic_key"] == "original"
