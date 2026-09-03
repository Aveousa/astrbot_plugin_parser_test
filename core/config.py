from __future__ import annotations

import json
import zoneinfo
from collections.abc import Mapping, MutableMapping
from datetime import timezone
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool
    worker_proxy_enabled: bool | None
    worker_proxy_url: str | None
    cookies: str | None
    video_codec_list: list | None
    video_quality: str | None
    nsfw: str | None
    multi_image_forward: bool | None
    max_page: int | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    SUPPORTED = frozenset({"bilibili", "douyin", "xhs", "pixiv"})
    bilibili: ParserItem
    douyin: ParserItem
    xhs: ParserItem
    pixiv: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return [key for key in self._nodes if key in self.SUPPORTED]

    def enabled_platforms(self) -> list[str]:
        return [
            k
            for k, v in self._nodes.items()
            if k in self.SUPPORTED and getattr(v, "enable", True)
        ]


class PluginConfig(ConfigNode):
    whitelist: list[str]
    blacklist: list[str]

    arbiter: bool
    require_at_in_group: bool
    debounce_interval: int

    source_max_size: int
    source_max_minute: int

    audio_to_file: bool
    single_heavy_render_card: bool
    forward_threshold: int

    # 信息卡片只有一个开关：开启即渲染并发送，关闭即完全跳过卡片链路。
    card_enabled: bool
    card_template: str
    card_custom_template: str | None
    card_dynamic_color: bool
    emoji_style: str

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None

    clean_cron: str

    parsers_template: list[dict[str, Any]]

    # 必须与 metadata.yaml 中的 name 保持一致。这样测试版会使用独立的
    # 安装目录、配置、Cookie 和卡片模板目录，可与原版同时运行。
    _plugin_name = "astrbot_plugin_parser_test"
    _supported_parser_names = ("bilibili", "douyin", "xhs", "pixiv")

    def __init__(self, config: AstrBotConfig, context: Context):
        defaults_changed = self._migrate_card_switches(config)
        # 旧版本配置没有卡片字段时安全补默认值，避免 ConfigNode 对缺失
        # 字段返回 None 进而改变旧的发送策略。
        defaults = {
            "card_enabled": True,
            "card_template": "apple",
            "card_custom_template": "",
            "card_dynamic_color": False,
            "emoji_style": "APPLE",
        }
        for key, value in defaults.items():
            if config.get(key) is None:
                config[key] = value
                defaults_changed = True
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = self._data.get("emoji_cdn", "https://emojicdn.elk.sh")

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        try:
            self.timezone = zoneinfo.ZoneInfo(tz or "Asia/Shanghai")
        except zoneinfo.ZoneInfoNotFoundError:
            # 精简开发环境可能没有 tzdata；不影响解析和渲染，退回 UTC。
            self.timezone = timezone.utc

        # ---------- 路径 ----------
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        installed_template_file = self.plugin_dir / "default_template.json"
        bundled_template_file = Path(__file__).parent.parent / "default_template.json"
        self.default_template_file = (
            installed_template_file
            if installed_template_file.exists()
            else bundled_template_file
        )
        self.template_dir = self.data_dir / "templates"
        self.template_dir.mkdir(parents=True, exist_ok=True)

        if defaults_changed:
            self.save_config()

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = self.load_parser_template(
                self.default_template_file
            )
            self.save_config()

        self._migrate_parser_template()

        self.parser = ParserConfig(self.parsers_template)

    @staticmethod
    def _migrate_card_switches(config: MutableMapping[str, Any]) -> bool:
        """将旧三开关压缩为一个，并保留其历史上的实际生效结果。"""
        legacy_keys = ("card_render_enabled", "card_send_enabled")
        legacy_values = [
            bool(config[key])
            for key in legacy_keys
            if config.get(key) is not None
        ]
        configured_value = config.get("card_enabled")
        effective_value = (
            bool(configured_value)
            if configured_value is not None
            else all(legacy_values) if legacy_values else True
        )
        if legacy_values:
            effective_value = effective_value and all(legacy_values)

        changed = configured_value is None or bool(configured_value) != effective_value
        if changed:
            config["card_enabled"] = effective_value

        for key in legacy_keys:
            if key in config:
                config.pop(key)
                changed = True
        return changed

    def available_card_templates(self) -> list[str]:
        """返回内置与用户模板名称，供配置页和运行时校验使用。"""
        names = {"default", "compact", "apple"}
        for directory in (self.template_dir, self.plugin_dir / "templates"):
            if directory.is_dir():
                names.update(path.stem for path in directory.glob("*.html"))
        return sorted(names)

    def _migrate_parser_template(self) -> None:
        """移除旧平台配置，并为四个保留平台补齐最新默认字段。"""
        defaults = self.load_parser_template(self.default_template_file)
        if not any(
            isinstance(item, dict)
            and item.get("__template_key") in self._supported_parser_names
            for item in defaults
        ):
            # 安装目录若仍是旧版本模板，使用随当前代码发布的模板作为迁移基准。
            bundled = Path(__file__).parent.parent / "default_template.json"
            if bundled != self.default_template_file:
                defaults = self.load_parser_template(bundled)
        if not defaults:
            return
        existing = {
            str(item.get("__template_key")): item
            for item in self.parsers_template
            if isinstance(item, dict)
            and item.get("__template_key") in self._supported_parser_names
        }
        normalized: list[dict[str, Any]] = []
        for default in defaults:
            if not isinstance(default, dict):
                continue
            key = str(default.get("__template_key", ""))
            if key not in self._supported_parser_names:
                # 安装目录可能残留旧版本模板；不要把已移除平台重新写回配置。
                continue
            merged = default.copy()
            # 保留用户已填写的 cookie、代理等有效字段；默认值补新字段。
            if old := existing.get(key):
                merged.update(old)
                # 早期默认模板曾使用单值 ``video_codecs``；迁移为当前
                # 配置页的列表字段，避免升级后丢失用户的编码偏好。
                if key == "bilibili":
                    legacy_codecs = old.get("video_codecs")
                    if "video_codec_list" not in old and legacy_codecs:
                        merged["video_codec_list"] = (
                            legacy_codecs
                            if isinstance(legacy_codecs, list)
                            else [legacy_codecs]
                        )
                    merged.pop("video_codecs", None)
            normalized.append(merged)

        if not normalized:
            return
        if list(self.parsers_template) != normalized:
            self.parsers_template[:] = normalized
            self.save_config()

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                if not isinstance(template, list):
                    raise TypeError("解析器模板根节点必须为列表")
                logger.info(f"[parser] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[parser] 加载模板失败: {e}")
            return []

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()
