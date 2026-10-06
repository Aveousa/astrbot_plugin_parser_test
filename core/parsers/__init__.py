"""插件支持的解析器。

只导出产品当前支持的平台；未导出的旧解析器不会被导入，也不会注册到
``BaseParser``，从而避免无意触发其网络请求或配置项。
"""

from .base import BaseParser
from .bilibili import BilibiliParser
from .douyin import DouyinParser
from .pixiv import PixivParser
from .xhs import XHSParser
from .music import (
    KugouMusicParser,
    NetEaseMusicParser,
    QQMusicParser,
    QishuiMusicParser,
)

__all__ = [
    "BaseParser",
    "BilibiliParser",
    "DouyinParser",
    "XHSParser",
    "PixivParser",
    "QQMusicParser",
    "NetEaseMusicParser",
    "KugouMusicParser",
    "QishuiMusicParser",
]
