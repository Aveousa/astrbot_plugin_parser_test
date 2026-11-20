from __future__ import annotations

import asyncio
import base64
import json
import sys
import types

import pytest

from core.parsers.kugou_browser import KugouBrowserLogin, _PNG, _proxy_options


IMAGE = _PNG + b"fake-image"


class Response:
    status = 200
    headers = {}

    def __init__(self, url="https://login-user.kugou.com/v2/qrcode"):
        self.url = url

    async def body(self):
        return json.dumps({"data": {"qrcode_img": "data:image/png;base64," +
                                   base64.b64encode(IMAGE).decode()}}).encode()


class Page:
    frames = []

    def __init__(self, mode):
        self.mode = mode
        self.entered = asyncio.Event()

    def on(self, event, callback):
        self.callback = callback

    async def goto(self, url, **kwargs):
        assert url == "https://login-user.kugou.com/login/?appid=1001"
        self.entered.set()
        if self.mode == "fail":
            raise ValueError("sensitive URL")
        if self.mode == "block":
            await asyncio.Event().wait()
        self.callback(Response("https://login-user.kugou.com.evil.test/v2/qrcode"))
        self.callback(Response())
        await asyncio.sleep(0)


class Context:
    def __init__(self, mode):
        self.page = Page(mode)
        self.closed = False
        self.items = []

    async def new_page(self):
        return self.page

    async def cookies(self):
        return self.items

    async def close(self):
        self.closed = True


class Browser:
    def __init__(self, mode):
        self.context = Context(mode)
        self.closed = False

    async def new_context(self):
        return self.context

    async def close(self):
        self.closed = True


class Playwright:
    def __init__(self, mode):
        self.chromium = self
        self.browser = Browser(mode)
        self.stopped = False
        self.options = None

    async def launch(self, **options):
        self.options = options
        return self.browser

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True


def install_fake(monkeypatch, mode="success"):
    fake = Playwright(mode)
    package = types.ModuleType("playwright")
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: fake
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return fake


def cookie(name, value, domain=".kugou.com", path="/", expires=-1, secure=True):
    return dict(name=name, value=value, domain=domain, path=path,
                expires=expires, secure=secure)


def test_success_scope_filter_and_cleanup(monkeypatch):
    async def run():
        fake = install_fake(monkeypatch)
        login = KugouBrowserLogin(types.SimpleNamespace(proxy="http://user:pass@localhost:8080"))
        assert await login.create_qr() == IMAGE
        assert fake.options["proxy"] == {"server": "http://localhost:8080", "username": "user", "password": "pass"}
        assert fake.options["headless"] is True
        assert await login.poll() is None
        fake.browser.context.items = [
            cookie("KuGoo", "KugooID%3D123%26t%3Dabc"),
            cookie("kg_mid", "a", path="/"),
            cookie("kg_mid", "b", domain="login-user.kugou.com", path="/login", expires=4102444800, secure=False),
            cookie("kg_mid", "other", domain="kugou.com.evil.test"),
            cookie("kg_mid", "qq", domain=".qq.com"),
            cookie("tracking", "secret"),
        ]
        result = await login.poll()
        assert len(result) == 3
        assert [(x.domain, x.path, x.secure, x.expires) for x in result[1:]] == [
            (".kugou.com", "/", True, 0),
            ("login-user.kugou.com", "/login", False, 4102444800),
        ]
        with pytest.raises(RuntimeError):
            await login.create_qr()
        await login.close()
        await login.close()
        assert fake.browser.context.closed and fake.browser.closed and fake.stopped
        assert await login.poll() is None
    asyncio.run(run())


def test_failed_navigation_sanitized_and_cleaned(monkeypatch):
    async def run():
        fake = install_fake(monkeypatch, "fail")
        login = KugouBrowserLogin(types.SimpleNamespace(proxy=None))
        with pytest.raises(RuntimeError, match="^Browser QR creation failed$"):
            await login.create_qr()
        assert fake.browser.context.closed and fake.browser.closed and fake.stopped
    asyncio.run(run())


@pytest.mark.parametrize("stop", [True, False])
def test_cancel_or_stop_during_creation(monkeypatch, stop):
    async def run():
        fake = install_fake(monkeypatch, "block")
        login = KugouBrowserLogin(types.SimpleNamespace(proxy=None))
        task = asyncio.create_task(login.create_qr())
        await fake.browser.context.page.entered.wait()
        with pytest.raises(RuntimeError):
            await login.create_qr()
        if stop:
            login.stop()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await login.close()
        assert fake.browser.context.closed and fake.browser.closed and fake.stopped
    asyncio.run(run())


def test_account_scope_and_proxy_sanitization():
    async def run():
        login = KugouBrowserLogin(types.SimpleNamespace(proxy=None))
        context = Context("success")
        login._context = context
        context.items = [cookie("KugooID", "123"), cookie("t", "token", path="/different")]
        assert await login.poll() is None
        context.items.append(cookie("t", "token"))
        assert len(await login.poll()) == 3
        await login.close()
    asyncio.run(run())
    with pytest.raises(ValueError, match="^Invalid browser proxy$"):
        _proxy_options("http://secret:password@host:bad")
