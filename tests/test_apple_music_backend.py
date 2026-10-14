import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from core.apple_music_backend import (
    AppleMusicBackend, AppleMusicBackendError, BackendSettings,
    codec_priority_for_send_format, normalize_file_codec_priority,
    normalize_track_url,
)
from core.apple_music_web import AppleMusicWeb


TRACK = "https://music.apple.com/cn/album/落日/1514034441?i=1514034916"


def config(tmp_path):
    return SimpleNamespace(
        cache_dir=tmp_path / "cache", data_dir=tmp_path,
        max_duration=900, max_size=1024 * 1024, common_timeout=10,
        card_enabled=True, proxy=None,
        parser=SimpleNamespace(applemusic=SimpleNamespace(use_proxy=False)),
    )


@pytest.mark.parametrize("send_format,expected", [
    ("voice", ("aac-web",)),
    ("VOICE", ("aac-web",)),
    ("file", ("alac", "aac-web")),
    (None, ("alac", "aac-web")),
])
def test_codec_priority_tracks_audio_send_format(send_format, expected):
    assert codec_priority_for_send_format(send_format) == expected


@pytest.mark.parametrize("value,expected", [
    (["alac", "aac-web"], ("alac", "aac-web")),
    ("aac-web,alac", ("aac-web", "alac")),
    (["ALAC", "alac", "invalid", "aac"], ("alac", "aac")),
    ([], ("alac", "aac-web")),
    (None, ("alac", "aac-web")),
])
def test_file_codec_priority_normalizes_and_keeps_default(value, expected):
    assert normalize_file_codec_priority(value) == expected


@pytest.mark.parametrize("url", [TRACK, "https://music.apple.com/cn/song/落日/1514034916", "https://music.apple.com/cn/song/1514034916"])
def test_normalizes_only_one_track(url):
    assert normalize_track_url(url) == ("https://music.apple.com/cn/song/1514034916", "1514034916")


@pytest.mark.parametrize("url", [
    None, "http://music.apple.com/cn/song/1", "https://evil.com/cn/song/1",
    "https://music.apple.com.evil.com/cn/song/1", "https://user@music.apple.com/cn/song/1",
    "https://music.apple.com:443/cn/song/1", "https://music.apple.com/cn/album/123",
    "https://music.apple.com/cn/album/123?i=1&i=2", "https://music.apple.com/cn/song/../1",
    "https://music.apple.com/cn/playlist/x/123", "https://music.apple.com/cn/song/0",
])
def test_rejects_other_urls(url):
    with pytest.raises(AppleMusicBackendError):
        normalize_track_url(url)


@pytest.mark.parametrize("url", ["file:///tmp", "http://user:pass@localhost", "http://localhost?q=secret", "http://localhost/path", "http://localhost:70000"])
def test_wrapper_configuration_rejects_credential_and_non_http_urls(url):
    with pytest.raises(AppleMusicBackendError):
        BackendSettings(wrapper_url=url).validate()


def test_old_config_defaults_do_not_enable_backend(tmp_path):
    settings = BackendSettings.from_config(config(tmp_path))
    assert settings.enabled is False
    assert settings.python == ""
    assert settings.wrapper_url == "http://127.0.0.1:80"


@pytest.mark.parametrize("port", ["invalid", {}, float("inf"), True, 0])
def test_bad_optional_configuration_does_not_break_plugin_loading(tmp_path, port):
    async def run():
        cfg = config(tmp_path)
        cfg.download_timeout = "invalid"
        cfg.parser.applemusic.backend_enabled = True
        cfg.parser.applemusic.wrapper_decrypt_port = port
        backend = AppleMusicBackend(cfg)
        try:
            assert backend.settings.timeout == 300
            status = await backend.status("admin")
            assert status["connected"] is False
            assert "解密端口" in status["message"]
        finally:
            await backend.close()
    asyncio.run(run())


