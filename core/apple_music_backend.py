"""Optional wrapper-v2/gamdl adapter. Never imports gamdl into AstrBot.

Only the trusted, administrator-configured Python executable is launched;
account secrets never appear in argv, configuration or subprocess output.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp

from .cache import clear_cache_dir, create_parse_cache_dir, get_active_cache_dir, get_cache_size
from .exception import DownloadException


WRAPPER_VERSION = "0.0.2"
GAMDL_VERSION = "3.9.1"

# Keep the quality policy tied to the delivery format. AAC Web is small and
# broadly accepted by voice adapters; files prefer lossless ALAC and fall back
# through the user-configured gamdl codec order.
VOICE_CODEC_PRIORITY = ("aac-web",)
FILE_CODEC_PRIORITY = ("alac", "aac-web")
SUPPORTED_FILE_CODECS = frozenset({
    "alac", "aac-web", "aac-he-web", "aac", "aac-he",
    "aac-binaural", "aac-downmix", "aac-he-binaural", "aac-he-downmix",
    "atmos", "ac3",
})
MANAGED_BOOLEAN_SETTINGS = (
    "backend_enabled",
    "return_audio",
    "show_playlist_cover",
    "show_playlist_url",
)
MANAGED_SETTINGS = frozenset({
    *MANAGED_BOOLEAN_SETTINGS,
    "audio_send_format",
    "file_codec_priority",
})


def codec_priority_for_send_format(send_format: object) -> tuple[str, ...]:
    """Return the gamdl codec order for the configured delivery format."""
    if str(send_format or "").strip().lower() == "voice":
        return VOICE_CODEC_PRIORITY
    return FILE_CODEC_PRIORITY


def normalize_file_codec_priority(value: object) -> tuple[str, ...]:
    """Normalize the configured file codec order without passing arbitrary CLI values."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = ()
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        codec = item.strip().lower()
        if codec in SUPPORTED_FILE_CODECS and codec not in result:
            result.append(codec)
    return tuple(result) or FILE_CODEC_PRIORITY


class AppleMusicBackendError(DownloadException):
    """A deliberately sanitized error safe to show in the dashboard/chat."""


def normalize_track_url(value: str) -> tuple[str, str]:
    """Accept one Apple Music song, never an album/playlist/arbitrary URL."""
    if not isinstance(value, str) or len(value) > 2048:
        raise AppleMusicBackendError("请输入 Apple Music 单曲链接")
    try:
        parsed = urlsplit(value.strip())
        valid_host = (
            parsed.scheme == "https"
            and parsed.netloc == "music.apple.com"
            and not parsed.username
        )
    except ValueError:
        valid_host = False
    if not valid_host:
        raise AppleMusicBackendError("只接受 https://music.apple.com 的单曲链接")
    match = re.fullmatch(r"/([a-z]{2})/(song|album)/(?:[^/]+/)?([1-9]\d{0,19})/?", parsed.path)
    if not match or any(unquote(part) in (".", "..") for part in parsed.path.split("/")):
        raise AppleMusicBackendError("链接不是可识别的 Apple Music 单曲")
    country, kind, identifier = match.groups()
    if kind == "album":
        values = parse_qs(parsed.query).get("i", [])
        if len(values) != 1 or not re.fullmatch(r"[1-9]\d{0,19}", values[0]):
            raise AppleMusicBackendError("专辑链接必须用 i 参数指定一首歌曲")
        identifier = values[0]
    return f"https://music.apple.com/{country}/song/{identifier}", identifier


