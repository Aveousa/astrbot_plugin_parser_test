"""Per-group parser switches backed by the plugin configuration."""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Mapping

from .apple_music_backend import AppleMusicBackendError
from .session_scope import normalize_session_scope, session_scope_in


class GroupParserConfigError(AppleMusicBackendError):
    """Expected, safe-to-report group-policy validation error."""

GROUP_PARSER_SETTINGS_KEY = "group_parser_settings"
QQ_OFFICIAL_KNOWN_GROUPS_KEY = "qq_official_known_groups"
GROUP_KEY_VERSION = "v1"
GROUP_SETTINGS_MAX_GROUPS = 1000
GROUP_SETTINGS_MAX_PARSERS = 32
PARSER_LABELS = {
    "bilibili": "Bilibili",
    "douyin": "抖音",
    "xhs": "小红书",
    "pixiv": "Pixiv",
    "qqmusic": "QQ 音乐",
    "netease": "网易云音乐",
    "kugou": "酷狗音乐",
    "qishui": "汽水音乐",
    "kuwo": "酷我音乐",
    "applemusic": "Apple Music",
}
GROUP_ID_RE = re.compile(r"^[0-9]{1,32}$")
QQ_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
ADAPTER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
GROUP_UMO_RE = re.compile(
    r"^(?P<platform>[A-Za-z0-9_.-]{1,96}):GroupMessage:(?P<group>[A-Za-z0-9_.-]{1,128})$"
)


def group_settings_key(platform_id: object, group_id: object) -> str:
    platform, group = str(platform_id or ""), str(group_id or "")
    if not ADAPTER_ID_RE.fullmatch(platform) or not QQ_GROUP_ID_RE.fullmatch(group):
        raise AppleMusicBackendError("群聊或适配器实例标识无效")
    return f"{GROUP_KEY_VERSION}:{platform}:{group}"


def supported_parser_keys(config) -> list[str]:
    """Return parsers configured and globally enabled in this plugin."""

    parser_config = getattr(config, "parser", None)
    nodes = getattr(parser_config, "_nodes", None)
    if isinstance(nodes, dict):
        values = nodes.items()
        template_keys = None
    else:
        template = getattr(config, "parsers_template", None)
        template_keys = {
            str(item.get("__template_key"))
            for item in template if isinstance(item, dict) and item.get("__template_key")
        } if isinstance(template, list) else None
        values = (
            (item.get("__template_key"), item)
            for item in template
            if isinstance(item, dict)
        ) if isinstance(template, list) else ()
    keys = []
    for raw_key, item in values:
        key = str(raw_key)
        enabled = (
            item.get("enable", True)
            if isinstance(item, Mapping)
            else getattr(item, "enable", True)
        )
        if (template_keys is None or key in template_keys) and key in PARSER_LABELS and enabled is True:
            keys.append(key)
    return sorted(set(keys), key=lambda key: list(PARSER_LABELS).index(key))


def parser_key_for_keyword(keyword: str) -> str | None:
    """Map parser routing keywords to stable parser switches."""

    if keyword in PARSER_LABELS:
        return keyword
    lowered = keyword.casefold()
    if lowered.startswith("music:"):
        candidate = lowered.split(":", 1)[1]
        return candidate if candidate in PARSER_LABELS else None
    # Qishui links live under music.douyin.com, so detect this service before
    # the broader Douyin host/platform match below.
    if "qishui" in lowered:
        return "qishui"
    if (
        "bili" in lowered
        or lowered in {"av", "bv", "bm", "favlist"}
        or lowered.startswith(("/dynamic/", "/opus/", "/read/", "live.bili"))
    ):
        return "bilibili"
    if (
        "douyin" in lowered
        or "aweme" in lowered
        or lowered in {"webcast.amemv", "iesdouyin"}
        or lowered == ""
    ):
        return "douyin"
    if "xiaohongshu" in lowered or lowered.startswith("xhs"):
        return "xhs"
    if "pixiv" in lowered or lowered in {"pid", "pixivid"}:
        return "pixiv"
    if "163cn.tv" in lowered or ".163.com" in lowered:
        return "netease"
    if "music.apple.com" in lowered or "itunes.apple.com" in lowered:
        return "applemusic"
    if ".qq.com" in lowered:
        return "qqmusic"
    if "kuwo" in lowered:
        return "kuwo"
    if "kugou" in lowered:
        return "kugou"
    if lowered in {"music", "music_base"}:
        return None
    # Music links have several routing keywords but share per-service settings.
    for key in ("qqmusic", "netease", "kugou", "qishui", "kuwo", "applemusic"):
        if key in lowered or (key == "qqmusic" and "y.qq.com" in lowered):
            return key
    return None