def test_music_parser_settings_are_persisted_and_returned_without_cookie_values(tmp_path):
    async def run():
        cfg = config(tmp_path)
        cfg.parser.netease = SimpleNamespace(
            enable=True, cookies="private-cookie", return_audio=True,
            audio_send_format="voice", show_playlist_cover=True,
            show_playlist_url=True,
        )
        cfg.parser.qqmusic = SimpleNamespace(
            enable=True, cookies="", return_audio=True,
            audio_send_format="voice", show_playlist_cover=True,
            show_playlist_url=True,
            file_quality_priority=["lossless", "high", "standard"],
        )
        cfg.parser.kugou = SimpleNamespace(
            enable=False, cookies="", return_audio=True,
            audio_send_format="voice", show_playlist_cover=True,
            show_playlist_url=True,
        )
        saves = []
        cfg.save_config = lambda: saves.append(True)
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=False))
        try:
            result = await backend.update_music_parser_settings(
                "admin",
                {"qqmusic": {
                    "enable": False,
                    "return_audio": False,
                    "audio_send_format": "file",
                    "file_quality_priority": ["high", "lossless", "standard"],
                    "show_playlist_cover": False,
                    "show_playlist_url": False,
                }},
            )
            entry = result["parsers"]["qqmusic"]
            assert entry["enabled"] is False
            assert entry["settings"]["audio_send_format"] == "file"
            assert entry["settings"]["return_audio"] is False
            assert entry["settings"]["file_quality_priority"] == [
                "high", "lossless", "standard",
            ]
            assert cfg.parser.qqmusic.file_quality_priority == [
                "high", "lossless", "standard",
            ]
            assert saves == [True]
            assert "private-cookie" not in json.dumps(result)
        finally:
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("priority", [
    [], ["high", "high"], ["hires"], ["lossless", "high", "standard", "lossless"],
])
def test_music_settings_reject_invalid_qq_file_quality_priority(tmp_path, priority):
    async def run():
        cfg = config(tmp_path)
        cfg.parser.qqmusic = SimpleNamespace(
            enable=True, cookies="", return_audio=True,
            audio_send_format="file", show_playlist_cover=True,
            show_playlist_url=True,
            file_quality_priority=["lossless", "high", "standard"],
        )
        cfg.save_config = lambda: None
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=False))
        try:
            with pytest.raises(AppleMusicBackendError, match="音质优先级无效"):
                await backend.update_music_parser_settings(
                    "admin", {"qqmusic": {"file_quality_priority": priority}}
                )
        finally:
            await backend.close()

    asyncio.run(run())


def test_music_cookie_save_returns_parser_identity_without_echoing_cookie(tmp_path):
    async def run():
        cfg = config(tmp_path)
        cfg.parser.qqmusic = SimpleNamespace(enable=True, cookies="")
        cfg.save_config = lambda: None
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=False))
        try:
            result = await backend.update_music_cookie(
                "admin", "qqmusic", "uin=12345; qqmusic_key=secret-token"
            )
            assert result["parser_key"] == "qqmusic"
            assert "secret-token" not in json.dumps(result)
            assert cfg.parser.qqmusic.cookies == "uin=12345; qqmusic_key=secret-token"
        finally:
            await backend.close()

    asyncio.run(run())


def test_music_cookie_status_is_checked_manually_and_bound_to_cookie_value(tmp_path):
    async def run():
        cookie = "uin=12345; qqmusic_key=private-key"
        parser_item = SimpleNamespace(enable=True, cookies=cookie)
        cfg = config(tmp_path)
        cfg.parser.qqmusic = parser_item

        async def valid_check():
            return True

        parser = SimpleNamespace(
            platform=SimpleNamespace(name="qqmusic"),
            _QQ_COOKIE_CHECK_URL="https://c6.y.qq.com/profile-check",
            cookiejar=SimpleNamespace(cookies_str=cookie),
            _qq_cookie_header=lambda _url: parser.cookiejar.cookies_str,
            has_auth_credentials=lambda: True,
            check_cookie=valid_check,
        )
        backend = AppleMusicBackend(
            cfg,
            BackendSettings(enabled=False),
            runtime_parser_provider=lambda: [parser],
        )
        try:
            initial = backend._parser_config_entries()["qqmusic"]
            assert initial["cookie_validity"] == "unknown"
            assert initial["cookie_status"] == "Cookie 待检查"

            result = await backend.check_music_cookie("admin", "qqmusic")
            assert result["cookie_validity"] == "valid"
            assert result["cookie_status"] == "Cookie 有效"
            assert "private-key" not in json.dumps(result)
            assert backend._parser_config_entries()["qqmusic"]["cookie_validity"] == "valid"

            async def unavailable_check():
                raise RuntimeError("private-key in third-party error")

            parser.check_cookie = unavailable_check
            result = await backend.check_music_cookie("admin", "qqmusic")
            assert result["cookie_validity"] == "valid"
            assert "状态未更新" in result["message"]

            parser.cookiejar.cookies_str = "uin=12345; qqmusic_key=replaced-key"
            parser_item.cookies = parser.cookiejar.cookies_str
            changed = backend._parser_config_entries()["qqmusic"]
            assert changed["cookie_validity"] == "unknown"
            assert changed["cookie_status"] == "Cookie 待检查"
            assert "replaced-key" not in json.dumps(changed)
        finally:
            await backend.close()

    asyncio.run(run())


