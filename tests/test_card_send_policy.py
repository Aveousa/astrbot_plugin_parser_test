from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.data import (
    AudioContent,
    DynamicContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    Platform,
    SendGroup,
    VideoContent,
)
from core.exception import DownloadException


@pytest.fixture
def sender_module(monkeypatch: pytest.MonkeyPatch):
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(error=lambda *a, **k: None)
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    message = types.ModuleType("astrbot.core.message")
    message.__path__ = []
    components = types.ModuleType("astrbot.core.message.components")
    platform = types.ModuleType("astrbot.core.platform")
    platform.__path__ = []
    event_mod = types.ModuleType("astrbot.core.platform.astr_message_event")

    class BaseMessageComponent: ...

    class Image(BaseMessageComponent):
        def __init__(self, path=None):
            self.path = path

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path)

    class Video(Image): ...
    class Record(Image): ...

    class File(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Plain(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Node(BaseMessageComponent):
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.content = kwargs.get("content")

    class Nodes(BaseMessageComponent):
        def __init__(self, nodes): self.nodes = nodes

    for name, value in {
        "BaseMessageComponent": BaseMessageComponent,
        "File": File,
        "Image": Image,
        "Node": Node,
        "Nodes": Nodes,
        "Plain": Plain,
        "Record": Record,
        "Video": Video,
    }.items():
        setattr(components, name, value)
    event_mod.AstrMessageEvent = object

    config = types.ModuleType("core.config")
    config.PluginConfig = object
    renderer = types.ModuleType("core.render")
    renderer.Renderer = object
    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.core": core,
        "astrbot.core.message": message,
        "astrbot.core.message.components": components,
        "astrbot.core.platform": platform,
        "astrbot.core.platform.astr_message_event": event_mod,
        "core.config": config,
        "core.render": renderer,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "core.sender", raising=False)
    return importlib.import_module("core.sender")


def _result() -> ParseResult:
    return ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        contents=[VideoContent(Path("video.mp4"))],
    )


class _Event:
    def __init__(self):
        self.sent = []
        self.bot = None
        self.sender_id = "456"
        self.sender_name = "Link sender"

    def chain_result(self, segments):
        return segments

    async def send(self, message):
        self.sent.append(message)

    def get_self_id(self):
        return "bot"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return self.sender_name


class _Renderer:
    def __init__(self, card_path: Path = Path("card.png")):
        self.card_path = card_path
        self.calls: list[ParseResult] = []

    async def render_card(self, result: ParseResult):
        self.calls.append(result)
        return self.card_path


def _card_config(**overrides):
    config = {
        "forward_threshold": 2,
        "compact_forward_non_audio": False,
        "card_enabled": True,
        "show_download_fail_tip": True,
        "audio_to_file": True,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


def test_forward_nodes_use_triggering_sender_identity(sender_module):
    sender = sender_module.MessageSender(
        _card_config(),
        _Renderer(),
    )
    event = _Event()

    folded = sender._merge_segments_if_needed(
        event,
        [sender_module.Plain("parsed content")],
        force_merge=True,
    )

    node = folded[0].nodes[0]
    assert node.kwargs["uin"] == "456"
    assert node.kwargs["name"] == "Link sender"


def test_forward_nodes_fall_back_to_parser_identity_without_sender(sender_module):
    sender = sender_module.MessageSender(
        _card_config(),
        _Renderer(),
    )
    event = _Event()
    event.sender_id = ""
    event.sender_name = ""

    folded = sender._merge_segments_if_needed(
        event,
        [sender_module.Plain("parsed content")],
        force_merge=True,
    )

    node = folded[0].nodes[0]
    assert node.kwargs["uin"] == "bot"
    assert node.kwargs["name"] == "解析器"


def test_compact_forward_non_audio_mode_folds_multiple_contents(
    sender_module,
):
    images = [ImageContent(Path("one.png")), ImageContent(Path("two.png"))]
    result = ParseResult(platform=Platform("bilibili", "Bilibili"), contents=images)
    sender = sender_module.MessageSender(
        _card_config(forward_threshold=100, compact_forward_non_audio=True),
        _Renderer(),
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in event.sent[0][0].nodes] == [
        "card.png", "one.png", "two.png",
    ]


def test_compact_forward_non_audio_mode_flattens_explicit_groups(
    sender_module,
):
    first = ImageContent(Path("one.png"))
    second = ImageContent(Path("two.png"))
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        send_groups=[
            SendGroup(contents=[first], force_merge=False),
            SendGroup(contents=[second], force_merge=False),
        ],
    )
    sender = sender_module.MessageSender(
        _card_config(forward_threshold=100, compact_forward_non_audio=True),
        _Renderer(),
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in event.sent[0][0].nodes] == [
        "card.png", "one.png", "two.png",
    ]