def group_enabled_parsers(config, platform_id: object, group_id: object) -> set[str]:
    """Intersect group overrides with global enablement (fail open)."""

    globally_enabled = set(supported_parser_keys(config))
    try:
        key = group_settings_key(platform_id, group_id)
    except AppleMusicBackendError:
        return globally_enabled
    stored = _stored_group_settings(config)
    group = stored.get(key)
    if not isinstance(group, dict):
        return globally_enabled
    disabled = group.get("disabled_parsers")
    if not isinstance(disabled, list):
        return globally_enabled
    disabled_set = {value for value in disabled if isinstance(value, str)}
    return globally_enabled - disabled_set


def group_parsing_enabled(config, platform_id: object, group_id: object) -> bool:
    """Return whether a live group's canonical UMO is outside the parser blacklist."""

    try:
        platform, group = str(platform_id or ""), str(group_id or "")
        group_settings_key(platform, group)
    except AppleMusicBackendError:
        return True
    scope = f"{platform}:GroupMessage:{group}"
    raw = getattr(config, "_data", None)
    blacklist = raw.get("blacklist") if isinstance(raw, Mapping) else None
    if not isinstance(blacklist, list):
        blacklist = getattr(config, "blacklist", [])
    if not isinstance(blacklist, list):
        return True
    return not session_scope_in(scope, blacklist)


def _with_group_blacklist_state(blacklist: list, scope: str, enabled: bool) -> list:
    """Mirror PluginConfig's add/remove semantics without saving mid-transaction."""

    if enabled:
        return [
            value for value in blacklist
            if not (isinstance(value, str) and normalize_session_scope(value) == scope)
        ]

    updated = []
    inserted = False
    for value in blacklist:
        if isinstance(value, str) and normalize_session_scope(value) == scope:
            if not inserted:
                updated.append(scope)
                inserted = True
            continue
        updated.append(value)
    if not inserted:
        updated.append(scope)
    return updated


def _stored_group_settings(config) -> dict:
    raw = getattr(config, "_data", None)
    if not isinstance(raw, Mapping):
        return {}
    stored = raw.get(GROUP_PARSER_SETTINGS_KEY)
    if isinstance(stored, dict):
        # Compatibility for a short-lived development format. PluginConfig
        # migrates it to the schema-stable list before parser initialization.
        return stored
    if not isinstance(stored, list):
        return {}
    result = {}
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        try:
            key = group_settings_key(entry.get("platform_id"), entry.get("group_id"))
        except AppleMusicBackendError:
            continue
        result[key] = entry
    return result


async def _fetch_live_groups(adapter, *, no_cache: bool = False) -> dict[str, str]:
    meta = getattr(adapter, "meta", None)
    meta = meta() if callable(meta) else meta
    platform_id = getattr(meta, "id", None)
    if not isinstance(platform_id, str) or not platform_id:
        return {}
    get_client = getattr(adapter, "get_client", None)
    client = get_client() if callable(get_client) else None
    call_action = getattr(client, "call_action", None)
    if not callable(call_action):
        raise AppleMusicBackendError("当前 OneBot 实例尚未连接，无法读取群聊列表")
    try:
        if no_cache:
            groups = await call_action("get_group_list", no_cache=True)
        else:
            groups = await call_action("get_group_list")
    except Exception:
        raise AppleMusicBackendError("当前无法读取 OneBot 群聊列表，请检查适配器连接") from None
    if not isinstance(groups, list):
        raise AppleMusicBackendError("OneBot 返回的群聊列表无效，请检查适配器连接")
    result: dict[str, str] = {}
    for item in groups:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id", ""))
        if not GROUP_ID_RE.fullmatch(group_id):
            continue
        name = item.get("group_name")
        result[group_id] = name.strip()[:128] if isinstance(name, str) else ""
    return result