def test_missing_music_parser_uses_bundled_defaults_without_reporting_enabled(tmp_path):
    async def run():
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=False))
        try:
            status = await backend.status("admin")
            music = {key: entry for key, entry in status["music_parsers"].items()}
            assert music["netease"]["known"] is True
            assert music["netease"]["configured"] is False
            assert music["netease"]["enabled"] is False
            assert music["netease"]["settings"]["audio_send_format"] == "voice"
            assert music["netease"]["settings"]["return_audio"] is True
            assert "private-cookie" not in json.dumps(status)
        finally:
            await backend.close()

    asyncio.run(run())


def test_string_false_does_not_opt_into_backend(tmp_path):
    cfg = config(tmp_path)
    cfg.parser.applemusic.backend_enabled = "false"
    assert BackendSettings.from_config(cfg).enabled is False


@pytest.mark.parametrize("raw,valid", [
    (json.dumps({"version": "0.0.2", "auth": {"state": "logged_out"}}).encode(), True),
    (b'{"secret":"' + b"x" * (1024 * 1024) + b'"}', False),
    (b'["private-token"]', False),
    (b'{"secret":"private-token"', False),
], ids=["chunked", "oversized", "non-object", "invalid-json"])
def test_wrapper_json_is_read_across_http_chunks_with_size_limit(tmp_path, raw, valid):
    async def run():
        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(raw[:8])
            await asyncio.sleep(0.01)
            await response.write(raw[8:])
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_get("/me", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True, wrapper_url=f"http://127.0.0.1:{port}"))
        try:
            if valid:
                assert (await backend._http("GET", "/me"))[1] == json.loads(raw)
            else:
                with pytest.raises(AppleMusicBackendError, match="无效响应") as caught:
                    await backend._http("GET", "/me")
                assert "private-token" not in str(caught.value)
        finally:
            await backend.close()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("auth", None), ("auth", []), ("runtime", None),
    ("runtime", []), ("runtime", "private-token"),
])
def test_malformed_wrapper_status_is_sanitized(tmp_path, field, value):
    async def run():
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True))
        payload = {"version": "0.0.2", "auth": {"state": "logged_out"}, "runtime": {}}
        payload[field] = value
        async def http(*args, **kwargs):
            return 200, payload
        backend._http = http
        try:
            status = await backend.status("admin")
            assert status["connected"] is False
            assert "可识别" in status["message"]
            assert "private-token" not in json.dumps(status)
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("token", [None, {}, [], 123, "private\r\nheader", "private;session=secret", "private token"])
def test_invalid_subscription_tokens_never_reach_apple(tmp_path, token):
    async def run():
        backend = AppleMusicBackend(config(tmp_path))
        try:
            active, storefront, message = await backend._subscription({"auth": {"dev_token": "valid-token", "music_user_token": token}})
            assert active is None and storefront is None
            assert "未取得账号令牌" in message
            assert "private" not in message
            assert backend._session is None
        finally:
            await backend.close()
    asyncio.run(run())


