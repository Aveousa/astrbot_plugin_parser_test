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
  file_codec_priority:['alac','aac-web'],show_playlist_cover:true,show_playlist_url:true,force_chinese_language:false},
 music_parsers:{
  netease:{key:'netease',label:'网易云音乐',known:true,configured:true,enabled:true,available:true,cookie_configured:true,cookie_status:'Cookie 已配置',settings:{enable:true,return_audio:true,parse_lyrics:false,audio_send_format:'voice',show_playlist_cover:true,show_playlist_url:true}},
  qqmusic:{key:'qqmusic',label:'QQ 音乐',known:true,configured:true,enabled:true,available:false,cookie_configured:false,cookie_validity:'unconfigured',cookie_status:'Cookie 未配置',settings:{enable:true,return_audio:true,parse_lyrics:false,audio_send_format:'voice',file_quality_priority:['lossless','high','standard'],show_playlist_cover:true,show_playlist_url:true}},
  kugou:{key:'kugou',label:'酷狗音乐',known:true,configured:true,enabled:false,available:false,cookie_configured:false,cookie_status:'解析器未启用',settings:{enable:false,return_audio:true,audio_send_format:'voice',show_playlist_cover:true,show_playlist_url:true}}},
 parsers:[
  {key:'netease',label:'网易云音乐',known:true,configured:true,enabled:true,cookie_configured:true,cookie_status:'Cookie 已配置'},
  {key:'qqmusic',label:'QQ 音乐',known:true,configured:true,enabled:true,cookie_configured:false,cookie_status:'账号 Cookie 不完整'},
 {key:'kugou',label:'酷狗音乐',known:true,configured:true,enabled:false,cookie_configured:false,cookie_status:'解析器未启用'}],
};
const groupState = {parsers:[
 {key:'bilibili',label:'Bilibili',globally_enabled:true},
 {key:'douyin',label:'抖音',globally_enabled:true},
 {key:'xhs',label:'小红书',globally_enabled:true},
 {key:'pixiv',label:'Pixiv',globally_enabled:true},
 {key:'netease',label:'网易云音乐',globally_enabled:true}],
 groups:[
  {platform_id:'Napcat_V5',platform_label:'NapCat',group_id:'90001',name:'解析测试群',enabled:['bilibili','douyin','xhs','pixiv','netease'],parsing_enabled:true,globally_enabled:['bilibili','douyin','xhs','pixiv','netease']},
  {platform_id:'Lagrange',platform_label:'Lagrange.OneBot',group_id:'90002',name:'另一个测试群',enabled:['bilibili','douyin','xhs','pixiv','netease'],parsing_enabled:false,globally_enabled:['bilibili','douyin','xhs','pixiv','netease']}]};