@dataclass(frozen=True)
class BackendSettings:
    enabled: bool = False
    wrapper_url: str = "http://127.0.0.1:80"
    decrypt_host: str = "127.0.0.1"
    decrypt_port: int = 10020
    python: str = ""
    timeout: int = 300

    @classmethod
    def from_config(cls, config) -> BackendSettings:
        try:
            item = config.parser.applemusic
        except (AttributeError, TypeError):
            return cls()

        def integer(value, default, invalid):
            if value is None:
                return default
            try:
                if isinstance(value, bool):
                    raise ValueError
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return invalid

        return cls(
            enabled=getattr(item, "backend_enabled", False) is True,
            wrapper_url=str(getattr(item, "wrapper_url", None) or cls.wrapper_url).strip(),
            decrypt_host=str(getattr(item, "wrapper_decrypt_host", None) or cls.decrypt_host).strip(),
            # Bad optional config must not break card parsing/plugin loading.
            # An invalid port stays invalid, so status reports a useful error.
            decrypt_port=integer(getattr(item, "wrapper_decrypt_port", None), cls.decrypt_port, 0),
            python=str(getattr(item, "gamdl_python", None) or "").strip(),
            timeout=max(30, min(integer(getattr(config, "download_timeout", None), 300, 300), 1800)),
        )

    def validate(self) -> str:
        try:
            parsed = urlsplit(self.wrapper_url)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise AppleMusicBackendError("wrapper 地址须为无账号、参数或路径的 HTTP(S) 地址")
        if not self.decrypt_host or any(c in self.decrypt_host for c in "/\\\r\n "):
            raise AppleMusicBackendError("wrapper 解密主机配置不正确")
        if not 1 <= self.decrypt_port <= 65535:
            raise AppleMusicBackendError("wrapper 解密端口须在 1–65535 之间")
        return self.wrapper_url.rstrip("/")