def test_disabled_backend_makes_no_network_requests(tmp_path):
    async def run():
        backend = AppleMusicBackend(config(tmp_path))
        async def unexpected(*args, **kwargs):
            pytest.fail("disabled backend performed I/O")
        backend._me = unexpected
        result = await backend.status("admin")
        assert result["enabled"] is False
        assert result["subscription_active"] is None
        assert [item["key"] for item in result["platform_accounts"]] == [
            "douyin", "bilibili", "qqmusic", "kugou", "applemusic"
        ]
        assert result["platform_accounts"][-1] == {
            "key": "applemusic", "label": "Apple Music",
            "available": False, "text": "账号后端未启用",
        }
        await backend.close()
    asyncio.run(run())


def test_platform_account_provider_is_normalized_without_exposing_cookie_data(tmp_path):
    cfg = config(tmp_path)
    cfg.parser.qqmusic = SimpleNamespace(enable=True, cookies="identity-only")
    cfg.parser.applemusic.backend_enabled = True
    backend = AppleMusicBackend(
        cfg,
        BackendSettings(enabled=True),
        platform_status_provider=lambda: [
            {"key": "douyin", "label": "forged", "available": True,
             "text": "登录 Cookie 已配置", "cookie": "private-session"},
            {"key": "qqmusic", "available": False, "text": "账号 Cookie 不完整"},
            {"key": "unknown", "available": True, "text": "ignored"},
        ],
    )

    accounts = backend.platform_accounts("authenticated")

    assert accounts[0] == {
        "key": "douyin", "label": "抖音",
        "available": True, "text": "登录 Cookie 已配置",
    }
    assert accounts[1]["text"] == "状态未就绪"
    assert accounts[2]["text"] == "账号 Cookie 不完整"
    assert accounts[-1]["available"] is True
    assert accounts[-1]["text"] == "已登录"
    assert "private-session" not in json.dumps(accounts)


async def wrapper_fixture(tmp_path):
    state = {"auth": "logged_out", "version": "0.0.2", "requests": []}
    async def me(request):
        return web.json_response({
            "version": state["version"], "runtime": {"playback_ready": True},
            "auth": {"state": state["auth"], "music_user_token": "private-user-token", "dev_token": "private-dev-token"},
        })
    async def login(request):
        state["requests"].append("login")
        assert (await request.json())["username"] == "example@invalid.test"
        state["auth"] = "awaiting_2fa"
        return web.json_response({"token": "must-not-be-returned"}, status=202)
    async def two_factor(request):
        state["requests"].append("2fa")
        if (await request.json())["code"] != "123456":
            return web.json_response({"error": "private-error-body"}, status=401)
        state["auth"] = "authenticated"
        return web.json_response({"token": "must-not-be-returned"})
    async def clear(request):
        state["auth"] = "logged_out"
        return web.json_response({})
    app = web.Application()
    app.router.add_get("/me", me)
    app.router.add_post("/login", login)
    app.router.add_post("/login/2fa", two_factor)
    app.router.add_delete("/login", clear)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True, wrapper_url=f"http://127.0.0.1:{port}"))
    async def subscription(me):
        return True, "cn", "订阅已核验"
    backend._subscription = subscription
    return runner, backend, state


def test_login_two_factor_owner_isolation_and_redacted_status(tmp_path):
    async def run():
        runner, backend, state = await wrapper_fixture(tmp_path)
        try:
            result = await backend.login("admin-a", "example@invalid.test", "test-password")
            assert result["needs_2fa"] is True
            assert (await backend.status("admin-a"))["can_submit_2fa"] is True
            assert (await backend.status("admin-b"))["can_submit_2fa"] is False
            with pytest.raises(AppleMusicBackendError, match="另一位管理员"):
                await backend.two_factor("admin-b", "123456")
            with pytest.raises(AppleMusicBackendError, match="HTTP 401") as caught:
                await backend.two_factor("admin-a", "000000")
            assert "private-error-body" not in str(caught.value)
            await backend.two_factor("admin-a", "123456")
            status = await backend.status("admin-a")
            assert status["auth_state"] == "authenticated"
            assert status["subscription_active"] is True
            assert status["platform_accounts"][-1]["available"] is True
            assert status["platform_accounts"][-1]["text"] == "已登录"
            assert "private" not in json.dumps(status)
            assert "must-not-be-returned" not in json.dumps(result)
            cleared = await backend.clear_session("admin-a")
            assert "磁盘" in cleared["message"]
            assert state["auth"] == "logged_out"
        finally:
            await backend.close()
            await runner.cleanup()
    asyncio.run(run())


