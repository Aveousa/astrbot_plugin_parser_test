"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginPage;
  let nonce = "";
  let snapshot = null;
  let busy = false;
  let timer = null;
  let refreshing = false;
  let needs2fa = false;
  let lastOperation = "";
  let savedSettings = "";
  const codecOptions = [
    ["alac", "ALAC · 无损"], ["aac-web", "AAC Web"],
    ["aac-he-web", "HE-AAC Web"], ["aac", "AAC"],
    ["aac-he", "HE-AAC"], ["aac-binaural", "AAC 双耳"],
    ["aac-downmix", "AAC 下混"], ["aac-he-binaural", "HE-AAC 双耳"],
    ["aac-he-downmix", "HE-AAC 下混"], ["atmos", "Dolby Atmos"],
    ["ac3", "Dolby Digital"],
  ];

  function notice(message, error = false) {
    $("notice").querySelector(".notice-text").textContent = message;
    $("notice").dataset.error = String(error);
  }
  function field(id, value, tone = "") {
    $(id).textContent = value;
    $(id).dataset.tone = tone;
  }
  function controls() {
    const connected = Boolean(snapshot?.enabled && snapshot?.connected && nonce);
    $("refresh").disabled = refreshing;
    $("login-fields").disabled = !connected || busy || needs2fa;
    $("two-factor-form").hidden = !needs2fa;
    $("two-factor-fields").disabled = !connected || busy || !needs2fa;
    $("clear-session").disabled = !connected || busy;
    $("clear-cache").disabled = !nonce || busy;
    $("settings-fields").disabled = !nonce || busy;
    updateSettingsState();
    $("verify-fields").disabled = !connected || busy || needs2fa || snapshot?.auth_state !== "authenticated"
      || snapshot?.subscription_active !== true || !snapshot?.gamdl?.available
      || !snapshot?.gamdl?.ffmpeg || !snapshot?.gamdl?.ffprobe;
  }
  function render(data) {
    snapshot = data;
    nonce = data.nonce || "";
    needs2fa = Boolean(data.can_submit_2fa);
    field("wrapper-state", !data.enabled ? "未启用" : data.connected ? `已连接 · ${data.version}` : "无法连接", data.connected ? "positive" : "");
    const labels = { authenticated: "已登录", logged_out: "未登录", awaiting_2fa: "等待验证码", in_progress: "登录中", failed: "登录失败" };
    field("auth-state", labels[data.auth_state] || "未确认", data.auth_state === "authenticated" ? "positive" : "");
    field("subscription-state", data.subscription_active === true ? "订阅有效" : data.subscription_active === false ? "无有效订阅" : "未核验",
      data.subscription_active === true ? "positive" : data.subscription_active === false ? "negative" : "");
    field("storefront", data.storefront ? data.storefront.toUpperCase() : "—");
    // Keep the summary concise while the detailed panel remains explicit.
    field("subscription-state-detail", data.subscription_active === true ? "订阅有效" : data.subscription_active === false ? "无有效订阅" : "未核验",
      data.subscription_active === true ? "positive" : data.subscription_active === false ? "negative" : "");
    field("gamdl-state", data.gamdl.available ? `就绪 · ${data.gamdl.version}` : data.gamdl.version ? `版本不兼容 · ${data.gamdl.version}` : "未配置或不可用");
    field("ffmpeg-state", data.gamdl.ffmpeg && data.gamdl.ffprobe ? "就绪" : "未就绪");
    field("playback-state", data.playback_ready ? "就绪" : "未就绪");
    $("subscription-detail").textContent = data.subscription_message;
    renderCache(data.cache);
    renderSettings(data.settings);
    controls();
  }
  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let size = bytes;
    let index = -1;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return `${size.toFixed(size >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  }
  function renderCache(cache) {
    const bytes = Number(cache?.bytes) || 0;
    const files = Number(cache?.files) || 0;
    $("cache-size").textContent = formatBytes(bytes);
    $("cache-files").textContent = `${files} 个文件`;
    $("cache-meter-fill").style.width = `${Math.min(100, Math.max(2, (bytes / (1024 * 1024 * 512)) * 100))}%`;
  }
  function readSettings() {
    return {
      backend_enabled: $("setting-backend").checked,
      return_audio: $("setting-return-audio").checked,
      audio_send_format: document.querySelector("input[name='audio-format']:checked")?.value || "file",
      file_codec_priority: [...$("codec-priority-list").querySelectorAll("select")].map((select) => select.value),
      show_playlist_cover: $("setting-cover").checked,
      show_playlist_url: $("setting-url").checked,
    };
  }
  function renderSettings(settings) {
    if (!settings) return;
    $("setting-backend").checked = Boolean(settings.backend_enabled);
    $("setting-return-audio").checked = Boolean(settings.return_audio);
    $("setting-cover").checked = settings.show_playlist_cover !== false;
    $("setting-url").checked = settings.show_playlist_url !== false;
    const format = settings.audio_send_format === "voice" ? "voice" : "file";
    document.querySelector(`input[name='audio-format'][value='${format}']`).checked = true;
    renderCodecs(Array.isArray(settings.file_codec_priority) ? settings.file_codec_priority : ["alac", "aac-web"]);
    savedSettings = JSON.stringify(readSettings());
    updateSettingsState();
  }
  function codecRow(value) {
    const row = document.createElement("div");
    row.className = "codec-row";
    const handle = document.createElement("span");
    handle.className = "codec-order";
    handle.setAttribute("aria-hidden", "true");
    handle.textContent = "≡";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "文件音质编码");
    codecOptions.forEach(([optionValue, label]) => {
      const option = document.createElement("option");
      option.value = optionValue;
      option.textContent = label;
      option.selected = optionValue === value;
      select.append(option);
    });
    const controls = document.createElement("span");
    controls.className = "codec-controls";
    [["↑", "上移", -1], ["↓", "下移", 1], ["×", "移除", 0]].forEach(([text, label, direction]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      button.setAttribute("aria-label", label);
      button.addEventListener("click", () => {
        if (!direction) {
          if ($("codec-priority-list").children.length > 1) row.remove();
        } else {
          const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
          if (sibling) $("codec-priority-list").insertBefore(direction < 0 ? row : sibling, direction < 0 ? sibling : row);
        }
        updateSettingsState();
      });
      controls.append(button);
    });
    row.append(handle, select, controls);
    return row;
  }
  function renderCodecs(values) {
    $("codec-priority-list").replaceChildren(...values.map(codecRow));
  }
  function updateSettingsState() {
    if (!$("settings-state") || !$("save-settings")) return;
    const settings = readSettings();
    const duplicates = settings.file_codec_priority.length !== new Set(settings.file_codec_priority).size;
    const changed = Boolean(savedSettings) && JSON.stringify(settings) !== savedSettings;
    $("settings-state").textContent = duplicates ? "同一音质不能重复添加。" : changed ? "有尚未保存的更改。" : "设置已与磁盘配置同步。";
    $("settings-state").dataset.error = String(duplicates);
    $("save-settings").disabled = !nonce || busy || !changed || duplicates;
    $("add-codec").disabled = settings.file_codec_priority.length >= codecOptions.length;
    const rows = [...$("codec-priority-list").querySelectorAll(".codec-row")];
    rows.forEach((row, index) => {
      row.querySelector("button[aria-label='上移']").disabled = index === 0;
      row.querySelector("button[aria-label='下移']").disabled = index === rows.length - 1;
      row.querySelector("button[aria-label='移除']").disabled = rows.length === 1;
    });
    $("format-help").textContent = settings.audio_send_format === "voice"
      ? "语音模式固定使用 AAC Web；需配置可访问的文件回调地址，否则降级为文件。"
      : "文件模式按下方优先级下载；不可用时自动回退。";
  }
  function schedulePoll() {
    clearTimeout(timer);
    timer = setTimeout(poll, 1000);
  }
  function showOperation(operation) {
    busy = operation.state === "running" || Boolean(operation.busy);
    controls();
    if (busy) {
      notice(operation.busy ? "另一位管理员正在操作，请稍后刷新。" : "操作进行中，请等待。关闭页面不会取消服务端任务。");
      schedulePoll();
      return;
    }
    if (operation.state === "idle") return;
    if (typeof operation.needs_2fa === "boolean") needs2fa = operation.needs_2fa;
    controls();
    notice(operation.message, operation.state === "error");
    if (operation.action === "verify") {
      const details = operation.bytes ? ` · ${(operation.bytes / 1048576).toFixed(2)} MB · ${Math.round(operation.duration)} 秒` : "";
      $("verification-result").textContent = operation.message + details;
    }
    if (operation.action === "clear-cache" && operation.state === "complete") {
      $("cache-size").textContent = "0 B";
      $("cache-files").textContent = "0 个文件";
      $("cache-meter-fill").style.width = "2%";
    }
    if (operation.action === "save-settings" && operation.state === "complete" && operation.settings) {
      renderSettings(operation.settings);
    }
    if (needs2fa) $("code").focus();
  }
  async function poll() {
    try {
      const operation = await bridge.apiGet("apple-music/operation");
      const key = JSON.stringify(operation);
      showOperation(operation);
      if (!busy && key !== lastOperation) {
        lastOperation = key;
        await refresh(false);
        showOperation(operation);
      }
    } catch (_) {
      busy = false;
      controls();
      notice("无法检查任务状态，请刷新页面；服务端任务可能仍在运行。", true);
    }
  }
  async function refresh(showNotice = true) {
    if (refreshing) return;
    refreshing = true;
    controls();
    try {
      const data = await bridge.apiGet("apple-music/status");
      render(data);
      if (showNotice) notice(data.message, data.enabled && !data.connected);
      if (data.operation?.state === "running" || data.operation?.busy) showOperation(data.operation);
    } catch (_) {
      nonce = "";
      snapshot = null;
      notice("无法连接管理 API。请确认 AstrBot 支持 Plugin Pages、插件已重载且后台登录未过期。", true);
    } finally {
      refreshing = false;
      controls();
    }
  }
  async function submit(action, body) {
    if (busy || !nonce) return;
    busy = true;
    controls();
    notice("正在提交操作…");
    try {
      const operation = await bridge.apiPost(`apple-music/${action}`, { ...body, nonce });
      showOperation(operation);
    } catch (error) {
      busy = false;
      controls();
      // API errors are sanitized server-side; never echo a submitted body.
      notice(error.message || "提交失败，请刷新后重试", true);
    }
  }
  $("refresh").addEventListener("click", () => refresh());
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const body = { username: $("username").value.trim(), password: $("password").value };
    $("password").value = "";
    submit("login", body);
  });
  $("two-factor-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const code = $("code").value.trim();
    $("code").value = "";
    submit("two-factor", { code });
  });
  $("clear-session").addEventListener("click", () => {
    if (window.confirm("仅清除 wrapper 内存会话。磁盘登录缓存仍保留，重启服务可能恢复登录。继续？")) {
      submit("clear-session", { confirm: true });
    }
  });
  $("clear-cache").addEventListener("click", () => {
    if (window.confirm("将删除插件缓存中的音频、卡片和临时文件，但不会影响 Apple Music 登录。继续？")) {
      submit("clear-cache", { confirm: true });
    }
  });
  $("settings-form").addEventListener("change", updateSettingsState);
  $("add-codec").addEventListener("click", () => {
    const used = new Set(readSettings().file_codec_priority);
    const next = codecOptions.find(([value]) => !used.has(value));
    if (next) $("codec-priority-list").append(codecRow(next[0]));
    updateSettingsState();
  });
  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const settings = readSettings();
    if (settings.file_codec_priority.length !== new Set(settings.file_codec_priority).size) return;
    submit("save-settings", { settings });
  });
  $("verify-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submit("verify", { url: $("track-url").value.trim() });
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(timer);
    $("password").value = "";
    $("code").value = "";
  });
  if (!bridge) {
    notice("请从 AstrBot 插件详情打开此页，不要直接打开 HTML 文件。", true);
    $("refresh").disabled = true;
    return;
  }
  const readyTimeout = setTimeout(() => notice("尚未连接后台页面桥接，请从支持 Plugin Pages 的 AstrBot 后台打开此页。", true), 8000);
  bridge.ready().then(() => { clearTimeout(readyTimeout); refresh(); });
})();