class AppleMusicBackend:
    def __init__(self, config, settings: BackendSettings | None = None):
        self.cfg = config
        self.settings = settings or BackendSettings.from_config(config)
        self._session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()
        self.pending_owner: str | None = None
        self.pending_until = 0.0
        self._probe_cache: tuple[float, dict] | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._tasks: set[asyncio.Task] = set()
        self.closed = False

    def _require_enabled(self) -> str:
        if self.closed:
            raise AppleMusicBackendError("Apple Music 后端已停止，请重载插件")
        if not self.settings.enabled:
            raise AppleMusicBackendError("请先在插件配置中启用 Apple Music 可选后端")
        return self.settings.validate()

    def management_settings(self) -> dict:
        """Return the small, non-secret configuration exposed to this page."""

        item = self.cfg.parser.applemusic
        return {
            "backend_enabled": getattr(item, "backend_enabled", False) is True,
            "return_audio": getattr(item, "return_audio", False) is True,
            "audio_send_format": (
                "voice"
                if str(getattr(item, "audio_send_format", "file")).lower() == "voice"
                else "file"
            ),
            "file_codec_priority": list(
                normalize_file_codec_priority(
                    getattr(item, "file_codec_priority", None)
                )
            ),
            "show_playlist_cover": getattr(item, "show_playlist_cover", True) is not False,
            "show_playlist_url": getattr(item, "show_playlist_url", True) is not False,
        }

    async def update_management_settings(self, owner: str, values: object) -> dict:
        """Validate and persist only the Apple Music fields exposed by the page."""

        if not isinstance(values, dict) or not values:
            raise AppleMusicBackendError("没有可保存的 Apple Music 设置")
        unknown = set(values) - MANAGED_SETTINGS
        if unknown:
            raise AppleMusicBackendError("设置中包含不支持的字段")
        for field in MANAGED_BOOLEAN_SETTINGS:
            if field in values and not isinstance(values[field], bool):
                raise AppleMusicBackendError("Apple Music 开关设置无效")
        if "audio_send_format" in values and values["audio_send_format"] not in {
            "file", "voice"
        }:
            raise AppleMusicBackendError("Apple Music 音频发送格式无效")
        if "file_codec_priority" in values:
            codecs = values["file_codec_priority"]
            if (
                not isinstance(codecs, list)
                or not 1 <= len(codecs) <= len(SUPPORTED_FILE_CODECS)
                or any(not isinstance(codec, str) for codec in codecs)
                or len(codecs) != len(set(codecs))
                or any(codec not in SUPPORTED_FILE_CODECS for codec in codecs)
            ):
                raise AppleMusicBackendError("Apple Music 文件音质优先级无效")

        async with self.lock:
            self.check_owner(owner)
            item = self.cfg.parser.applemusic
            previous = {key: getattr(item, key, None) for key in values}
            try:
                for key, value in values.items():
                    setattr(item, key, list(value) if key == "file_codec_priority" else value)
                save = getattr(self.cfg, "save_config", None)
                if not callable(save):
                    raise RuntimeError("configuration is not persistent")
                await asyncio.get_running_loop().run_in_executor(None, save)
            except Exception:
                for key, value in previous.items():
                    setattr(item, key, value)
                raise AppleMusicBackendError("Apple Music 设置保存失败，请稍后重试") from None

            # BackendSettings is immutable and normally created at plugin load.
            # Refresh it so backend_enabled takes effect without a plugin reload.
            self.settings = BackendSettings.from_config(self.cfg)
            self._probe_cache = None
            return {
                "message": "Apple Music 解析设置已保存并立即生效",
                "settings": self.management_settings(),
            }

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict:
        # read(n) may return only the first available HTTP chunk. Read to EOF
        # with an upper bound instead of parsing a truncated JSON document.
        raw = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise ValueError("response exceeds size limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("response is not an object")
        return payload

    async def _http(self, method: str, path: str, *, body=None, timeout=12) -> tuple[int, dict]:
        base = self._require_enabled()
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            )
        try:
            async with self._session.request(
                method, base + path, json=body, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                # Ignore error bodies: wrapper/Apple errors can contain tokens.
                if response.status not in (200, 202):
                    return response.status, {}
                return response.status, await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError):
            raise AppleMusicBackendError("无法连接 wrapper，或请求超时；请检查独立服务地址") from None
        except (ValueError, UnicodeError):
            raise AppleMusicBackendError("wrapper 返回了无效响应") from None

    async def _me(self) -> dict:
        status, payload = await self._http("GET", "/me")
        if status != 200:
            raise AppleMusicBackendError(f"wrapper 状态检查失败（HTTP {status}）")
        if payload.get("version") != WRAPPER_VERSION:
            raise AppleMusicBackendError(f"需要 wrapper-v2 API {WRAPPER_VERSION}，当前版本不兼容")
        if not isinstance(payload.get("auth"), dict):
            raise AppleMusicBackendError("wrapper 未提供可识别的登录状态")
        if not isinstance(payload.get("runtime", {}), dict):
            raise AppleMusicBackendError("wrapper 未提供可识别的运行状态")
        return payload

    async def _subscription(self, me: dict) -> tuple[bool | None, str | None, str]:
        auth = me["auth"]
        token, user_token = auth.get("dev_token"), auth.get("music_user_token")
        if not all(
            isinstance(value, str) and 0 < len(value) <= 32768
            and all(33 <= ord(character) <= 126 for character in value)
            and ";" not in value
            for value in (token, user_token)
        ):
            return None, None, "未取得账号令牌，尚不能检查订阅"
        try:
            assert self._session is not None
            async with self._session.get(
                "https://amp-api.music.apple.com/v1/me/account",
                params={"meta": "subscription"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://music.apple.com",
                    "Cookie": f"media-user-token={user_token}",
                },
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None, None, f"Apple 订阅检查失败（HTTP {response.status}），不代表没有订阅"
                payload = await self._read_json(response)
            subscription = payload.get("meta", {}).get("subscription", {})
            active = subscription.get("active")
            storefront = subscription.get("storefront")
            return (
                active if isinstance(active, bool) else None,
                storefront if isinstance(storefront, str) and re.fullmatch(r"[a-z]{2}", storefront) else None,
                "订阅已核验" if isinstance(active, bool) else "Apple 未返回订阅状态",
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError):
            return None, None, "暂时无法核验订阅，请稍后刷新"

    async def probe_gamdl(self) -> dict:
        if self._probe_cache and time.monotonic() - self._probe_cache[0] < 60:
            return dict(self._probe_cache[1])
        result = {"available": False, "version": None, "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
        if self.settings.python:
            try:
                data = await self._run(
                    [self.settings.python, "-c", "from gamdl.cli.cli import main; from importlib.metadata import version; print(version('gamdl'))"],
                    timeout=15, capture=True,
                )
                version = data.decode().strip()
                if re.fullmatch(r"\d+\.\d+\.\d+", version):
                    result.update(version=version, available=version == GAMDL_VERSION)
            except AppleMusicBackendError:
                pass
        self._probe_cache = time.monotonic(), result
        return dict(result)

    async def status(self, owner: str) -> dict:
        loop = asyncio.get_running_loop()
        cache_bytes, cache_files = await loop.run_in_executor(
            None, get_cache_size, self.cfg.cache_dir
        )
        result = {
            "enabled": self.settings.enabled,
            "connected": False,
            "version": None,
            "auth_state": "unknown",
            "subscription_active": None,
            "storefront": None,
            "playback_ready": False,
            "can_submit_2fa": False,
            "message": "请在插件配置中启用可选后端并配置 wrapper / gamdl Python",
            "subscription_message": "尚未核验订阅",
            "gamdl": {"available": False, "version": None, "ffmpeg": False, "ffprobe": False},
            "cache": {"bytes": cache_bytes, "files": cache_files},
            "settings": self.management_settings(),
        }
        if not self.settings.enabled or self.closed:
            return result
        try:
            me, probe = await asyncio.gather(self._me(), self.probe_gamdl(), return_exceptions=True)
            if isinstance(probe, dict):
                result["gamdl"] = probe
            if isinstance(me, Exception):
                raise me
            auth = me["auth"]
            state = auth.get("state")
            result.update(
                connected=True, version=WRAPPER_VERSION,
                auth_state=state if state in ("authenticated", "logged_out", "awaiting_2fa", "in_progress", "failed") else "unknown",
                playback_ready=me.get("runtime", {}).get("playback_ready") is True,
                can_submit_2fa=self.pending_owner == owner and time.monotonic() < self.pending_until,
                message="wrapper 已连接",
            )
            if state == "authenticated":
                active, storefront, message = await self._subscription(me)
                result.update(subscription_active=active, storefront=storefront, subscription_message=message)
        except AppleMusicBackendError as exc:
            result["message"] = exc.message
        return result

    async def clear_cache(self, owner: str) -> dict:
        """Clear generated media/cards while keeping the configured root."""

        async with self.lock:
            self.check_owner(owner)
            loop = asyncio.get_running_loop()
            removed_files, removed_bytes = await loop.run_in_executor(
                None, clear_cache_dir, self.cfg.cache_dir
            )
            return {
                "message": "缓存已清理，账号登录状态未受影响",
                "removed_files": removed_files,
                "removed_bytes": removed_bytes,
            }

    def check_owner(self, owner: str, *, require_pending=False) -> None:
        if time.monotonic() >= self.pending_until:
            self.pending_owner = None
        if self.pending_owner and self.pending_owner != owner:
            raise AppleMusicBackendError("另一位管理员正在完成双重认证，请稍后再试")
        if require_pending and self.pending_owner != owner:
            raise AppleMusicBackendError("没有属于当前管理员的验证码会话，请重新登录")

    async def login(self, owner: str, username: str, password: str) -> dict:
        async with self.lock:
            self.check_owner(owner)
            await self._me()  # Verify protocol BEFORE sending credentials.
            status, _ = await self._http("POST", "/login", body={"username": username, "password": password}, timeout=90)
            if status == 202:
                self.pending_owner, self.pending_until = owner, time.monotonic() + 600
                return {"needs_2fa": True, "message": "请输入受信任设备收到的六位验证码（10 分钟内）"}
            if status != 200:
                raise AppleMusicBackendError(f"Apple 登录失败（HTTP {status}）；请检查账号或稍后重试")
            self.pending_owner = None
            return {"needs_2fa": False, "message": "登录请求已完成，请刷新核验登录和订阅状态"}

    async def two_factor(self, owner: str, code: str) -> dict:
        async with self.lock:
            self.check_owner(owner, require_pending=True)
            status, _ = await self._http("POST", "/login/2fa", body={"code": code}, timeout=90)
            if status not in (200, 202):
                raise AppleMusicBackendError(f"验证码提交失败（HTTP {status}），请重试或重新登录")
            if status == 202:
                return {"needs_2fa": True, "message": "仍需双重认证，请检查验证码后重试"}
            self.pending_owner = None
            return {"needs_2fa": False, "message": "验证码已提交，请刷新核验登录和订阅状态"}

    async def clear_session(self, owner: str) -> dict:
        async with self.lock:
            self.check_owner(owner)
            # This is NOT a full logout: Apple's on-disk mpl_db is unchanged.
            status, _ = await self._http("DELETE", "/login", timeout=20)
            if status != 200:
                raise AppleMusicBackendError(f"清除内存会话失败（HTTP {status}）")
            self.pending_owner = None
            return {"needs_2fa": False, "message": "已清除内存会话；wrapper 磁盘登录缓存仍在，重启可能恢复登录"}

    async def _run(self, argv: list[str], *, timeout: int, capture=False, cwd=None) -> bytes:
        if self.closed:
            raise AppleMusicBackendError("Apple Music 后端已停止")
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x08000000}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, **kwargs,
            )
        except (OSError, ValueError):
            raise AppleMusicBackendError("无法启动可选工具，请检查 gamdl Python / FFmpeg 配置") from None
        self._processes.add(process)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode:
                raise AppleMusicBackendError("可选工具执行失败；请确认 gamdl 版本、登录状态和运行依赖")
            return (stdout or b"")[:65536]
        except TimeoutError:
            raise AppleMusicBackendError("Apple Music 操作超时，已终止任务；可稍后重试") from None
        finally:
            await self._stop_process(process)
            self._processes.discard(process)

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                # gamdl can launch FFmpeg. Terminate the bounded process tree.
                child = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                await asyncio.wait_for(child.wait(), 10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, TimeoutError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    def download_task(
        self, url: str, duration: float, *, codec_priority: tuple[str, ...] | None = None
    ) -> asyncio.Task[Path]:
        task = asyncio.create_task(
            self.download(url, duration=duration, codec_priority=codec_priority)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def download(
        self,
        url: str,
        *,
        duration: float = 0,
        codec_priority: tuple[str, ...] | None = None,
    ) -> Path:
        canonical, identifier = normalize_track_url(url)
        try:
            if isinstance(duration, bool):
                raise ValueError
            duration = float(duration)
        except (TypeError, ValueError, OverflowError):
            duration = 0
        if not math.isfinite(duration) or duration <= 0:
            raise AppleMusicBackendError("未取得完整歌曲时长，暂不获取音频")
        if duration > self.cfg.max_duration:
            raise AppleMusicBackendError("歌曲时长超过插件配置限制")
        async with self.lock:
            self._require_enabled()
            if self.pending_owner and time.monotonic() < self.pending_until:
                raise AppleMusicBackendError("请先在 Apple Music 管理页完成双重认证")
            me = await self._me()
            if me["auth"].get("state") != "authenticated":
                raise AppleMusicBackendError("请先在 Apple Music 管理页登录")
            active, _, _ = await self._subscription(me)
            if active is not True:
                raise AppleMusicBackendError("未能确认有效 Apple Music 订阅，请先在管理页检查")
            probe = await self.probe_gamdl()
            if not probe["available"] or not probe["ffmpeg"] or not probe["ffprobe"]:
                raise AppleMusicBackendError(f"请配置独立环境中的 gamdl {GAMDL_VERSION}，并安装 FFmpeg / FFprobe")
            if codec_priority is None:
                try:
                    send_format = getattr(self.cfg.parser.applemusic, "audio_send_format", "file")
                except (AttributeError, TypeError):
                    send_format = "file"
                if str(send_format or "").strip().lower() == "voice":
                    codec_priority = VOICE_CODEC_PRIORITY
                else:
                    try:
                        configured_priority = getattr(
                            self.cfg.parser.applemusic, "file_codec_priority", None
                        )
                    except (AttributeError, TypeError):
                        configured_priority = None
                    codec_priority = normalize_file_codec_priority(configured_priority)
            elif not isinstance(codec_priority, (list, tuple)):
                raise AppleMusicBackendError("Apple Music 音质配置无效")
            codec_priority = tuple(codec_priority)
            if not codec_priority or any(
                not isinstance(codec, str) or codec not in SUPPORTED_FILE_CODECS
                for codec in codec_priority
            ):
                raise AppleMusicBackendError("Apple Music 音质配置无效")
            codec_arg = ",".join(codec_priority)
            directory = create_parse_cache_dir(get_active_cache_dir(self.cfg.cache_dir), "applemusic")
            output, temporary = directory / "output", directory / "temporary"
            output.mkdir()
            temporary.mkdir()
            await self._run([
                self.settings.python, "-m", "gamdl", "--no-config-file", "--no-exceptions",
                "--log-level", "ERROR", "--use-wrapper", "--wrapper-url", self.settings.validate(),
                "--wrapper-decrypt-host", self.settings.decrypt_host,
                "--wrapper-decrypt-port", str(self.settings.decrypt_port),
                "--song-codec-priority", codec_arg, "--no-synced-lyrics",
                "--output-path", str(output), "--temp-path", str(temporary),
                "--album-folder-template", "album", "--compilation-folder-template", "album",
                "--no-album-folder-template", "album",
                "--single-disc-file-template", identifier,
                "--multi-disc-file-template", identifier,
                "--no-album-file-template", identifier, canonical,
            ], timeout=self.settings.timeout, cwd=str(directory))
            # gamdl can exit zero after a track error, so exit code is not proof.
            files = [p for p in output.rglob("*.m4a") if p.is_file() and not p.is_symlink()]
            if len(files) != 1 or not files[0].resolve().is_relative_to(output.resolve()):
                raise AppleMusicBackendError("gamdl 未生成唯一的歌曲音频；登录不代表下载可用")
            path = files[0]
            if not 0 < path.stat().st_size <= self.cfg.max_size:
                raise AppleMusicBackendError("生成的音频为空或超过插件大小限制")
            raw = await self._run([
                shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,codec_tag_string", "-of", "json", str(path),
            ], timeout=20, capture=True)
            try:
                info = json.loads(raw)
                actual = float(info["format"]["duration"])
                playable = any(
                    isinstance(s, dict) and s.get("codec_type") == "audio"
                    and s.get("codec_name") in {
                        "aac", "alac", "ac3", "eac3", "ec-3",
                    }
                    and s.get("codec_tag_string") != "enca"
                    for s in info["streams"]
                )
                # Check the decoded file, not a preview URL or a mere file name.
                if not playable or not math.isfinite(actual) or actual <= 0 or actual > self.cfg.max_duration:
                    raise ValueError
                if duration and abs(actual - duration) > max(5, duration * 0.05):
                    raise AppleMusicBackendError("音频时长与歌曲不一致，可能只有试听，已拒绝发送")
            except (KeyError, TypeError, ValueError):
                raise AppleMusicBackendError("输出文件未通过音频格式/时长检查") from None
            return path

    async def close(self) -> None:
        self.closed = True
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for process in tuple(self._processes):
            await self._stop_process(process)
        if self._session:
            await self._session.close()
