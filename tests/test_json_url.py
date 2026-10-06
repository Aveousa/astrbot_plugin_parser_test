from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def utils_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    monkeypatch.delitem(sys.modules, "core.utils", raising=False)
    return importlib.import_module("core.utils")


def test_extract_json_url_keeps_existing_detail_qqdocurl_support(utils_module):
    data = {"meta": {"detail_1": {"qqdocurl": "https://example.com/doc"}}}
    assert utils_module.extract_json_url(data) == "https://example.com/doc"


def test_extract_json_url_reads_supported_miniapp_legacy_url(utils_module):
    data = {
        "meta": {
            "miniapp": {
                "legacyUrl": "https%3A%2F%2Fwww.douyin.com%2Fvideo%2F1234567890123456789"
            }
        }
    }
    assert utils_module.extract_json_url(data).startswith(
        "https://www.douyin.com/video/1234567890123456789"
    )


def test_extract_json_url_prefers_nested_supported_share_over_unrelated_url(utils_module):
    data = {
        "prompt": "https://example.com/landing",
        "meta": {
            "detail": {
                "nested": "open https:\\/\\/www.xiaohongshu.com\\/explore\\/abc?x=1 now"
            }
        },
    }
    assert utils_module.extract_json_url(data) == "https://www.xiaohongshu.com/explore/abc?x=1"


def test_extract_json_url_recognizes_netease_short_share(utils_module):
    data = {
        "prompt": "https://example.com/landing",
        "meta": {
            "detail": {
                "nested": "open https://163cn.tv/AbC_123 now",
            }
        },
    }

    assert utils_module.extract_json_url(data) == "https://163cn.tv/AbC_123"