def test_compact_forward_non_audio_mode_folds_card_with_single_video(
    sender_module,
):
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        contents=[VideoContent(Path("video.mp4"))],
    )
    sender = sender_module.MessageSender(
        _card_config(forward_threshold=100, compact_forward_non_audio=True),
        _Renderer(),
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in event.sent[0][0].nodes] == [
        "card.png", "video.mp4",
    ]


def test_compact_forward_non_audio_mode_keeps_single_content_unfolded(
    sender_module,
):
    image = ImageContent(Path("one.png"))
    result = ParseResult(platform=Platform("bilibili", "Bilibili"), contents=[image])
    sender = sender_module.MessageSender(
        _card_config(
            forward_threshold=0,
            compact_forward_non_audio=True,
            card_enabled=False,
        ),
        _Renderer(),
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].__class__.__name__ == "Image"
    assert event.sent[0][0].path == "one.png"


def test_compact_forward_non_audio_mode_keeps_audio_outside_forward(
    sender_module,
):
    contents = [
        ImageContent(Path("one.png")),
        AudioContent(Path("song.mp3")),
        ImageContent(Path("two.png")),
    ]
    result = ParseResult(platform=Platform("netease", "网易云音乐"), contents=contents)
    sender = sender_module.MessageSender(
        _card_config(
            forward_threshold=100,
            compact_forward_non_audio=True,
            audio_to_file=False,
        ),
        _Renderer(),
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in event.sent[0][0].nodes] == [
        "card.png", "one.png", "two.png",
    ]
    assert event.sent[1][0].__class__.__name__ == "Record"
    assert event.sent[1][0].path == "song.mp3"


@pytest.mark.parametrize(
    "platform_name", ["netease", "qqmusic", "applemusic", "kugou"]
)
def test_compact_forward_non_audio_mode_folds_lyrics_card_with_audio(
    sender_module,
    tmp_path: Path,
    platform_name: str,
):
    class LyricsRenderer(_Renderer):
        async def render_lyrics_card(self, _lyrics, **_kwargs):
            return tmp_path / "lyrics.png"

    config = _card_config(
        forward_threshold=100,
        compact_forward_non_audio=True,
        audio_to_file=False,
        parser=SimpleNamespace(
            **{platform_name: SimpleNamespace(parse_lyrics=True)},
        ),
    )
    result = ParseResult(
        platform=Platform("netease", "缃戞槗浜戦煶涔?"),
        contents=[AudioContent(Path("song.mp3"))],
        extra={
            "music_type": "track",
            "lyrics": {"text": "[00:01.00]Line", "title": "Song"},
        },
    )
    result.platform = Platform(platform_name, platform_name)
    sender = sender_module.MessageSender(config, LyricsRenderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in event.sent[0][0].nodes] == [
        "card.png", str(tmp_path / "lyrics.png"),
    ]
    assert event.sent[1][0].__class__.__name__ == "Record"
    assert event.sent[1][0].path == "song.mp3"


def test_single_card_switch_governs_render_and_send(sender_module):
    renderer = _Renderer()
    config = SimpleNamespace(
        forward_threshold=10,
        card_enabled=False,
        show_download_fail_tip=True,
        audio_to_file=True,
    )
    sender = sender_module.MessageSender(config, renderer)
    event = _Event()

    result = _result()
    asyncio.run(sender.send_parse_result(event, result))
    assert not renderer.calls
    assert len(event.sent) == 1
    assert event.sent[0][0].path == "video.mp4"

    config.card_enabled = True
    event = _Event()
    assert asyncio.run(sender._send_result_card(event, result))
    assert len(renderer.calls) == 1
    assert len(event.sent) == 1


