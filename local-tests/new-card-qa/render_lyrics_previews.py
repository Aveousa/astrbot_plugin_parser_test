"""Render Guizang Social lyrics fixtures through the production renderer."""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from PIL import Image, ImageDraw
ROOT = Path(__file__).resolve().parents[2]
TASK = Path(__file__).resolve().parent
OUT = TASK / "previews"
ASSETS = TASK / "assets"
sys.path.insert(0, str(ROOT))
from core.render import Renderer

def make_cover():
    ASSETS.mkdir(parents=True, exist_ok=True)
    path = ASSETS / "lyrics-cover.png"
    image = Image.new("RGB", (720, 720))
    draw = ImageDraw.Draw(image)
    for y in range(720):
        ratio = y / 719
        rgb = tuple(round(a * (1 - ratio) + b * ratio) for a, b in zip((57, 82, 92), (174, 138, 99)))
        draw.line((0, y, 719, y), fill=rgb)
    draw.polygon([(0, 490), (175, 355), (320, 512), (500, 346), (720, 470), (720, 720), (0, 720)], fill=(38, 57, 62))
    draw.rectangle((0, 625, 720, 720), fill=(29, 44, 51))
    image.save(path)
    return path

def cases():
    cover = make_cover()
    complete = {"lyrics": "[00:00.00]作词：沈知遥\n[00:04.00]演唱：林朝\n[00:08.00]海风把旧日的回声轻轻吹远\n[00:12.00]你说别急着和整个世界告别\n\n[Verse 2]\n[00:24.00]窗边那盏灯还留着温柔的颜色\n[00:28.00]潮汐在夜里反复写下我们的名字\n[00:32.00]如果明天醒来云还停在山顶\n[00:36.00]就把没说的话交给清晨去记得\n\n[Chorus]\n[00:48.00]等一场潮汐经过安静的房间\n[00:52.00]让没寄出的信慢慢靠近海岸线\n[00:56.00]等一阵海风吹散长夜的想念\n[01:00.00]我们会在日出以前再次相见\n[01:04.00]作曲：周以安", "platform_key": "netease", "platform_display_name": "网易云音乐", "song_title": "潮汐之间", "artists": "林朝 / City Echo", "album": "海边的房间", "duration_seconds": 217, "release_text": "2024-04-24", "show_release_time": True, "cover_path": cover, "source_label": "歌词来源：网易云音乐 · 单曲 ID 1847263501"}
    missing = {"lyrics": "[00:03.00]一束光落在无人的窗台\n[00:08.00]雨声替城市说了晚安", "platform_key": "qishui", "platform_display_name": "汽水音乐", "song_title": "雨停之前", "show_release_time": False}
    title = "这是一首关于回到海边与漫长告别的歌" * 3
    line = "当深蓝色的潮水越过岸边，我们仍然记得那些没有说出口的愿望与约定"
    lyrics = "\n".join(f"[{i // 60:02d}:{i % 60:02d}.00]{line}，第{i // 4 + 1}句仍在继续" for i in range(0, 96, 4))
    long_text = {"lyrics": lyrics, "platform_key": "applemusic", "platform_display_name": "Apple Music", "song_title": title, "artists": "林朝与城市回声合唱团 / The Long Night Ensemble", "album": "在潮汐之间写下的漫长备忘录", "duration_seconds": 382, "release_text": "2025-10-19", "show_release_time": True, "cover_path": cover, "source_label": "歌词来源：Apple Music · 很长的来源说明字段用于检查底栏省略号与边界安全"}
    return [("lyrics_complete", complete), ("lyrics_missing_fields", missing), ("lyrics_long_text", long_text)]