def test_wrong_wrapper_version_never_receives_credentials(tmp_path):
    async def run():
        runner, backend, state = await wrapper_fixture(tmp_path)
        state["version"] = "0.0.1"
        try:
            with pytest.raises(AppleMusicBackendError, match="不兼容"):
                await backend.login("admin", "example@invalid.test", "secret")
            assert state["requests"] == []
            assert (await backend.status("admin"))["connected"] is False
        finally:
            await backend.close()
            await runner.cleanup()
    asyncio.run(run())


def test_expired_two_factor_session_requires_login(tmp_path):
    backend = AppleMusicBackend(config(tmp_path))
    backend.pending_owner = "admin"
    backend.pending_until = time.monotonic() - 1
    with pytest.raises(AppleMusicBackendError, match="重新登录"):
        backend.check_owner("admin", require_pending=True)


@pytest.mark.parametrize("actual,produces,size,codec_tag,expected", [
    (306, True, 20, "mp4a", None), (30, True, 20, "mp4a", "试听"),
    (306, False, 20, "mp4a", "未生成"), (306, True, 0, "mp4a", "为空"),
    (1200, True, 20, "mp4a", "时长检查"),
    (float("nan"), True, 20, "mp4a", "时长检查"),
    (float("inf"), True, 20, "mp4a", "时长检查"),
    (306, True, 20, "enca", "格式/时长检查"),
])
def test_download_checks_file_and_full_duration(tmp_path, actual, produces, size, codec_tag, expected):
    async def run():
        cfg = config(tmp_path)
        cfg.parser.applemusic.audio_send_format = "file"
        cfg.parser.applemusic.file_codec_priority = ["aac-web", "alac"]
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=True, python=sys.executable))
        commands = []
        async def me():
            return {"auth": {"state": "authenticated"}}
        async def subscription(me):
            return True, "cn", "ok"
        async def probe():
            return {"available": True, "ffmpeg": True, "ffprobe": True}
        async def execute(argv, **kwargs):
            commands.append(argv)
            if "--output-path" in argv:
                if produces:
                    output = Path(argv[argv.index("--output-path") + 1])
                    (output / "track.m4a").write_bytes(b"x" * size)
                return b""
            return json.dumps({"format": {"duration": actual}, "streams": [{"codec_type": "audio", "codec_name": "aac", "codec_tag_string": codec_tag}]}).encode()
        backend._me, backend._subscription, backend.probe_gamdl, backend._run = me, subscription, probe, execute
        if expected:
            with pytest.raises(AppleMusicBackendError, match=expected):
                await backend.download(TRACK, duration=306)
        else:
            path = await backend.download(TRACK, duration=306)
            assert path.is_relative_to(tmp_path / "cache")
            assert path.stat().st_size == size
        assert "--no-config-file" in commands[0]
        assert commands[0][commands[0].index("--song-codec-priority") + 1] == "aac-web,alac"
        assert commands[0][-1] == "https://music.apple.com/cn/song/1514034916"
        assert "1514034916" in commands[0]
        await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("duration", [0, None, "invalid", True, -1, float("nan"), float("inf")])
def test_missing_duration_does_not_launch_tools(tmp_path, duration):
    async def run():
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True))
        with pytest.raises(AppleMusicBackendError, match="完整歌曲时长"):
            await backend.download(TRACK, duration=duration)
        await backend.close()
    asyncio.run(run())


def test_subprocess_timeout_stops_process(tmp_path):
    async def run():
        backend = AppleMusicBackend(config(tmp_path))
        with pytest.raises(AppleMusicBackendError, match="超时"):
            await backend._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        assert not backend._processes
        await backend.close()
    asyncio.run(run())


