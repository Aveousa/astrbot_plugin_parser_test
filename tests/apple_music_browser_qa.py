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
const configuration = __CONFIGURATION__;
window.qaConfiguration = configuration;
window.qaBodies = [];
window.qaFailNextSave = false;
window.qaFailedPlatforms = [];
window.qaFailNextGroupSave = false;
window.qaFailNextGroupRead = false;
window.qaGroupBodies = [];
const groupState = {parsers:[
 {key:'bilibili',label:'Bilibili',globally_enabled:true},
 {key:'douyin',label:'抖音',globally_enabled:true},
 {key:'xhs',label:'小红书',globally_enabled:true},
 {key:'pixiv',label:'Pixiv',globally_enabled:true},
 {key:'netease',label:'网易云音乐',globally_enabled:true}],
 groups:[
  {platform_id:'Napcat_V5',platform_label:'NapCat',group_id:'90001',name:'解析测试群',enabled:['bilibili','douyin','xhs','pixiv','netease'],parsing_enabled:true,globally_enabled:['bilibili','douyin','xhs','pixiv','netease']},
  {platform_id:'Lagrange',platform_label:'Lagrange.OneBot',group_id:'90002',name:'另一个测试群',enabled:['bilibili','douyin','xhs','pixiv','netease'],parsing_enabled:false,globally_enabled:['bilibili','douyin','xhs','pixiv','netease']},
  {platform_id:'QQBot',platform_label:'QQ 官方机器人 (WebSocket)',group_id:'openid-A',name:'官方测试群',enabled:['bilibili','douyin','xhs','pixiv','netease'],parsing_enabled:true,globally_enabled:['bilibili','douyin','xhs','pixiv','netease']}]};
