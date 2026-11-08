from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core.apple_music_backend import AppleMusicBackend, AppleMusicBackendError
from core.apple_music_web import AppleMusicWeb
from core.config import ConfigNode, ParserConfig, PluginConfig
from core.group_parser_config import GroupParserConfigService
from core.management_config import ManagementConfigService


ROOT = Path(__file__).resolve().parents[1]


def configuration(tmp_path):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    data = {key: copy.deepcopy(field["default"]) for key, field in schema.items() if "default" in field}
    data["parsers_template"] = [
        {"__template_key": key, **{name: copy.deepcopy(field["default"]) for name, field in template["items"].items()}}
        for key, template in schema["parsers_template"]["templates"].items()
    ]
    cfg = PluginConfig.__new__(PluginConfig)
    ConfigNode.__init__(cfg, data)
    cfg.parser = ParserConfig(data["parsers_template"])
    cfg.data_dir = tmp_path
    cfg.cache_dir = tmp_path / "cache"
    cfg.plugin_dir = ROOT
    cfg.template_dir = tmp_path / "templates"
    cfg.cookie_dir = tmp_path / "cookies"
    cfg.cookie_dir.mkdir()
    cfg.timezone = "Asia/Shanghai"
    cfg.max_size = cfg.source_max_size * 1024 * 1024
    cfg.max_duration = cfg.source_max_minute * 60
    cfg.saved = []
    cfg.save_config = lambda: cfg.saved.append(copy.deepcopy(cfg._data))
    return cfg


def png():
    output = io.BytesIO()
    with Image.new("RGB", (8, 8), "red") as picture:
        picture.save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode()


def test_describe_whitelist_no_secrets_or_automatic_creation(tmp_path):
    cfg = configuration(tmp_path)
    cfg.parser.douyin.cookies = "private-session"
    del cfg.parser._nodes["pixiv"]
    cfg.card_custom_font = ["files/card_custom_font/private.ttf"]
    backend = AppleMusicBackend(cfg)
    service = ManagementConfigService(cfg, backend)
    result = service.describe()
    assert "private-session" not in json.dumps(result)
    assert result["global_values"]["card_custom_font"] == ["private.ttf"]
    assert "whitelist" not in result["global_fields"]
    assert "group_parser_settings" not in result["global_fields"]
    assert len(result["parsers"]) == 10
    pixiv = next(item for item in result["parsers"] if item["key"] == "pixiv")
    assert not pixiv["configured"] and not pixiv["values"]["enable"]
    assert "backend_enabled" in result["wrapper_fields"]
    assert "backend_enabled" not in next(item for item in result["parsers"] if item["key"] == "applemusic")["fields"]
    with pytest.raises(AppleMusicBackendError):
        asyncio.run(service.save("pixiv", {"enable": True}, owner="admin"))
    assert not cfg.saved


@pytest.mark.parametrize("scope,settings", [
    ("global", {"source_max_size": True}), ("global", {"source_max_size": 501}),
    ("global", {"clean_cron": "bad"}), ("global", {"card_custom_template": "../secret"}),
    ("global", {"card_custom_font": ["C:/secret.ttf"]}),
    ("global", {"proxy": "file:///tmp"}), ("global", {"blacklist": []}),
    ("wrapper", {"wrapper_url": "http://user:pass@localhost"}),
    ("wrapper", {"wrapper_decrypt_port": 65536}),
    ("bilibili", {"video_quality": "invalid"}),
    ("bilibili", {"video_codec_list": ["AVC", "AVC"]}),
    ("douyin", {"cookies": "secret"}), ("missing", {"enable": True}),
    ("global", {}),
])
def test_invalid_values_never_persist(tmp_path, scope, settings):
    cfg = configuration(tmp_path)
    before = copy.deepcopy(cfg._data)
    service = ManagementConfigService(cfg, AppleMusicBackend(cfg))
    with pytest.raises(AppleMusicBackendError):
        asyncio.run(service.save(scope, settings, owner="admin"))
    assert cfg._data == before
    assert not cfg.saved