def test_web_jobs_do_not_retain_password_and_close_cancels(tmp_path):
    async def run():
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True))
        started = asyncio.Event()
        async def login(*args):
            started.set()
            await asyncio.Event().wait()
        backend.login = login
        page = AppleMusicWeb(backend, "test-plugin")
        response = page.start("admin", "login", {"username": "example@invalid.test", "password": "secret"})
        await started.wait()
        assert response["state"] == "running"
        assert "secret" not in json.dumps(page.operation("admin"))
        assert page.operation("other")["busy"] is True
        with pytest.raises(AppleMusicBackendError, match="运行中"):
            page.start("admin", "clear-session", {"confirm": True})
        await page.close()
        assert page.operation("admin")["state"] == "cancelled"
        await backend.close()
    asyncio.run(run())


def test_web_apis_require_auth_nonce_json_and_cleanup_only_own_routes(tmp_path, monkeypatch):
    async def run():
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True))
        page = AppleMusicWeb(backend, "test-plugin")
        request = SimpleNamespace(username=None, headers={"content-type": "application/json"})
        body = {}
        async def raw():
            return json.dumps(body).encode()
        async def json_body(default=None):
            return body
        request.body, request.json = raw, json_body
        api = SimpleNamespace(request=request, json_response=lambda data, **kwargs: (data, kwargs))
        monkeypatch.setitem(sys.modules, "astrbot.api.web", api)
        context = SimpleNamespace(registered_web_apis=[])
        def register(route, handler, methods, desc):
            context.registered_web_apis.append((route, handler, methods, desc))
        context.register_web_api = register
        assert page.register(context)
        routes = {route.rsplit("/", 1)[-1]: handler for route, handler, _, _ in context.registered_web_apis}
        assert "save-settings" in routes
        assert "clear-cache" in routes
        assert "check-music-cookie" in routes
        assert (await routes["status"]())[1]["status_code"] == 401
        assert (await routes["login"]())[1]["status_code"] == 401
        request.username = "admin"
        assert (await routes["login"]())[1]["status_code"] == 403
        body["nonce"] = page.nonce("someone-else")
        assert (await routes["login"]())[1]["status_code"] == 403
        body["nonce"] = "非 ASCII 会话"
        assert (await routes["login"]())[1]["status_code"] == 403
        body["nonce"] = page.nonce("admin")
        request.headers["content-type"] = "application/x-www-form-urlencoded"
        assert (await routes["login"]())[1]["status_code"] == 415
        request.headers["content-type"] = "application/json"
        body.update(username="a" * 9000)
        assert (await routes["login"]())[1]["status_code"] == 413
        # A reloaded plugin's replacement must survive old-instance termination.
        replacement = (context.registered_web_apis[0][0], lambda: None, ["GET"], "new")
        context.registered_web_apis[0] = replacement
        await page.close()
        assert context.registered_web_apis == [replacement]
        await backend.close()
    asyncio.run(run())


def test_plugin_logo_route_serves_current_plugin_root_file(tmp_path, monkeypatch):
    async def run():
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        logo_path = plugin_dir / "logo.png"
        logo_path.write_bytes(b"png logo bytes")
        cfg = config(tmp_path)
        cfg.plugin_dir = plugin_dir
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=True))
        page = AppleMusicWeb(backend, "test-plugin")
        request = SimpleNamespace(username="admin", headers={"content-type": "application/json"})
        api = SimpleNamespace(request=request, json_response=lambda data, **kwargs: (data, kwargs))
        monkeypatch.setitem(sys.modules, "astrbot.api.web", api)
        context = SimpleNamespace(registered_web_apis=[])
        context.register_web_api = lambda route, handler, methods, desc: context.registered_web_apis.append(
            (route, handler, methods, desc)
        )
        assert page.register(context)
        route = next(item for item in context.registered_web_apis if item[0].endswith("/apple-music/logo"))

        response = await route[1]()

        payload, options = response
        assert payload["data"]["data_url"] == "data:image/png;base64,cG5nIGxvZ28gYnl0ZXM="
        assert options["headers"]["Cache-Control"] == "no-store"
        await page.close()
        await backend.close()

    asyncio.run(run())


