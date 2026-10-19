"""Render Jinja2 card fixtures through the existing Renderer + Playwright path."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from PIL import Image, ImageDraw
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.data import AudioContent, Author, DynamicContent, FileContent, GraphicsContent, ImageContent, ParseResult, Platform, TextContent, VideoContent
from core.render import Renderer

ROOT = Path(__file__).resolve().parents[2]
TASK_DIR = Path(__file__).resolve().parent
ASSET_DIR = TASK_DIR / "assets"
PREVIEW_DIR = TASK_DIR / "previews"

def make_cover(filename: str, size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int], foreground: tuple[int, int, int]) -> Path:
    """Create restrained abstract test swatches for media framing QA."""
    width, height = size
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(round(top[c] * (1 - ratio) + bottom[c] * ratio) for c in range(3))
        draw.line((0, y, width, y), fill=color)
    draw.polygon([(0, round(height*.68)), (round(width*.24), round(height*.52)), (round(width*.43), round(height*.71)), (round(width*.67), round(height*.48)), (width, round(height*.66)), (width, height), (0, height)], fill=foreground)
    draw.rectangle((0, round(height*.87), width, height), fill=tuple(round(channel*.74) for channel in foreground))
    path = ASSET_DIR / filename
    image.save(path)
    return path

def build_renderer() -> Renderer:
    config = SimpleNamespace(card_template="Guizang_card", card_custom_template="", card_dynamic_color=False, card_custom_font=[], cache_dir=TASK_DIR/"cache", data_dir=TASK_DIR/"data", template_dir=TASK_DIR/"templates", plugin_dir=ROOT, logo_path=ROOT/"logo.png", emoji_style="APPLE")
    renderer = Renderer(config)
    renderer._emoji_source = None
    return renderer

def build_results() -> list[tuple[str, ParseResult]]:
    song_cover = make_cover("song-cover.png", (720,520), (57,82,92), (174,138,99), (38,57,62))
    playlist_cover = make_cover("playlist-cover.png", (720,420), (69,99,137), (191,131,103), (44,64,96))
    video_cover = make_cover("video-cover.png", (720,405), (42,65,78), (116,144,147), (35,51,59))
    graphics_cover = make_cover("graphics-cover.png", (720,880), (118,130,103), (222,190,131), (65,79,61))
    dynamic_cover = make_cover("dynamic-cover.png", (720,540), (87,72,105), (184,139,145), (56,49,75))
    track_covers = [make_cover(f"track-cover-{i}.png", (160,160), *colors) for i,colors in enumerate([((46,72,88),(175,130,77),(35,51,59)), ((88,68,58),(191,145,98),(60,45,41)), ((46,82,73),(159,170,121),(31,59,49))],1)]
    timestamp = int(datetime(2026,10,19,9,30).timestamp())
    song = ParseResult(platform=Platform("applemusic","Apple Music"), author=Author("林朝 / City Echo"), title="海边的房间", timestamp=timestamp, url="https://music.apple.com/cn/album/room-by-the-sea/123?i=456", contents=[ImageContent(song_cover)], extra={"info":"专辑：潮汐之间\n时长：04:18","card_info":"专辑：潮汐之间\n时长：04:18"}, like_count=28400, comment_count=326, favorite_count=12000, share_count=108)
    playlist_rows = [("一封未寄出的信","林朝","潮汐之间",track_covers[0]),("雨停之前，请别关灯","City Echo","夜行备忘",track_covers[1]),("潮声从窗外经过","林朝 / 江叙","潮汐之间",track_covers[2]),("留在车站的蓝色伞",None,"凌晨四点",None)]
    playlist = ParseResult(platform=Platform("netease","网易云音乐"), author=Author("晚风编辑部"), title="夜路收藏：把城市留给自己", text="给晚归的路上一点安静。", timestamp=timestamp, url="https://music.163.com/playlist?id=123456", contents=[ImageContent(playlist_cover)], extra={"info":"播放：48.2万\n创建于 2024 年","card_info":"播放：48.2万\n创建于 2024 年","playlist_track_count":38,"playlist_tracks":[{"index":i,"title":title,"artist":artist,"album":album,"cover_content":ImageContent(pic) if pic else None} for i,(title,artist,album,pic) in enumerate(playlist_rows,1)]}, comment_count=91, favorite_count=8700, share_count=412)
    video_text=("一段沿着海岸线拍下的傍晚散步，镜头从旧码头开始，经过防波堤，最后停在潮水退去后的沙滩。风很轻，天色慢慢变成深蓝，远处还有最后一班船。")*3
    video=ParseResult(platform=Platform("bilibili","Bilibili"), author=Author("周一的慢镜头"), title="把傍晚走到海边去", text=video_text, timestamp=timestamp, url="https://www.bilibili.com/video/BV1example", contents=[VideoContent(TASK_DIR/"sample-video.mp4",cover=video_cover,duration=91)], like_count=127000, comment_count=2400, favorite_count=38800, share_count=7600)
    graphics=ParseResult(platform=Platform("xhs","小红书"), author=Author("阿禾的日常"), title="把一束光留在旧窗台", text="下午四点的光沿着窗框落下来，植物的影子刚好停在书页上。\n\n没有特别安排的一天，也值得认真记住。", timestamp=timestamp, url="https://www.xiaohongshu.com/explore/example", contents=[GraphicsContent(graphics_cover,text="窗台、旧书和初秋的光。",alt="在南向窗边拍摄，保留了自然光影。"),ImageContent(track_covers[1])], extra={"info":"拍摄设备：手机自然光","card_info":"拍摄设备：手机自然光","has_motion_photo":True}, like_count=9380, comment_count=205, favorite_count=3100, share_count=95)
    missing=ParseResult(platform=Platform("pixiv","Pixiv"))
    mixed=ParseResult(platform=Platform("pixiv","Pixiv"),author=Author("跨媒体样例"),title="声音、图片与附件并列呈现",text="非视觉媒体不会抢占首图封面位，附件名称与持续时间作为补充信息保留。",timestamp=timestamp,contents=[AudioContent(TASK_DIR/"sample-audio.mp3",duration=224),ImageContent(song_cover),FileContent(TASK_DIR/"notes.txt",name="夜行歌单.txt"),TextContent("伴随说明：为不同媒体类型保留原有渲染优先级。")],like_count=23)
    long_text="\n\n".join(["当内容变长，卡片仍应保留自然换行、清楚的段落间距和完整的信息层级。它不应该挤压封面，也不该让统计模块与页脚相互重叠。这一段用于检查中文、Latin ABCDEFG、数字 123456789 与 Emoji 🎧 混排。" for _ in range(10)])
    long=ParseResult(platform=Platform("douyin","抖音"), author=Author("作者名称测试超长时截断，且头像缺失时应显示统一占位符"), title="这是一段用于检验极长标题布局的样本文字会被限制在两行之内"*2, text=long_text, timestamp=timestamp, url="https://example.com/a/very/long/path/that/should/wrap/instead/of/overflowing/card/content?with=query&another=parameter", contents=[ImageContent(song_cover)], like_count=990000000, comment_count=120000, share_count=11)
    repost=ParseResult(platform=Platform("bilibili","Bilibili"), title="原动态：周末去看展", text="从入口处的装置开始，沿着光线走到最后一个展厅。")
    dynamic=ParseResult(platform=Platform("douyin","抖音"), author=Author("陈列间"), title="一个字段齐全的动态样例", text="现场人很多，但展厅很安静。", timestamp=timestamp, url="https://v.douyin.com/example", contents=[DynamicContent(TASK_DIR/"motion.mp4",gif_path=dynamic_cover),ImageContent(playlist_cover)], extra={"has_motion_photo":True,"is_live_stream":True,"info":"地点：沿河美术馆\n展期：至 11 月 3 日","card_info":"地点：沿河美术馆\n展期：至 11 月 3 日"}, repost=repost, like_count=1200, comment_count=36, favorite_count=88, share_count=11)
    return [("song",song),("playlist",playlist),("video",video),("graphics",graphics),("missing_fields",missing),("long_text",long),("dynamic_repost_live",dynamic),("mixed_media",mixed)]

async def render_all() -> list[dict[str,Any]]:
    ASSET_DIR.mkdir(parents=True,exist_ok=True); PREVIEW_DIR.mkdir(parents=True,exist_ok=True)
    renderer=build_renderer(); report=[]
    try:
        for case,result in build_results():
            context=await renderer._result_context(result); html=renderer.render_html(result,context); path=PREVIEW_DIR/f"{case}.png"
            if not await renderer._render_playwright_png(html,path,base_url=str(renderer._template_base_dir())): raise RuntimeError(f"Playwright render failed: {case}")
            inspection = await inspect_html(renderer, html)
            report.append({"case":case,"path":str(path),"bytes":path.stat().st_size,**inspection})
        dynamic_config = SimpleNamespace(card_template="Guizang_card", card_custom_template="", card_dynamic_color=True, card_custom_font=[], cache_dir=TASK_DIR/"cache", data_dir=TASK_DIR/"data", template_dir=TASK_DIR/"templates", plugin_dir=ROOT, logo_path=ROOT/"logo.png", emoji_style="APPLE")
        dynamic_config.card_dynamic_color = True
        dynamic_renderer = Renderer(dynamic_config)
        dynamic_renderer._emoji_source = None
        try:
            dynamic_result = build_results()[0][1]
            dynamic_context = await dynamic_renderer._result_context(dynamic_result)
            dynamic_html = dynamic_renderer.render_html(dynamic_result, dynamic_context)
            dynamic_path = PREVIEW_DIR/"dynamic_color.png"
            if not await dynamic_renderer._render_playwright_png(dynamic_html, dynamic_path, base_url=str(dynamic_renderer._template_base_dir())):
                raise RuntimeError("Playwright render failed: dynamic_color")
            dynamic_inspection = await inspect_html(dynamic_renderer, dynamic_html)
            report.append({"case":"dynamic_color","path":str(dynamic_path),"bytes":dynamic_path.stat().st_size,**dynamic_inspection})
        finally:
            await dynamic_renderer.close()
    finally: await renderer.close()
    return report

async def inspect_html(renderer: Renderer, html: str) -> dict[str,Any]:
    """Inspect real Chromium layout bounds and local resource decoding."""
    if not await renderer.start() or renderer._browser_context is None:
        raise RuntimeError("Playwright browser context unavailable for layout inspection")
    page = await renderer._browser_context.new_page()
    inspection_path = PREVIEW_DIR/".inspect-card.html"
    try:
        inspection_path.write_text(renderer._inject_base_url(html, str(renderer._template_base_dir())), encoding="utf-8")
        await page.goto(inspection_path.resolve().as_uri(), wait_until="load")
        await page.wait_for_function("Array.from(document.images).every((image) => image.complete)")
        inspection = await page.evaluate(r"""() => {
          const root = document.querySelector('[data-card-root]');
          if (!root) return { error: 'data-card-root not found' };
          const rootRect = root.getBoundingClientRect();
          const selectors = ['.card-header', '.content', '.cover', '.playlist-tracks', '.extra', '.meta', '.url', '.repost', '.live-photo-hint', '.card-brand'];
          const regions = Object.fromEntries(selectors.map(selector => {
            const element = root.querySelector(selector);
            if (!element) return [selector, null];
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return [selector, {top: Math.round(rect.top-rootRect.top), bottom: Math.round(rect.bottom-rootRect.top), height: Math.round(rect.height), display: style.display, fontSize: style.fontSize}];
          }));
          const overflow = Array.from(root.querySelectorAll('*')).flatMap(element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            const isClamped = style.display.includes('box') && style.webkitLineClamp !== 'none';
            const intentionalMastheadGlyphOverflow = element.matches('.masthead__signature') && element.querySelector('.masthead__glyph') !== null;
            if (rect.width > 0 && !(element.matches('h1') && element.parentElement?.classList.contains('story')) && !intentionalMastheadGlyphOverflow && !isClamped && (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1)) {
              return [{tag: element.tagName.toLowerCase(), className: typeof element.className === 'string' ? element.className : '', scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight}];
            }
            return [];
          });
          const images = Array.from(root.querySelectorAll('img')).map(image => ({className: image.className, complete: image.complete, naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight}));
          const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
          const accentRgb = /^#([0-9a-f]{6})$/i.exec(accent);
          const accentTriple = accentRgb ? accentRgb[1].match(/../g).map(value => parseInt(value,16)) : null;
          const nonAccentColor = Array.from(root.querySelectorAll('*')).flatMap(element => {
            const style = getComputedStyle(element);
            return [style.color, style.backgroundColor, style.borderTopColor, style.borderLeftColor].filter(color => {
          const rgb = color.match(/\d+/g);
              if (!rgb || !accentTriple) return false;
              const channels = rgb.slice(1,4).map(Number);
              return channels.every((value, index) => Math.abs(value-accentTriple[index]) <= 1);
            });
          });
          const header = root.querySelector('.card-header');
          const identity = header?.querySelector('.identity')?.getBoundingClientRect();
          const badges = root.querySelector('.card-badges')?.getBoundingClientRect();
          const headerCollision = Boolean(identity && badges && identity.right > badges.left && identity.left < badges.right && identity.bottom > badges.top && identity.top < badges.bottom);
          return {width: Math.round(rootRect.width), height: Math.round(rootRect.height), rootScrollWidth: root.scrollWidth, rootClientWidth: root.clientWidth, regions, overflow, images, accent, font: getComputedStyle(document.documentElement).fontFamily, unexpectedLargeTitleWeight: Array.from(root.querySelectorAll('h1')).map(h => ({size:getComputedStyle(h).fontSize,weight:getComputedStyle(h).fontWeight})), cardBackground: getComputedStyle(root).backgroundImage, cardBackgroundColor: getComputedStyle(root).backgroundColor, glow: getComputedStyle(root, '::before').backgroundImage, nonAccentColorCount: nonAccentColor.length, headerCollision, imageLoadFailures: images.filter(image => !image.complete || !image.naturalWidth).map(image => image.className)};
        }""")
        if inspection.get("error") or inspection.get("imageLoadFailures") or inspection.get("headerCollision") or inspection.get("rootScrollWidth", 0) > inspection.get("rootClientWidth", 0):
            raise AssertionError(f"Browser layout/resource QA failed: {inspection}")
        return inspection
    finally:
        await page.close()
        inspection_path.unlink(missing_ok=True)

def make_contact_sheet(report: list[dict[str,Any]]) -> Path:
    cell_w,cell_h=560,900; rows=(len(report)+1)//2; sheet=Image.new("RGB",(cell_w*2,cell_h*rows),(239,239,235)); draw=ImageDraw.Draw(sheet)
    for i,item in enumerate(report):
        x=(i%2)*cell_w+10; y=(i//2)*cell_h; draw.text((x,y+8),item["case"],fill=(10,10,10))
        with Image.open(item["path"]) as source:
            preview=source.convert("RGB"); preview.thumbnail((540,cell_h-45)); sheet.paste(preview,(x,y+30))
    path=PREVIEW_DIR/"contact-sheet.png"; sheet.save(path); return path

def check_template_parity() -> dict[str,Any]:
    """Make sure the redesign retains every data branch and template field from apple.html."""
    from collections import Counter
    from jinja2 import Environment, nodes

    env=Environment()
    paths=[ROOT/"core/templates/apple.html", ROOT/"core/templates/Guizang_card.html"]
    trees=[env.parse(path.read_text(encoding="utf-8")) for path in paths]
    def signature(tree: nodes.Template) -> dict[str,Any]:
        return {
            "ifs": sorted(str(node.test) for node in tree.find_all(nodes.If)),
            "loops": sorted((str(node.target),str(node.iter)) for node in tree.find_all(nodes.For)),
            "filters": sorted((node.name,str(node.node)) for node in tree.find_all(nodes.Filter)),
            "attributes": sorted({node.attr for node in tree.find_all(nodes.Getattr)}),
            "imports": sorted({node.name for node in tree.find_all(nodes.Name) if node.ctx == "load"}),
        }
    original, redesigned=map(signature,trees)
    apple_tree, redesigned_tree = trees
    platform_test = next(
        node.test
        for node in apple_tree.find_all(nodes.If)
        if isinstance(node.test, nodes.Compare)
        and isinstance(node.test.expr, nodes.Getattr)
        and node.test.expr.attr == "name"
        and isinstance(node.test.expr.node, nodes.Name)
        and node.test.expr.node.name == "platform"
    )
    platform_test_signature = str(platform_test)
    normalized_ifs = Counter(
        "MUSIC_PLATFORM_CHECK" if str(node.test) == platform_test_signature or (
            isinstance(node.test, nodes.Name) and node.test.name == "is_music"
        ) else str(node.test)
        for node in redesigned_tree.find_all(nodes.If)
    )
    required_ifs = Counter(
        "MUSIC_PLATFORM_CHECK" if str(node.test) == platform_test_signature else str(node.test)
        for node in apple_tree.find_all(nodes.If)
    )
    normalized_ifs.subtract(required_ifs)
    extra_branches = sum(normalized_ifs.values())
    normalized_ifs.update(required_ifs)
    differences = {
        "missing_if_branches": list((required_ifs - normalized_ifs).elements()),
        "missing_template_names": sorted(set(original["imports"]) - set(redesigned["imports"])),
        "missing_context_attributes": sorted(set(original["attributes"]) - set(redesigned["attributes"])),
        "loop_mismatch": original["loops"] != redesigned["loops"],
        "missing_filter_uses": list(
            (Counter(original["filters"]) - Counter(redesigned["filters"])).elements()
        ),
    }
    music_assignments = [
        node for node in redesigned_tree.find_all(nodes.Assign)
        if isinstance(node.target, nodes.Name) and node.target.name == "is_music"
    ]
    if len(music_assignments) != 1 or str(music_assignments[0].node) != platform_test_signature:
        differences["music_condition_mismatch"] = [str(node.node) for node in music_assignments]
    mismatches = {key:value for key,value in differences.items() if value}
    if mismatches:
        raise AssertionError(f"Jinja compatibility mismatch: {mismatches}")
    counts = {key:len(value) for key,value in original.items()}
    counts["retained_if_branches"] = sum(required_ifs.values())
    counts["additional_visual_branches"] = extra_branches
    return counts, {}

def main() -> None:
    parity_counts, mismatches=check_template_parity()
    report=asyncio.run(render_all()); contact=make_contact_sheet(report); report_path=TASK_DIR/"render-report.json"
    report_path.write_text(json.dumps({"renderer":"core.render.Renderer + Playwright Chromium Headless Shell","template":"core/templates/Guizang_card.html","template_parity":{"counts":parity_counts,"mismatches":mismatches},"cases":report,"contact_sheet":str(contact)},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"report":str(report_path),"cases":report,"contact_sheet":str(contact)},ensure_ascii=False,indent=2))

if __name__ == "__main__": main()
