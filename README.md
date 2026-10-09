# astrbot_plugin_parser_test

AstrBot 链接解析插件。当前支持 Bilibili、抖音、小红书和 Pixiv，解析结果会统一映射为 `ParseResult`，再按配置发送媒体和可选信息卡片。

测试版的插件安装标识为 `astrbot_plugin_parser_test`，可与原版
`astrbot_plugin_parser` 同时安装。两者的配置、Cookie、缓存和自定义卡片模板目录相互隔离。


## 支持范围

| 平台 | 入口 | 主要内容 |
| --- | --- | --- |
| Bilibili | BV/av、短链、视频、动态、专栏、直播、收藏夹 | 视频、图文、音频 |
| 抖音 | 分享短链、视频/图文链接、作品 ID | 视频、图文、实况图 |
| 小红书 | 分享短链、`explore` / `discovery` 链接 | 视频、图文 |
| Pixiv | 作品链接、`pid`、小说链接 | 插画、漫画、动图、小说 |

抖音分享页未返回完整实况字段时，插件会使用抖音解析器中配置的登录
Cookie 补全媒体信息；建议填写有效 Cookie，以保证实况图识别和下载成功率。

抖音解析器还提供独立的 `启用抖音专用代理` 和 `抖音专用代理地址`。
地址应填写已部署 `cloudflare_worker_v2.js` 的 Worker 根 URL。仅当开关开启且
地址非空、格式有效时，`www.douyin.com` 详情 API 会通过 `/douyin/*` 请求，抖音
分享页、幻灯片 API 与媒体下载会通过 `/download` 请求；其他平台不受影响。登录
Cookie 可能随请求转发，因此只应填写自己部署或确认可信的 Worker 地址。



## 卡片配置

在 AstrBot 插件配置页可使用以下选项：

- `渲染并发送信息卡片`：开启时生成并发送卡片；关闭后不生成卡片，原媒体发送逻辑不受影响。
- `卡片模板`：默认使用 `Apple 风格卡片`（`apple`）；也可选择 `标准卡片`（`default`）、`紧凑卡片`（`compact`）或 `自定义模板`。
- `自定义卡片模板文件名`：选择自定义模板后填写文件名（不含 `.html`）。
- `表情样式`：默认 `Apple`。iOS Unicode 组合表情、肤色和 ZWJ 表情会以完整序列传递给模板，并优先使用 Apple Emoji 字体回退链。

卡片模板采用 Jinja2，布局由 Playwright 驱动的 Chrome Headless Shell 渲染。可将自定义 `*.html` 放到 AstrBot 插件数据目录的 `astrbot_plugin_parser_test/templates/` 中，在插件页选择“自定义模板”并填写文件名。用户模板会覆盖同名内置模板。

完整的数据上下文、模板扩展方式和回归项目见 [卡片渲染与回归说明](docs/CARD_RENDERING.md)。

## 互动统计

`ParseResult` 新增以下跨平台字段：

- `like_count`：点赞数
- `comment_count`：评论数
- `favorite_count`：收藏数
- `share_count`：转发/分享数

模板还可使用 `result.engagement` 或简写的 `result.likes`、`result.comments`、`result.favorites`、`result.shares`。没有公开数据的平台字段保持为 `None`，不会被错误显示为 0。

## 安装与依赖

通过 AstrBot 插件市场安装即可。独立开发环境需安装 `requirements.txt`，其中卡片渲染新增：

> 使用 AstrBot 的 Git 安装方式时，仓库 URL 的最后一段也必须是
> `astrbot_plugin_parser_test`（例如将仓库重命名为该名称后再安装）。部分 AstrBot
> 版本会在读取 `metadata.yaml` 之前按 URL 名称创建插件目录；如果继续使用
> `.../astrbot_plugin_parser.git`，即使元数据已改名，仍可能因原版目录已存在而拒绝安装。

```bash
python -m pip install -r requirements.txt
python -m pip install astrbot
```

建议先安装插件依赖，再安装 AstrBot 主程序；插件运行时通过 AstrBot 提供的消息组件和配置 API 接入。

`playwright` 负责驱动 Chrome Headless Shell 将渲染后的 HTML 裁剪为卡片 PNG。安装 Python 依赖后还需安装浏览器二进制：

```bash
python -m playwright install chromium-headless-shell
```

渲染器在插件初始化时启动并复用一个 Headless Shell 进程，避免每张卡片重复启动浏览器。若浏览器未安装、初始化或截图失败，插件会记录错误并跳过卡片发送，但原媒体解析与发送流程不会中断，也不会切换到其他卡片渲染器。

卡片 PNG、下载的图片/视频和 Emoji 资源均保留在原有插件缓存目录，继续由 `clean_cron` 对应的 `CacheCleaner` 统一执行“删除整个缓存目录后重建”的清理策略；截图期间生成的临时 HTML 会在该次渲染结束后立即删除。

## 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `开启解析` | ADMIN | 开启当前会话解析 |
| `关闭解析` | ADMIN | 关闭当前会话解析 |
| `blogin` | ADMIN | Bilibili 扫码登录 |
| `qqlogin` | ADMIN | QQ 音乐扫码登录（发送 Apple 风格登录卡片） |

## 流程

1. 从当前消息的文本或 JSON 卡片提取链接。
2. 完成会话过滤、仲裁和防抖。
3. 由四个保留解析器之一生成 `ParseResult`。
4. `MessageSender` 根据媒体类型、合并阈值和全局卡片开关构建发送计划。
5. 需要卡片时，`Renderer` 用 Jinja2 生成 HTML，交由 Playwright 的 Chrome Headless Shell 裁剪卡片根节点为 PNG；失败则记录日志并跳过卡片，随后仍按原有策略发送媒体。

## 致谢

本项目核心代码来自 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)与[astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)。
感谢[karin-plugin-kkk](https://github.com/ikenxuan/karin-plugin-kkk)项目提供的抖音实况图（组）的实现方式！
