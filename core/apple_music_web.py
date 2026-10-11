"""Authenticated AstrBot Plugin Pages endpoints for the optional backend."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Callable

from .apple_music_backend import AppleMusicBackend, AppleMusicBackendError, normalize_track_url


class AppleMusicWeb:
    def __init__(self, backend: AppleMusicBackend, plugin_name: str):
        self.backend = backend
        self.plugin_name = plugin_name
        self._secret = secrets.token_bytes(32)
        self._task: asyncio.Task | None = None
        self._job: dict = {"state": "idle"}
        self._owner: str | None = None
        self._registrations: list[tuple] = []
        self._context = None
        self.closed = False

    def nonce(self, owner: str) -> str:
        # Per-user and per-plugin-load. Never uses the static-page asset token.
        return hmac.new(self._secret, owner.encode(), hashlib.sha256).hexdigest()

    def operation(self, owner: str) -> dict:
        if self._owner != owner:
            return {"state": "idle", "busy": bool(self._task and not self._task.done())}
        return dict(self._job)

    def start(self, owner: str, action: str, body: dict) -> dict:
        if self.closed:
            raise AppleMusicBackendError("管理页已关闭，请重载插件")
        # Cache maintenance is a local plugin operation and remains available
        # even when the optional Apple Music wrapper is disabled.
        if action not in {"clear-cache", "save-settings"}:
            self.backend._require_enabled()
        if self._task and not self._task.done() or self.backend.lock.locked():
            raise AppleMusicBackendError("已有登录或下载任务运行中，请稍后再试")
        self.backend.check_owner(owner, require_pending=action == "two-factor")
        if action == "login":
            username, password = body.get("username"), body.get("password")
            if not isinstance(username, str) or not 1 <= len(username.strip()) <= 254:
                raise AppleMusicBackendError("请输入 Apple 账号")
            if not isinstance(password, str) or not 1 <= len(password) <= 1024:
                raise AppleMusicBackendError("请输入密码")
            coroutine = self.backend.login(owner, username.strip(), password)
        elif action == "two-factor":
            code = body.get("code")
            if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
                raise AppleMusicBackendError("请输入六位数字验证码")
            coroutine = self.backend.two_factor(owner, code)
        elif action == "clear-session":
            if body.get("confirm") is not True:
                raise AppleMusicBackendError("请确认仅清除内存会话，不删除 wrapper 磁盘登录缓存")
            coroutine = self.backend.clear_session(owner)
        elif action == "clear-cache":
            if body.get("confirm") is not True:
                raise AppleMusicBackendError("请确认清除插件缓存文件")
            coroutine = self.backend.clear_cache(owner)
        elif action == "save-settings":
            coroutine = self.backend.update_management_settings(
                owner, body.get("settings")
            )
        elif action == "verify":
            url, _ = normalize_track_url(body.get("url"))
            coroutine = self._verify(url)
        else:
            raise AppleMusicBackendError("未知操作")
        # Do not retain the request body/password in operation records.
        self._owner = owner
        self._job = {"state": "running", "action": action, "message": "任务已提交，请等待"}
        self._task = asyncio.create_task(self._execute(coroutine))
        return self.operation(owner)

    async def _verify(self, url: str) -> dict:
        # Reuse public parser metadata, so a 30-second preview fails verification.
        from .parsers.music import AppleMusicParser, _duration_seconds

        parser = AppleMusicParser(self.backend.cfg, None)
        parser.apple_music_backend = None
        try:
            searched = next((p.search(url) for _, p in parser._key_patterns if p.search(url)), None)
            if not searched:
                raise AppleMusicBackendError("未找到单曲解析规则")
            result = await parser._handle_track(searched, metadata_only=True)
            # No cover download is needed for this diagnostic operation.
            duration = _duration_seconds(result.extra.get("duration"))
            if not duration:
                raise AppleMusicBackendError("未取得歌曲完整时长，暂不进行全曲验证")
            path = await self.backend.download(url, duration=duration)
            return {"message": "单曲音频已生成并通过格式/时长检查", "bytes": path.stat().st_size, "duration": duration}
        finally:
            await parser.close_session()

    async def _execute(self, coroutine) -> None:
        try:
            result = await coroutine
            self._job.update(state="complete", **result)
        except asyncio.CancelledError:
            self._job.update(state="cancelled", message="任务已取消")
            raise
        except AppleMusicBackendError as exc:
            self._job.update(state="error", message=exc.message)
        except Exception:
            # Parser/third-party exception strings may include account data.
            self._job.update(state="error", message="操作失败，请检查服务配置、登录状态和歌曲可用性")

    def register(self, context) -> bool:
        try:
            from astrbot.api.web import json_response, request
        except ImportError:
            return False
        if not callable(getattr(context, "register_web_api", None)):
            return False
        self._context = context

        def response(data=None, *, message=None, status_code=200):
            payload = {"status": "error", "message": message} if message else {"status": "ok", "data": data}
            return json_response(payload, status_code=status_code, headers={"Cache-Control": "no-store"})

        def authenticated_owner() -> str | None:
            try:
                owner = request.username
                return owner if isinstance(owner, str) and owner else None
            except (RuntimeError, AttributeError):
                return None

        async def get_status():
            owner = authenticated_owner()
            if not owner:
                return response(message="需要 AstrBot 后台登录", status_code=401)
            if self.closed:
                return response(message="插件管理页已关闭", status_code=503)
            data = await self.backend.status(owner)
            data.update(nonce=self.nonce(owner), operation=self.operation(owner))
            return response(data)

        async def get_operation():
            owner = authenticated_owner()
            if not owner:
                return response(message="需要 AstrBot 后台登录", status_code=401)
            return response(self.operation(owner))

        def post_handler(action: str) -> Callable:
            async def handler():
                owner = authenticated_owner()
                if not owner:
                    return response(message="需要 AstrBot 后台登录", status_code=401)
                if self.closed:
                    return response(message="插件管理页已关闭", status_code=503)
                # bridge posts JSON with dashboard Authorization; a cross-site
                # form cannot supply this per-user nonce or pass the JSON gate.
                if not request.headers.get("content-type", "").lower().startswith("application/json"):
                    return response(message="需要 JSON 请求", status_code=415)
                if len(await request.body()) > 8192:
                    return response(message="请求过大", status_code=413)
                body = await request.json(default=None)
                if not isinstance(body, dict):
                    return response(message="无效请求", status_code=400)
                nonce = body.get("nonce")
                if (
                    not isinstance(nonce, str) or not nonce.isascii() or len(nonce) != 64
                    or not hmac.compare_digest(nonce, self.nonce(owner))
                ):
                    return response(message="管理会话已更新，请刷新页面后重试", status_code=403)
                try:
                    return response(self.start(owner, action, body))
                except AppleMusicBackendError as exc:
                    return response(message=exc.message, status_code=400)
            return handler

        routes = [("status", get_status, ["GET"]), ("operation", get_operation, ["GET"])]
        routes += [(action, post_handler(action), ["POST"]) for action in ("login", "two-factor", "clear-session", "clear-cache", "save-settings", "verify")]
        for endpoint, handler, methods in routes:
            route = f"/{self.plugin_name}/apple-music/{endpoint}"
            context.register_web_api(route, handler, methods, "Apple Music 账号管理")
            self._registrations.append((route, handler, methods))
        return True

    async def close(self) -> None:
        self.closed = True
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        # Remove only OUR handlers. Never remove a replacement from a newer load.
        registered = getattr(self._context, "registered_web_apis", None)
        if isinstance(registered, list):
            own_handlers = {handler for _, handler, _ in self._registrations}
            registered[:] = [entry for entry in registered if entry[1] not in own_handlers]
        self._registrations.clear()
