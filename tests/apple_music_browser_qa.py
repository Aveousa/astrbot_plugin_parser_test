"""Manual browser QA with the real AstrBot bridge and a simulated API.

Run: python tests/apple_music_browser_qa.py --output <temporary-directory>
No real account/password is used; this does not verify Apple downloading.
"""

import argparse
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
HARNESS = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0"><iframe title="Plugin page" src="/page/index.html"
 style="border:0;width:100%;height:100vh;display:block"></iframe><script>
const state = {enabled:true,connected:true,version:'0.0.2',auth_state:'logged_out',
 subscription_active:null,storefront:null,playback_ready:true,can_submit_2fa:false,
 message:'wrapper 已连接',subscription_message:'尚未核验订阅',nonce:'qa-nonce',
 gamdl:{available:true,version:'3.9.1',ffmpeg:true,ffprobe:true},cache:{bytes:7340032,files:9},
 platform_accounts:[
  {key:'douyin',label:'抖音',available:true,text:'登录 Cookie 已配置'},
  {key:'bilibili',label:'Bilibili',available:true,text:'登录 Cookie 已配置'},
  {key:'qqmusic',label:'QQ 音乐',available:false,text:'账号 Cookie 不完整'},
  {key:'kugou',label:'酷狗音乐',available:false,text:'账号 Cookie 未配置'},
  {key:'applemusic',label:'Apple Music',available:false,text:'未登录'}],
 settings:{backend_enabled:true,return_audio:false,audio_send_format:'file',
  file_codec_priority:['alac','aac-web'],show_playlist_cover:true,show_playlist_url:true},
 music_parsers:{
  netease:{key:'netease',label:'网易云音乐',known:true,configured:true,enabled:true,available:true,cookie_configured:true,cookie_status:'Cookie 已配置',settings:{enable:true,return_audio:true,audio_send_format:'voice',show_playlist_cover:true,show_playlist_url:true}},
  qqmusic:{key:'qqmusic',label:'QQ 音乐',known:true,configured:true,enabled:true,available:false,cookie_configured:false,cookie_validity:'unconfigured',cookie_status:'Cookie 未配置',settings:{enable:true,return_audio:true,audio_send_format:'voice',show_playlist_cover:true,show_playlist_url:true}},
  kugou:{key:'kugou',label:'酷狗音乐',known:true,configured:true,enabled:false,available:false,cookie_configured:false,cookie_status:'解析器未启用',settings:{enable:false,return_audio:true,audio_send_format:'voice',show_playlist_cover:true,show_playlist_url:true}}},
 parsers:[
  {key:'netease',label:'网易云音乐',known:true,configured:true,enabled:true,cookie_configured:true,cookie_status:'Cookie 已配置'},
  {key:'qqmusic',label:'QQ 音乐',known:true,configured:true,enabled:true,cookie_configured:false,cookie_status:'账号 Cookie 不完整'},
  {key:'kugou',label:'酷狗音乐',known:true,configured:true,enabled:false,cookie_configured:false,cookie_status:'解析器未启用'}],
};
let job = {state:'idle'};
window.qaState = state;
window.qaRequests = [];
window.addEventListener('message', event => {
 const message = event.data;
 if (message?.channel !== 'astrbot-plugin-page') return;
 const send = data => event.source.postMessage({channel:'astrbot-plugin-page',...data}, location.origin);
 if (message.kind === 'ready') {send({kind:'context',context:{pluginName:'test-plugin',pageName:'apple_music',isDark:false}}); return;}
 if (message.kind !== 'request') return;
 const endpoint = message.endpoint?.replace('apple-music/','');
 window.qaRequests.push(endpoint);
 let data;
 if (endpoint === 'status') data = {...state,operation:job};
 else if (endpoint === 'operation') data = job;
 else if (endpoint === 'logo') data = {data_url:'__LOGO_DATA_URL__'};
 else if (message.action === 'api:post') {
  if (message.body?.nonce !== 'qa-nonce') {send({kind:'response',requestId:message.requestId,ok:false,error:'invalid nonce'});return;}
  job = {state:'running',action:endpoint,message:'任务已提交，请等待'};
  data = {...job};
  setTimeout(() => {
   if (endpoint === 'login') {
   state.auth_state='awaiting_2fa';state.can_submit_2fa=true;
    state.platform_accounts[4]={key:'applemusic',label:'Apple Music',available:false,text:'等待验证码'};
    job={state:'complete',action:endpoint,needs_2fa:true,message:'请输入六位验证码'};
   } else if (endpoint === 'two-factor') {
    state.auth_state='authenticated';state.can_submit_2fa=false;
    state.subscription_active=true;state.storefront='cn';state.subscription_message='订阅已核验';
    state.platform_accounts[4]={key:'applemusic',label:'Apple Music',available:true,text:'已登录'};
    job={state:'complete',action:endpoint,needs_2fa:false,message:'验证码已提交'};
   } else if (endpoint === 'verify') {
    job={state:'complete',action:endpoint,message:'单曲音频已生成并通过格式/时长检查',bytes:6000000,duration:306};
   } else if (endpoint === 'clear-session') {
    state.auth_state='logged_out';state.subscription_active=null;state.can_submit_2fa=false;
    state.platform_accounts[4]={key:'applemusic',label:'Apple Music',available:false,text:'未登录'};
    job={state:'complete',action:endpoint,needs_2fa:false,message:'已清除内存会话；磁盘登录缓存仍在'};
   } else if (endpoint === 'clear-cache') {
    state.cache={bytes:0,files:0};
    job={state:'complete',action:endpoint,message:'缓存已清理，账号登录状态未受影响',removed_files:9,removed_bytes:7340032};
   } else if (endpoint === 'save-settings') {
    state.settings=message.body.settings;
    job={state:'complete',action:endpoint,message:'Apple Music 解析设置已保存并立即生效',settings:state.settings};
   } else if (endpoint === 'save-music-settings') {
    const submitted=message.body.parsers || {};
    Object.values(state.music_parsers).forEach(parser => {
      const settings=submitted[parser.key];
      if (!settings) return;
      parser.settings=settings;
      parser.enabled=settings.enable;
    });
    job={state:'complete',action:endpoint,message:'音乐解析设置已保存并立即生效',parsers:Object.fromEntries(Object.entries(state.music_parsers).filter(([key]) => submitted[key]))};
    } else if (endpoint === 'save-music-cookie') {
    const parser=state.music_parsers[message.body.parser];
    if (parser) {
      parser.cookie_configured=!message.body.clear && Boolean(message.body.cookie);
      parser.cookie_validity=parser.cookie_configured?'unknown':'unconfigured';
      parser.available=false;
      parser.cookie_status=parser.cookie_configured?'Cookie 待检查':'Cookie 未配置';
    }
    job={state:'complete',action:endpoint,message:'Cookie 已保存',parser_key:message.body.parser,parser:parser};
   } else if (endpoint === 'check-music-cookie') {
    const parser=state.music_parsers[message.body.parser];
    parser.cookie_validity='valid';parser.available=true;parser.cookie_status='Cookie 有效';
    state.platform_accounts[2]={key:'qqmusic',label:'QQ 音乐',available:true,text:'Cookie 有效'};
    job={state:'complete',action:endpoint,parser_key:message.body.parser,cookie_validity:'valid',cookie_status:'Cookie 有效',message:'QQ 音乐 Cookie 检查有效'};
   }
  }, 100);
 }
 send({kind:'response',requestId:message.requestId,ok:true,data});
});
</script></body></html>"""


def run(output: Path):
    import astrbot
    bridge = Path(astrbot.__file__).parent / "dashboard" / "plugin_page_bridge.js"
    if not bridge.is_file():
        raise RuntimeError("This QA needs an AstrBot install with Plugin Pages")
    output.mkdir(parents=True, exist_ok=True)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                logo_data = base64.b64encode((ROOT / "logo.png").read_bytes()).decode("ascii")
                harness = HARNESS.replace("__LOGO_DATA_URL__", f"data:image/png;base64,{logo_data}")
                data, content_type = harness.encode(), "text/html; charset=utf-8"
            elif self.path == "/api/plugin/page/bridge-sdk.js":
                data, content_type = bridge.read_bytes(), "application/javascript"
            elif self.path.startswith("/page/"):
                name = self.path.removeprefix("/page/")
                if name not in ("index.html", "style.css", "app.js"):
                    self.send_error(404)
                    return
                data = (ROOT / "pages" / "apple_music" / name).read_bytes()
                content_type = {"html": "text/html; charset=utf-8", "css": "text/css", "js": "application/javascript"}[name.rsplit(".", 1)[-1]]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 980})
            page.on("pageerror", lambda error: errors.append(f"{error} | {getattr(error, 'stack', '')}"))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            frame = page.frame_locator("iframe")
            def screenshot(name):
                # Screenshots extending beyond an iframe viewport get clipped.
                # Expand the QA harness, not the shipped page, for a full capture.
                viewport = page.viewport_size
                height = frame.locator("body").evaluate("el => el.scrollHeight")
                page.set_viewport_size({"width": viewport["width"], "height": max(height, viewport["height"])})
                page.screenshot(path=str(output / name))
                page.set_viewport_size(viewport)
            try:
                frame.locator("#wrapper-state").get_by_text("已连接", exact=False).wait_for(timeout=5000)
            except Exception:
                print(json.dumps({"initial_wrapper_state": frame.locator("#wrapper-state").inner_text(), "browser_errors": errors}, ensure_ascii=False))
                raise
            frame.locator("#plugin-logo").wait_for(state="visible")
            assert frame.locator(".brand").locator("img").count() == 1
            assert frame.locator("#plugin-logo").evaluate("el => getComputedStyle(el).objectFit") == "contain"
            assert frame.locator("[data-platform-status]").count() == 5
            assert frame.locator("[data-platform-status='douyin']").get_attribute("data-available") == "true"
            assert frame.locator("[data-platform-status='qqmusic']").get_attribute("data-available") == "false"
            assert frame.locator("[data-platform-status='qqmusic'] small").inner_text() == "账号 Cookie 不完整"
            assert frame.locator("[data-platform-status='applemusic'] small").inner_text() == "未登录"
            light_palette = frame.locator("html").evaluate("el => Object.fromEntries(['--bg','--accent','--surface-soft'].map(key => [key, getComputedStyle(el).getPropertyValue(key).trim()]))")
            assert light_palette == {"--bg": "#F6F6F6", "--accent": "#8785A2", "--surface-soft": "#FFE2E2"}, light_palette
            assert frame.locator(".primary-button").first.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(255, 199, 199)"
            music_list = frame.locator("#music-parser-list")
            assert music_list.locator("[data-music-parser]").count() == 3
            qq_music = music_list.locator("[data-music-parser='qqmusic']")
            qq_music.locator("[data-music-setting='audio_send_format']").select_option("file")
            frame.locator("#music-settings-state").get_by_text("已同步到磁盘", exact=False).wait_for()
            assert page.evaluate("qaState.music_parsers.qqmusic.settings.audio_send_format === 'file'")
            qq_music.locator("[data-music-cookie]").fill("uin=12345; qqmusic_key=example-key")
            qq_music.locator(".music-cookie-save").click()
            page.wait_for_function("window.qaState.music_parsers.qqmusic.cookie_configured === true")
            qq_music.locator("[data-music-cookie]").wait_for(state="visible")
            frame.locator("#notice").get_by_text("Cookie 已保存", exact=False).wait_for()
            assert qq_music.locator("[data-music-cookie]").input_value() == ""
            assert frame.locator("#notice").get_by_text("Cookie 已保存", exact=False).is_visible()
            qq_music.locator("[data-cookie-check='qqmusic']").click()
            frame.locator("[data-music-parser='qqmusic'] .music-cookie-state").get_by_text("Cookie 有效").wait_for()
            assert frame.locator("[data-music-parser='qqmusic'] .music-cookie-state").get_attribute("data-validity") == "valid"
            assert frame.locator("[data-platform-status='qqmusic'] small").inner_text() == "Cookie 有效"
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'check-music-cookie').length === 1")
            music_list.screenshot(path=str(output / "music-cookie-status.png"))
            screenshot("desktop-light.png")
            assert frame.locator("#setting-backend").is_checked()
            frame.locator("#setting-return-audio").check()
            frame.locator("#settings-state").get_by_text("磁盘配置同步", exact=False).wait_for()
            frame.locator("input[name='audio-format'][value='voice']").check()
            frame.locator("#settings-state").get_by_text("磁盘配置同步", exact=False).wait_for()
            frame.locator("#setting-cover").uncheck()
            frame.locator("#settings-state").get_by_text("磁盘配置同步", exact=False).wait_for()
            frame.locator(".codec-row").first.locator("button[aria-label='下移']").click()
            assert frame.locator(".codec-row select").first.input_value() == "aac-web"
            # Settings are debounced and persisted automatically after the
            # last control change; the old explicit save button is hidden.
            frame.locator("#settings-state").get_by_text("磁盘配置同步", exact=False).wait_for()
            assert page.evaluate("qaState.settings.return_audio && qaState.settings.audio_send_format === 'voice' && !qaState.settings.show_playlist_cover && qaState.settings.file_codec_priority.join(',') === 'aac-web,alac'")
            assert not frame.locator("#save-settings").is_visible()
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'save-settings').length >= 4")
            frame.locator("#clear-cache").click()
            frame.locator("#clear-cache").get_by_text("再次点击确认清理", exact=False).wait_for()
            frame.locator("#clear-cache").click()
            frame.locator("#notice").get_by_text("已清理 9 个文件", exact=False).wait_for()
            assert frame.locator("#cache-size").get_by_text("0 B", exact=True).is_visible()
            frame.locator("#username").fill("qa@example.invalid")
            frame.locator("#password").fill("not-a-real-password")
            frame.locator("#login").click()
            assert frame.locator("#password").input_value() == ""
            frame.locator("#two-factor-form").wait_for(state="visible")
            frame.locator("#code").fill("123456")
            frame.locator("#two-factor-form button").click()
            frame.locator("#subscription-state").get_by_text("订阅有效").wait_for()
            assert frame.locator("[data-platform-status='applemusic']").get_attribute("data-available") == "true"
            assert frame.locator("[data-platform-status='applemusic'] small").inner_text() == "已登录"
            assert frame.locator("#code").input_value() == ""
            frame.locator("#verify-form button").click()
            frame.locator("#verification-result").get_by_text("306 秒", exact=False).wait_for()
            screenshot("desktop-verified.png")
            page.evaluate("document.querySelector('iframe').contentWindow.postMessage({channel:'astrbot-plugin-page',kind:'context',context:{isDark:true}},location.origin)")
            frame.locator("html[data-theme='dark']").wait_for()
            dark_surface = frame.locator("html").evaluate(
                "el => getComputedStyle(el).getPropertyValue('--surface').trim()"
            )
            assert dark_surface == "#292832", dark_surface
            dark_accent = frame.locator("html").evaluate(
                "el => getComputedStyle(el).getPropertyValue('--accent').trim()"
            )
            assert dark_accent == "#FFC7C7", dark_accent
            screenshot("desktop-dark.png")
            page.set_viewport_size({"width": 390, "height": 844})
            screenshot("mobile-dark.png")
            overflow = frame.locator("body").evaluate("""el => ({
              fits: el.scrollWidth <= window.innerWidth,
              body: [el.scrollWidth, window.innerWidth],
              elements: [...document.querySelectorAll('*')]
                .filter(node => node.getBoundingClientRect().right > window.innerWidth + 1)
                .slice(0, 12)
                .map(node => [node.tagName, node.id, node.className, Math.round(node.getBoundingClientRect().right)])
            })""")
            assert overflow["fits"], overflow
            page.evaluate("document.querySelector('iframe').contentWindow.postMessage({channel:'astrbot-plugin-page',kind:'context',context:{isDark:false}},location.origin)")
            frame.locator("html[data-theme='light']").wait_for()
            screenshot("mobile-light.png")
            page.on("dialog", lambda dialog: dialog.accept())
            frame.locator("#clear-session").click()
            frame.locator("#auth-state").get_by_text("未登录").wait_for()
            assert frame.locator("[data-platform-status='applemusic']").get_attribute("data-available") == "false"
            assert frame.locator("#track-url").is_disabled()
            page.evaluate("qaState.enabled=false;qaState.connected=false;qaState.message='请在插件配置中启用可选后端';qaState.platform_accounts[4]={key:'applemusic',label:'Apple Music',available:false,text:'账号后端未启用'}")
            frame.locator("#refresh").click()
            frame.locator("#wrapper-state").get_by_text("未启用").wait_for()
            assert frame.locator("[data-platform-status='applemusic'] small").inner_text() == "账号后端未启用"
            assert frame.locator("#password").is_disabled()
            assert not errors, errors
            print(json.dumps({"screenshots": str(output), "browser_errors": errors, "flows": ["platform-status", "apple-settings-autosave", "music-settings-autosave", "music-cookie", "music-cookie-validity-check", "clear-cache", "login", "2fa", "verify", "clear-session", "disabled-backend"], "viewports": ["1200x980", "390x844"], "real_apple_login": False}))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
