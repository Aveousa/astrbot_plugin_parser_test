"use strict";

// Schema-backed forms are independent of the long-running Apple login job and
// the explicitly submitted group drafts. Only changed fields are ever posted.
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginPage;
  const pending = new Map();
  const editors = new Set();
  let nonce = "";
  let saving = false;
  let loading = false;
  let timer;
  let configuration;
  let revision = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function feedback(message, failed = false) {
    $("configuration-state").textContent = message;
    $("configuration-state").dataset.error = String(failed);
    $("retry-configuration").hidden = !failed || !pending.size;
  }
  function status(row, message, failed = false) {
    const target = row.querySelector(".config-field-status");
    target.textContent = message;
    target.dataset.error = String(failed);
    const retry = row.querySelector(".config-field-retry");
    if (retry) retry.hidden = !failed || !pending.get(row.dataset.scope)?.has(row.dataset.key);
  }
  function queue(scope, key, value, row) {
    if (!pending.has(scope)) pending.set(scope, new Map());
    pending.get(scope).set(key, { value, row, revision: ++revision });
    status(row, "等待保存…");
    feedback("有更改正在等待保存…");
    clearTimeout(timer);
    timer = setTimeout(flush, 250);
  }
  async function post(endpoint, body) {
    if (!nonce) throw new Error("管理会话未就绪，请刷新页面后重试");
    return bridge.apiPost(`apple-music/${endpoint}`, { ...body, nonce });
  }
  async function flush() {
    if (saving || !pending.size || !nonce) return;
    saving = true;
    $("retry-configuration").hidden = true;
    try {
      while (pending.size) {
        const [scope, changes] = pending.entries().next().value;
        const submitted = new Map(changes);
        const settings = Object.fromEntries([...submitted].map(([key, item]) => [key, item.value]));
        for (const { row } of submitted.values()) status(row, "正在保存…");
        feedback("正在保存并应用配置…");
        try {
          const result = await post("save-configuration", { scope, settings });
          configuration = result.configuration || configuration;
          for (const [key, item] of submitted) {
            if (changes.get(key) !== item) continue; // Never discard a newer edit.
            changes.delete(key);
            status(item.row, "已保存 · 已生效");
          }
          if (!changes.size) pending.delete(scope);
          window.dispatchEvent(new Event("management-config-saved"));
        } catch (error) {
          for (const { row } of submitted.values()) status(row, error.message || "保存失败，请重试", true);
          feedback("保存失败，更改已保留；请检查字段提示并重试。", true);
          return;
        }
      }
      feedback("所有更改已保存并生效，无需重载插件。");
    } finally {
      saving = false;
    }
  }
  function labelText(field, value) {
    const index = (field.options || []).indexOf(value);
    return field.labels?.[index] || value;
  }
  function populate(select, options, field, selected) {
    options.forEach((value) => {
      const option = element("option", "", labelText(field, value));
      option.value = value;
      option.selected = value === selected;
      select.append(option);
    });
  }
  function listEditor(scope, key, field, value, row, disabled) {
    const root = element("div", "config-priority");
    let values = Array.isArray(value) ? [...value] : [];
    const options = field.options || (key === "file_codec_priority" ? ["alac", "aac-web", "aac-he-web", "aac", "aac-he", "aac-binaural", "aac-downmix", "aac-he-binaural", "aac-he-downmix", "atmos", "ac3"] : []);
    function render(focusIndex) {
      root.replaceChildren();
      values.forEach((value, index) => {
        const line = element("div", "config-priority-row");
        const select = element("select");
        select.setAttribute("aria-label", `${field.description} 第 ${index + 1} 项`);
        populate(select, options.filter((item) => item === value || !values.includes(item)), field, value);
        select.disabled = disabled;
        select.addEventListener("change", () => { values[index] = select.value; commit(index); });
        line.append(select);
        [["上移", -1], ["下移", 1], ["移除", 0]].forEach(([text, step]) => {
          const button = element("button", "quiet-action", text);
          button.type = "button";
          button.setAttribute("aria-label", `${text}第 ${index + 1} 项`);
          button.disabled = disabled || (step ? index + step < 0 || index + step >= values.length : values.length <= 1);
          button.addEventListener("click", () => {
            if (step) [values[index], values[index + step]] = [values[index + step], values[index]];
            else values.splice(index, 1);
            commit(Math.max(0, index + step));
          });
          line.append(button);
        });
        root.append(line);
      });
      const add = element("button", "quiet-action", "添加优先项");
      add.type = "button";
      add.disabled = disabled || values.length >= options.length;
      add.addEventListener("click", () => { values.push(options.find((item) => !values.includes(item))); commit(values.length - 1); });
      root.append(add);
      if (focusIndex !== undefined) root.querySelectorAll("select")[focusIndex]?.focus({ preventScroll: true });
    }
    function commit(index) { queue(scope, key, [...values], row); render(index); }
    render();
    return root;
  }
  function cookieEditor(scope, field, row, configured, disabled) {
    const root = element("div", "config-sensitive");
    const input = element("textarea");
    input.id = `config-${scope}-cookies`;
    input.rows = 3;
    input.maxLength = 16384;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "只写入，不回显现有 Cookie";
    input.disabled = disabled;
    input.addEventListener("input", () => { if (input.value) editors.add(input); else editors.delete(input); });
    const actions = element("div", "config-actions");
    const save = element("button", "secondary-button", "保存 Cookie");
    const clear = element("button", "quiet-action", "清除 Cookie");
    const state = element("span", "panel-note", configured ? "已配置 Cookie（未验证有效性）" : "未配置 Cookie");
    save.type = clear.type = "button";
    save.disabled = clear.disabled = disabled;
    async function submit(remove) {
      if (remove && !window.confirm("确认清除该解析器的 Cookie 及对应本地登录凭据？")) return;
      if (!remove && !input.value.trim()) { status(row, "请输入 Cookie，或使用清除按钮。", true); input.focus(); return; }
      input.disabled = save.disabled = clear.disabled = true;
      status(row, "正在保存 Cookie…");
      try {
        const result = await post("save-parser-cookie", { parser: scope, cookie: remove ? "" : input.value, clear: remove });
        input.value = "";
        editors.delete(input);
        configuration = result.configuration || configuration;
        state.textContent = remove ? "未配置 Cookie" : "已配置 Cookie（未验证有效性）";
        status(row, "Cookie 已保存 · 已生效");
        window.dispatchEvent(new Event("management-config-saved"));
      } catch (error) { status(row, error.message || "Cookie 保存失败，请重试", true); }
      finally { input.disabled = save.disabled = clear.disabled = disabled; }
    }
    save.addEventListener("click", () => submit(false));
    clear.addEventListener("click", () => submit(true));
    actions.append(save, clear, state);
    if (["qqmusic", "netease", "kugou"].includes(scope)) {
      const check = element("button", "quiet-action", "检查 Cookie 有效性");
      check.type = "button";
      check.disabled = disabled;
      check.dataset.cookieCheck = scope;
      check.addEventListener("click", () => window.dispatchEvent(new CustomEvent("management-check-cookie", { detail: scope })));
      actions.append(check);
      window.addEventListener("management-operation", (event) => {
        if (!root.isConnected) return;
        const operation = event.detail;
        check.disabled = disabled || operation.state === "running" || Boolean(operation.busy);
        if (operation.action === "check-music-cookie" && operation.parser_key === scope && operation.state === "complete") {
          state.textContent = operation.cookie_status || operation.message;
          status(row, operation.message || "Cookie 检查完成", operation.cookie_validity === "invalid");
        }
      });
    }
    root.append(input, actions);
    return root;
  }
  function fileEditor(key, field, value, row, disabled) {
    const root = element("div", "config-sensitive");
    const input = element("input");
    input.type = "file";
    input.id = `config-global-${key}`;
    input.accept = field.file_types.map((type) => `.${type}`).join(",");
    const actions = element("div", "config-actions");
    const upload = element("button", "secondary-button", "上传并应用");
    const clear = element("button", "quiet-action", "恢复默认");
    const state = element("span", "panel-note", value?.length ? `当前：${value.join("、")}` : "使用默认资源");
    upload.type = clear.type = "button";
    input.disabled = upload.disabled = clear.disabled = disabled;
    input.addEventListener("change", () => { if (input.files.length) editors.add(input); else editors.delete(input); });
    async function submit(remove) {
      if (remove && !window.confirm("确认恢复默认资源？已上传文件将保留，不影响正在生成的卡片。")) return;
      const file = input.files[0];
      if (!remove && (!file || file.size > field.max_bytes || !file.size)) {
        status(row, `请选择有效文件，最大 ${Math.round(field.max_bytes / 1024 / 1024)} MB。`, true); return;
      }
      input.disabled = upload.disabled = clear.disabled = true;
      status(row, "正在上传并应用…");
      try {
        const body = { field: key, clear: remove };
        if (!remove) {
          body.name = file.name;
          body.encoded = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(",")[1]);
            reader.onerror = () => reject(new Error("读取文件失败，请重选文件"));
            reader.readAsDataURL(file);
          });
        }
        const result = await post("upload-configuration-asset", body);
        configuration = result.configuration || configuration;
        const names = configuration.global_values[key] || [];
        state.textContent = names.length ? `当前：${names.join("、")}` : "使用默认资源";
        input.value = "";
        editors.delete(input);
        status(row, "资源已保存 · 后续卡片立即生效");
        window.dispatchEvent(new Event("management-config-saved"));
      } catch (error) { status(row, error.message || "上传失败，请重试", true); }
      finally { input.disabled = upload.disabled = clear.disabled = disabled; }
    }
    upload.addEventListener("click", () => submit(false));
    clear.addEventListener("click", () => submit(true));
    actions.append(upload, clear, state);
    root.append(input, actions);
    return root;
  }
  function fieldRow(scope, key, field, value, configured, cookieConfigured = false) {
    const disabled = !configured;
    const row = element("div", "config-field");
    row.dataset.scope = scope;
    row.dataset.key = key;
    const heading = element("label", "config-label", field.description);
    heading.htmlFor = `config-${scope}-${key}`;
    const help = element("p", "config-help", field.hint || (field.type === "list" ? "按从上到下的顺序尝试。" : ""));
    help.id = `config-help-${scope}-${key}`;
    const state = element("p", "config-field-status");
    state.setAttribute("role", "status");
    row.append(heading, help);
    let input;
    if (key === "cookies") input = cookieEditor(scope, field, row, cookieConfigured, disabled);
    else if (field.type === "file") input = fileEditor(key, field, value, row, disabled);
    else if (field.type === "list") input = listEditor(scope, key, field, value, row, disabled);
    else {
      input = element(field.options ? "select" : "input");
      input.id = `config-${scope}-${key}`;
      input.disabled = disabled;
      input.setAttribute("aria-describedby", help.id);
      if (field.options) populate(input, field.options, field, value);
      else if (field.type === "bool") {
        input.type = "checkbox";
        input.checked = Boolean(value);
        input.setAttribute("role", "switch");
        row.classList.add("config-toggle");
      } else if (field.type === "int") {
        input.type = "number";
        input.required = true;
        input.min = field.slider?.min ?? 0;
        input.max = field.slider?.max ?? 10000;
        input.step = 1;
        input.value = value ?? "";
      } else {
        input.type = "text";
        input.maxLength = 2048;
        input.value = value ?? "";
        input.autocomplete = "off";
      }
      input.addEventListener("input", () => editors.add(input));
      input.addEventListener("change", () => {
        if (!input.checkValidity()) { status(row, "请填写有效值，数字需在指定范围内。", true); return; }
        editors.delete(input);
        queue(scope, key, field.type === "bool" ? input.checked : field.type === "int" ? Number(input.value) : input.value, row);
      });
      input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); input.blur(); } });
    }
    row.append(input, state);
    if (key !== "cookies" && field.type !== "file") {
      const retry = element("button", "quiet-action config-field-retry", "重试保存");
      retry.type = "button";
      retry.hidden = true;
      retry.addEventListener("click", flush);
      row.append(retry);
    }
    return row;
  }
  function fields(root, scope, metadata, values, configured, cookieConfigured) {
    const grid = element("div", "config-grid");
    Object.entries(metadata).forEach(([key, field]) => grid.append(fieldRow(scope, key, field, values[key], configured, cookieConfigured)));
    root.append(grid);
  }
  function render(data) {
    $("global-configuration").replaceChildren();
    $("cache-configuration").replaceChildren();
    for (const [label, keys] of Object.entries(data.groups)) {
      const isCache = keys.includes("clean_cron");
      const panel = isCache ? $("cache-configuration") : element("section", "panel config-panel");
      panel.append(element("h3", "config-group-title", label));
      fields(panel, "global", Object.fromEntries(keys.map((key) => [key, data.global_fields[key]])), data.global_values, true);
      if (isCache) panel.append(element("p", "panel-note", "五段 Cron，例如 30 2 * * * 表示每天 02:30；按 AstrBot 配置时区执行，活跃缓存仍受保护。"));
      else $("global-configuration").append(panel);
    }
    $("wrapper-fields").replaceChildren();
    if (!data.wrapper_configured) $("wrapper-fields").append(element("p", "panel-note", "Apple Music 尚未配置，请先在 AstrBot 插件配置中添加；此处不会自动添加。"));
    fields($("wrapper-fields"), "wrapper", data.wrapper_fields, data.wrapper_values, data.wrapper_configured);
    $("parser-configuration-list").replaceChildren();
    for (const parser of data.parsers) {
      const panel = element("details", "panel config-panel parser-config-panel");
      panel.dataset.parser = parser.key;
      panel.open = parser.key === "bilibili";
      const summary = element("summary", "config-parser-heading");
      summary.append(element("h3", "", parser.label), element("span", "panel-note", parser.configured ? "已配置 · 展开设置" : "未配置 · 不会自动启用"));
      panel.append(summary);
      if (!parser.configured) panel.append(element("p", "panel-note", "请先在 AstrBot 插件配置中添加此解析器；已有群聊策略会继续保留。"));
      fields(panel, parser.key, parser.fields, parser.values, parser.configured, parser.cookie_configured);
      $("parser-configuration-list").append(panel);
    }
  }
  async function load() {
    if (!bridge || loading) return;
    if (pending.size || saving || editors.size) { feedback("保留当前未完成的编辑；保存完成后可再次刷新。", Boolean(pending.size)); return; }
    loading = true;
    feedback("正在读取配置…");
    try {
      configuration = await bridge.apiGet("apple-music/configuration");
      render(configuration);
      feedback("更改会自动保存，后续任务立即采用新配置。");
    } catch (error) {
      feedback(error.message || "读取配置失败，请刷新重试。", true);
    } finally { loading = false; }
  }
  window.addEventListener("management-status", (event) => {
    const wasReady = Boolean(nonce);
    nonce = event.detail.nonce;
    if (!wasReady && nonce && pending.size && !saving) void flush();
  });
  window.addEventListener("beforeunload", (event) => {
    if (!pending.size && !saving && !editors.size) return;
    event.preventDefault(); event.returnValue = "";
  });
  $("retry-configuration").addEventListener("click", flush);
  $("refresh").addEventListener("click", load);
  if (bridge) bridge.ready().then(load);
  else feedback("请从 AstrBot 后台打开管理页。", true);
})();
