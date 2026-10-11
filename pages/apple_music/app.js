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

  function notice(message, error = false) {
    $("notice").textContent = message;
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
    field("gamdl-state", data.gamdl.available ? `就绪 · ${data.gamdl.version}` : data.gamdl.version ? `版本不兼容 · ${data.gamdl.version}` : "未配置或不可用");
    field("ffmpeg-state", data.gamdl.ffmpeg && data.gamdl.ffprobe ? "就绪" : "未就绪");
    field("playback-state", data.playback_ready ? "就绪" : "未就绪");
    $("subscription-detail").textContent = data.subscription_message;
    controls();
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