def test_disabled_card_switch_skips_direct_render_attempt(sender_module):
    renderer = _Renderer()
    config = SimpleNamespace(
        forward_threshold=2,
        card_enabled=False,
        show_download_fail_tip=True,
        audio_to_file=True,
    )
    sender = sender_module.MessageSender(config, renderer)
    event = _Event()

    assert not asyncio.run(sender._send_result_card(event, _result()))
    assert not renderer.calls


@pytest.mark.parametrize(
    "platform_name", ["netease", "qqmusic", "applemusic", "kugou"]
)
def test_lyrics_card_is_sent_after_regular_result_and_uses_platform_logo(
    sender_module, tmp_path: Path, platform_name: str
):
    order = []

    class LyricsRenderer:
        async def render_lyrics_card(self, lyrics, **kwargs):
            order.append(("render", kwargs["platform_key"], lyrics))
            return tmp_path / f"{platform_name}.png"

    config = _card_config(
        parser=SimpleNamespace(**{platform_name: SimpleNamespace(parse_lyrics=True)})
    )
    sender = sender_module.MessageSender(config, LyricsRenderer())
    async def regular_result(_event, _result):
        order.append(("regular",))

    sender._send_parse_result = regular_result
    result = ParseResult(
        platform=Platform(platform_name, platform_name),
        title="Song",
        extra={
            "music_type": "track",
            "lyrics": {"text": "[00:01.00]Line", "title": "Song"},
        },
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert order == [("regular",), ("render", platform_name, "[00:01.00]Line")]
    assert len(event.sent) == 1
    assert event.sent[0][0].path.endswith(f"{platform_name}.png")


def test_kugou_lyrics_card_logs_render_and_send_states(
    sender_module, tmp_path: Path,
):
    infos = []
    sender_module.logger.info = infos.append

    class LyricsRenderer:
        async def render_lyrics_card(self, lyrics, **_kwargs):
            assert lyrics == "[00:01.00]Line"
            return tmp_path / "kugou.png"

    config = _card_config(
        parser=SimpleNamespace(kugou=SimpleNamespace(parse_lyrics=True))
    )
    sender = sender_module.MessageSender(config, LyricsRenderer())
    result = ParseResult(
        platform=Platform("kugou", "酷狗音乐"),
        title="Song",
        extra={
            "music_type": "track",
            "lyrics": {"text": "[00:01.00]Line", "title": "Song"},
        },
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].__class__.__name__ == "Plain"
    assert event.sent[1][0].path.endswith("kugou.png")
    assert any("已取得歌词，开始生成歌词卡片" in message for message in infos)
    assert any("歌词卡片生成成功" in message for message in infos)
    assert any("歌词卡片发送成功" in message for message in infos)
    assert all("[00:01.00]Line" not in message for message in infos)


@pytest.mark.parametrize("show_release_time", [True, False])
def test_lyrics_card_release_time_visibility_switch(
    sender_module, tmp_path: Path, show_release_time: bool
):
    captured = {}

    class LyricsRenderer:
        async def render_lyrics_card(self, lyrics, **kwargs):
            captured.update(kwargs)
            return tmp_path / "lyrics.png"

    config = _card_config(
        lyrics_show_release_time=show_release_time,
        parser=SimpleNamespace(
            netease=SimpleNamespace(parse_lyrics=True),
        ),
    )
    sender = sender_module.MessageSender(config, LyricsRenderer())
    result = ParseResult(
        platform=Platform("netease", "缃戞槗浜戦煶涔?"),
        title="Song",
        extra={
            "music_type": "track",
            "lyrics": {
                "text": "[00:01.00]Line",
                "title": "Song",
                "release_text": "2024-04-24",
            },
        },
    )

    asyncio.run(sender._send_lyrics_card(_Event(), result))

    assert captured["release_text"] == (
        "2024-04-24" if show_release_time else None
    )
    assert captured["show_release_time"] is show_release_time


@pytest.mark.parametrize("platform_name", ["bilibili", "douyin", "xhs", "pixiv"])
def test_global_card_is_sent_once_for_every_platform(
    sender_module, platform_name: str
):
    image = ImageContent(Path("image.png"))
    result = ParseResult(
        platform=Platform(platform_name, platform_name),
        contents=[image],
        # A legacy per-group false preference cannot suppress the global card.
        send_groups=[SendGroup(contents=[image], force_merge=False, render_card=False)],
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].path == "image.png"


def test_card_does_not_make_a_single_image_folded(sender_module):
    image = ImageContent(Path("image.png"))
    result = ParseResult(platform=Platform("douyin", "抖音"), contents=[image])
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(forward_threshold=2), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].__class__.__name__ == "Image"
    assert event.sent[1][0].path == "image.png"