async def inspect(renderer, html, name):
    if not await renderer.start() or renderer._browser_context is None:
        raise RuntimeError("Playwright browser unavailable")
    page = await renderer._browser_context.new_page()
    path = OUT / f".{name}.inspect.html"
    try:
        path.write_text(renderer._inject_base_url(html, str(ROOT / "core" / "templates")), encoding="utf-8")
        await page.goto(path.resolve().as_uri(), wait_until="load")
        await page.wait_for_function("Array.from(document.images).every((image) => image.complete)")
        result = await page.evaluate(r"""() => {
          const root = document.querySelector("[data-card-root]"); if (!root) return {error: "root missing"};
          const rect = s => {const e=root.querySelector(s);if(!e)return null;const r=e.getBoundingClientRect();return {top:Math.round(r.top),bottom:Math.round(r.bottom),height:Math.round(r.height)}};
          const lines=[...root.querySelectorAll(".lyric-line")],last=lines.length?lines[lines.length-1].getBoundingClientRect():null,foot=root.querySelector(".card-footer")?.getBoundingClientRect();
          const images=[...root.querySelectorAll("img")].map(i=>({className:i.className,loaded:i.complete&&i.naturalWidth>0,width:i.naturalWidth}));
          const overflow=[...root.querySelectorAll("*")].filter(e=>{const s=getComputedStyle(e);return e.clientWidth&&!(s.display.includes("box")&&s.webkitLineClamp!=="none")&&!(s.overflow==="hidden"&&s.textOverflow==="ellipsis")&&(e.scrollWidth>e.clientWidth+1||e.scrollHeight>e.clientHeight+1)}).map(e=>e.className||e.tagName);
          return {width:root.clientWidth,scrollWidth:root.scrollWidth,height:root.clientHeight,title:rect(".song-title"),cover:rect(".cover"),lyrics:rect(".lyrics-section"),lastLineBottom:last?Math.round(last.bottom):null,footer:foot?{top:Math.round(foot.top),bottom:Math.round(foot.bottom)}:null,lineCount:lines.length,images,imageFailures:images.filter(i=>!i.loaded),overflow};
        }""")
        collision = result.get("footer") and result.get("lastLineBottom", 0) > result["footer"]["top"]
        if result.get("error") or result["imageFailures"] or result["overflow"] or result["scrollWidth"] > result["width"] or collision:
            raise AssertionError(f"Lyrics layout/resource QA failed for {name}: {result}")
        return result
    finally:
        await page.close()
        path.unlink(missing_ok=True)

async def render_all():
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = SimpleNamespace(card_template="Guizang_card", card_custom_template="", lyrics_card_template="Guizangsocial_lyrics_card", card_dynamic_color=False, card_custom_font=[], cache_dir=TASK / "cache", data_dir=TASK / "data", template_dir=TASK / "templates", plugin_dir=ROOT, logo_path=ROOT / "logo.png", emoji_style="APPLE")
    renderer = Renderer(cfg)
    renderer._emoji_source = None
    real_screenshot = renderer._render_playwright_png
    html_by_name = {}
    async def capture(html, target, *, base_url=None):
        html_by_name[target.name] = html
        return await real_screenshot(html, target, base_url=base_url)
    renderer._render_playwright_png = capture
    report = []
    try:
        for name, context in cases():
            target = OUT / f"{name}.png"
            result = await renderer.render_lyrics_card(**context, target_path=target)
            if result is None: raise RuntimeError(f"Lyrics rendering failed: {name}")
            metrics = await inspect(renderer, html_by_name[target.name], name)
            report.append({"case": name, "path": str(target), "bytes": target.stat().st_size, **metrics})
    finally:
        await renderer.close()
    return report

def template_parity():
    from jinja2 import Environment, nodes
    env = Environment()
    def signature(path):
        tree = env.parse(path.read_text(encoding="utf-8"))
        return {"ifs": sorted(str(n.test) for n in tree.find_all(nodes.If)), "loops": sorted((str(n.target), str(n.iter)) for n in tree.find_all(nodes.For)), "filters": sorted((n.name, str(n.node)) for n in tree.find_all(nodes.Filter)), "attributes": sorted({n.attr for n in tree.find_all(nodes.Getattr)}), "names": sorted({n.name for n in tree.find_all(nodes.Name) if n.ctx == "load"})}
    original = signature(ROOT / "core/templates/lyrics_card.html")
    redesigned = signature(ROOT / "core/templates/Guizangsocial_lyrics_card.html")
    mismatches = {k: {"lyrics_card": original[k], "Guizangsocial_lyrics_card": redesigned[k]} for k in original if original[k] != redesigned[k]}
    if mismatches: raise AssertionError(f"Lyrics template Jinja mismatch: {mismatches}")
    return {"counts": {k: len(v) for k, v in original.items()}, "mismatches": mismatches}

def main():
    parity = template_parity()
    report = asyncio.run(render_all())
    payload = {"renderer": "Renderer.render_lyrics_card + Playwright Chromium Headless Shell", "template": "core/templates/Guizangsocial_lyrics_card.html", "template_parity": parity, "cases": report}
    report_path = TASK / "lyrics-render-report.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
