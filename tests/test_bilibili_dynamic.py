from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from msgspec import convert

from core.parsers.bilibili.dynamic import DynamicData


def _dynamic_response() -> dict:
    author = {
        "name": "转发作者",
        "face": "https://img.example/avatar.png",
        "mid": 123,
        "pub_time": "刚刚",
        "pub_ts": "1792756828",
    }
    return {
        "item": {
            "id_str": "1262370036001013827",
            "type": "DYNAMIC_TYPE_FORWARD",
            "visible": True,
            "modules": {
                "module_author": author,
                "module_dynamic": {
                    "desc": {"text": "转发动态正文", "rich_text_nodes": []},
                    "major": None,
                },
                "module_stat": {
                    "like": {"count": 1375},
                    "comment": {"count": 42},
                    "forward": {"count": 3},
                },
            },
            "orig": {
                "id_str": "1260004273493639190",
                "type": "DYNAMIC_TYPE_DRAW",
                "visible": True,
                "modules": {
                    "module_author": author,
                    "module_dynamic": {
                        "desc": None,
                        "major": {
                            "type": "MAJOR_TYPE_DRAW",
                            "draw": {
                                "items": [
                                    {
                                        "src": f"https://img.example/{index}.png",
                                        "width": 1080,
                                        "height": 1440,
                                    }
                                    for index in range(9)
                                ]
                            },
                        },
                    },
                    "module_stat": None,
                },
            },
        }
    }


def test_dynamic_data_reads_forward_body_original_and_draw_images():
    dynamic = convert(_dynamic_response(), DynamicData).item

    assert dynamic.type == "DYNAMIC_TYPE_FORWARD"
    assert dynamic.name == "转发作者"
    assert dynamic.text == "转发动态正文"
    assert dynamic.dynamic_url == "https://t.bilibili.com/1262370036001013827"
    assert dynamic.orig is not None
    assert dynamic.orig.dynamic_url == "https://t.bilibili.com/1260004273493639190"
    assert dynamic.orig.name == "转发作者"
    assert len(dynamic.orig.image_urls) == 9
    assert dynamic.orig.image_urls[0] == "https://img.example/0.png"


def test_parse_dynamic_builds_card_ready_repost(monkeypatch):
    import bilibili_api.dynamic as dynamic_api

    from core.parsers.bilibili import BilibiliParser

    class FakeDynamic:
        def __init__(self, dynamic_id, _credential):
            assert dynamic_id == 1262370036001013827

        async def get_info(self):
            return _dynamic_response()

    monkeypatch.setattr(dynamic_api, "Dynamic", FakeDynamic)

    parser = BilibiliParser.__new__(BilibiliParser)
    parser.headers = {}
    parser.cfg = SimpleNamespace(
        parser=SimpleNamespace(bilibili=SimpleNamespace(use_proxy=False))
    )
    async def credential():
        return None

    parser.login = SimpleNamespace(credential=credential())
    parser.downloader = SimpleNamespace(
        download_img=lambda url, **_kwargs: Path(url.rsplit("/", 1)[-1])
    )

    result = asyncio.run(parser.parse_dynamic(1262370036001013827))

    assert result.url == "https://t.bilibili.com/1262370036001013827"
    assert result.author is not None and result.author.name == "转发作者"
    assert result.text == "转发动态正文"
    assert result.like_count == 1375
    assert result.comment_count == 42
    assert result.share_count == 3
    assert result.extra["bilibili_text_card_only"] is True
    assert result.repost is not None
    assert result.repost.url == "https://t.bilibili.com/1260004273493639190"
    assert result.repost.author is not None and result.repost.author.name == "转发作者"
    assert len(result.repost.contents) == 9