def test_audio_as_voice_overrides_global_audio_file_setting(sender_module):
    result = ParseResult(
        platform=Platform("netease", "网易云音乐"),
        contents=[AudioContent(Path("song.mp3"))],
        extra={"audio_as_voice": True},
    )
    sender = sender_module.MessageSender(_card_config(audio_to_file=True), _Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].__class__.__name__ == "Record"


def test_audio_as_file_overrides_global_audio_record_setting(sender_module):
    result = ParseResult(
        platform=Platform("applemusic", "Apple Music"),
        contents=[AudioContent(Path("song.m4a"))],
        extra={"audio_as_file": True},
    )
    sender = sender_module.MessageSender(_card_config(audio_to_file=False), _Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[1][0].__class__.__name__ == "File"


def test_apple_voice_uses_astrbot_file_url_without_local_wav_conversion(
    sender_module, monkeypatch
):
    actions = []

    class _Bot:
        async def call_action(self, action, **kwargs):
            actions.append((action, kwargs))

    class _TokenService:
        async def register_file(self, path, timeout=None):
            assert path == "song.m4a"
            assert timeout == 300
            return "one-shot-token"

    import sys

    fake_core = sys.modules["astrbot.core"]
    fake_core.astrbot_config = {"callback_api_base": "http://astrbot:6185/"}
    fake_core.file_token_service = _TokenService()
    result = ParseResult(
        platform=Platform("applemusic", "Apple Music"),
        contents=[AudioContent(Path("song.m4a"))],
        extra={"audio_as_voice": True, "audio_send_via_url": True},
    )
    event = _Event()
    event.bot = _Bot()
    sender = sender_module.MessageSender(_card_config(audio_to_file=True), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1  # only the card; audio used direct OneBot call
    assert actions[0][1]["message"] == [
        {"type": "record", "data": {"file": "http://astrbot:6185/api/file/one-shot-token"}}
    ]


def test_apple_file_uses_onebot_file_protocol(sender_module):
    actions = []

    class _Bot:
        async def call_action(self, action, **kwargs):
            actions.append((action, kwargs))

    class _TokenService:
        async def register_file(self, path, timeout=None):
            assert path == "song.m4a"
            return "one-shot-token"

    import sys

    fake_core = sys.modules["astrbot.core"]
    fake_core.astrbot_config = {"callback_api_base": "http://astrbot:6185"}
    fake_core.file_token_service = _TokenService()
    result = ParseResult(
        platform=Platform("applemusic", "Apple Music"),
        title="月声尽,心灯明",
        contents=[AudioContent(Path("song.m4a"))],
        extra={"audio_as_file": True, "audio_send_via_url": True},
    )
    event = _Event()
    event.bot = _Bot()
    sender = sender_module.MessageSender(_card_config(audio_to_file=False), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1  # card only; file went directly through OneBot
    assert actions == [
        (
            "upload_group_file",
            {
                "group_id": 123,
                "file": "http://astrbot:6185/api/file/one-shot-token",
                "name": "月声尽,心灯明.m4a",
            },
        )
    ]


def test_private_file_uses_onebot_private_upload_action(sender_module):
    actions = []

    class _Bot:
        async def call_action(self, action, **kwargs):
            actions.append((action, kwargs))

    class _PrivateEvent(_Event):
        def get_group_id(self):
            return None

    event = _PrivateEvent()
    event.bot = _Bot()

    assert asyncio.run(
        sender_module.MessageSender._send_audio_url_via_onebot(
            event,
            "http://astrbot:6185/api/file/one-shot-token",
            message_type="file",
            name="song.m4a",
            platform="netease",
        )
    )
    assert actions == [
        (
            "upload_private_file",
            {
                "user_id": 456,
                "file": "http://astrbot:6185/api/file/one-shot-token",
                "name": "song.m4a",
            },
        )
    ]


def test_onebot_file_upload_logs_resolved_quality(sender_module):
    infos = []
    sender_module.logger.info = infos.append

    class _Bot:
        async def call_action(self, _action, **_kwargs):
            return {"status": "ok"}

    event = _Event()
    event.bot = _Bot()
    asyncio.run(sender_module.MessageSender._send_audio_url_via_onebot(
        event,
        "http://astrbot:6185/api/file/one-shot-token",
        message_type="file",
        name="song.flac",
        platform="qqmusic",
        quality="lossless",
    ))

    assert any("quality=lossless" in message for message in infos)
    assert all("one-shot-token" not in message for message in infos)


def test_local_audio_url_logs_its_ttl(sender_module):
    import sys

    infos = []
    sender_module.logger.info = infos.append
    fake_core = sys.modules["astrbot.core"]
    fake_core.astrbot_config = {"callback_api_base": "http://astrbot:6185"}

    class _TokenService:
        async def register_file(self, path, timeout=None):
            assert path == "song.m4a"
            assert timeout == 300
            return "one-shot-token"

    fake_core.file_token_service = _TokenService()
    url = asyncio.run(
        sender_module.MessageSender._register_local_audio_url(
            Path("song.m4a"), platform="qqmusic", name="song.m4a"
        )
    )

    assert url == "http://astrbot:6185/api/file/one-shot-token"
    assert any("expires_in=300s" in message for message in infos)


def test_music_attachment_filename_is_sanitized(sender_module):
    filename = sender_module.MessageSender._audio_attachment_filename(
        '../bad:name?.m4a', Path('track.m4a'), '音乐'
    )

    assert filename == "_bad_name_.m4a"


def test_apple_audio_without_onebot_url_is_not_sent_natively(sender_module):
    import sys

    fake_core = sys.modules["astrbot.core"]
    fake_core.astrbot_config = {"callback_api_base": ""}
    fake_core.file_token_service = object()
    result = ParseResult(
        platform=Platform("applemusic", "Apple Music"),
        contents=[AudioContent(Path("song.m4a"))],
        extra={"audio_as_file": True, "audio_send_via_url": True},
    )
    event = _Event()
    event.bot = object()
    sender = sender_module.MessageSender(_card_config(audio_to_file=False), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1  # no native AstrBot File fallback


def test_apple_voice_without_file_service_does_not_use_native_fallback(sender_module):
    import sys

    fake_core = sys.modules["astrbot.core"]
    fake_core.astrbot_config = {"callback_api_base": ""}
    fake_core.file_token_service = object()
    result = ParseResult(
        platform=Platform("applemusic", "Apple Music"),
        contents=[AudioContent(Path("song.m4a"))],
        extra={"audio_as_voice": True, "audio_send_via_url": True},
    )
    event = _Event()
    sender = sender_module.MessageSender(_card_config(audio_to_file=False), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1


def test_netease_audio_uses_remote_url_for_onebot_and_keeps_local_download(
    sender_module,
):
    actions = []

    class _Bot:
        async def call_action(self, action, **kwargs):
            actions.append((action, kwargs))

    result = ParseResult(
        platform=Platform("netease", "网易云音乐"),
        contents=[AudioContent(Path("song.mp3"))],
        extra={
            "audio_as_voice": True,
            "audio_send_url": "https://music.163.com/song/media/outer/url?id=12345",
        },
    )
    event = _Event()
    event.bot = _Bot()
    sender = sender_module.MessageSender(_card_config(audio_to_file=True), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].path == "card.png"
    assert actions == [
        (
            "send_group_msg",
            {
                "group_id": 123,
                "message": [
                    {
                        "type": "record",
                        "data": {
                            "file": "https://music.163.com/song/media/outer/url?id=12345"
                        },
                    }
                ],
            },
        )
    ]


def test_qqmusic_audio_uses_remote_url_for_onebot(sender_module):
    actions = []

    class _Bot:
        async def call_action(self, action, **kwargs):
            actions.append((action, kwargs))

    result = ParseResult(
        platform=Platform("qqmusic", "QQ音乐"),
        contents=[AudioContent(Path("song.mp3"))],
        extra={
            "audio_as_voice": True,
            "audio_send_url": "https://aqqmusic.tc.qq.com/song.mp3?vkey=test",
        },
    )
    event = _Event()
    event.bot = _Bot()
    sender = sender_module.MessageSender(_card_config(audio_to_file=True), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert actions[0][0] == "send_group_msg"
    assert actions[0][1]["message"] == [
        {
            "type": "record",
            "data": {"file": "https://aqqmusic.tc.qq.com/song.mp3?vkey=test"},
        }
    ]


def test_netease_file_uses_tokenized_onebot_file_path(sender_module):
    actions = []

    class _Bot:
        async def call_action(self, action, **kwargs):
            actions.append((action, kwargs))

    class _TokenService:
        async def register_file(self, path, timeout=None):
            assert path == "song.mp3"
            assert timeout == 300
            return "netease-token"

    import sys

    fake_core = sys.modules["astrbot.core"]
    fake_core.astrbot_config = {"callback_api_base": "http://astrbot:6185/"}
    fake_core.file_token_service = _TokenService()
    result = ParseResult(
        platform=Platform("netease", "网易云音乐"),
        title="试听歌曲",
        contents=[AudioContent(Path("song.mp3"))],
        extra={"audio_as_file": True, "audio_send_via_url": True},
    )
    event = _Event()
    event.bot = _Bot()
    sender = sender_module.MessageSender(_card_config(audio_to_file=False), _Renderer())

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert actions == [
        (
            "upload_group_file",
            {
                "group_id": 123,
                "file": "http://astrbot:6185/api/file/netease-token",
                "name": "试听歌曲.mp3",
            },
        )
    ]


def test_playlist_cover_used_by_card_is_not_sent_again(sender_module):
    cover = ImageContent(Path("playlist-cover.png"))
    result = ParseResult(
        platform=Platform("qqmusic", "QQ音乐"),
        contents=[cover],
        extra={"playlist_cover_only": True},
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(forward_threshold=10), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 1
    assert event.sent[0][0].path == "card.png"


@pytest.mark.parametrize("include_media", [False, True])
def test_bilibili_dynamic_shows_card_and_media_without_text_fallback(
    sender_module, include_media: bool
):
    result = ParseResult(
        platform=Platform("bilibili", "B站"),
        text="动态正文只展示在信息卡片中。",
        contents=(
            [ImageContent(Path("dynamic-image.png"))] if include_media else []
        ),
        extra={"bilibili_text_card_only": True},
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert event.sent[0][0].path == "card.png"
    if include_media:
        assert len(event.sent) == 2
        assert [segment.__class__.__name__ for segment in event.sent[1]] == ["Image"]
        assert event.sent[1][0].path == "dynamic-image.png"
    else:
        assert len(event.sent) == 1


@pytest.mark.parametrize("compact_forward", [False, True])
def test_bilibili_opus_sends_card_then_only_images(
    sender_module, compact_forward: bool
):
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        text="完整的动态正文，应只出现在信息卡片中。",
        contents=[
            ImageContent(Path("opus-one.png")),
            ImageContent(Path("opus-two.png")),
        ],
        extra={"bilibili_opus_images_only": True},
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(
        _card_config(
            forward_threshold=100,
            compact_forward_non_audio=compact_forward,
        ),
        renderer,
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert renderer.calls[0].text == "完整的动态正文，应只出现在信息卡片中。"
    assert event.sent[0][0].path == "card.png"
    sent_image_paths = []
    for message in event.sent[1:]:
        for segment in message:
            if segment.__class__.__name__ == "Nodes":
                for node in segment.nodes:
                    assert len(node.content) == 1
                    assert node.content[0].__class__.__name__ == "Image"
                    sent_image_paths.append(node.content[0].path)
            else:
                assert segment.__class__.__name__ == "Image"
                sent_image_paths.append(segment.path)
    assert sent_image_paths == ["opus-one.png", "opus-two.png"]


def test_bilibili_opus_without_card_still_sends_images_without_text(
    sender_module,
):
    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        text="卡片关闭时也不应单独发送正文。",
        contents=[ImageContent(Path("opus-image.png"))],
        extra={"bilibili_opus_images_only": True},
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(
        _card_config(card_enabled=False),
        renderer,
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert not renderer.calls
    assert len(event.sent) == 1
    assert [segment.__class__.__name__ for segment in event.sent[0]] == ["Image"]
    assert event.sent[0][0].path == "opus-image.png"


@pytest.mark.parametrize("include_image", [False, True])
def test_bilibili_opus_card_failure_does_not_fall_back_to_body_text(
    sender_module, include_image: bool
):
    class Renderer:
        async def render_card(self, result):
            raise RuntimeError("template failed")

    result = ParseResult(
        platform=Platform("bilibili", "Bilibili"),
        text="正文不应作为兜底纯文本发送。",
        contents=(
            [ImageContent(Path("opus-image.png"))] if include_image else []
        ),
        extra={"bilibili_opus_images_only": True},
    )
    sender = sender_module.MessageSender(_card_config(), Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    if include_image:
        assert len(event.sent) == 1
        assert [segment.__class__.__name__ for segment in event.sent[0]] == [
            "Image"
        ]
        assert event.sent[0][0].path == "opus-image.png"
    else:
        assert event.sent == []


def test_bilibili_opus_parser_keeps_full_body_for_card_and_images_for_sending(
    monkeypatch: pytest.MonkeyPatch,
):
    import core.parsers.bilibili as bilibili_parser_module
    from core.parsers.bilibili import BilibiliParser

    parser = BilibiliParser.__new__(BilibiliParser)
    parser.headers = {}
    parser.cfg = SimpleNamespace(
        parser=SimpleNamespace(bilibili=SimpleNamespace(use_proxy=False))
    )
    parser.downloader = SimpleNamespace(
        download_img=lambda url, **kwargs: Path(url.rsplit("/", 1)[-1] + ".jpg")
    )

    class Opus:
        async def get_info(self):
            def text_paragraph(value):
                return {
                    "para_type": 1,
                    "text": {
                        "nodes": [
                            {
                                "type": "TEXT_NODE_TYPE_WORD",
                                "word": {"words": value},
                            }
                        ]
                    },
                }

            def image_paragraph(value):
                return {
                    "para_type": 2,
                    "pic": {
                        "pics": [{"url": f"https://img.example/{value}"}],
                        "style": 1,
                    },
                }

            return {
                "item": {
                    "id_str": "123",
                    "type": 0,
                    "basic": {"title": "Opus title"},
                    "modules": [
                        {
                            "module_type": "MODULE_TYPE_AUTHOR",
                            "module_author": {
                                "name": "Author",
                                "face": "",
                                "mid": 1,
                                "pub_time": "now",
                                "pub_ts": "123",
                            },
                        },
                        {
                            "module_type": "MODULE_TYPE_CONTENT",
                            "module_content": {
                                "paragraphs": [
                                    text_paragraph("first paragraph"),
                                    image_paragraph("one"),
                                    text_paragraph("second paragraph"),
                                    image_paragraph("two"),
                                    text_paragraph("last paragraph"),
                                ]
                            },
                        },
                        {
                            "module_type": "MODULE_TYPE_STAT",
                            "module_stat": {},
                        },
                    ],
                }
            }

    async def credential():
        return None

    parser.login = SimpleNamespace(credential=credential())
    monkeypatch.setattr(bilibili_parser_module, "Opus", lambda *_: Opus())

    result = asyncio.run(parser.parse_opus(123))

    assert result.text == "first paragraph\n\nsecond paragraph\n\nlast paragraph"
    assert result.extra["bilibili_opus_images_only"] is True
    assert [content.__class__.__name__ for content in result.contents] == [
        "ImageContent",
        "ImageContent",
    ]


def test_douyin_gallery_card_precedes_folded_media(sender_module):
    images = [ImageContent(Path("one.png")), ImageContent(Path("two.png"))]
    result = ParseResult(
        platform=Platform("douyin", "抖音"),
        contents=images,
        send_groups=[SendGroup(contents=images, force_merge=True)],
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    folded = event.sent[1][0]
    assert folded.__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in folded.nodes] == ["one.png", "two.png"]


@pytest.mark.parametrize("platform_name", ["douyin", "xhs"])
def test_motion_photo_adds_info_image_to_folded_media(
    sender_module, platform_name: str
):
    images = [ImageContent(Path("one.png")), ImageContent(Path("two.png"))]
    result = ParseResult(
        platform=Platform(platform_name, platform_name),
        contents=images,
        extra={"has_motion_photo": True},
    )
    sender = sender_module.MessageSender(
        _card_config(forward_threshold=100), _Renderer()
    )
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    folded = event.sent[1][0]
    assert folded.__class__.__name__ == "Nodes"
    assert [node.content[0].path for node in folded.nodes] == [
        "one.png",
        "two.png",
        str(sender_module.MessageSender._LIVE_PHOTO_INFO_PATH),
    ]


def test_legacy_card_group_does_not_duplicate_result_card(sender_module):
    image = ImageContent(Path("image.png"))
    result = ParseResult(
        platform=Platform("pixiv", "Pixiv"),
        contents=[image],
        send_groups=[
            SendGroup(contents=[], force_merge=False, render_card=True),
            SendGroup(contents=[image], force_merge=False, render_card=False),
        ],
    )
    renderer = _Renderer()
    sender = sender_module.MessageSender(_card_config(), renderer)
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert renderer.calls == [result]
    assert len(event.sent) == 2
    assert event.sent[0][0].path == "card.png"
    assert event.sent[1][0].path == "image.png"


def test_renderer_exception_does_not_block_media_send(sender_module):
    class Renderer:
        async def render_card(self, result):
            raise RuntimeError("template failed")

    config = _card_config(forward_threshold=10)
    sender = sender_module.MessageSender(config, Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, _result()))

    # 渲染异常只跳过卡片，视频消息仍然正常发送。
    assert len(event.sent) == 1
    assert len(event.sent[0]) == 1
    assert event.sent[0][0].__class__.__name__ == "Video"


def test_failed_card_keeps_explicit_gallery_forwarding(sender_module):
    class Renderer:
        async def render_card(self, result):
            raise RuntimeError("browser unavailable")

    images = [ImageContent(Path("one.png")), ImageContent(Path("two.png"))]
    result = ParseResult(
        platform=Platform("douyin", "抖音"),
        contents=images,
        send_groups=[SendGroup(contents=images, force_merge=True)],
    )
    sender = sender_module.MessageSender(_card_config(), Renderer())
    event = _Event()

    asyncio.run(sender.send_parse_result(event, result))

    assert len(event.sent) == 1
    assert event.sent[0][0].__class__.__name__ == "Nodes"
    assert len(event.sent[0][0].nodes) == 2


@pytest.mark.parametrize(
    "content_type",
    [ImageContent, GraphicsContent, VideoContent, DynamicContent],
)
def test_failed_visual_media_uses_local_error_placeholder(
    sender_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content_type,
):
    """图片类及视频类下载失败时应发送本地图片，而非文字提示。"""
    error_media = tmp_path / "error_media.png"
    error_media.write_bytes(b"placeholder")
    monkeypatch.setattr(sender_module.MessageSender, "_ERROR_MEDIA_PATH", error_media)

    async def build_segments():
        async def failed_download():
            raise DownloadException("network failure")

        content = content_type(asyncio.create_task(failed_download()))
        result = ParseResult(
            platform=Platform("douyin", "抖音"),
            contents=[content],
        )
        sender = sender_module.MessageSender(_card_config(), _Renderer())
        return await sender._build_segments(result, sender._build_send_plan(result))

    segments = asyncio.run(build_segments())

    assert len(segments) == 1
    assert segments[0].__class__.__name__ == "Image"
    assert segments[0].path == str(error_media)
