# Apple Music 管理页（实验性）

默认仍只展示歌曲/歌单信息卡片，不需要 Cookie。可选管理页用于更新 wrapper
的登录状态，并验证 gamdl 是否能实际生成单曲音频。没有往插件必装依赖中加入 gamdl。

## 使用入口

在支持 **Plugin Pages** 的 AstrBot 后台，打开插件详情，再打开
**Apple Music 账号管理**。开发时核对过 AstrBot 4.27.3 的页面和 Web API。
旧版不支持页面时仍可解析信息卡片；不应把 HTML 直接作为独立网站打开。

首次配置一次后，日常操作是：打开本页 → 刷新状态 → 必要时登录 → 输入验证码。
账号、密码、验证码不通过 Bot 聊天发送，也不保存到插件配置。

## 一次性后端部署

### 1. wrapper-v2

本版适配 [glomatico/wrapper-v2](https://github.com/glomatico/wrapper-v2)
**API 0.0.2**，不是旧版 wrapper 的 HTTP/TCP 协议。
必须自行部署独立服务；插件不会下载安装 Apple 二进制库或自动启动容器。

该项目是研究性工具，需 Linux/Docker 运行环境，以及你自行合法取得的兼容
Apple Music Android 原生库。上游当前文档指定 3.6.0-beta build 1109，并校验库哈希；
请按其 README 完成库提取、系统库暂存和镜像构建，勿从不可信来源下载库或镜像。
Windows 主机可通过配置好的 Docker Linux 环境运行；插件管理页本身不要求 Linux。

- HTTP 服务：容器内默认端口 **80**，用于 `/me`、`/login`、`/login/2fa`。
- TCP 解密服务：容器内默认端口 **10020**，与 HTTP 管理端口不同。
- HTTP/TCP 均只向本机或可信内网开放。上游 Compose 的端口映射需自行限制绑定地址；
  不要把没有鉴权的 wrapper 公开到互联网。
- wrapper 的 `WRAPPER_BASE_DIR` 应映射到持久卷；上游默认尝试在启动时恢复 Apple 会话。
  恢复是否成功取决于 Apple 会话是否有效，并非永久登录或保证自动续期。

若 AstrBot 在另一个容器，`127.0.0.1` 指向它自身，不是 wrapper。
同 Docker 网络可配置服务名及容器内端口；跨机器应使用受保护的可信网络或 HTTPS。
请确保 AstrBot 和 gamdl 的执行环境都能访问配置的地址。

### 2. 独立 gamdl 环境

不要在 AstrBot 的 Python 环境直接安装 gamdl。请在**AstrBot 进程所在的容器**中创建独立虚拟环境并安装
**gamdl==3.9.1**。插件会从 AstrBot 容器内启动这个 Python 子进程；`gamdl_python` 必须是
该容器内部可访问的绝对路径，不能填写宿主机路径，也不能填写另一个 gamdl 容器内部的路径。
例如：

```powershell
py -3.12 -m venv D:\tools\apple-music-gamdl
D:\tools\apple-music-gamdl\Scripts\python.exe -m pip install gamdl==3.9.1
```

Linux 示例：

```bash
python3 -m venv /opt/apple-music-gamdl
/opt/apple-music-gamdl/bin/python -m pip install gamdl==3.9.1
```

Docker 部署时，应在 AstrBot 容器内执行等价操作（下面的
`astrbot` 替换成你的容器名；临时 `docker exec` 安装会在重建容器后丢失，建议写入
AstrBot 自定义 Dockerfile）：

```bash
docker exec -it astrbot sh
python3 -m venv /opt/gamdl-venv
/opt/gamdl-venv/bin/python -m pip install --no-cache-dir gamdl==3.9.1
exit
docker exec astrbot /opt/gamdl-venv/bin/python -c \
  "from gamdl.cli.cli import main; from importlib.metadata import version; print(version('gamdl'))"
docker exec astrbot sh -lc 'command -v ffmpeg && command -v ffprobe'
```

如果镜像没有 `python3-venv` 或 FFmpeg，需要在自定义镜像构建阶段安装它们，而不是只在
宿主机安装。验证命令应输出 `3.9.1`，且两个媒体工具都应在 AstrBot 容器中找到。

FFmpeg 和 FFprobe 必须在 **AstrBot 容器内、AstrBot 进程的 PATH** 中可见。
gamdl 是子进程，不会被 import 到 AstrBot；其标准输出/错误不会转发给后台或日志，
避免第三方错误泄露令牌。子进程无交互 stdin、有超时，卸载时停止运行任务。
如果 AstrBot 和 gamdl 是两个独立容器，当前适配不能直接通过 `gamdl_python` 调用后者；
最简单的做法是把 gamdl 虚拟环境和 FFmpeg 安装到 AstrBot 的自定义镜像中（仍然使用独立
虚拟环境）。不要为了调用 gamdl 把 Docker socket 暴露给 AstrBot。wrapper 可以继续作为
独立服务运行，但要使用 AstrBot 容器能访问的 Docker 网络服务名，而不是各自容器里的
`127.0.0.1`。
本版固定 AAC Web 单曲，不启用 ALAC/Atmos、整张专辑或歌单下载。

### 3. 插件配置

在 Apple Music 解析器配置中设置：

| 字段 | 示例 / 默认值 |
| --- | --- |
| 启用可选 Apple Music 账号后端 | 首次配置后开启，默认关闭 |
| wrapper HTTP 地址 | `http://127.0.0.1:80` |
| wrapper 解密 TCP 主机 | `127.0.0.1` |
| wrapper 解密 TCP 端口 | `10020` |
| 独立 gamdl 环境的 Python 路径 | Windows 为 `…\Scripts\python.exe`；Linux 为 `…/bin/python` |
| 返回单曲音频 | 默认关闭，先在管理页验证后再开启 |

保存并**重载插件**。gamdl 未配置时仍能使用 wrapper 登录管理，但无法验证下载。
配置只填写非秘密的地址/路径，不能在 wrapper URL 中夹带账号密码。

## 登录、核验与会话

1. 刷新状态，确认 wrapper 版本兼容。
2. 在管理页输入 Apple 账号和密码。请求直接从 AstrBot 服务端发往你配置的 wrapper。
3. wrapper 返回需要双重认证时，输入受信任设备收到的六位验证码。
   插件中的验证码会话绑定当前后台用户，10 分钟内有效；失败可重试。
4. 刷新后分别核验「已登录」「订阅有效」「gamdl 就绪」。
   订阅接口暂不可达显示「未核验」，不会误报没有订阅。
5. 用单曲链接执行音频验证。文件大小、AAC 格式及完整时长检查通过才报告成功。
   `gamdl` 退出码为 0 或获得试听不算成功。

验证音频仅写入插件缓存，由现有缓存清理器统一清理，不在管理页暴露带账号的播放 URL。
开启返回音频后，Bot 保留已有卡片并附加单曲音频；下载失败沿用现有下载失败提示。
仍需遵守歌曲地区、订阅和账号授权限制；不保证每首歌可下载。

**清除内存会话不是完整注销。**它只调用 wrapper 的 `DELETE /login`，可取消当前登录
或清除内存令牌；Apple 的磁盘 `mpl_db` 不变，重启 wrapper 可能恢复登录。
彻底删除登录缓存应由你在独立服务中按上游说明处理，插件不操作该持久卷。

## 安全与验证边界

页面使用 AstrBot 的后台鉴权及原生页面桥接；业务 API 不接受页面资源 token 作为登录凭据。
敏感操作还要求当前后台用户的会话 nonce 和 JSON 请求。密码/验证码提交后清空输入框，
不写浏览器本地存储。远程使用请为 AstrBot 配置 HTTPS，并只允许可信管理员进入后台。
浏览器自身或密码管理器的存储行为不受插件控制。

已覆盖模拟 wrapper 的真实本机 HTTP 登录 → 双重认证 → 状态流程、权限拒绝、
会话隔离、任务取消、默认卡片兼容以及试听/空文件拒绝测试。
开发环境尚未部署真实 wrapper，也未输入 Apple 账号，因此这些自动化结果
**不等于真实账号已登录或已成功下载 Apple 全曲**。需按上述步骤部署后在管理页验证。