def test_global_parser_and_wrapper_save_apply_immediately(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        backend = AppleMusicBackend(cfg)
        calls = []
        async def apply(globals_changed, parsers_changed):
            assert cfg.saved[-1] == cfg._data
            calls.append((globals_changed, parsers_changed))
        service = ManagementConfigService(cfg, backend, apply_runtime=apply)
        await service.save("global", {"source_max_size": 100, "proxy": ""}, owner="admin")
        await service.save("douyin", {"enable": False, "motion_photo_send_mode": "video_only"}, owner="admin")
        await service.save("wrapper", {"wrapper_url": "http://127.0.0.1:9000", "wrapper_decrypt_port": 60000}, owner="admin")
        assert cfg.source_max_size == 100 and cfg.proxy is None
        assert cfg.parser.douyin.enable is False
        assert backend.settings.wrapper_url == "http://127.0.0.1:9000"
        assert backend.settings.decrypt_port == 60000
        assert calls == [({"source_max_size", "proxy"}, set()), (set(), {"douyin"}), (set(), {"applemusic"})]
        await backend.close()
    asyncio.run(run())


def test_failed_persistence_does_not_apply_runtime(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        async def apply(*args):
            raise AssertionError("must not apply an uncommitted write")
        cfg.save_config = lambda: (_ for _ in ()).throw(OSError("private disk path"))
        service = ManagementConfigService(cfg, AppleMusicBackend(cfg), apply_runtime=apply)
        with pytest.raises(AppleMusicBackendError, match="未应用修改"):
            await service.save("global", {"debounce_interval": 10}, owner="admin")
        assert cfg.debounce_interval == 300
    asyncio.run(run())


def test_runtime_failure_rolls_back_only_transaction_and_disk(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        cfg._data["qq_official_known_groups"] = [{"platform_id": "qq", "group_id": "historic"}]
        async def apply(*args):
            if cfg.common_timeout == 30:
                raise RuntimeError("private error")
        service = ManagementConfigService(cfg, AppleMusicBackend(cfg), apply_runtime=apply)
        with pytest.raises(AppleMusicBackendError, match="已回滚"):
            await service.save("global", {"common_timeout": 30}, owner="admin")
        assert cfg.common_timeout == 15
        assert cfg.saved[-1] == cfg._data
        assert cfg.qq_official_known_groups[0]["group_id"] == "historic"
    asyncio.run(run())


def test_cancelled_save_finishes_runtime_before_releasing_locks(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        backend = AppleMusicBackend(cfg)
        entered, release = asyncio.Event(), asyncio.Event()
        async def apply(*args):
            entered.set()
            await release.wait()
        service = ManagementConfigService(cfg, backend, apply_runtime=apply)
        task = asyncio.create_task(service.save("global", {"debounce_interval": 10}, owner="admin"))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert backend.config_write_lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cfg.debounce_interval == 10 and cfg.saved[-1]["debounce_interval"] == 10
        assert not backend.lock.locked() and not backend.config_write_lock.locked()
    asyncio.run(run())


def test_teardown_waits_for_config_apply_and_late_writes_are_rejected(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        backend = AppleMusicBackend(cfg)
        entered, release = asyncio.Event(), asyncio.Event()
        async def apply(*args):
            entered.set()
            await release.wait()
        page = AppleMusicWeb(backend, "test")
        page.config_service = ManagementConfigService(cfg, backend, apply_runtime=apply)
        task = asyncio.create_task(page.config_service.save("global", {"debounce_interval": 10}, owner="admin"))
        await entered.wait()
        closing = asyncio.create_task(page.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.gather(task, closing)
        await backend.close()
        with pytest.raises(AppleMusicBackendError, match="已关闭"):
            await page.config_service.save("global", {"debounce_interval": 20}, owner="admin")
        assert cfg.debounce_interval == 10
    asyncio.run(run())


def test_shared_config_lock_preserves_parallel_group_and_parser_changes(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        backend = AppleMusicBackend(cfg)
        service = ManagementConfigService(cfg, backend)
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(id="qq-instance", name="qq_official"))
        groups = GroupParserConfigService(cfg, None, config_lock=backend.config_write_lock)
        groups._adapter_by_id = lambda key: adapter
        await asyncio.gather(
            service.save("global", {"source_max_minute": 20}, owner="admin"),
            service.save("bilibili", {"show_ai_summary": False}, owner="admin"),
            groups.remember_qq_official_group("qq-instance", "new-group"),
        )
        assert cfg.saved[-1] == cfg._data
        assert cfg.source_max_minute == 20 and cfg.parser.bilibili.show_ai_summary is False
        assert len(cfg.qq_official_known_groups) == 1
    asyncio.run(run())


@pytest.mark.parametrize("key", ["douyin", "xhs", "pixiv", "bilibili", "qqmusic", "netease", "kugou"])
def test_cookie_clear_replacement_and_rollback(tmp_path, key):
    async def run():
        cfg = configuration(tmp_path)
        item = cfg.parser.get(key)
        item.cookies = "old=secret"
        path = cfg.cookie_dir / f"{key}_cookies.txt"
        path.write_bytes(b"old cached cookies")
        bili = cfg.cookie_dir / "bilibili_credential.json"
        bili.write_text('{"SESSDATA":"old"}')
        backend = AppleMusicBackend(cfg)
        service = ManagementConfigService(cfg, backend)
        await service.save_cookie("admin", key, None, clear=True)
        assert item.cookies == "" and not path.exists()
        if key == "bilibili":
            assert not bili.exists()
        await service.save_cookie("admin", key, "new=secret")
        assert item.cookies == "new=secret"
        assert "new=secret" not in json.dumps(service.describe())
        path.write_bytes(b"new cache")
        cfg.save_config = lambda: (_ for _ in ()).throw(OSError("disk failed"))
        with pytest.raises(AppleMusicBackendError):
            await service.save_cookie("admin", key, None, clear=True)
        assert item.cookies == "new=secret" and path.read_bytes() == b"new cache"
        assert cfg._data["__parser_management_history_v1"][key]["cookie_configured"] is True
    asyncio.run(run())


def test_valid_logo_upload_and_restore_preserve_old_resource(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        service = ManagementConfigService(cfg, AppleMusicBackend(cfg))
        result = await service.upload("admin", "custom_logo", "../logo.png", png())
        path = tmp_path / cfg.custom_logo[0]
        assert path.is_file() and path.is_relative_to(tmp_path / "files" / "custom_logo")
        assert result["configuration"]["global_values"]["custom_logo"] == [path.name]
        assert not list(tmp_path.rglob("*.part"))
        await service.upload("admin", "custom_logo", clear=True)
        assert cfg.custom_logo == [] and path.is_file()
    asyncio.run(run())


@pytest.mark.parametrize("field,name,data", [
    ("custom_logo", "logo.png", "not-base64"), ("custom_logo", "logo.jpg", "YQ=="),
    ("card_custom_font", "font.ttf", "YQ=="), ("card_custom_font", "font.woff2", "d09GMg=="),
])
def test_invalid_upload_never_creates_formal_file(tmp_path, field, name, data):
    cfg = configuration(tmp_path)
    service = ManagementConfigService(cfg, AppleMusicBackend(cfg))
    with pytest.raises(AppleMusicBackendError):
        asyncio.run(service.upload("admin", field, name, data))
    assert not list(tmp_path.rglob("*.part")) and not cfg.saved


def test_upload_save_failure_removes_unreferenced_new_file(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        cfg.save_config = lambda: (_ for _ in ()).throw(OSError("failed"))
        service = ManagementConfigService(cfg, AppleMusicBackend(cfg))
        with pytest.raises(AppleMusicBackendError):
            await service.upload("admin", "custom_logo", "logo.png", png())
        assert cfg.custom_logo == [] and not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob("*.part"))
    asyncio.run(run())


def test_cancelled_upload_waits_for_persistence_and_keeps_committed_asset(tmp_path):
    async def run():
        cfg = configuration(tmp_path)
        entered, release = threading.Event(), threading.Event()
        def save():
            entered.set()
            assert release.wait(3)
            cfg.saved.append(copy.deepcopy(cfg._data))
        cfg.save_config = save
        backend = AppleMusicBackend(cfg)
        service = ManagementConfigService(cfg, backend)
        task = asyncio.create_task(service.upload("admin", "custom_logo", "logo.png", png()))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert backend.config_write_lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / cfg.custom_logo[0]).is_file()
        assert cfg.saved[-1]["custom_logo"] == cfg.custom_logo
        assert not list(tmp_path.rglob("*.part"))
    asyncio.run(run())


def test_real_parser_replacement_updates_cached_options_and_clears_cookie(tmp_path):
    from test_main_matching import _load_main_module
    async def run():
        main = _load_main_module()
        cfg = configuration(tmp_path)
        for key, item in cfg.parser.items():
            item.enable = key in {"bilibili", "xhs", "qqmusic"}
        cfg.parser.xhs.cookies = "web_session=old-session"
        plugin = main.ParserPlugin.__new__(main.ParserPlugin)
        plugin.cfg = cfg
        plugin.parser_map = {}
        plugin.downloader = SimpleNamespace(max_size=cfg.source_max_size)
        plugin.debouncer = SimpleNamespace(interval=cfg.debounce_interval)
        backend = plugin.apple_music_backend = main.AppleMusicBackend(cfg)
        service = main.ManagementConfigService(cfg, backend, apply_runtime=plugin._apply_management_config)
        plugin._register_parser()
        original = {p.platform.name: p for p in plugin.parser_map.values()}
        session = original["xhs"].session
        assert session is original["xhs"].session
        await service.save("bilibili", {"video_quality": "_1080P", "video_codec_list": ["HEV", "AVC"]}, owner="admin")
        updated = {p.platform.name: p for p in plugin.parser_map.values()}
        assert updated["bilibili"] is not original["bilibili"]
        assert updated["qqmusic"] is original["qqmusic"]
        assert updated["bilibili"].video_quality.name == "_1080P"
        assert [c.name for c in updated["bilibili"].video_codecs] == ["HEV", "AVC"]
        await service.save_cookie("admin", "xhs", None, clear=True)
        final = {p.platform.name: p for p in plugin.parser_map.values()}
        assert final["xhs"] is not original["xhs"]
        assert not final["xhs"].cookiejar.cookies and "cookie" not in final["xhs"].headers
        # A late response on the old instance cannot resurrect its disk Cookie.
        original["xhs"].cookiejar.save_to_file()
        assert not (cfg.cookie_dir / "xhs_cookies.txt").exists()
        await asyncio.gather(*plugin._parser_cleanup_tasks)
        assert session.closed
        for parser in final.values():
            await parser.close_session()
        await backend.close()
    asyncio.run(run())


def test_configuration_routes_require_auth_nonce_and_json_and_work_without_wrapper(tmp_path, monkeypatch):
    async def run():
        cfg = configuration(tmp_path)
        backend = AppleMusicBackend(cfg)
        page = AppleMusicWeb(backend, "test")
        page.config_service = ManagementConfigService(cfg, backend)
        request = SimpleNamespace(username=None, headers={"content-type": "application/json"})
        body = {}
        async def raw(): return json.dumps(body).encode()
        async def parsed(default=None): return body
        request.body, request.json = raw, parsed
        monkeypatch.setitem(sys.modules, "astrbot.api.web", SimpleNamespace(request=request, json_response=lambda data, **kw: (data, kw)))
        context = SimpleNamespace(registered_web_apis=[])
        context.register_web_api = lambda *entry: context.registered_web_apis.append(entry)
        page.register(context)
        routes = {route.rsplit("/", 1)[-1]: handler for route, handler, _, _ in context.registered_web_apis}
        for endpoint in ("configuration", "save-configuration", "save-parser-cookie", "upload-configuration-asset"):
            assert (await routes[endpoint]())[1]["status_code"] == 401
        request.username = "admin"
        assert (await routes["save-configuration"]())[1]["status_code"] == 403
        body.update(nonce=page.nonce("admin"), scope="global", settings={"card_enabled": False})
        request.headers["content-type"] = "text/plain"
        assert (await routes["save-configuration"]())[1]["status_code"] == 415
        request.headers["content-type"] = "application/json"
        assert (await routes["save-configuration"]())[0]["status"] == "ok"
        assert cfg.card_enabled is False
        body["settings"] = {"proxy": "x" * 65536}
        assert (await routes["save-configuration"]())[1]["status_code"] == 413
        await page.close()
        await backend.close()
    asyncio.run(run())
