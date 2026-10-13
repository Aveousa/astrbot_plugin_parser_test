"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginPage;
  let nonce = "";
  let snapshot = null;
  let busy = false;
  let activeAction = "";
  let timer = null;
  let refreshing = false;
  let needs2fa = false;
  let lastOperation = "";
  let savedSettings = "";
  let settingsTimer = null;
  let settingsDirty = false;
  let submittedSettings = "";
  let settingsSaveFailed = false;
  let savedMusic = "";
  let musicDirty = false;
  let musicSaveFailed = false;
  let musicTimer = null;
  let submittedMusic = "";
  let clearCacheArmed = false;
  let clearCacheTimer = null;
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
  async function loadPluginLogo() {
    const logo = $("plugin-logo");
    logo.addEventListener("load", () => { logo.hidden = false; }, { once: true });
    logo.addEventListener("error", () => { logo.hidden = true; }, { once: true });
    try {
      // Read via the authenticated Plugin Pages bridge instead of a direct
      // image request, which cannot add the dashboard Authorization header.
      const result = await bridge.apiGet("apple-music/logo");
      if (typeof result?.data_url === "string" && result.data_url.startsWith("data:image/png;base64,")) {
        logo.src = result.data_url;
      }
    } catch (_) {
      logo.hidden = true;
    }
  }
  function field(id, value, tone = "") {
    $(id).textContent = value;
    $(id).dataset.tone = tone;
  }
  function renderPlatformAccounts(accounts) {
    const byKey = new Map(
      (Array.isArray(accounts) ? accounts : [])
        .filter((item) => item && typeof item.key === "string")
        .map((item) => [item.key, item])
    );
    document.querySelectorAll("[data-platform-status]").forEach((item) => {
      const status = byKey.get(item.dataset.platformStatus);
      const available = status?.available === true;
      item.dataset.available = String(available);
      item.querySelector("small").textContent = typeof status?.text === "string"
        ? status.text : "状态未确认";
    });
  }
  function controls() {
    const connected = Boolean(snapshot?.enabled && snapshot?.connected && nonce);
    $("refresh").disabled = refreshing;
    $("login-fields").disabled = !connected || busy || needs2fa;
    $("two-factor-form").hidden = !needs2fa;
    $("two-factor-fields").disabled = !connected || busy || !needs2fa;
    $("clear-session").disabled = !connected || busy;
    $("clear-cache").disabled = !nonce || busy;
    $("settings-fields").disabled = !nonce || (busy && activeAction !== "save-settings");
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
    renderPlatformAccounts(data.platform_accounts);
    renderCache(data.cache);
    renderSettings(data.settings);
    renderMusicParsers(data.music_parsers);
    controls();
  }
  const musicLabels = { netease: "网易云音乐", qqmusic: "QQ 音乐", kugou: "酷狗音乐" };

  function renderMusicParsers(parsers) {
    const list = $("music-parser-list");
    if (!list || !parsers || typeof parsers !== "object") return;
    const dirty = musicDirty || activeAction === "save-music-settings";
    const pendingSettings = dirty ? readMusicSettings() : null;
    list.replaceChildren(...Object.entries(parsers)
      .filter(([key]) => Object.hasOwn(musicLabels, key))
      .map(([key, parser]) => createMusicParserRow(key, parser, pendingSettings?.[key])));
    if (!dirty) {
      savedMusic = JSON.stringify(readMusicSettings());
      musicSaveFailed = false;
    }
    updateMusicSettingsState();
  }

  function createMusicParserRow(key, parser, pending) {
    const card = document.createElement("article");
    card.className = "music-parser-row";
    card.dataset.musicParser = key;
    const settings = parser.settings && typeof parser.settings === "object" ? parser.settings : {};
    const configured = parser.configured === true;
    const heading = document.createElement("header");
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = musicLabels[key];
    const subtitle = document.createElement("small");
    subtitle.textContent = configured ? (parser.enabled ? "已启用" : "未启用")
      : parser.known ? "预设配置，当前未启用" : "未配置";
    identity.append(title, subtitle);
    const status = document.createElement("span");
    status.className = "music-cookie-state";
    status.dataset.available = String(parser.available === true);
    status.dataset.validity = parser.cookie_validity || (parser.cookie_configured ? "unknown" : "unconfigured");
    status.textContent = parser.known ? parser.cookie_status || "Cookie 状态未确认" : "未配置";
    heading.append(identity, status);
    card.append(heading);

    if (!parser.known) {
      const note = document.createElement("p");
      note.className = "music-unconfigured";
      note.textContent = "未配置";
      card.append(note);
      return card;
    }

    const fieldset = document.createElement("fieldset");
    fieldset.disabled = !nonce || busy;
    fieldset.dataset.musicParserFieldset = key;
    if (!configured) {
      const note = document.createElement("p");
      note.className = "music-unconfigured";
      note.textContent = "显示预设或历史配置；恢复解析器配置后可编辑。";
      fieldset.append(note);
    }
    fieldset.append(
      musicToggle(key, "enable", "启用解析器", configured && parser.enabled === true, configured),
      musicToggle(key, "return_audio", "解析单曲时返回音频", pending?.return_audio ?? settings.return_audio !== false, configured),
      musicToggle(key, "show_playlist_cover", "显示歌单封面", pending?.show_playlist_cover ?? settings.show_playlist_cover !== false, configured),
      musicToggle(key, "show_playlist_url", "显示歌单原始链接", pending?.show_playlist_url ?? settings.show_playlist_url !== false, configured),
    );

    const formatLabel = document.createElement("label");
    formatLabel.className = "music-format-setting";
    formatLabel.append(document.createTextNode("单曲音频发送模式"));
    const format = document.createElement("select");
    format.dataset.musicSetting = "audio_send_format";
    format.dataset.parserKey = key;
    format.disabled = !configured;
    const selected = pending?.audio_send_format ?? settings.audio_send_format ?? "voice";
    [["voice", "语音（默认）"], ["file", "文件（OneBot）"]].forEach(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      option.selected = value === selected;
      format.append(option);
    });
    formatLabel.append(format);
    fieldset.append(formatLabel);

    const editor = document.createElement("div");
    editor.className = "music-cookie-editor";
    const cookieLabel = document.createElement("label");
    cookieLabel.append(document.createTextNode("Cookie"));
    const cookie = document.createElement("textarea");
    cookie.dataset.musicCookie = key;
    cookie.disabled = !configured;
    cookie.setAttribute("aria-label", `${musicLabels[key]} Cookie`);
    cookie.placeholder = parser.cookie_configured
      ? "Cookie 已配置；出于安全考虑不回显。输入新值可替换。"
      : "可选：粘贴此平台登录 Cookie";
    cookieLabel.append(cookie);
    const actions = document.createElement("div");
    actions.className = "music-cookie-actions";
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "quiet-button";
    clear.textContent = "清除 Cookie";
    clear.disabled = !configured || !parser.cookie_configured;
    clear.addEventListener("click", () => saveMusicCookie(key, "", true));
    const save = document.createElement("button");
    save.type = "button";
    save.className = "music-cookie-save";
    save.textContent = "保存 Cookie";
    save.disabled = !configured;
    save.addEventListener("click", () => {
      const value = cookie.value.trim();
      if (!value) { notice("请先输入 Cookie；清除现有值请使用“清除 Cookie”。", true); return; }
      saveMusicCookie(key, value, false);
    });
    actions.append(clear);
    if (key === "qqmusic" || key === "kugou") {
      const check = document.createElement("button");
      check.type = "button";
      check.className = "quiet-button music-cookie-check";
      check.dataset.cookieCheck = key;
      check.textContent = "检查有效性";
      check.disabled = !configured || !parser.cookie_configured;
      check.setAttribute("aria-label", `检查${musicLabels[key]} Cookie 有效性`);
      check.addEventListener("click", () => {
        submit("check-music-cookie", { parser: key });
      });
      actions.append(check);
    }
    actions.append(save);
    editor.append(cookieLabel, actions);
    fieldset.append(editor);
    fieldset.addEventListener("change", scheduleMusicSave);
    card.append(fieldset);
    return card;
  }

  function musicToggle(parserKey, setting, labelText, checked, enabled) {
    const label = document.createElement("label");
    label.className = "setting-row music-setting-row";
    const text = document.createElement("span");
    text.textContent = labelText;
    const control = document.createElement("span");
    control.className = "switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.musicSetting = setting;
    input.dataset.parserKey = parserKey;
    input.checked = Boolean(checked);
    input.disabled = !enabled;
    const track = document.createElement("span");
    track.className = "switch-track";
    track.setAttribute("aria-hidden", "true");
    control.append(input, track);
    label.append(text, control);
    return label;
  }

  function readMusicSettings() {
    const result = {};
    document.querySelectorAll("[data-music-setting]").forEach((control) => {
      const key = control.dataset.parserKey;
      result[key] ??= {};
      result[key][control.dataset.musicSetting] = control.type === "checkbox" ? control.checked : control.value;
    });
    return result;
  }

  function updateMusicSettingsState() {
    const state = $("music-settings-state");
    if (!state) return;
    state.textContent = musicSaveFailed ? "自动保存失败；修改选项可重试。"
      : musicDirty ? "正在保存音乐解析设置…" : "音乐解析设置已同步到磁盘。";
    state.dataset.error = String(musicSaveFailed);
  }

  function scheduleMusicSave() {
    clearTimeout(musicTimer);
    const settings = readMusicSettings();
    if (!nonce || !savedMusic || JSON.stringify(settings) === savedMusic) return;
    musicDirty = true;
    updateMusicSettingsState();
    musicTimer = setTimeout(saveMusicSettings, 350);
  }

  function saveMusicSettings() {
    clearTimeout(musicTimer);
    if (busy || !nonce || !savedMusic) return;
    const settings = readMusicSettings();
    if (JSON.stringify(settings) === savedMusic) {
      musicDirty = false;
      updateMusicSettingsState();
      return;
    }
    submittedMusic = JSON.stringify(settings);
    musicDirty = true;
    musicSaveFailed = false;
    updateMusicSettingsState();
    submit("save-music-settings", { parsers: settings });
  }

  function saveMusicCookie(parserKey, cookie, clear) {
    submit("save-music-cookie", { parser: parserKey, cookie, clear });
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
    if (activeAction === "save-settings" || settingsDirty || changedSettings()) return;
    $("setting-backend").checked = Boolean(settings.backend_enabled);
    $("setting-return-audio").checked = Boolean(settings.return_audio);
    $("setting-cover").checked = settings.show_playlist_cover !== false;
    $("setting-url").checked = settings.show_playlist_url !== false;
    const format = settings.audio_send_format === "voice" ? "voice" : "file";
    document.querySelector(`input[name='audio-format'][value='${format}']`).checked = true;
    renderCodecs(Array.isArray(settings.file_codec_priority) ? settings.file_codec_priority : ["alac", "aac-web"]);
    savedSettings = JSON.stringify(readSettings());
    settingsDirty = false;
    settingsSaveFailed = false;
    clearTimeout(settingsTimer);
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
        scheduleSettingsSave();
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
    $("settings-state").textContent = duplicates ? "同一音质不能重复添加。" : settingsSaveFailed ? "自动保存失败；可调整设置重试，或点击“重试保存”。" : settingsDirty ? "正在保存更改…" : changed ? "更改将在短暂等待后自动保存。" : "设置已与磁盘配置同步。";
    $("settings-state").dataset.error = String(duplicates);
    $("save-settings").hidden = !settingsSaveFailed;
    $("save-settings").disabled = !settingsSaveFailed || !nonce || busy || duplicates;
    $("save-settings").querySelector("span").textContent = settingsSaveFailed ? "↻" : "→";
    $("save-settings").firstChild.textContent = settingsSaveFailed ? "重试保存 " : "保存设置 ";
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
  function scheduleSettingsSave() {
    clearTimeout(settingsTimer);
    const settings = readSettings();
    const duplicates = settings.file_codec_priority.length !== new Set(settings.file_codec_priority).size;
    if (!nonce || !savedSettings || !changedSettings() || duplicates) {
      settingsDirty = false;
      updateSettingsState();
      return;
    }
    settingsDirty = true;
    updateSettingsState();
    settingsTimer = setTimeout(saveSettings, 350);
  }
  function changedSettings() {
    return Boolean(savedSettings) && JSON.stringify(readSettings()) !== savedSettings;
  }
  async function saveSettings() {
    clearTimeout(settingsTimer);
    if (busy || !nonce || !changedSettings()) {
      if (busy && changedSettings()) settingsDirty = true;
      return;
    }
    const settings = readSettings();
    const duplicates = settings.file_codec_priority.length !== new Set(settings.file_codec_priority).size;
    if (duplicates) {
      settingsDirty = false;
      updateSettingsState();
      return;
    }
    settingsDirty = true;
    settingsSaveFailed = false;
    submittedSettings = JSON.stringify(settings);
    updateSettingsState();
    submit("save-settings", { settings });
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
      resetClearCacheConfirmation();
      const details = Number.isFinite(Number(operation.removed_files))
        ? ` · 已清理 ${operation.removed_files} 个文件`
        : "";
      notice(operation.message + details, false);
    }
    if (operation.action === "save-settings" && operation.state === "complete" && operation.settings) {
      const currentSettings = readSettings();
      const hasNewerChanges = JSON.stringify(currentSettings) !== submittedSettings;
      savedSettings = JSON.stringify(operation.settings);
      activeAction = "";
      if (hasNewerChanges) {
        settingsDirty = true;
        settingsSaveFailed = false;
        updateSettingsState();
        settingsTimer = setTimeout(saveSettings, 0);
      } else {
        settingsDirty = false;
        settingsSaveFailed = false;
        renderSettings(operation.settings);
        notice(operation.message || "Apple Music 设置已自动保存");
      }
    }
    if (operation.action === "save-settings" && operation.state === "error") {
      const hasNewerChanges = JSON.stringify(readSettings()) !== submittedSettings;
      activeAction = "";
      settingsDirty = false;
      settingsSaveFailed = true;
      updateSettingsState();
      $("settings-state").dataset.error = "true";
      if (hasNewerChanges) scheduleSettingsSave();
    }
    if (operation.action === "save-music-settings" && operation.state === "complete" && operation.parsers) {
      const current = JSON.stringify(readMusicSettings());
      const returned = { ...operation.parsers };
      Object.entries(returned).forEach(([key, item]) => { returned[key] = item?.settings || item; });
      savedMusic = current === submittedMusic ? JSON.stringify({ ...readMusicSettings(), ...returned }) : submittedMusic;
      musicDirty = current !== submittedMusic;
      musicSaveFailed = false;
      updateMusicSettingsState();
      notice(operation.message || "音乐解析设置已自动保存");
      if (musicDirty) scheduleMusicSave();
    }
    if (operation.action === "save-music-settings" && operation.state === "error") {
      musicDirty = false;
      musicSaveFailed = true;
      updateMusicSettingsState();
    }
    if (operation.action === "save-music-cookie" && operation.state === "complete") {
      const parserKey = operation.parser_key;
      const textarea = parserKey ? document.querySelector(`[data-music-cookie="${parserKey}"]`) : null;
      if (textarea) textarea.value = "";
    }
    if (operation.action === "check-music-cookie" && operation.state === "complete") {
      notice(operation.message || "Cookie 检查完成", operation.cookie_validity === "invalid");
    }
    if (operation.action !== "save-settings") {
      activeAction = "";
      if (operation.state !== "running" && changedSettings()) scheduleSettingsSave();
    }
    if (operation.action !== "save-music-settings") activeAction = "";
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
    activeAction = action;
    busy = true;
    controls();
    notice(action === "save-settings" || action === "save-music-settings"
      ? "正在自动保存解析器设置…"
      : action === "save-music-cookie" ? "正在安全保存 Cookie…"
      : action === "check-music-cookie" ? "正在检查 Cookie 有效性…"
      : "正在提交操作…");
    try {
      const operation = await bridge.apiPost(`apple-music/${action}`, { ...body, nonce });
      showOperation(operation);
    } catch (error) {
      busy = false;
      activeAction = "";
      controls();
      // API errors are sanitized server-side; never echo a submitted body.
      notice(error.message || "提交失败，请刷新后重试", true);
      if (action === "save-settings") {
        settingsDirty = false;
        settingsSaveFailed = true;
        updateSettingsState();
        $("settings-state").dataset.error = "true";
      }
      if (action === "save-music-settings") {
        musicDirty = false;
        musicSaveFailed = true;
        updateMusicSettingsState();
      }
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
  function resetClearCacheConfirmation() {
    clearCacheArmed = false;
    clearTimeout(clearCacheTimer);
    const button = $("clear-cache");
    if (!button) return;
    button.dataset.confirming = "false";
    button.querySelector(".clear-cache-label").textContent = "一键清理缓存";
  }
  $("clear-cache").addEventListener("click", () => {
    if (!clearCacheArmed) {
      clearCacheArmed = true;
      const button = $("clear-cache");
      button.dataset.confirming = "true";
      button.querySelector(".clear-cache-label").textContent = "再次点击确认清理";
      notice("缓存清理会删除插件生成的音频、卡片和临时文件；再次点击确认。", false);
      clearCacheTimer = setTimeout(resetClearCacheConfirmation, 5000);
      return;
    }
    resetClearCacheConfirmation();
    submit("clear-cache", { confirm: true });
  });
  $("settings-form").addEventListener("change", () => {
    updateSettingsState();
    scheduleSettingsSave();
  });
  $("music-parser-list")?.addEventListener("change", scheduleMusicSave);
  $("add-codec").addEventListener("click", () => {
    const used = new Set(readSettings().file_codec_priority);
    const next = codecOptions.find(([value]) => !used.has(value));
    if (next) $("codec-priority-list").append(codecRow(next[0]));
    updateSettingsState();
    scheduleSettingsSave();
  });
  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings();
  });
  $("verify-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submit("verify", { url: $("track-url").value.trim() });
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(timer);
    clearTimeout(clearCacheTimer);
    clearTimeout(settingsTimer);
    clearTimeout(musicTimer);
    $("password").value = "";
    $("code").value = "";
  });
  if (!bridge) {
    notice("请从 AstrBot 插件详情打开此页，不要直接打开 HTML 文件。", true);
    $("refresh").disabled = true;
    return;
  }
  const readyTimeout = setTimeout(() => notice("尚未连接后台页面桥接，请从支持 Plugin Pages 的 AstrBot 后台打开此页。", true), 8000);
  bridge.ready().then(() => { clearTimeout(readyTimeout); loadPluginLogo(); refresh(); });
})();
