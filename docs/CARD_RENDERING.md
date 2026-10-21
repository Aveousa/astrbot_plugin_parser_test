# 卡片渲染、统计字段与回归说明

本文记录本版本的卡片层重构边界，供维护者新增模板或平台解析器时参考。

## 1. 架构边界

原有主链路保持不变：

```text
消息 → Parser → ParseResult → MessageSender → AstrBot 消息组件
                                      │
                                      └─ Renderer（可选）→ PNG 卡片
```

- 解析器只负责填充实体；不直接拼接 HTML，也不决定卡片发送时机。
- `Renderer.render_card(result)` 是卡片层唯一入口，失败返回 `None`，不会阻断媒体下载/发送。
- `MessageSender` 仍然负责分组、合并转发、媒体类型转换和文本兜底。
- `MessageSender` 在每个 `ParseResult` 的媒体发送前统一处理一次信息卡片；全局卡片开关开启时，任何平台和媒体类型都会尝试渲染并发送该卡片。
- `SendGroup` 仅描述媒体分组和是否折叠转发；旧的 `SendGroup.render_card` 字段只为兼容旧解析器保留，不再决定卡片是否发送。
- 发送器对渲染器和卡片预览发送再做一层异常隔离；卡片失败时不会发送该卡片，
  自动合并策略会按实际媒体段数重算，避免失败的卡片改变媒体发送方式。

## 2. 配置与发送策略

| 配置键 | 默认值 | 作用 |
| --- | --- | --- |
| `card_custom_font` | `[]` | 上传 OTF、TTF 或 WOFF2 字体；留空时使用内置 `core/resources/douyin_sans.otf` |
| `card_enabled` | `true` | 是否渲染并发送信息卡片 |
| `card_template` | `apple` | 选择模板名 |
| `card_custom_template` | `""` | 选择 `custom` 后使用的模板文件名 |
| `lyrics_card_template` | `lyrics_card` | 独立选择歌词卡片模板；可选标准模板或 `Guizangsocial_lyrics_card`（配置显示名 `Guizang Social`） |
| `emoji_style` | `APPLE` | 表情字体风格标识 |
| `single_heavy_render_card` | `false` | 已废弃，仅兼容旧配置；不再影响卡片策略 |
| `compact_forward_non_audio` | `false` | 开启后同一解析结果中的信息卡片、歌词卡片及图片/视频/文本等非音频内容统一整理；有多个非音频内容时折叠为一条转发消息，音频始终单独发送 |

歌词卡片会优先采用原始 LRC 的空行和 `Verse`/`Chorus` 等段落标记；相邻时间戳相差至少 8 秒时通常分段，达到 12 秒时即使上一行以逗号等连接符结尾也分段。歌词制作人员标记会单独起段，逗号连接的连续句则避免误分段。

卡片策略按整个 `ParseResult` 决定，而不是按单个媒体分组决定：

1. 默认模式下，`card_enabled` 开启时先独立渲染并发送一张信息卡片；
2. 卡片渲染或发送失败只记录日志，后续媒体发送和折叠策略继续执行；
3. 默认模式下卡片不计入 `forward_threshold`，也不会被放进媒体的合并转发节点。

开启 `compact_forward_non_audio` 后，发送器会先收集信息卡片、歌词卡片以及解析结果中的图片、视频、动态、文本和普通文件，再按内容类型统一整理：非音频内容有多个项目时合并为一条转发消息，只有一个时普通发送；`AudioContent`（无论最终发送为语音还是音频文件）始终单独发送。因此一次解析最终最多产生一条非音频转发消息和一条音频消息。

抖音图集会显式保留媒体分组：默认模式下单张（含实况）作品发送“信息卡片 + 原媒体”，多张（含实况）作品先发送信息卡片，再把全部解析媒体作为一份合并转发发送；开启紧凑模式后，信息卡片也会与原媒体一起纳入同一条非音频转发消息。

启动时会迁移解析器配置：移除已下线平台的条目，为四个保留平台补齐默认项；旧版 Bilibili 的 `video_codecs` 单值会自动转换为 `video_codec_list`，保留用户原有编码偏好。

## 3. 模板扩展

模板查找优先级如下：

1. `<AstrBot 插件数据目录>/astrbot_plugin_parser_test/templates/`
2. `<插件安装目录>/templates/`
3. `core/templates/`（内置信息模板 `default.html`、`compact.html`、`apple.html`、`Guizang_card.html`，以及标准与 Guizang Social 歌词模板 `lyrics_card.html`、`Guizangsocial_lyrics_card.html`）