def _known_qq_official_groups(config, platform_id: str) -> dict[str, str]:
    """Return QQ Official groups observed in messages from this bot instance.

    QQ Official's public API does not expose a bot-wide group roster, so the
    plugin can only manage groups that have sent it a message at least once.
    """

    raw = getattr(config, "_data", None)
    stored = raw.get(QQ_OFFICIAL_KNOWN_GROUPS_KEY) if isinstance(raw, Mapping) else None
    if not isinstance(stored, list):
        return {}
    result: dict[str, str] = {}
    for item in stored:
        if not isinstance(item, Mapping) or item.get("platform_id") != platform_id:
            continue
        group_id = item.get("group_id")
        if not isinstance(group_id, str) or not QQ_GROUP_ID_RE.fullmatch(group_id):
            continue
        name = item.get("name")
        result[group_id] = (
            name.strip()[:128]
            if isinstance(name, str) and name.strip()
            else f"QQ 官方群 {group_id}"
        )
    return result


def _group_key_from_blacklist_entry(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = GROUP_UMO_RE.fullmatch(normalize_session_scope(value))
    if match is None:
        return None
    try:
        return group_settings_key(match.group("platform"), match.group("group"))
    except AppleMusicBackendError:
        return None


class GroupParserConfigService:
    """Validated API/service logic for group parser policy management."""

    def __init__(self, cfg, context, *, lock: asyncio.Lock | None = None):
        self.cfg = cfg
        self.context = context
        self.lock = lock or asyncio.Lock()

    def _adapters(self) -> list:
        manager = getattr(self.context, "platform_manager", None)
        instances = getattr(manager, "platform_insts", ())
        result = []
        if isinstance(instances, Mapping):
            adapters = list(instances.values())
        elif isinstance(instances, (list, tuple, set)):
            adapters = list(instances)
        elif instances is None:
            adapters = []
        else:
            try:
                adapters = list(instances)
            except TypeError:
                adapters = []
        for adapter in adapters:
            meta = self._adapter_meta(adapter)
            if getattr(meta, "name", None) in {"aiocqhttp", "qq_official"} and isinstance(
                getattr(meta, "id", None), str
            ) and getattr(meta, "id", None):
                result.append(adapter)
        return result

    @staticmethod
    def _adapter_meta(adapter):
        meta = getattr(adapter, "meta", None)
        return meta() if callable(meta) else meta

    def _adapter_by_id(self, platform_id: str):
        return next(
            (
                adapter
                for adapter in self._adapters()
                if getattr(self._adapter_meta(adapter), "id", None) == platform_id
            ),
            None,
        )

    async def _group_snapshot(self, *, no_cache: bool = False) -> tuple[list[dict], set[str]]:
        globally_enabled = supported_parser_keys(self.cfg)
        result = []
        queried_platform_ids: set[str] = set()
        for adapter in self._adapters():
            meta = self._adapter_meta(adapter)
            if meta is None or not isinstance(getattr(meta, "id", None), str):
                continue
            platform_id = meta.id
            if getattr(meta, "name", None) == "qq_official":
                # QQ Official does not provide an endpoint to enumerate every
                # group a bot has joined. Keep the management page scoped to
                # groups observed from real incoming messages.
                live = _known_qq_official_groups(self.cfg, platform_id)
            else:
                live = await _fetch_live_groups(adapter, no_cache=no_cache)
            queried_platform_ids.add(platform_id)
            for group_id, name in live.items():
                enabled = group_enabled_parsers(self.cfg, platform_id, group_id)
                result.append({
                    "platform_id": platform_id,
                    "platform_label": getattr(meta, "adapter_display_name", None)
                    or (
                        "QQ 官方机器人 (WebSocket)"
                        if getattr(meta, "name", None) == "qq_official"
                        else "OneBot V11"
                    ),
                    "group_id": group_id,
                    "name": name,
                    "enabled": sorted(
                        enabled, key=lambda key: list(PARSER_LABELS).index(key)
                    ),
                    "parsing_enabled": group_parsing_enabled(
                        self.cfg, platform_id, group_id
                    ),
                    "globally_enabled": globally_enabled,
                })
        result.sort(
            key=lambda item: (
                item["platform_label"].lower(),
                item["platform_id"].lower(),
                item["group_id"],
            )
        )
        return result, queried_platform_ids

    async def _groups(self, *, no_cache: bool = False) -> list[dict]:
        groups, _ = await self._group_snapshot(no_cache=no_cache)
        return groups

    async def get_groups(self, *, force_refresh: bool = False) -> dict:
        groups, _ = await self._group_snapshot(no_cache=force_refresh)
        globally_enabled = set(supported_parser_keys(self.cfg))
        return {
            "groups": groups,
            "parsers": [
                {"key": key, "label": label, "globally_enabled": key in globally_enabled}
                for key, label in PARSER_LABELS.items()
            ],
        }

    async def _persist_locked(
        self,
        updated: dict,
        original_storage: object,
        key_was_present: bool,
        *,
        updated_blacklist: list | None = None,
        original_blacklist: object = None,
        blacklist_was_present: bool = False,
    ) -> None:
        raw_data = getattr(self.cfg, "_data", None)
        if not isinstance(raw_data, dict):
            raise AppleMusicBackendError("插件配置不可写入")
        if len(updated) > GROUP_SETTINGS_MAX_GROUPS:
            raise AppleMusicBackendError("群聊配置数量过多")
        encoded = []
        for key, entry in updated.items():
            if not isinstance(entry, dict):
                continue
            parts = key.split(":", 2)
            if len(parts) != 3 or parts[0] != GROUP_KEY_VERSION:
                continue
            try:
                group_settings_key(entry.get("platform_id", parts[1]), entry.get("group_id", parts[2]))
            except AppleMusicBackendError:
                continue
            encoded.append({
                "platform_id": str(entry.get("platform_id", parts[1])),
                "group_id": str(entry.get("group_id", parts[2])),
                "disabled_parsers": sorted({
                    parser for parser in entry.get("disabled_parsers", [])
                    if isinstance(parser, str) and parser in PARSER_LABELS
                }),
            })
        raw_data[GROUP_PARSER_SETTINGS_KEY] = encoded
        if updated_blacklist is not None:
            raw_data["blacklist"] = updated_blacklist
        try:
            save = getattr(self.cfg, "save_config", None)
            if not callable(save):
                raise RuntimeError("configuration is not persistent")
            await asyncio.get_running_loop().run_in_executor(None, save)
        except Exception:
            if key_was_present:
                raw_data[GROUP_PARSER_SETTINGS_KEY] = original_storage
            else:
                raw_data.pop(GROUP_PARSER_SETTINGS_KEY, None)
            if updated_blacklist is not None:
                if blacklist_was_present:
                    raw_data["blacklist"] = original_blacklist
                else:
                    raw_data.pop("blacklist", None)
            raise AppleMusicBackendError("群聊解析器设置保存失败，请稍后重试") from None

    async def save_group_settings(self, values: object) -> dict:
        if not isinstance(values, list) or not 0 <= len(values) <= GROUP_SETTINGS_MAX_GROUPS:
            raise GroupParserConfigError("群聊配置无效，请刷新群列表后重试")
        globally_enabled = set(supported_parser_keys(self.cfg))
        submitted: dict[str, tuple[str, str, list[str], bool]] = {}
        for item in values:
            if not isinstance(item, dict) or set(item) != {
                "platform_id", "group_id", "enabled", "parsing_enabled"
            }:
                raise GroupParserConfigError("群聊配置字段无效")
            try:
                key = group_settings_key(item["platform_id"], item["group_id"])
            except AppleMusicBackendError as exc:
                raise GroupParserConfigError(exc.message) from None
            enabled = item["enabled"]
            parsing_enabled = item["parsing_enabled"]
            if (
                not isinstance(enabled, list)
                or len(enabled) > GROUP_SETTINGS_MAX_PARSERS
                or any(not isinstance(value, str) for value in enabled)
                or len(enabled) != len(set(enabled))
                or not set(enabled) <= set(PARSER_LABELS)
            ):
                raise GroupParserConfigError("群聊启用的解析器配置无效")
            if not isinstance(parsing_enabled, bool):
                raise GroupParserConfigError("群聊总解析开关状态无效")
            submitted[key] = (
                str(item["platform_id"]),
                str(item["group_id"]),
                enabled,
                parsing_enabled,
            )
        if len(submitted) != len(values):
            raise GroupParserConfigError("同一群聊不能重复提交")

        async with self.lock:
            globally_enabled = set(supported_parser_keys(self.cfg))
            try:
                live_groups, queried_platform_ids = await self._group_snapshot(
                    no_cache=True
                )
            except AppleMusicBackendError as exc:
                raise GroupParserConfigError(exc.message) from None
            if not queried_platform_ids:
                raise GroupParserConfigError(
                    "未检测到已连接的 OneBot V11 实例，无法安全确认退群配置"
                )
            live_group_ids = {
                group_settings_key(group["platform_id"], group["group_id"])
                for group in live_groups
            }
            qq_official_platform_ids = {
                str(getattr(self._adapter_meta(adapter), "id", ""))
                for adapter in self._adapters()
                if getattr(self._adapter_meta(adapter), "name", None) == "qq_official"
            }
            raw_data = getattr(self.cfg, "_data", None)
            if not isinstance(raw_data, dict):
                raise AppleMusicBackendError("插件配置不可写入")
            stored = _stored_group_settings(self.cfg)
            try:
                original_storage = copy.deepcopy(
                    raw_data.get(GROUP_PARSER_SETTINGS_KEY)
                )
                original = copy.deepcopy(stored) if isinstance(stored, dict) else {}
                blacklist_was_present = "blacklist" in raw_data
                original_blacklist = copy.deepcopy(
                    raw_data.get("blacklist", getattr(self.cfg, "blacklist", []))
                )
            except Exception:
                raise GroupParserConfigError("群聊配置数据无效，请检查插件配置后重试") from None
            if not isinstance(original_blacklist, list):
                raise GroupParserConfigError("群聊解析黑名单配置无效，请检查插件配置后重试")
            updated = dict(original)
            updated_blacklist = list(original_blacklist)
            cleaned_group_count = 0
            cleaned_blacklist_group_count = 0
            # Preserve stored switches for live groups that were not part of
            # this submission. The browser normally posts every group, but a
            # partial API client must not silently re-enable other groups.
            for key, (platform_id, group_id, enabled, parsing_enabled) in submitted.items():
                # A page may still contain a group that was left since it was
                # opened. Ignore that stale draft and let the fresh snapshot
                # below decide whether persisted state should be pruned.
                if key not in live_group_ids:
                    continue
                if not set(enabled) <= globally_enabled:
                    raise GroupParserConfigError(
                        "群聊启用的解析器中包含全局关闭的项目，请刷新群聊配置后重试"
                    )
                previous = updated.get(key)
                previous = previous if isinstance(previous, dict) else {}
                previous_disabled = previous.get("disabled_parsers", [])
                if not isinstance(previous_disabled, list):
                    previous_disabled = []
                # Preserve explicit group disables while a parser is globally
                # off; otherwise turning it on later would unexpectedly enable
                # it in every group where the admin had disabled it before.
                disabled = {
                    value for value in previous_disabled
                    if isinstance(value, str) and value in PARSER_LABELS and value not in globally_enabled
                } | (globally_enabled - set(enabled))
                disabled = sorted(disabled)
                if disabled:
                    updated[key] = {
                        "platform_id": platform_id,
                        "group_id": group_id,
                        "disabled_parsers": disabled,
                    }
                else:
                    updated.pop(key, None)
                scope = f"{platform_id}:GroupMessage:{group_id}"
                updated_blacklist = _with_group_blacklist_state(
                    updated_blacklist, scope, parsing_enabled
                )
            # Only a successfully queried OneBot instance can prove that a
            # stored group has left. QQ Official cannot provide a full group
            # roster, so absence from its observed-group cache is not evidence
            # that the bot has left that group.
            for key in list(updated):
                _, platform_id, _ = key.split(":", 2)
                if (
                    platform_id in queried_platform_ids
                    and platform_id not in qq_official_platform_ids
                    and key not in live_group_ids
                ):
                    updated.pop(key, None)
                    cleaned_group_count += 1

            cleanup_blacklist = getattr(
                self.cfg, "cleanup_blacklist_on_group_leave", None
            )
            if not isinstance(cleanup_blacklist, bool):
                raw_cleanup_blacklist = raw_data.get(
                    "cleanup_blacklist_on_group_leave", True
                )
                cleanup_blacklist = (
                    raw_cleanup_blacklist
                    if isinstance(raw_cleanup_blacklist, bool)
                    else True
                )
            if cleanup_blacklist:
                cleaned_blacklist_keys = set()
                kept_blacklist = []
                for value in updated_blacklist:
                    key = _group_key_from_blacklist_entry(value)
                    if (
                        key is not None
                        and key.split(":", 2)[1] in queried_platform_ids
                        and key.split(":", 2)[1] not in qq_official_platform_ids
                        and key not in live_group_ids
                    ):
                        cleaned_blacklist_keys.add(key)
                        continue
                    kept_blacklist.append(value)
                updated_blacklist = kept_blacklist
                cleaned_blacklist_group_count = len(cleaned_blacklist_keys)
            await self._persist_locked(
                updated,
                original_storage,
                GROUP_PARSER_SETTINGS_KEY in raw_data,
                updated_blacklist=updated_blacklist,
                original_blacklist=original_blacklist,
                blacklist_was_present=blacklist_was_present,
            )

        return {
            "message": "群聊总开关及解析器设置已确认并保存",
            "cleaned_group_count": cleaned_group_count,
            "cleaned_blacklist_group_count": cleaned_blacklist_group_count,
        }

    async def set_group_parser(
        self, platform_id: object, group_id: object, parser_key: object, enabled: bool
    ) -> list[str]:
        if not isinstance(enabled, bool):
            raise AppleMusicBackendError("解析器状态无效")
        if not isinstance(parser_key, str) or parser_key not in PARSER_LABELS:
            raise AppleMusicBackendError("未知的解析器")
        globally_enabled = set(supported_parser_keys(self.cfg))
        if enabled and parser_key not in globally_enabled:
            raise AppleMusicBackendError(f"{PARSER_LABELS[parser_key]}已在全局配置中关闭，群聊无法单独启用")
        try:
            key = group_settings_key(platform_id, group_id)
        except AppleMusicBackendError as exc:
            raise GroupParserConfigError(exc.message) from None
        target_adapter = self._adapter_by_id(str(platform_id))
        try:
            target_meta = self._adapter_meta(target_adapter) if target_adapter else None
            if target_meta is not None and getattr(target_meta, "name", None) == "qq_official":
                # This command runs inside a real group event, which itself is
                # the strongest membership evidence QQ Official exposes.
                known = _known_qq_official_groups(self.cfg, str(platform_id))
                if str(group_id) not in known:
                    await self.remember_qq_official_group(
                        platform_id, group_id, f"QQ 官方群 {group_id}"
                    )
                group_is_live = True
            else:
                group_is_live = (
                    target_adapter is not None
                    and str(group_id) in await _fetch_live_groups(target_adapter)
                )
        except AppleMusicBackendError as exc:
            raise GroupParserConfigError(exc.message) from None
        if not group_is_live:
            raise AppleMusicBackendError("群聊已不在当前 OneBot 实例中")

        async with self.lock:
            globally_enabled = set(supported_parser_keys(self.cfg))
            if enabled and parser_key not in globally_enabled:
                raise AppleMusicBackendError(
                    f"{PARSER_LABELS[parser_key]}已在全局配置中关闭，群聊无法单独启用"
                )
            raw_data = getattr(self.cfg, "_data", None)
            if not isinstance(raw_data, dict):
                raise AppleMusicBackendError("插件配置不可写入")
            store = _stored_group_settings(self.cfg)
            try:
                original_storage = copy.deepcopy(
                    raw_data.get(GROUP_PARSER_SETTINGS_KEY)
                )
                original = copy.deepcopy(store) if isinstance(store, dict) else {}
            except Exception:
                raise GroupParserConfigError("群聊配置数据无效，请检查插件配置后重试") from None
            store = dict(original)
            entry = store.get(key)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry["platform_id"] = str(platform_id)
            entry["group_id"] = str(group_id)
            disabled_values = entry.get("disabled_parsers", [])
            if not isinstance(disabled_values, list):
                disabled_values = []
            disabled = {
                value for value in disabled_values
                if isinstance(value, str) and value in PARSER_LABELS
            }
            if enabled:
                disabled.discard(parser_key)
            else:
                disabled.add(parser_key)
            if disabled:
                entry["disabled_parsers"] = sorted(disabled)
                store[key] = entry
            else:
                store.pop(key, None)
            await self._persist_locked(
                store,
                original_storage,
                GROUP_PARSER_SETTINGS_KEY in raw_data,
            )

        return sorted(
            group_enabled_parsers(self.cfg, platform_id, group_id),
            key=lambda key: list(PARSER_LABELS).index(key),
        )

    async def remember_qq_official_group(
        self, platform_id: object, group_id: object, name: object = None
    ) -> bool:
        """Persist a QQ Official group after observing an incoming message."""

        platform = str(platform_id or "")
        group = str(group_id or "")
        if not ADAPTER_ID_RE.fullmatch(platform) or not QQ_GROUP_ID_RE.fullmatch(group):
            return False
        adapter = self._adapter_by_id(platform)
        meta = self._adapter_meta(adapter) if adapter is not None else None
        if getattr(meta, "name", None) != "qq_official":
            return False

        label = name.strip()[:128] if isinstance(name, str) else ""
        async with self.lock:
            raw_data = getattr(self.cfg, "_data", None)
            if not isinstance(raw_data, dict):
                return False
            original = copy.deepcopy(raw_data.get(QQ_OFFICIAL_KNOWN_GROUPS_KEY))
            stored = raw_data.get(QQ_OFFICIAL_KNOWN_GROUPS_KEY)
            entries = [
                dict(item)
                for item in stored
                if isinstance(item, dict)
                and isinstance(item.get("platform_id"), str)
                and isinstance(item.get("group_id"), str)
                and ADAPTER_ID_RE.fullmatch(item["platform_id"])
                and QQ_GROUP_ID_RE.fullmatch(item["group_id"])
            ] if isinstance(stored, list) else []
            existing = next(
                (
                    item for item in entries
                    if item["platform_id"] == platform and item["group_id"] == group
                ),
                None,
            )
            if existing is not None:
                if not label:
                    return False
                if existing.get("name") == label:
                    return False
                existing["name"] = label
            else:
                if len(entries) >= GROUP_SETTINGS_MAX_GROUPS:
                    return False
                entries.append({
                    "platform_id": platform,
                    "group_id": group,
                    "name": label or f"QQ 官方群 {group}",
                })
            raw_data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = entries
            try:
                save = getattr(self.cfg, "save_config", None)
                if not callable(save):
                    raise RuntimeError("configuration is not persistent")
                await asyncio.get_running_loop().run_in_executor(None, save)
            except Exception:
                if original is None:
                    raw_data.pop(QQ_OFFICIAL_KNOWN_GROUPS_KEY, None)
                else:
                    raw_data[QQ_OFFICIAL_KNOWN_GROUPS_KEY] = original
                raise AppleMusicBackendError("QQ 官方群聊记录保存失败") from None
        return True