def test_native_astrbot_request_context_and_json_response(tmp_path):
    api = pytest.importorskip("astrbot.api.web")
    from starlette.requests import Request

    async def run():
        backend = AppleMusicBackend(config(tmp_path), BackendSettings(enabled=True))
        page = AppleMusicWeb(backend, "test-plugin")
        context = SimpleNamespace(registered_web_apis=[])
        def register(route, handler, methods, desc):
            context.registered_web_apis.append((route, handler, methods, desc))
        context.register_web_api = register
        assert page.register(context)
        routes = {route.rsplit("/", 1)[-1]: handler for route, handler, _, _ in context.registered_web_apis}
        async def status(owner):
            return {"enabled": True, "connected": False, "message": "offline"}
        async def login(*args):
            return {"needs_2fa": True, "message": "请输入验证码"}
        backend.status, backend.login = status, login
        async def call(endpoint, username=None, body=None):
            raw = json.dumps(body or {}).encode()
            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}
            request = Request({
                "type": "http", "method": "GET" if endpoint == "status" else "POST",
                "path": f"/test-plugin/apple-music/{endpoint}", "headers": [(b"content-type", b"application/json")],
                "query_string": b"", "server": ("localhost", 80), "client": ("127.0.0.1", 1234),
                "scheme": "http",
            }, receive)
            with api.bind_request_context(api.PluginRequest(request, plugin_name="test-plugin", username=username)):
                return await routes[endpoint]()
        try:
            assert (await routes["status"]()).status_code == 401  # No request context.
            assert (await call("status")).status_code == 401
            response = await call("status", "admin")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            nonce = json.loads(response.body)["data"]["nonce"]
            assert nonce == page.nonce("admin")
            assert (await call("login", "admin", {"nonce": nonce})).status_code == 400
            response = await call("login", "admin", {"nonce": nonce, "username": "qa@example.invalid", "password": "private-password"})
            assert response.status_code == 200
            await page._task
            assert page.operation("admin")["needs_2fa"] is True
            assert "private" not in json.dumps(page.operation("admin"))
        finally:
            await page.close()
            await backend.close()
        assert context.registered_web_apis == []
    asyncio.run(run())


def test_cache_status_and_clear_work_with_optional_backend_disabled(tmp_path):
    async def run():
        cfg = config(tmp_path)
        cfg.cache_dir.mkdir()
        (cfg.cache_dir / "audio.m4a").write_bytes(b"audio")
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=False))
        page = AppleMusicWeb(backend, "test-plugin")
        try:
            status = await backend.status("admin")
            assert status["cache"] == {"bytes": 5, "files": 1}
            operation = page.start("admin", "clear-cache", {"confirm": True})
            assert operation["state"] == "running"
            await page._task
            assert page.operation("admin")["state"] == "complete"
            assert list(cfg.cache_dir.iterdir()) == []
        finally:
            await page.close()
            await backend.close()

    asyncio.run(run())


def test_management_settings_are_validated_persisted_and_applied(tmp_path):
    async def run():
        cfg = config(tmp_path)
        item = cfg.parser.applemusic
        item.backend_enabled = False
        item.return_audio = False
        item.audio_send_format = "file"
        item.file_codec_priority = ["alac", "aac-web"]
        item.show_playlist_cover = True
        item.show_playlist_url = True
        saves = []
        cfg.save_config = lambda: saves.append(True)
        backend = AppleMusicBackend(cfg, BackendSettings(enabled=False))
        values = {
            "backend_enabled": True,
            "return_audio": True,
            "audio_send_format": "voice",
            "file_codec_priority": ["aac-web", "alac"],
            "show_playlist_cover": False,
            "show_playlist_url": False,
        }
        try:
            result = await backend.update_management_settings("admin", values)
            assert result["settings"] == values
            assert result["message"] == "Apple Music 解析设置已保存并立即生效"
            assert saves == [True]
            assert backend.settings.enabled is True
            assert item.file_codec_priority == ["aac-web", "alac"]
            with pytest.raises(AppleMusicBackendError, match="音质优先级无效"):
                await backend.update_management_settings(
                    "admin", {"file_codec_priority": ["alac", "alac"]}
                )
            with pytest.raises(AppleMusicBackendError, match="不支持的字段"):
                await backend.update_management_settings("admin", {"wrapper_url": "x"})
        finally:
            await backend.close()

    asyncio.run(run())