将 `my_card.html` 放入第一目录后，在插件页将 `card_template` 选择为 `custom`，再把 `card_custom_template` 填为 `my_card` 即可。文件名会做路径净化，不能通过配置读取目录外文件。
扫码登录卡片由内部模板 `qr_login_card.html` 渲染，QQ 音乐、酷狗音乐和 B 站共用；它不是 `card_template` 的可选项。将同名文件放入上述模板目录即可覆盖登录卡片外观。
内置信息卡片 `Guizang_card` 在配置页显示为 `Guizang Social`，模板文件为 `Guizang_card.html`。它是独立的 Swiss / 专业平面排版方案：直角封面、细分隔线、IKB 单一强调色和等宽统计数字；复用 `apple.html` 的全部上下文、媒体封面与条件分支，不替换或修改 `apple.html`。动态取色开启时仍复用取色后的平面背景与文字/分隔色，但不启用 Apple 模板的渐变和光晕，以保持 Swiss 配色规则。

歌词模板通过独立配置 `lyrics_card_template` 选择，避免影响信息卡片模板。默认 `lyrics_card` 保持现有歌词卡片；选择 `Guizang Social` 时运行时加载 `core/templates/Guizangsocial_lyrics_card.html`。两份 Guizang Social 模板与其余内置模板统一存放在 `core/templates/`。歌词版沿用信息卡片的字体、色彩、封面和页脚体系，保留标题、歌手、专辑、时长、发布时间开关、封面、平台 Logo、来源、逐行时间码以及制作人员与段落分隔标记。信息卡片和歌词卡片的配置项各自显示为 `Guizang Social`，模板文件独立。

信息卡片的真实模板上下文预览与兼容性报告位于 `local-tests/new-card-qa/`，使用 `python local-tests/new-card-qa/render_previews.py` 重现；歌词卡片完整字段、缺失字段与极长文本预览在同目录的 `previews/lyrics_*.png`，用 `python local-tests/new-card-qa/render_lyrics_previews.py` 重现，脚本同时检查模板条件/循环/字段与标准歌词模板一致，以及 Chromium 布局边界和图片解码。

Jinja2 自动转义已启用。内置可用过滤器：

| 过滤器 | 用途 |
| --- | --- |
| `format_count` | 将 `12000` 格式化为 `1.2万` |
| `emoji` | 保留 Unicode 表情序列，并加上选择的表情样式类 |
| `file_uri` | 将本地资源路径转换为 `file:///` URI |

模板上下文提供以下对象：

```jinja2
{{ result.title }}
{{ card.platform.display_name }}
{{ card.platform.logo_uri or '' }}
{{ card.author.name if card.author else '' }}
{{ card.text | emoji }}
{{ card.stats.likes | format_count }}
{{ card.live_photo_uri or '' }}
{{ card.live_photo_hint or '' }}
{% for item in card.stat_items %}
  {{ item.label }} {{ item.value | format_count }}
{% endfor %}
{% for media in contents if media.uri %}
  <img src="{{ media.uri }}" alt="{{ media.alt or '' }}">
{% endfor %}
```

`card.repost` 最多递归一层，避免异常数据形成无限递归。`contents` 中每项都有 `kind`、`uri`、`text`、`alt`、`duration` 与 `name` 字段；视频展示其封面而不是下载完整视频。

## 4. Apple 表情支持

渲染器识别 Unicode Emoji、变体选择器、肤色修饰符和 ZWJ 组合序列，整体包装为 `.emoji--apple`（或配置的样式）元素。内置 CSS 的字体回退链为：

```css
"Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif
```

因此 iOS 分享文案中的复合表情不会在 HTML 转义或断词过程中被拆开。渲染器会优先复用 `apilmoji` 缓存的本地 Apple PNG；CDN 不可达或资源尚未下载时仍保留原始 Unicode，并回退到可用的系统/Noto Emoji 字体。

渲染器使用 Playwright 驱动 Chrome Headless Shell 对 Jinja2 生成的 HTML 截取卡片根节点 PNG，不再通过 PDF、PyMuPDF、Pillow 或其他卡片绘图库转换。内置模板通过 `data-card-root` 标记裁掉卡片外留白；未提供标记的自定义模板回退截取 `body`。浏览器进程在插件生命周期内复用，避免为每张卡片重复启动。模板不存在、浏览器不可用或截图失败时，渲染器会记录异常并返回 `None`；发送器因此跳过卡片，但继续原媒体发送流程。

