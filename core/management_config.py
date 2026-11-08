"""Schema-backed, authenticated management configuration and live updates.

Only the explicitly exposed fields are writable. Transactions use the same
business -> configuration lock order as the existing music management API.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import io
import json
import os
import re
import struct
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from apscheduler.triggers.cron import CronTrigger
from PIL import Image

from .apple_music_backend import AppleMusicBackendError, BackendSettings, SUPPORTED_FILE_CODECS
from .config import save_config_to_completion


GLOBAL_GROUPS = {
    "解析行为": ("arbiter", "require_at_in_group", "debounce_interval", "source_max_size", "source_max_minute"),
    "消息发送": ("audio_to_file", "forward_threshold", "compact_forward_non_audio"),
    "卡片与外观": (
        "card_enabled", "card_template", "card_custom_template", "card_custom_font", "custom_logo",
        "card_dynamic_color", "lyrics_show_release_time", "lyrics_card_template", "emoji_style",
    ),
    "网络与下载": ("show_download_fail_tip", "download_timeout", "download_retry_times", "common_timeout", "proxy"),
    "缓存清理": ("clean_cron",),
}
WRAPPER_FIELDS = frozenset({
    "backend_enabled", "wrapper_url", "wrapper_decrypt_host", "wrapper_decrypt_port", "gamdl_python",
})
ASSET_LIMITS = {"card_custom_font": 20 * 1024 * 1024, "custom_logo": 2 * 1024 * 1024}


class ManagementConfigService:
    def __init__(self, cfg, backend, *, apply_runtime=None):
        self.cfg = cfg
        self.backend = backend
        self.apply_runtime = apply_runtime
        self.schema = json.loads((Path(__file__).parent.parent / "_conf_schema.json").read_text(encoding="utf-8"))
        self.globals = {key: self.schema[key] for keys in GLOBAL_GROUPS.values() for key in keys}
        self.templates = self.schema["parsers_template"]["templates"]

    def _node(self, scope):
        if scope == "global":
            return self.cfg, self.globals
        key = "applemusic" if scope == "wrapper" else scope
        if key not in self.templates:
            raise AppleMusicBackendError("未知的解析器配置")
        node = self.backend._parser_item(key)
        if node is None:
            raise AppleMusicBackendError("该解析器尚未配置，请先在 AstrBot 插件配置中添加")
        fields = self.templates[key]["items"]
        if scope == "wrapper":
            fields = {key: value for key, value in fields.items() if key in WRAPPER_FIELDS}
        elif key == "applemusic":
            fields = {key: value for key, value in fields.items() if key not in WRAPPER_FIELDS}
        return node, fields

    def _fields(self, fields):
        result = copy.deepcopy(fields)
        for key, options in result.items():
            if key == "cookies":
                options["write_only"] = True
            if key in ASSET_LIMITS:
                options["max_bytes"] = ASSET_LIMITS[key]
            if key == "card_template":
                available = getattr(self.cfg, "available_card_templates", None)
                if callable(available):
                    for name in available():
                        if name not in options["options"]:
                            options["options"].append(name)
                            options.setdefault("labels", []).append(name)
            if key == "max_page":
                options["slider"] = {"min": 0, "max": 10000, "step": 1}
            if key == "wrapper_decrypt_port":
                options["slider"] = {"min": 1, "max": 65535, "step": 1}
        return result

    def _values(self, node, fields):
        result = {}
        for key, field in fields.items():
            if key == "cookies":
                continue
            value = getattr(node, key, None)
            value = copy.deepcopy(field.get("default") if value is None else value)
            if key in ASSET_LIMITS:
                # Return only display names, never accept these back as paths.
                if isinstance(value, str):
                    value = [value]
                value = [str(item).replace("\\", "/").rsplit("/", 1)[-1] for item in (value or [])]
            result[key] = value
        return result

    def describe(self):
        parsers = []
        cookie_states = self.backend._parser_config_entries()
        for key, template in self.templates.items():
            node = self.backend._parser_item(key)
            fields = template["items"]
            if key == "applemusic":
                fields = {name: value for name, value in fields.items() if name not in WRAPPER_FIELDS}
            values = self._values(node, fields) if node is not None else {
                name: copy.deepcopy(value.get("default")) for name, value in fields.items() if name != "cookies"
            }
            if node is None:
                values["enable"] = False
            parsers.append({
                "key": key, "label": template["name"], "configured": node is not None,
                "fields": self._fields(fields), "values": values,
                "cookie_configured": bool(getattr(node, "cookies", None)) or bool(cookie_states.get(key, {}).get("cookie_configured")),
                "cookie_status": cookie_states.get(key, {}).get("cookie_status"),
            })
        apple = self.backend._parser_item("applemusic")
        wrapper = {key: value for key, value in self.templates["applemusic"]["items"].items() if key in WRAPPER_FIELDS}
        return {
            "groups": GLOBAL_GROUPS,
            "global_fields": self._fields(self.globals), "global_values": self._values(self.cfg, self.globals),
            "wrapper_fields": self._fields(wrapper), "wrapper_values": self._values(apple, wrapper),
            "wrapper_configured": apple is not None, "parsers": parsers,
        }

    def _validate(self, key, value, field):
        field = self._fields({key: field})[key]
        label = field["description"]
        kind = field["type"]
        if kind == "bool":
            valid = isinstance(value, bool)
        elif kind == "int":
            bounds = field.get("slider", {"min": 0, "max": 10000})
            valid = type(value) is int and bounds["min"] <= value <= bounds["max"]
        elif kind in {"string", "text"}:
            valid = isinstance(value, str) and len(value) <= (16384 if key == "cookies" else 2048)
        elif kind == "list":
            allowed = field.get("options", SUPPORTED_FILE_CODECS if key == "file_codec_priority" else ())
            valid = (
                isinstance(value, list) and 1 <= len(value) <= 32
                and all(isinstance(item, str) and item in allowed for item in value)
                and len(value) == len(set(value))
            )
        else:
            valid = False  # Uploaded paths cannot be supplied by JSON callers.
        if not valid:
            raise AppleMusicBackendError(f"{label}的值或范围无效")
        options = self._fields({key: field})[key].get("options")
        if options and kind != "list" and value not in options:
            raise AppleMusicBackendError(f"{label}包含不支持的选项")
        if key == "cookies" and any(char in value for char in ("\x00", "\r")):
            raise AppleMusicBackendError("Cookie 格式无效")
        if key == "clean_cron":
            try:
                CronTrigger.from_crontab(value, timezone=getattr(self.cfg, "timezone", None))
            except (TypeError, ValueError) as exc:
                raise AppleMusicBackendError("Cron 须为有效的五段表达式") from exc
        if key == "card_custom_template" and value and not re.fullmatch(r"[\w-]+(?:\.html)?", value):
            raise AppleMusicBackendError("模板文件名不能包含路径或特殊字符")
        if key in {"proxy", "worker_proxy_url", "audio_api_base"} and value:
            try:
                parsed = urlsplit(value)
                valid_url = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
                if not valid_url or any(char.isspace() for char in value):
                    raise ValueError
            except ValueError as exc:
                raise AppleMusicBackendError(f"{label}须为有效的 HTTP(S) 地址") from exc
        return None if key == "proxy" and not value else copy.deepcopy(value)

    @staticmethod
    async def _finish(coroutine):
        """Do not release a transaction/file lock while a worker is committing."""
        task = asyncio.create_task(coroutine)
        cancellation = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                break
        try:
            result = task.result()
        except BaseException:
            if cancellation is not None:
                raise cancellation
            raise
        if cancellation is not None:
            raise cancellation
        return result

    async def save(self, scope, settings, *, owner="", asset=False):
        if not isinstance(scope, str) or not isinstance(settings, dict) or not settings:
            raise AppleMusicBackendError("没有可保存的配置")
        node, fields = self._node(scope)
        if set(settings) - fields.keys() or ("cookies" in settings and not asset):
            raise AppleMusicBackendError("配置中包含不支持的字段，请使用专用上传或 Cookie 操作")
        values = {
            key: copy.deepcopy(value) if asset and key in set(ASSET_LIMITS) | {"cookies"} else self._validate(key, value, fields[key])
            for key, value in settings.items()
        }
        async with self.backend.lock, self.backend.config_write_lock:
            if self.backend.closed:
                raise AppleMusicBackendError("插件已关闭，请刷新页面后重试")
            self.backend.check_owner(owner)
            return await self._finish(self._commit(scope, values))

    async def _commit(self, scope, values):
        # Look up the node again under the write lock in case it was reloaded.
        node, _ = self._node(scope)
        raw = node._data
        before = {key: (key in raw, copy.deepcopy(raw.get(key))) for key in values}
        history_key = "__parser_management_history_v1"
        history_before = (history_key in self.cfg._data, copy.deepcopy(self.cfg._data.get(history_key)))
        cookie_files = {}
        previous_check = self.backend._music_cookie_checks.get(scope)
        if "cookies" in values:
            root = Path(self.cfg.cookie_dir).resolve()
            paths = [root / f"{scope}_cookies.txt"]
            if scope == "bilibili":
                paths.append(root / "bilibili_credential.json")
            for path in paths:
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise AppleMusicBackendError("Cookie 缓存路径不可用")
                cookie_files[path] = path.read_bytes() if path.exists() else None
        committed = False
        previous_backend = self.backend.settings
        try:
            for key, value in values.items():
                setattr(node, key, value)
            if "cookies" in values:
                history = self.cfg._data.setdefault(history_key, {})
                if not isinstance(history, dict):
                    history = self.cfg._data[history_key] = {}
                entry = history.get(scope)
                history[scope] = {**(entry if isinstance(entry, dict) else {}), "cookie_configured": bool(values["cookies"])}
            if scope == "wrapper":
                BackendSettings.from_config(self.cfg).validate()
            outcome = await save_config_to_completion(self.cfg)
            if outcome.error is not None:
                raise outcome.error
            committed = True
            # After persistence, replace files and parsers without yielding to
            # an old request that could restore its cached login in between.
            for path in cookie_files:
                path.unlink(missing_ok=True)
            self.backend.settings = BackendSettings.from_config(self.cfg)
            if self.apply_runtime is not None:
                await self.apply_runtime(
                    set(values) if scope == "global" else set(),
                    {"applemusic" if scope == "wrapper" else scope} if scope != "global" else set(),
                )
            if "cookies" in values:
                self.backend._music_cookie_checks.pop(scope, None)
        except BaseException as exc:
            self._restore(node, before)
            if "cookies" in values:
                if history_before[0]:
                    self.cfg._data[history_key] = history_before[1]
                else:
                    self.cfg._data.pop(history_key, None)
            self.backend.settings = previous_backend
            for path, content in cookie_files.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            if previous_check is not None:
                self.backend._music_cookie_checks[scope] = previous_check
            if not committed:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, AppleMusicBackendError):
                    raise
                raise AppleMusicBackendError("配置保存失败，未应用修改，请重试") from None
            rollback = await save_config_to_completion(self.cfg)
            if self.apply_runtime is not None:
                await self.apply_runtime(
                    set(values) if scope == "global" else set(),
                    {"applemusic" if scope == "wrapper" else scope} if scope != "global" else set(),
                )
            if rollback.error is not None:
                raise AppleMusicBackendError("运行时更新及配置回滚未完成，请检查磁盘并刷新配置") from None
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AppleMusicBackendError("运行时更新失败，配置已回滚") from exc
        if scope == "wrapper":
            self.backend.pending_owner = None
            self.backend.pending_until = 0
            self.backend._last_auth_state = "unknown"
        self.backend._probe_cache = None
        if outcome.cancellation is not None:
            raise outcome.cancellation
        return {"message": "配置已保存并生效，后续任务将使用新设置", "configuration": self.describe()}

    @staticmethod
    def _restore(node, before):
        for key, (present, value) in before.items():
            if present:
                setattr(node, key, value)
            else:
                node._data.pop(key, None)
                node.__dict__.get("_children", {}).pop(key, None)

    async def save_cookie(self, owner, key, cookie, *, clear=False):
        if not isinstance(key, str) or key not in self.templates or "cookies" not in self.templates[key]["items"] or not isinstance(clear, bool):
            raise AppleMusicBackendError("不支持的 Cookie 操作")
        cookie = "" if clear else self._validate("cookies", cookie, self.templates[key]["items"]["cookies"])
        return await self.save(key, {"cookies": cookie}, owner=owner, asset=True)

    @staticmethod
    def _validate_asset(field, suffix, payload):
        if field == "custom_logo":
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != "PNG" or image.width * image.height > 4096 * 4096:
                    raise ValueError("Invalid PNG")
                image.verify()
            return
        signatures = {".ttf": (b"\x00\x01\x00\x00", b"true"), ".otf": (b"OTTO",), ".woff2": (b"wOF2",)}
        if payload[:4] not in signatures.get(suffix, ()):
            raise ValueError("Invalid font signature")
        if suffix == ".woff2":
            if len(payload) < 48 or struct.unpack_from(">I", payload, 8)[0] != len(payload):
                raise ValueError("Invalid WOFF2 header")
        else:
            if len(payload) < 12:
                raise ValueError("Invalid font header")
            count = struct.unpack_from(">H", payload, 4)[0]
            if not count or 12 + count * 16 > len(payload):
                raise ValueError("Invalid font table")
            for index in range(count):
                offset, length = struct.unpack_from(">II", payload, 12 + index * 16 + 8)
                if offset + length > len(payload):
                    raise ValueError("Invalid font bounds")

    async def upload(self, owner, field, name=None, encoded=None, *, clear=False):
        if not isinstance(field, str) or field not in ASSET_LIMITS or not isinstance(clear, bool):
            raise AppleMusicBackendError("不支持的文件类型")
        if clear:
            return await self.save("global", {field: []}, owner=owner, asset=True)
        if not isinstance(name, str) or len(name) > 255 or not isinstance(encoded, str):
            raise AppleMusicBackendError("无效的上传文件")
        suffix = Path(name).suffix.lower()
        if suffix.lstrip(".") not in self.globals[field]["file_types"] or len(encoded) > (ASSET_LIMITS[field] + 2) // 3 * 4:
            raise AppleMusicBackendError("文件类型不支持或文件过大")
        try:
            payload = base64.b64decode(encoded, validate=True)
            if not 0 < len(payload) <= ASSET_LIMITS[field]:
                raise ValueError
            await asyncio.to_thread(self._validate_asset, field, suffix, payload)
        except (binascii.Error, ValueError, OSError, Image.DecompressionBombError) as exc:
            raise AppleMusicBackendError("文件内容无效，请上传有效的字体或 PNG") from exc
        root = Path(self.cfg.data_dir).resolve()
        directory = root / "files" / field
        if not directory.resolve().is_relative_to(root):
            raise AppleMusicBackendError("上传目录不可用")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{uuid.uuid4().hex}{suffix}"
        temporary = destination.with_suffix(".part")
        try:
            def write_asset():
                with temporary.open("xb") as target:
                    target.write(payload)
                os.replace(temporary, destination)
            await self._finish(asyncio.to_thread(write_asset))
            # Keep previous uploads for in-flight cards; clearing selects the
            # bundled default and never removes another user's files.
            return await self.save("global", {field: [destination.relative_to(root).as_posix()]}, owner=owner, asset=True)
        except BaseException:
            # A cancelled save may have committed. Never delete its live file.
            if destination.relative_to(root).as_posix() not in (getattr(self.cfg, field, []) or []):
                destination.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