function groupErrors() {return window.qaFailedPlatforms.map(platform_id=>({platform_id,platform_label:platform_id,error:'模拟群列表查询超时，请检查连接后重试'}));}
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
 else if (endpoint === 'group-settings' || endpoint === 'group-settings-refresh') {
  if (window.qaFailNextGroupRead) {window.qaFailNextGroupRead=false;send({kind:'response',requestId:message.requestId,ok:false,error:'模拟群列表读取失败，请重试'});return;}
  data = {...groupState,groups:groupState.groups.filter(item=>!window.qaFailedPlatforms.includes(item.platform_id)),instance_errors:groupErrors()};
 }
 else if (endpoint === 'logo') data = {data_url:'__LOGO_DATA_URL__'};
 else if (endpoint === 'configuration') data = configuration;
 else if (message.action === 'api:post') {
  if (message.body?.nonce !== 'qa-nonce') {send({kind:'response',requestId:message.requestId,ok:false,error:'invalid nonce'});return;}
  if (['save-configuration','save-parser-cookie','upload-configuration-asset'].includes(endpoint)) {
   window.qaBodies.push({...message.body,endpoint});
   if (window.qaFailNextSave) {window.qaFailNextSave=false;send({kind:'response',requestId:message.requestId,ok:false,error:'模拟保存失败，请重试'});return;}
   if (endpoint === 'save-configuration') {
    const scope=message.body.scope;
    const values=scope === 'global' ? configuration.global_values : scope === 'wrapper' ? configuration.wrapper_values : configuration.parsers.find(item=>item.key===scope).values;
    Object.assign(values,message.body.settings);
   } else if (endpoint === 'save-parser-cookie') {
    const target=configuration.parsers.find(item=>item.key===message.body.parser);
    target.cookie_configured=!message.body.clear && Boolean(message.body.cookie);
    if (state.music_parsers[target.key]) state.music_parsers[target.key].cookie_configured=target.cookie_configured;
   } else {
    configuration.global_values[message.body.field]=message.body.clear?[]:['uploaded-test.png'];
   }
   setTimeout(()=>send({kind:'response',requestId:message.requestId,ok:true,data:{message:'配置已保存并生效',configuration}}),200);
   return;
  }
  if (endpoint === 'save-group-settings') {
   const submitted = message.body.groups || [];
   window.qaGroupBodies.push(submitted);
   if (window.qaFailNextGroupSave) {window.qaFailNextGroupSave=false;send({kind:'response',requestId:message.requestId,ok:false,error:'模拟群聊配置保存失败，请重试'});return;}
   if (groupState.groups.every(item=>window.qaFailedPlatforms.includes(item.platform_id))) {send({kind:'response',requestId:message.requestId,ok:false,error:'没有可用的机器人实例，未保存任何修改，请检查连接后重试'});return;}
   const saved_groups=[],unsaved_groups=[],departed_groups=[];
   submitted.forEach(group => {
    const identity={platform_id:group.platform_id,group_id:group.group_id};
    if (window.qaFailedPlatforms.includes(group.platform_id)) {unsaved_groups.push({...identity,error:'模拟群列表查询超时'});return;}
    const target = groupState.groups.find(item => item.platform_id === group.platform_id && item.group_id === group.group_id);
    if (target) {
     target.enabled = group.enabled;target.parsing_enabled = group.parsing_enabled;
     if (Object.hasOwn(group,'remark')) target.remark=group.remark.trim();
     saved_groups.push(identity);
    }
    else departed_groups.push(identity);
   });
   data = {message:'群聊保存结果',saved_groups,unsaved_groups,departed_groups,instance_errors:groupErrors(),cleaned_group_count:0,cleaned_blacklist_group_count:0};
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
    import sys
    sys.path.insert(0, str(ROOT))
    from core.management_config import ASSET_LIMITS, GLOBAL_GROUPS, WRAPPER_FIELDS
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    globals_schema = {key: schema[key] for keys in GLOBAL_GROUPS.values() for key in keys}
    for key, limit in ASSET_LIMITS.items():
        globals_schema[key] = {**globals_schema[key], "max_bytes": limit}
    apple = schema["parsers_template"]["templates"]["applemusic"]["items"]
    config = {
        "groups": GLOBAL_GROUPS, "global_fields": globals_schema,
        "global_values": {key: field["default"] for key, field in globals_schema.items()},
        "wrapper_fields": {key: field for key, field in apple.items() if key in WRAPPER_FIELDS},
        "wrapper_values": {key: field["default"] for key, field in apple.items() if key in WRAPPER_FIELDS},
        "wrapper_configured": True, "parsers": [],
    }
    config["wrapper_values"]["backend_enabled"] = True
    for key, template in schema["parsers_template"]["templates"].items():
        fields = {name: value for name, value in template["items"].items() if key != "applemusic" or name not in WRAPPER_FIELDS}
        config["parsers"].append({"key": key, "label": template["name"], "configured": key != "pixiv", "fields": fields,
                                "values": {name: field["default"] for name, field in fields.items() if name != "cookies"}, "cookie_configured": False})
    bridge = Path(astrbot.__file__).parent / "dashboard" / "plugin_page_bridge.js"
    if not bridge.is_file():
        raise RuntimeError("This QA needs an AstrBot install with Plugin Pages")
    output.mkdir(parents=True, exist_ok=True)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                logo_data = base64.b64encode((ROOT / "logo.png").read_bytes()).decode("ascii")
                harness = HARNESS.replace("__LOGO_DATA_URL__", f"data:image/png;base64,{logo_data}").replace("__CONFIGURATION__", json.dumps(config, ensure_ascii=False))
                data, content_type = harness.encode(), "text/html; charset=utf-8"
            elif self.path == "/api/plugin/page/bridge-sdk.js":
                data, content_type = bridge.read_bytes(), "application/javascript"
            elif self.path.startswith("/page/"):
                name = self.path.removeprefix("/page/")
                if name not in ("index.html", "style.css", "style-refined.css", "app.js", "settings.js", "settings.css"):
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
            assert frame.locator("#group-list .group-card").count() == 3
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'group-settings').length") >= 1
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'group-settings-refresh').length") == 0
            frame.locator("#refresh-groups").click()
            page.wait_for_function("qaRequests.filter(endpoint => endpoint === 'group-settings-refresh').length === 1")
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'group-settings-refresh').length") == 1
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
            parser_toggle.focus()
            parser_toggle.scroll_into_view_if_needed()
            scroll_before = frame.locator("body").evaluate("() => window.scrollY")
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
            assert "清理 0 个退群解析器配置" in frame.locator("#group-settings-state").inner_text()
            assert page.evaluate("qaGroupState.groups[0].parsing_enabled === false")
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'group-settings-refresh').length === 2")
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

            # Fail one instance after edits have been drafted for all bots.
            first_group.locator(".group-total-toggle input").check()
            other_group.locator(".group-total-toggle input").check()
            official_group = frame.locator(".group-card").filter(has_text="官方测试群")
            official_group.locator(".group-total-toggle input").uncheck()
            page.evaluate("qaFailedPlatforms=['Lagrange']")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("已保存 2 个群聊；1 个群聊未保存", exact=False).wait_for()
            assert page.evaluate("qaGroupState.groups[0].parsing_enabled === true")
            assert page.evaluate("qaGroupState.groups[1].parsing_enabled === false")
            assert page.evaluate("qaGroupState.groups[2].parsing_enabled === false")
            assert frame.locator("#group-dirty-indicator").is_visible()
            assert frame.locator("#group-instance-errors").is_visible()
            assert "Lagrange" in frame.locator("#group-instance-error-list").inner_text()
            assert other_group.locator(".group-total-toggle input").is_checked()
            assert other_group.locator(".group-total-toggle input").is_disabled()
            assert not first_group.locator(".group-total-toggle input").is_disabled()
            assert page.evaluate("qaGroupBodies.at(-1).length") == 3
            frame.locator("[data-view-link='overview']").click()
            frame.locator("[data-view-link='groups']").click()
            assert other_group.locator(".group-total-toggle input").is_checked()
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("已保存 0 个群聊；1 个群聊未保存", exact=False).wait_for()
            assert page.evaluate("qaGroupBodies.at(-1).length") == 1
            assert frame.locator("#group-dirty-indicator").is_visible()

            # A transport error during retry must not discard the pending draft.
            page.evaluate("qaFailNextGroupRead=true")
            frame.locator("#retry-groups").click()
            frame.locator("#group-settings-state").get_by_text("模拟群列表读取失败", exact=False).wait_for()
            assert other_group.locator(".group-total-toggle input").is_checked()
            assert frame.locator("#group-dirty-indicator").is_visible()
            page.set_viewport_size({"width": 375, "height": 812})
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            screenshot("mobile-group-instance-error-light.png")
            frame.locator("#page-theme").select_option("dark", force=True)
            screenshot("mobile-group-instance-error-dark.png")
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            page.emulate_media(reduced_motion="reduce")
            page.set_viewport_size({"width": 812, "height": 375})
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            assert frame.locator("html").evaluate("() => matchMedia('(prefers-reduced-motion: reduce)').matches")
            page.emulate_media(reduced_motion="no-preference")
            frame.locator("#page-theme").select_option("auto", force=True)
            page.set_viewport_size({"width": 1200, "height": 980})

            # Recovery re-enables controls without silently applying the draft.
            page.evaluate("qaFailedPlatforms=[]")
            frame.locator("#retry-groups").click()
            frame.locator("#group-instance-errors").wait_for(state="hidden")
            assert other_group.locator(".group-total-toggle input").is_enabled()
            assert other_group.locator(".group-total-toggle input").is_checked()
            assert page.evaluate("qaGroupState.groups[1].parsing_enabled === false")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("群聊配置已保存", exact=False).wait_for()
            frame.locator("#group-dirty-indicator").wait_for(state="hidden")
            assert page.evaluate("qaGroupState.groups[1].parsing_enabled === true")
            assert page.evaluate("qaGroupBodies.at(-1).length === 1 && qaGroupBodies.at(-1)[0].platform_id === 'Lagrange'")

            # Whole-request save failure and all-instance query failure retain drafts.
            official_group.locator(".group-total-toggle input").check()
            page.evaluate("qaFailNextGroupSave=true")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("模拟群聊配置保存失败", exact=False).wait_for()
            assert official_group.locator(".group-total-toggle input").is_checked()
            assert page.evaluate("qaGroupState.groups[2].parsing_enabled === false")
            page.evaluate("qaFailedPlatforms=['Napcat_V5','Lagrange','QQBot']")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("未保存任何修改", exact=False).wait_for()
            assert frame.locator("#group-dirty-indicator").is_visible()
            assert official_group.locator(".group-total-toggle input").is_checked()
            frame.locator("#discard-group-settings").click()
            frame.locator("#refresh-groups").click()
            frame.locator("#group-instance-error-list li").nth(2).wait_for()
            assert frame.locator(".group-card").count() == 3
            assert official_group.locator(".group-total-toggle input").is_disabled()
            page.evaluate("qaFailedPlatforms=[]")
            frame.locator("#retry-groups").click()
            frame.locator("#group-instance-errors").wait_for(state="hidden")
            assert not official_group.locator(".group-total-toggle input").is_checked()

            # Local remarks work for both OneBot and QQ Official without renaming groups.
            first_remark = first_group.locator(".group-remark-field input")
            official_remark = official_group.locator(".group-remark-field input")
            original_groups = page.evaluate("qaGroupState.groups.map(({enabled,parsing_enabled,name})=>({enabled,parsing_enabled,name}))")
            first_remark.fill("先不保存的备注")
            assert first_group.locator("h2").inner_text() == "先不保存的备注"
            assert page.evaluate("!qaGroupState.groups[0].remark")
            assert frame.locator("#group-dirty-indicator").is_visible()
            frame.locator("#discard-group-settings").click()
            assert first_remark.input_value() == ""
            assert first_group.locator("h2").inner_text() == "解析测试群"
            first_remark.fill("  游戏交流群 🍰  ")
            official_remark.fill("官方群备注 <img src=x onerror=alert(1)>")
            official_remark.evaluate("el => el.setSelectionRange(3,5)")
            assert official_remark.evaluate("el => document.activeElement === el && el.selectionStart === 3 && el.selectionEnd === 5")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("群聊配置已保存", exact=False).wait_for()
            frame.locator("#group-dirty-indicator").wait_for(state="hidden")
            assert page.evaluate("qaGroupBodies.at(-1).length === 2")
            assert first_remark.input_value() == "游戏交流群 🍰"
            assert first_group.locator("h2").inner_text() == "游戏交流群 🍰"
            assert official_group.locator("h2").inner_text() == "官方群备注 <img src=x onerror=alert(1)>"
            assert official_group.locator("h2 img").count() == 0
            assert first_group.get_by_text("原始名称：解析测试群", exact=True).is_visible()
            assert page.evaluate("qaGroupState.groups.map(({enabled,parsing_enabled,name})=>({enabled,parsing_enabled,name}))") == original_groups
            assert page.evaluate("qaGroupState.groups[0].remark") == "游戏交流群 🍰"
            # Reload only the plugin page, keeping the simulated server config.
            page.evaluate("document.querySelector('iframe').contentWindow.location.reload()")
            frame.locator("[data-view-link='groups']").click()
            first_remark.wait_for(state="visible")
            assert first_remark.input_value() == "游戏交流群 🍰"
            assert official_remark.input_value() == "官方群备注 <img src=x onerror=alert(1)>"
            frame.locator("#group-search").fill("游戏交流群")
            assert frame.locator(".group-card").count() == 1
            frame.locator("#group-search").fill("官方群备注")
            assert frame.locator(".group-card").count() == 1
            frame.locator("#group-search").fill("解析测试群")
            assert frame.locator(".group-card").count() == 1
            frame.locator("#group-search").fill("")
            first_remark.fill("名" * 128)
            first_remark.press("End")
            first_remark.press("A")
            assert first_remark.input_value() == "名" * 128
            page.set_viewport_size({"width": 375, "height": 812})
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            screenshot("mobile-group-remarks-light.png")
            frame.locator("#page-theme").select_option("dark", force=True)
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            screenshot("mobile-group-remarks-dark.png")
            frame.locator("#page-theme").select_option("auto", force=True)
            page.set_viewport_size({"width": 1200, "height": 980})
            frame.locator("#discard-group-settings").click()
            assert first_remark.input_value() == "游戏交流群 🍰"

            # A save error keeps the remark draft; a retry persists it.
            first_remark.fill("失败后重试备注")
            page.evaluate("qaFailNextGroupSave=true")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("模拟群聊配置保存失败", exact=False).wait_for()
            assert first_remark.input_value() == "失败后重试备注"
            assert page.evaluate("qaGroupState.groups[0].remark") == "游戏交流群 🍰"
            assert frame.locator("#group-dirty-indicator").is_visible()
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("群聊配置已保存", exact=False).wait_for()
            frame.locator("#group-dirty-indicator").wait_for(state="hidden")
            assert page.evaluate("qaGroupState.groups[0].remark") == "失败后重试备注"

            # Partial success must retain the failed QQ bot's remark draft.
            first_remark.fill("OneBot备注已更新")
            official_remark.fill("官机草稿待重试")
            page.evaluate("qaFailedPlatforms=['QQBot']")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("已保存 1 个群聊；1 个群聊未保存", exact=False).wait_for()
            official_remark.wait_for(state="visible")
            assert official_remark.input_value() == "官机草稿待重试"
            assert official_remark.is_disabled()
            assert page.evaluate("qaGroupState.groups[0].remark") == "OneBot备注已更新"
            assert page.evaluate("qaGroupState.groups[2].remark") == "官方群备注 <img src=x onerror=alert(1)>"
            assert frame.locator("#group-dirty-indicator").is_visible()
            page.evaluate("qaFailedPlatforms=[]")
            frame.locator("#retry-groups").click()
            frame.locator("#group-instance-errors").wait_for(state="hidden")
            assert official_remark.input_value() == "官机草稿待重试"
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("群聊配置已保存", exact=False).wait_for()
            frame.locator("#group-dirty-indicator").wait_for(state="hidden")
            assert page.evaluate("qaGroupBodies.at(-1).length === 1 && qaGroupBodies.at(-1)[0].platform_id === 'QQBot'")
            assert page.evaluate("qaGroupState.groups[2].remark") == "官机草稿待重试"
            first_remark.fill("")
            official_remark.fill("")
            frame.locator("#save-group-settings").click()
            frame.locator("#group-settings-state").get_by_text("群聊配置已保存", exact=False).wait_for()
            frame.locator("#group-dirty-indicator").wait_for(state="hidden")
            assert first_group.locator("h2").inner_text() == "解析测试群"
            assert official_group.locator("h2").inner_text() == "官方测试群"
            assert page.evaluate("qaGroupState.groups.map(({enabled,parsing_enabled,name})=>({enabled,parsing_enabled,name}))") == original_groups

            frame.locator("[data-view-link='music']").click()
            frame.locator("[data-view='music']").wait_for(state="visible")
            assert frame.locator("[data-view='overview']").is_hidden()
            assert frame.locator(".platform-status-bar").is_visible()
            assert frame.locator("#music > .music-connection-summary").count() == 1
            assert frame.locator(".music-connection-summary").count() == 1
            assert frame.locator("#music").evaluate("el => el.children[1].matches('.music-connection-summary') && el.children[2].id === 'plugin-configuration'")
            config_list = frame.locator("#parser-configuration-list")
            assert config_list.locator("[data-parser]").count() == 10
            apple_music = frame.locator("#wrapper-configuration")
            assert apple_music.locator("#wrapper-configuration-title").inner_text() == "Apple Music wrapper 与账号环境"
            assert apple_music.locator("#account").count() == 1
            assert apple_music.locator("#verify").count() == 1
            assert frame.locator(".music-connection-summary").evaluate("el => el.compareDocumentPosition(document.querySelector('#plugin-configuration')) & Node.DOCUMENT_POSITION_FOLLOWING")
            assert frame.locator("#plugin-configuration").evaluate("el => el.compareDocumentPosition(document.querySelector('#wrapper-configuration')) & Node.DOCUMENT_POSITION_FOLLOWING")
            assert frame.locator("#verify").evaluate("el => el.compareDocumentPosition(document.querySelector('#parser-configuration')) & Node.DOCUMENT_POSITION_FOLLOWING")
            assert frame.locator("[data-parser='pixiv'] input").first.is_disabled()
            page.set_viewport_size({"width": 375, "height": 812})
            assert frame.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
            screenshot("mobile-apple-music-top.png")
            page.set_viewport_size({"width": 1200, "height": 980})
            frame.locator("#config-global-require_at_in_group").uncheck()
            page.wait_for_function("qaConfiguration.global_values.require_at_in_group === false")
            frame.locator("#configuration-state").get_by_text("所有更改已保存", exact=False).wait_for()
            frame.locator("#config-global-debounce_interval").fill("20")
            frame.locator("#config-global-debounce_interval").blur()
            page.wait_for_function("qaConfiguration.global_values.debounce_interval === 20")
            frame.locator("#configuration-state").get_by_text("所有更改已保存", exact=False).wait_for()
            # Edits arriving during an in-flight save must be submitted next.
            frame.locator("#config-global-debounce_interval").evaluate("""el => {
              el.value='30';el.dispatchEvent(new Event('change'));
              setTimeout(()=>{el.value='40';el.dispatchEvent(new Event('change'));},350);
            }""")
            page.wait_for_function("qaConfiguration.global_values.debounce_interval === 40")
            frame.locator("#configuration-state").get_by_text("所有更改已保存", exact=False).wait_for()
            assert frame.locator("#config-global-debounce_interval").input_value() == "40"
            page.evaluate("qaFailNextSave=true")
            frame.locator("#config-global-forward_threshold").fill("3")
            frame.locator("#config-global-forward_threshold").blur()
            frame.locator("#retry-configuration").wait_for(state="visible")
            frame.locator("#retry-configuration").click()
            page.wait_for_function("qaConfiguration.global_values.forward_threshold === 3")
            frame.locator("#configuration-state").get_by_text("所有更改已保存", exact=False).wait_for()
            qq_music = config_list.locator("[data-parser='qqmusic']")
            qq_music.locator("summary").click()
            qq_music.locator("#config-qqmusic-parse_lyrics").check()
            page.wait_for_function("qaConfiguration.parsers.find(p=>p.key==='qqmusic').values.parse_lyrics === true")
            qq_music.locator("#config-qqmusic-audio_send_format").select_option("file")
            page.wait_for_function("qaConfiguration.parsers.find(p=>p.key==='qqmusic').values.audio_send_format === 'file'")
            qq_music.locator("[data-key='file_quality_priority'] button[aria-label='下移第 1 项']").click()
            page.wait_for_function("qaConfiguration.parsers.find(p=>p.key==='qqmusic').values.file_quality_priority.join(',') === 'high,lossless,standard'")
            qq_music.locator("#config-qqmusic-cookies").fill("uin=12345; qqmusic_key=example-key")
            before_cookie = page.evaluate("qaRequests.filter(item=>item==='save-parser-cookie').length")
            assert page.evaluate("qaRequests.filter(item=>item==='save-parser-cookie').length") == before_cookie
            qq_music.get_by_role("button", name="保存 Cookie", exact=True).click()
            qq_music.locator("[data-key='cookies'] .config-field-status").get_by_text("Cookie 已保存", exact=False).wait_for()
            assert qq_music.locator("#config-qqmusic-cookies").input_value() == ""
            qq_music.locator("[data-cookie-check='qqmusic']").click()
            qq_music.locator("[data-key='cookies']").get_by_text("Cookie 有效", exact=True).wait_for()
            assert page.evaluate("qaRequests.filter(endpoint => endpoint === 'check-music-cookie').length === 1")
            assert frame.locator("#config-wrapper-backend_enabled").is_checked()
            apple = config_list.locator("[data-parser='applemusic']")
            apple.locator("summary").click()
            apple.locator("#config-applemusic-return_audio").check()
            page.wait_for_function("qaConfiguration.parsers.find(p=>p.key==='applemusic').values.return_audio === true")
            apple.locator("#config-applemusic-audio_send_format").select_option("voice")
            page.wait_for_function("qaConfiguration.parsers.find(p=>p.key==='applemusic').values.audio_send_format === 'voice'")
            frame.locator("#configuration-state").get_by_text("所有更改已保存", exact=False).wait_for()
            # Actual file input, explicit upload, and restore default.
            page.on("dialog", lambda dialog: dialog.accept())
            frame.locator("#config-global-custom_logo").set_input_files(str(ROOT / "logo.png"))
            logo_field = frame.locator("[data-scope='global'][data-key='custom_logo']")
            logo_field.get_by_role("button", name="上传并应用").click()
            logo_field.locator(".config-field-status").get_by_text("资源已保存", exact=False).wait_for()
            assert page.evaluate("qaConfiguration.global_values.custom_logo.length === 1")
            logo_field.get_by_role("button", name="恢复默认").click()
            page.wait_for_function("qaConfiguration.global_values.custom_logo.length === 0")
            qq_music.get_by_role("button", name="清除 Cookie", exact=True).click()
            page.wait_for_function("qaConfiguration.parsers.find(p=>p.key==='qqmusic').cookie_configured === false")
            screenshot("desktop-parser-settings.png")
            frame.locator("[data-view-link='overview']").click()
            assert frame.locator("#cache").is_visible()
            assert frame.locator("#cache").evaluate("el=>el.closest('[data-view]').dataset.view") == "overview"
            frame.locator("#config-global-clean_cron").fill("0 3 * * *")
            frame.locator("#config-global-clean_cron").blur()
            page.wait_for_function("qaConfiguration.global_values.clean_cron === '0 3 * * *'")
            frame.locator("#configuration-state").get_by_text("所有更改已保存", exact=False).wait_for(state="attached")
            frame.locator("#clear-cache").click()
            frame.locator("#clear-cache").get_by_text("再次点击确认清理", exact=False).wait_for()
            frame.locator("#clear-cache").click()
            frame.locator("#cache-size").get_by_text("0 B", exact=True).wait_for()
            frame.locator("[data-view-link='music']").click()
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
            print(json.dumps({"screenshots": str(output), "browser_errors": errors, "flows": ["overview-music-group-navigation", "group-settings-draft-discard-save", "group-unsaved-indicator-and-focus-scroll-stability", "group-search-empty-state", "group-onebot-qq-partial-save", "group-failed-instance-draft-preservation", "group-retry-recovery-and-changed-only-submission", "group-all-instances-failed", "group-save-and-read-errors", "group-warning-light-dark-landscape-reduced-motion", "classic-soft-style-toggle", "platform-status", "apple-settings-autosave", "music-settings-autosave", "music-cookie", "music-cookie-validity-check", "clear-cache", "login", "2fa", "verify", "clear-session", "disabled-backend"], "viewports": ["1200x980", "390x844", "812x375"], "responsive_widths": responsive_widths, "saved_style": page.evaluate("document.querySelector('iframe').contentWindow.localStorage.getItem('apple-music-management-style')"), "real_apple_login": False}))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