图片、图文、视频与动态媒体没有可用预览图时，卡片统一使用 `core/resources/error_preview.png` 作为预览占位图。

三个内置模板统一通过 `@font-face` 加载 `core/resources/douyin_sans.otf`，字体族名为 `Douyin Sans`；统计栏使用 `like.png`、`comment.png`、`favorites.png`、`share.png` 四个资源图标。平台 Logo 通过 `card.platform.logo_uri` 提供；检测到抖音实况图时，`card.live_photo_uri` 和 `card.live_photo_hint` 会提供右上角实况标识与底部提示。自定义模板也可以直接使用这些 URI 和 `card_font_uri`。

### 运行前提

安装 Python 依赖后，必须额外安装与 `playwright` 版本匹配的浏览器二进制：

```bash
python -m playwright install chromium-headless-shell
```

`chromium.launch(headless=True)` 会使用 Playwright 安装的 `chrome-headless-shell`。Linux 容器以 root 运行时，渲染器仅为该场景加入 `--no-sandbox`；其他环境保留浏览器默认沙箱。浏览器二进制缺失时，控制台会输出上述安装命令并仅跳过卡片，不会启用其他渲染器，也不会中断 Bilibili、抖音、小红书或 Pixiv 的原媒体发送。

### 缓存生命周期

最终卡片 PNG 与下载的图片、视频、Emoji 资源都写入原有的 `cfg.cache_dir`。截图用的隐藏临时 HTML 在页面关闭后立即删除；若进程异常导致残留，仍会被既有 `CacheCleaner._clean_plugin_cache()` 的“`shutil.rmtree(cache_dir)` 后 `mkdir`”策略一并清理。用户自定义模板位于独立的 `template_dir`，不在定时缓存清理范围内。

## 5. 互动统计映射

统一实体字段在 `core/data.py`：

| 统一字段 | Bilibili | 抖音 | 小红书 | Pixiv |
| --- | --- | --- | --- | --- |
| `like_count` | `stat.like` | `statistics.digg_count` | `interactInfo.likedCount` | `likeCount` |
| `comment_count` | `stat.reply` | `statistics.comment_count` | `interactInfo.commentCount` | `commentCount` |
| `favorite_count` | `stat.favorite` | `statistics.collect_count` | `interactInfo.collectedCount` | `bookmarkCount` |
| `share_count` | `stat.share` | `statistics.share_count` | `interactInfo.shareCount` | 平台未提供时为空 |

`EngagementStats.from_mapping()` 兼容整数、字符串、`1.2万`、`1亿` 和 `{count: ...}` 型 API 节点。缺失数据使用 `None`，模板仅显示存在的统计项。

## 6. 回归清单

提交前至少执行：

```bash
python -m pip install -r requirements.txt
python -m pip install astrbot
python -m playwright install chromium-headless-shell
python -c "import astrbot; print(astrbot.__version__)"
python -m compileall -q core main.py
python -m pytest -q
```

本次 Guizang Social 信息/歌词模板及配置改动后的本地全量回归：`541 passed, 1 warning`。另执行 `python -m compileall -q core main.py local-tests/new-card-qa`、配置 JSON 解析与 `git diff --check`；原始 `apple.html` 无差异。信息卡片九个 Chromium 预览场景与歌词卡片三个预览场景均无布局溢出或图片加载失败。当前唯一警告来自第三方 `google.genai` 依赖对 Python 3.17 的弃用提示。

覆盖项：

- JSON 卡片 URL 提取只优先识别四个保留平台；
- Cookie 解析继续通过既有回归用例；
- 统一统计字段的数值归一化、模板上下文和 Apple Emoji 包装；
- 关闭 `card_enabled` 时，不生成也不发送卡片，但媒体发送计划保持可用；
- `default`、`compact`、`apple` 和用户覆盖模板均可被加载，并可读取同目录的相对静态资源；
- Playwright 通过 `chrome-headless-shell` 输出 PNG，复用浏览器进程，并在截图后删除临时 HTML；
- 既有缓存清理任务会统一清理卡片、下载媒体和 Emoji 缓存；
- Bilibili、抖音、小红书、Pixiv 的统计字段在 API 字段缺失时不影响原有解析结果。

QQ 空间及其他已移除平台的解析器、配置模板、测试与 URL 优先级已一并移除，不再纳入回归范围。