let job = {state:'idle'};
window.qaState = state;
window.qaGroupState = groupState;
window.qaRequests = [];
window.addEventListener('message', event => {
 const message = event.data;
 if (message?.channel !== 'astrbot-plugin-page') return;
 const send = data => event.source.postMessage({channel:'astrbot-plugin-page',...data}, location.origin);
 if (message.kind === 'ready') {send({kind:'context',context:{pluginName:'test-plugin',pageName:'parser_management',isDark:false}}); return;}
 if (message.kind !== 'request') return;
 const endpoint = message.endpoint?.replace('apple-music/','');
 window.qaRequests.push(endpoint);
 let data;
 if (endpoint === 'status') data = {...state,operation:job};
 else if (endpoint === 'operation') data = job;
 else if (endpoint === 'group-settings') data = groupState;
 else if (endpoint === 'logo') data = {data_url:'__LOGO_DATA_URL__'};
 else if (message.action === 'api:post') {
  if (message.body?.nonce !== 'qa-nonce') {send({kind:'response',requestId:message.requestId,ok:false,error:'invalid nonce'});return;}
  if (endpoint === 'save-group-settings') {
   const submitted = message.body.groups || [];
   submitted.forEach(group => {
    const target = groupState.groups.find(item => item.platform_id === group.platform_id && item.group_id === group.group_id);
    if (target) {target.enabled = group.enabled;target.parsing_enabled = group.parsing_enabled;}
   });
   data = {message:'群聊总开关及解析器设置已确认并保存'};
   send({kind:'response',requestId:message.requestId,ok:true,data});
   return;
  }
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
                if name not in ("index.html", "style.css", "style-refined.css", "app.js"):
                    self.send_error(404)
                    return
                data = (ROOT / "pages" / "parser_management" / name).read_bytes()
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
            def screenshot(name, full_page=True):
                # Screenshots extending beyond an iframe viewport get clipped.
                # Expand the QA harness, not the shipped page, for a full capture.
                viewport = page.viewport_size
                height = frame.locator("body").evaluate("el => el.scrollHeight")
                page.set_viewport_size({"width": viewport["width"], "height": max(height, viewport["height"])})
                page.screenshot(path=str(output / name), full_page=full_page)
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
            assert frame.locator("[data-view-link='overview']").is_visible()
            assert frame.locator("[data-view='overview']").is_visible()
            assert frame.locator("[data-view='music']").is_hidden()
            assert frame.locator("[data-view='groups']").is_hidden()
            assert frame.locator(".platform-status-bar").is_hidden()

            assert frame.locator("html").get_attribute("data-page-style") == "classic"
            classic_accent = frame.locator("html").evaluate(
                "el => getComputedStyle(el).getPropertyValue('--accent').trim()"
            )
            classic_palette = frame.locator("html").evaluate(
                "el => Object.fromEntries(['--bg','--accent','--surface-soft'].map(key => [key, getComputedStyle(el).getPropertyValue(key).trim()]))"
            )
            assert classic_palette["--bg"] == "#F6F6F6", classic_palette
            screenshot("desktop-classic.png", full_page=False)
            page.set_viewport_size({"width": 390, "height": 844})
            screenshot("mobile-classic.png", full_page=False)
            frame.locator("[data-view-link='groups']").click()
            page.wait_for_timeout(100)
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            screenshot("mobile-groups.png", full_page=False)
            page.set_viewport_size({"width": 1200, "height": 980})
            frame.locator("[data-view-link='overview']").click()
            page.set_viewport_size({"width": 1200, "height": 980})
            frame.locator("#page-style").select_option("soft")
            assert frame.locator("html").get_attribute("data-page-style") == "soft"
            screenshot("desktop-soft.png", full_page=False)
            page.set_viewport_size({"width": 390, "height": 844})
            screenshot("mobile-soft.png", full_page=False)
            page.set_viewport_size({"width": 1200, "height": 980})
            light_palette = frame.locator("html").evaluate("el => Object.fromEntries(['--bg','--accent','--surface-soft'].map(key => [key, getComputedStyle(el).getPropertyValue(key).trim()]))")
            assert light_palette == {"--bg": "#F6F6F6", "--accent": "#8785A2", "--surface-soft": "#FFE2E2"}, light_palette
            assert frame.locator("html").evaluate("localStorage.getItem('apple-music-management-style')") == "soft"
            assert frame.locator(".primary-button").first.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(255, 199, 199)"
            frame.locator("[data-view-link='groups']").click()
            frame.locator("#group-list .group-card").first.wait_for()
            assert frame.locator("#group-list .group-card").count() == 2
            assert frame.locator("[data-view='groups']").is_visible()
            assert frame.locator("[data-view='music']").is_hidden()
            assert frame.locator(".platform-status-bar").is_hidden()
            first_group = frame.locator(".group-card").filter(has_text="解析测试群")
            other_group = frame.locator(".group-card").filter(has_text="另一个测试群")
            total_toggle = first_group.locator(".group-total-toggle input")
            assert total_toggle.is_checked()
            assert not other_group.locator(".group-total-toggle input").is_checked()
            bilibili_toggle = first_group.locator(".group-parser-toggle").filter(has_text="Bilibili").locator("input")
            initial_saves = page.evaluate("qaRequests.filter(endpoint => endpoint === 'save-group-settings').length")
            total_toggle.uncheck()
            frame.locator("#group-dirty-indicator").get_by_text("有未保存更改", exact=True).wait_for()
            assert frame.locator("#group-dirty-indicator").is_visible()
            assert page.evaluate("qaGroupState.groups[0].parsing_enabled === true")
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'save-group-settings').length") == initial_saves
            frame.locator("#discard-group-settings").click()
            assert first_group.locator(".group-total-toggle input").is_checked()
            total_toggle = first_group.locator(".group-total-toggle input")
            total_toggle.uncheck()
            bilibili_toggle.uncheck()
            frame.locator("#group-dirty-indicator").get_by_text("有未保存更改", exact=True).wait_for()
            assert frame.locator("#group-dirty-indicator").is_visible()
            assert frame.locator("#group-save-actions").bounding_box()["x"] > frame.locator("#group-settings-state").bounding_box()["x"]
            assert page.evaluate("qaGroupState.groups[0].enabled.includes('bilibili')")
            assert page.evaluate("qaGroupState.groups[0].parsing_enabled === true")
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'save-group-settings').length") == initial_saves
            frame.locator("#discard-group-settings").click()
            assert first_group.locator(".group-parser-toggle").filter(has_text="Bilibili").locator("input").is_checked()
            parser_toggle = first_group.locator(".group-parser-toggle").filter(has_text="Bilibili").locator("input")
            group_list = frame.locator("#group-list")
            group_list.evaluate("el => el.scrollIntoView({block: 'end'})")
            frame.locator("body").evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(50)
            scroll_before = frame.locator("body").evaluate("() => window.scrollY")
            parser_toggle.focus()
            focus_before = parser_toggle.evaluate("el => document.activeElement === el")
            parser_toggle.uncheck()
            assert parser_toggle.evaluate("el => document.activeElement === el") == focus_before
            assert abs(frame.locator("body").evaluate("() => window.scrollY") - scroll_before) <= 2
            frame.locator("#discard-group-settings").click()
            assert frame.locator("body").evaluate("() => window.scrollY") > 0
            first_group = frame.locator(".group-card").filter(has_text="解析测试群")
            first_group.locator(".group-total-toggle input").uncheck()
            first_group.locator(".group-parser-toggle").filter(has_text="Bilibili").locator("input").uncheck()
            frame.locator("#save-group-settings").click()
            page.wait_for_function("!window.qaGroupState.groups[0].enabled.includes('bilibili')")
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'save-group-settings').length") == initial_saves + 1
            assert frame.locator("#group-settings-state").get_attribute("data-error") != "true"
            assert page.evaluate("qaGroupState.groups[0].parsing_enabled === false")
            page.evaluate("qaGroupState.groups[0].enabled = ['bilibili','douyin','xhs','pixiv']")
            frame.locator(".group-card").filter(has_text="解析测试群").locator(".group-total-toggle input").focus()
            scroll_before_sync = frame.locator("body").evaluate("() => window.scrollY")
            initial_group_reads = page.evaluate("qaRequests.filter(endpoint => endpoint === 'group-settings').length")
            frame.locator("body").evaluate("() => window.dispatchEvent(new Event('focus'))")
            page.wait_for_function("count => qaRequests.filter(endpoint => endpoint === 'group-settings').length > count", arg=initial_group_reads)
            page.wait_for_function("document.querySelector('iframe').contentDocument.querySelectorAll('.group-card')[0].querySelector('.group-enabled-count').textContent.startsWith('4/')")
            assert not frame.locator(".group-card").filter(has_text="解析测试群").locator(".group-parser-toggle").filter(has_text="网易云音乐").locator("input").is_checked()
            assert frame.locator(".group-card").filter(has_text="解析测试群").locator(".group-parser-toggle").filter(has_text="网易云音乐").locator("small").inner_text() == "本群已关闭"
            assert abs(frame.locator("body").evaluate("() => window.scrollY") - scroll_before_sync) <= 2
            frame.locator("#group-search").fill("no-such-group")
            frame.locator("#group-list .group-empty").get_by_text("没有匹配的群聊").wait_for()
            frame.locator("#group-search").fill("")
            screenshot("desktop-groups.png")
            frame.locator("[data-view-link='music']").click()
            frame.locator("[data-view='music']").wait_for(state="visible")
            assert frame.locator("[data-view='overview']").is_hidden()
            assert frame.locator(".platform-status-bar").is_visible()
            music_list = frame.locator("#music-parser-list")
            assert music_list.locator("[data-music-parser]").count() == 3
            qq_music = music_list.locator("[data-music-parser='qqmusic']")
            netease_music = music_list.locator("[data-music-parser='netease']")
            assert qq_music.locator("[data-music-setting='parse_lyrics']").count() == 1
            assert netease_music.locator("[data-music-setting='parse_lyrics']").count() == 1
            qq_music.locator("[data-music-setting='parse_lyrics']").check()
            frame.locator("#music-settings-state").get_by_text("已同步到磁盘", exact=False).wait_for()
            assert page.evaluate("qaState.music_parsers.qqmusic.settings.parse_lyrics === true")
            qq_music.locator("[data-music-setting='audio_send_format']").select_option("file")
            frame.locator("#music-settings-state").get_by_text("已同步到磁盘", exact=False).wait_for()
            assert page.evaluate("qaState.music_parsers.qqmusic.settings.audio_send_format === 'file'")
            qq_music.locator(".music-quality-editor .codec-row").first.locator("button[aria-label='下移']").click()
            frame.locator("#music-settings-state").get_by_text("已同步到磁盘", exact=False).wait_for()
            assert page.evaluate("qaState.music_parsers.qqmusic.settings.file_quality_priority.join(',') === 'high,lossless,standard'")
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
            frame.locator("#setting-chinese-language").check()
            frame.locator("#settings-state").get_by_text("磁盘配置同步", exact=False).wait_for()
            frame.locator(".codec-row").first.locator("button[aria-label='下移']").click()
            assert frame.locator(".codec-row select").first.input_value() == "aac-web"
            # Settings are debounced and persisted automatically after the
            # last control change; the old explicit save button is hidden.
            frame.locator("#settings-state").get_by_text("磁盘配置同步", exact=False).wait_for()
            assert page.evaluate("qaState.settings.return_audio && qaState.settings.audio_send_format === 'voice' && !qaState.settings.show_playlist_cover && qaState.settings.force_chinese_language && qaState.settings.file_codec_priority.join(',') === 'aac-web,alac'")
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
            assert dark_surface == "#302F39", dark_surface
            dark_accent = frame.locator("html").evaluate(
                "el => getComputedStyle(el).getPropertyValue('--accent').trim()"
            )
            assert dark_accent == "#B4B2C7", dark_accent
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
            responsive_widths = []
            for width in (375, 390, 620, 768, 880, 1024, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                fits = frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
                assert fits, f"horizontal overflow at {width}px"
                responsive_widths.append(width)
            page.set_viewport_size({"width": 390, "height": 844})
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
            page.wait_for_function("document.querySelector('iframe').contentDocument.querySelector('#wrapper-state').textContent === '未启用'")
            assert frame.locator("[data-platform-status='applemusic'] small").inner_text() == "账号后端未启用"
            assert frame.locator("#password").is_disabled()
            assert not errors, errors
            print(json.dumps({"screenshots": str(output), "browser_errors": errors, "flows": ["overview-music-group-navigation", "group-settings-draft-discard-save", "group-unsaved-indicator-and-focus-scroll-stability", "group-search-empty-state", "classic-soft-style-toggle", "platform-status", "apple-settings-autosave", "music-settings-autosave", "music-cookie", "music-cookie-validity-check", "clear-cache", "login", "2fa", "verify", "clear-session", "disabled-backend"], "viewports": ["1200x980", "390x844"], "responsive_widths": responsive_widths, "saved_style": page.evaluate("document.querySelector('iframe').contentWindow.localStorage.getItem('apple-music-management-style')"), "real_apple_login": False}))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
