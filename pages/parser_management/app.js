"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginPage;
  const pageStyleStorageKey = "apple-music-management-style";
  const pageThemeStorageKey = "apple-music-management-theme";
  let pageThemePreference = "auto";
  let astrbotTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  let applyingPageTheme = false;
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
  let logoRequest = 0;
  let groupSnapshot = null;
  let groupDraft = new Map();
  let groupDirty = false;
  let groupSaving = false;
  let groupLoading = false;
  let groupLoadError = "";
  let groupSaveResult = "";
  const viewTitles = { overview: "总览", music: "音乐管理", groups: "群聊配置" };
  const codecOptions = [
    ["alac", "ALAC · 无损"], ["aac-web", "AAC Web"],
    ["aac-he-web", "HE-AAC Web"], ["aac", "AAC"],
    ["aac-he", "HE-AAC"], ["aac-binaural", "AAC 双耳"],
    ["aac-downmix", "AAC 下混"], ["aac-he-binaural", "HE-AAC 双耳"],
    ["aac-he-downmix", "HE-AAC 下混"], ["atmos", "Dolby Atmos"],
    ["ac3", "Dolby Digital"],
  ];
  const qqQualityOptions = [
    ["lossless", "FLAC 无损"], ["high", "高音质 · 320 kbps"],
    ["standard", "标准音质 · 128 kbps"],
  ];

  function applyPageStyle(style, persist = true) {
    const selected = style === "soft" ? "soft" : "classic";
    document.documentElement.dataset.pageStyle = selected;
    const selector = $("page-style");
    if (selector && selector.value !== selected) selector.value = selected;
    if (!persist) return;
    try {
      window.localStorage.setItem(pageStyleStorageKey, selected);
    } catch (_) {
      // A blocked local storage should not prevent using either visual style.
    }
  }
  function initializePageStyle() {
    let selected = "classic";
    try {
      const saved = window.localStorage.getItem(pageStyleStorageKey);
      if (saved === "classic" || saved === "soft") selected = saved;
    } catch (_) {
      // Private browsing policies may block storage; the default remains usable.
    }
    applyPageStyle(selected, false);
    $("page-style")?.addEventListener("change", (event) => applyPageStyle(event.currentTarget.value));
  }

  function applyPageTheme(preference, persist = true) {
    pageThemePreference = preference === "light" || preference === "dark" ? preference : "auto";
    const theme = pageThemePreference === "auto" ? astrbotTheme : pageThemePreference;
    if (document.documentElement.dataset.theme !== theme) {
      applyingPageTheme = true;
      document.documentElement.dataset.theme = theme;
    }
    const selector = $("page-theme");
    if (selector && selector.value !== pageThemePreference) selector.value = pageThemePreference;
    const themeSwitch = $("theme-switch");
    if (themeSwitch) {
      themeSwitch.dataset.value = pageThemePreference;
      themeSwitch.querySelectorAll('input[name="theme-choice"]').forEach((input) => {
        input.checked = input.value === pageThemePreference;
      });
    }
    if (!persist) return;
    try {
      window.localStorage.setItem(pageThemeStorageKey, pageThemePreference);
    } catch (_) {
      // The current theme still works when browser storage is unavailable.
    }
  }
  function initializePageTheme() {
    let preference = "auto";
    try {
      const saved = window.localStorage.getItem(pageThemeStorageKey);
      if (saved === "light" || saved === "dark") preference = saved;
    } catch (_) {
      // Continue with AstrBot's theme when browser storage is unavailable.
    }
    new MutationObserver(() => {
      if (applyingPageTheme) {
        applyingPageTheme = false;
        return;
      }
      astrbotTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      if (pageThemePreference !== "auto") applyPageTheme(pageThemePreference, false);
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    applyPageTheme(preference, false);
    $("page-theme")?.addEventListener("change", (event) => applyPageTheme(event.currentTarget.value));
    $("theme-switch")?.addEventListener("change", (event) => {
      if (!(event.target instanceof HTMLInputElement)) return;
      const selector = $("page-theme");
      selector.value = event.target.value;
      selector.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  function notice(message, error = false) {
    const root = $("notice");
    if (!root) return;
    root.querySelector(".notice-text").textContent = message;
    root.dataset.error = String(error);
  }
  function setView(view, { updateHash = true, focus = false } = {}) {
    const selected = Object.hasOwn(viewTitles, view) ? view : "overview";
    document.querySelectorAll("[data-view]").forEach((panel) => {
      panel.hidden = panel.dataset.view !== selected;
    });
    document.querySelectorAll("[data-view-link]").forEach((link) => {
      const active = link.dataset.viewLink === selected;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    $("view-title").textContent = viewTitles[selected];
    document.title = viewTitles[selected] + " · 虚质解离器";
    if (updateHash && location.hash !== "#" + selected) history.replaceState(null, "", "#" + selected);
    const platformStatus = document.querySelector(".platform-status-bar");
    if (platformStatus) platformStatus.hidden = selected !== "music";
    if (focus) {
      const panel = document.querySelector(`[data-view="${selected}"]`);
      if (panel) {
        panel.tabIndex = -1;
        panel.focus({ preventScroll: true });
      }
    }
    if (selected === "groups" && !groupDirty && !groupLoading) void loadGroupSettings();
  }
  function initializeNavigation() {
    document.querySelectorAll("[data-view-link], [data-open-view]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        const view = link.dataset.viewLink || link.dataset.openView;
        setView(view, { focus: true });
      });
    });
    window.addEventListener("hashchange", () => {
      const view = location.hash.slice(1);
      if (viewTitles[view]) setView(view, { updateHash: false, focus: true });
    });
    setView(location.hash.slice(1), { updateHash: false });
  }
  async function loadPluginLogo() {
    const request = ++logoRequest;
    const logo = $("plugin-logo");
    if (logo.complete && !logo.naturalWidth) logo.hidden = true;
    logo.onload = () => { logo.hidden = false; };
    logo.onerror = () => { logo.hidden = true; };
    try {
      // Read via the authenticated Plugin Pages bridge instead of a direct
      // image request, which cannot add the dashboard Authorization header.
      const result = await bridge.apiGet("apple-music/logo");
      if (request !== logoRequest) return;
      if (typeof result?.data_url === "string" && result.data_url.startsWith("data:image/png;base64,")) {
        const replacement = new Image();
        replacement.onload = () => {
          if (request !== logoRequest) return;
          logo.src = result.data_url;
          logo.hidden = false;
        };
        replacement.src = result.data_url;
      }
    } catch (_) {
      // Keep the bundled logo visible if the authenticated API is unavailable.
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
    updateGroupActions();
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
    const wrapperState = $("wrapper-state").textContent;
    field("music-wrapper-state", wrapperState, data.connected ? "positive" : "");
    const enabledParsers = Array.isArray(data.parsers) ? data.parsers.filter((item) => item?.enabled).length : 0;
    field("parser-summary", enabledParsers + " 个已启用");
    if (groupSnapshot === null && !groupLoading) void loadGroupSettings();
    controls();
  }
  const musicLabels = { netease: "网易云音乐", qqmusic: "QQ 音乐", kugou: "酷狗音乐" };

  function groupKey(group) {
    return String(group.platform_id) + ":" + String(group.group_id);
  }
  function cloneGroupSettings(source) {
    return new Map((Array.isArray(source) ? source : []).map((group) => [
      groupKey(group), {
        enabled: new Set(Array.isArray(group.enabled) ? group.enabled : []),
        parsing_enabled: group.parsing_enabled !== false,
      },
    ]));
  }
  function groupSettingsDifferFromSnapshot() {
    const groups = Array.isArray(groupSnapshot?.groups) ? groupSnapshot.groups : [];
    return groups.some((group) => {
      const saved = new Set(Array.isArray(group.enabled) ? group.enabled : []);
      const draft = groupDraft.get(groupKey(group)) || { enabled: new Set(), parsing_enabled: true };
      return (group.parsing_enabled !== false) !== draft.parsing_enabled
        || saved.size !== draft.enabled.size
        || [...saved].some((key) => !draft.enabled.has(key));
    });
  }
  function renderGroupsPreservingView() {
    const active = document.activeElement;
    const activeCard = active?.closest?.(".group-card");
    const focusedInput = active instanceof HTMLInputElement && activeCard ? {
      groupKey: activeCard.dataset.groupKey,
      label: active.getAttribute("aria-label"),
    } : null;
    const focusedElementId = active instanceof HTMLElement && active.id ? active.id : "";
    const scrollY = window.scrollY;
    renderGroups();
    window.scrollTo({ top: scrollY, behavior: "instant" });
    if (focusedInput) {
      const card = [...document.querySelectorAll(".group-card")]
        .find((item) => item.dataset.groupKey === focusedInput.groupKey);
      const input = [...(card?.querySelectorAll("input") || [])]
        .find((item) => item.getAttribute("aria-label") === focusedInput.label);
      input?.focus({ preventScroll: true });
    } else if (focusedElementId) {
      $(focusedElementId)?.focus({ preventScroll: true });
    }
  }
  function renderGroups() {
    const list = $("group-list");
    if (!list) return;
    list.setAttribute("aria-busy", String(groupLoading));
    if (groupSnapshot === null) {
      list.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "group-empty";
      empty.textContent = groupLoading ? "正在读取当前 OneBot 已加入的群聊…"
        : groupLoadError || "尚未读取群聊列表。请点击刷新群列表重试。";
      list.append(empty);
      $("group-summary").textContent = groupLoading ? "正在读取…" : "读取失败";
      updateGroupActions();
      return;
    }
    const groups = Array.isArray(groupSnapshot.groups) ? groupSnapshot.groups : [];
    $("group-summary").textContent = groups.length + " 个群聊";
    const query = ($("group-search")?.value || "").trim().toLocaleLowerCase();
    const visible = groups.filter((group) => [group.name, group.group_id, group.platform_id, group.platform_label]
      .some((part) => String(part || "").toLocaleLowerCase().includes(query)));
    list.replaceChildren();
    if (!groups.length || !visible.length) {
      const empty = document.createElement("div");
      empty.className = "group-empty";
      empty.textContent = !groups.length
        ? "未发现已加入的 OneBot 群聊。请确认 OneBot V11 已连接并授权读取群列表。"
        : "没有匹配的群聊，请尝试其他群名、群号或 Bot 实例。";
      list.append(empty);
      updateGroupActions();
      return;
    }
    const parserMeta = new Map((Array.isArray(groupSnapshot.parsers) ? groupSnapshot.parsers : [])
      .map((parser) => [parser.key, parser]));
    const fragment = document.createDocumentFragment();
    visible.forEach((group) => {
      const key = groupKey(group);
      const draft = groupDraft.get(key) || { enabled: new Set(), parsing_enabled: true };
      const enabled = draft.enabled;
      const globallyEnabled = new Set(Array.isArray(group.globally_enabled) ? group.globally_enabled : []);
      const card = document.createElement("article");
      card.className = "group-card";
      card.dataset.groupKey = key;
      const heading = document.createElement("header");
      const identity = document.createElement("div");
      identity.className = "group-identity";
      const title = document.createElement("h2");
      title.textContent = group.name || ("群聊 " + group.group_id);
      const meta = document.createElement("p");
      meta.textContent = (group.platform_label || "OneBot V11") + " · 群号 " + group.group_id + " · Bot 实例 " + group.platform_id;
      identity.append(title, meta);
      const count = document.createElement("span");
      count.className = "group-enabled-count";
      count.textContent = enabled.size + "/" + globallyEnabled.size + " 个解析器启用";
      heading.append(identity, count);
      const total = document.createElement("label");
      total.className = "group-total-toggle";
      const totalText = document.createElement("span");
      totalText.className = "group-total-label";
      const totalTitle = document.createElement("strong");
      totalTitle.textContent = "本群总解析开关";
      const totalStatus = document.createElement("small");
      totalStatus.textContent = draft.parsing_enabled
        ? "整体解析已开启 · 可用解析器会参与匹配和仲裁"
        : "整体解析已关闭 · 等同于 /关闭解析；单项设置会保留";
      totalText.append(totalTitle, totalStatus);
      const totalSwitch = document.createElement("span");
      totalSwitch.className = "switch";
      const totalInput = document.createElement("input");
      totalInput.type = "checkbox";
      totalInput.checked = draft.parsing_enabled;
      totalInput.disabled = groupSaving;
      totalInput.setAttribute("aria-label", (group.name || group.group_id) + "：本群总解析开关");
      totalInput.addEventListener("change", () => {
        groupSaveResult = "";
        const current = groupDraft.get(key) || { enabled: new Set(), parsing_enabled: true };
        groupDraft.set(key, { ...current, parsing_enabled: totalInput.checked });
        totalStatus.textContent = totalInput.checked
          ? "整体解析已开启 · 可用解析器会参与匹配和仲裁"
          : "整体解析已关闭 · 等同于 /关闭解析；单项设置会保留";
        groupDirty = groupSettingsDifferFromSnapshot();
        updateGroupActions();
      });
      const totalTrack = document.createElement("span");
      totalTrack.className = "switch-track";
      totalTrack.setAttribute("aria-hidden", "true");
      totalSwitch.append(totalInput, totalTrack);
      total.append(totalText, totalSwitch);
      const controls = document.createElement("fieldset");
      controls.className = "group-parser-grid";
      const legend = document.createElement("legend");
      legend.textContent = "本群参与解析的服务";
      controls.append(legend);
      (Array.isArray(groupSnapshot.parsers) ? groupSnapshot.parsers : []).forEach((parser) => {
        const globalEnabled = parserMeta.get(parser.key)?.globally_enabled === true;
        const label = document.createElement("label");
        label.className = "group-parser-toggle";
        label.dataset.globalEnabled = String(globalEnabled);
        const text = document.createElement("span");
        text.className = "group-parser-label";
        const strong = document.createElement("strong");
        strong.textContent = parser.label;
        const status = document.createElement("small");
        status.textContent = globalEnabled ? (enabled.has(parser.key) ? "参与本群解析" : "本群已关闭") : "全局未启用";
        text.append(strong, status);
        const switchWrap = document.createElement("span");
        switchWrap.className = "switch";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = enabled.has(parser.key);
        input.disabled = !globalEnabled || groupSaving;
        input.setAttribute("aria-label", (group.name || group.group_id) + "：" + parser.label + (globalEnabled ? "" : "（需先全局启用）"));
        input.addEventListener("change", () => {
          groupSaveResult = "";
          const current = groupDraft.get(key) || { enabled: new Set(), parsing_enabled: true };
          const next = new Set(current.enabled);
          if (input.checked) next.add(parser.key);
          else next.delete(parser.key);
          groupDraft.set(key, { ...current, enabled: next });
          status.textContent = globalEnabled ? (input.checked ? "参与本群解析" : "本群已关闭") : "全局未启用";
          count.textContent = next.size + "/" + globallyEnabled.size + " 个解析器启用";
          groupDirty = groupSettingsDifferFromSnapshot();
          updateGroupActions();
        });
        const track = document.createElement("span");
        track.className = "switch-track";
        track.setAttribute("aria-hidden", "true");
        switchWrap.append(input, track);
        label.append(text, switchWrap);
        controls.append(label);
      });
      card.append(heading, total, controls);
      fragment.append(card);
    });
    list.append(fragment);
    updateGroupActions();
  }
  function updateGroupActions() {
    const state = $("group-settings-state");
    if (!state) return;
    state.textContent = groupLoading ? "正在读取 OneBot 群聊…"
      : groupLoadError ? groupLoadError
      : groupSaving ? "正在保存群聊解析器配置…"
      : groupDirty ? "群聊设置有未保存更改；点击“确认并保存”后生效。"
      : groupSaveResult || (groupSnapshot !== null ? "群聊设置已与磁盘配置同步。" : "尚未读取群聊列表。");
    state.dataset.error = String(Boolean(groupLoadError));
    const dirtyIndicator = $("group-dirty-indicator");
    if (dirtyIndicator) dirtyIndicator.hidden = !groupDirty || groupSaving;
    const unavailable = !nonce || groupSnapshot === null;
    const save = $("save-group-settings");
    const discard = $("discard-group-settings");
    if (save) save.disabled = unavailable || groupSaving || !groupDirty;
    if (discard) discard.disabled = unavailable || groupSaving || !groupDirty;
    if ($("refresh-groups")) $("refresh-groups").disabled = groupLoading || groupSaving;
    if ($("group-search")) $("group-search").disabled = groupSnapshot === null || groupLoading;
    document.querySelectorAll(".group-parser-grid input").forEach((input) => {
      input.disabled = input.closest(".group-parser-toggle")?.dataset.globalEnabled !== "true" || groupSaving;
    });
    document.querySelectorAll(".group-total-toggle input").forEach((input) => {
      input.disabled = groupSaving;
    });
  }
  async function loadGroupSettings({ discardDraft = false, preserveSaveResult = false } = {}) {
    if (!bridge || groupLoading || (groupDirty && !discardDraft)) return;
    let shouldRenderGroups = groupSnapshot === null;
    if (discardDraft && groupDirty && groupSnapshot !== null) {
      groupDraft = cloneGroupSettings(groupSnapshot.groups);
      groupDirty = false;
      shouldRenderGroups = true;
    }
    groupLoading = true;
    groupLoadError = "";
    if (!preserveSaveResult) groupSaveResult = "";
    if (groupSnapshot === null) renderGroups();
    else updateGroupActions();
    try {
      const endpoint = discardDraft
        ? "apple-music/group-settings-refresh"
        : "apple-music/group-settings";
      const data = await bridge.apiGet(endpoint);
      shouldRenderGroups = shouldRenderGroups || JSON.stringify(data) !== JSON.stringify(groupSnapshot);
      groupSnapshot = data;
      groupDraft = cloneGroupSettings(data.groups);
      groupDirty = false;
      groupLoadError = "";
    } catch (error) {
      groupLoadError = error.message || "读取群聊配置失败，请检查 OneBot 连接后重试。";
      if (discardDraft) groupDirty = false;
    } finally {
      groupLoading = false;
      if (shouldRenderGroups) renderGroupsPreservingView();
      else updateGroupActions();
    }
  }
  function refreshGroupSettings() {
    if (groupDirty && !window.confirm("刷新群聊列表会丢弃尚未保存的更改，是否继续？")) return;
    void loadGroupSettings({ discardDraft: true });
  }
  function discardGroupChanges() {
    if (groupSaving || groupSnapshot === null) return;
    groupDraft = cloneGroupSettings(groupSnapshot.groups);
    groupDirty = false;
    renderGroupsPreservingView();
  }
  async function saveGroupSettings() {
    if (!nonce || !groupDirty || groupSaving || groupSnapshot === null) return;
    const groups = (Array.isArray(groupSnapshot.groups) ? groupSnapshot.groups : []).map((group) => ({
      platform_id: group.platform_id, group_id: group.group_id,
      enabled: [...(groupDraft.get(groupKey(group))?.enabled || [])],
      parsing_enabled: groupDraft.get(groupKey(group))?.parsing_enabled !== false,
    }));
    groupSaving = true;
    groupLoadError = "";
    updateGroupActions();
    notice("正在保存群聊解析器设置…");
    try {
      const result = await bridge.apiPost("apple-music/save-group-settings", { groups, nonce });
      groupSnapshot = {
        ...groupSnapshot,
        groups: groupSnapshot.groups.map((group) => ({
          ...group,
          enabled: [...(groupDraft.get(groupKey(group))?.enabled || [])],
          parsing_enabled: groupDraft.get(groupKey(group))?.parsing_enabled !== false,
        })),
      };
      groupDraft = cloneGroupSettings(groupSnapshot.groups);
      groupDirty = false;
      const cleaned = Number.isInteger(result?.cleaned_group_count)
        ? result.cleaned_group_count : 0;
      const cleanedBlacklist = Number.isInteger(result?.cleaned_blacklist_group_count)
        ? result.cleaned_blacklist_group_count : 0;
      groupSaveResult = `群聊配置已保存；清理 ${cleaned} 个退群解析器配置、${cleanedBlacklist} 个黑名单群聊`;
      notice(groupSaveResult);
      await loadGroupSettings({ discardDraft: true, preserveSaveResult: true });
    } catch (error) {
      groupLoadError = error.message || "群聊配置保存失败，请重试。";
      notice(groupLoadError, true);
    } finally {
      groupSaving = false;
      renderGroupsPreservingView();
    }
  }

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
    const toggles = [
      musicToggle(key, "enable", "启用解析器", configured && parser.enabled === true, configured),
      musicToggle(key, "return_audio", "解析单曲时返回音频", pending?.return_audio ?? settings.return_audio !== false, configured),
      musicToggle(key, "show_playlist_cover", "显示歌单封面", pending?.show_playlist_cover ?? settings.show_playlist_cover !== false, configured),
      musicToggle(key, "show_playlist_url", "显示歌单原始链接", pending?.show_playlist_url ?? settings.show_playlist_url !== false, configured),
    ];
    if (key === "qqmusic" || key === "netease") {
      toggles.push(musicToggle(
        key,
        "parse_lyrics",
        "返回歌词解析",
        pending?.parse_lyrics ?? settings.parse_lyrics === true,
        configured,
      ));
    }
    fieldset.append(...toggles);

    const formatLabel = document.createElement("div");
    formatLabel.className = "music-format-setting";
    const formatTitle = document.createElement("span");
    formatTitle.textContent = "单曲音频发送模式";
    formatLabel.append(formatTitle);
    const format = document.createElement("select");
    format.setAttribute("aria-label", `${musicLabels[key]}单曲音频发送模式`);
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

    if (key === "qqmusic") {
      const qualityEditor = document.createElement("div");
      qualityEditor.className = "music-quality-editor";
      const qualityTitle = document.createElement("div");
      qualityTitle.className = "codec-title";
      const title = document.createElement("span");
      const strong = document.createElement("strong");
      strong.textContent = "文件音质优先级";
      const help = document.createElement("small");
      help.textContent = "仅文件模式使用；语音固定高音质优先并回退标准。QQ 接口暂无独立 Hi-Res 档。";
      title.append(strong, help);
      const add = document.createElement("button");
      add.type = "button";
      add.className = "add-button";
      add.dataset.fileQualityAdd = key;
      add.textContent = "＋ 添加";
      const list = document.createElement("div");
      list.className = "codec-list";
      list.dataset.fileQualityPriority = key;
      const qualities = pending?.file_quality_priority ?? settings.file_quality_priority
        ?? ["lossless", "high", "standard"];
      list.replaceChildren(...qualities.map((quality) => musicQualityRow(key, quality)));
      add.addEventListener("click", () => {
        const selected = readMusicSettings()[key]?.file_quality_priority || [];
        const next = qqQualityOptions.find(([value]) => !selected.includes(value));
        if (next) list.append(musicQualityRow(key, next[0]));
        updateMusicQualityControls(key);
        scheduleMusicSave();
      });
      qualityTitle.append(title, add);
      qualityEditor.append(qualityTitle, list);
      fieldset.append(qualityEditor);
    }

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
    if (key === "qqmusic") updateMusicQualityControls(key);
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
    document.querySelectorAll("[data-file-quality-priority]").forEach((list) => {
      const key = list.dataset.fileQualityPriority;
      result[key] ??= {};
      result[key].file_quality_priority = [...list.querySelectorAll("select")]
        .map((select) => select.value);
    });
    return result;
  }

  function musicQualityRow(parserKey, value) {
    const row = document.createElement("div");
    row.className = "codec-row";
    const handle = document.createElement("span");
    handle.className = "codec-order";
    handle.setAttribute("aria-hidden", "true");
    handle.textContent = "≡";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "QQ 音乐文件音质");
    qqQualityOptions.forEach(([optionValue, label]) => {
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
        const list = row.parentElement;
        if (!direction) {
          if (list.children.length > 1) row.remove();
        } else {
          const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
          if (sibling) list.insertBefore(direction < 0 ? row : sibling, direction < 0 ? sibling : row);
        }
        updateMusicQualityControls(parserKey);
        scheduleMusicSave();
      });
      controls.append(button);
    });
    row.append(handle, select, controls);
    select.addEventListener("change", () => {
      updateMusicQualityControls(parserKey);
      scheduleMusicSave();
    });
    return row;
  }

  function updateMusicQualityControls(parserKey) {
    const list = document.querySelector(`[data-file-quality-priority='${parserKey}']`);
    if (!list) return;
    const rows = [...list.querySelectorAll(".codec-row")];
    rows.forEach((row, index) => {
      row.querySelector("button[aria-label='上移']").disabled = index === 0;
      row.querySelector("button[aria-label='下移']").disabled = index === rows.length - 1;
      row.querySelector("button[aria-label='移除']").disabled = rows.length === 1;
    });
    const values = rows.map((row) => row.querySelector("select").value);
    const add = document.querySelector(`[data-file-quality-add='${parserKey}']`);
    if (add) add.disabled = values.length >= qqQualityOptions.length;
  }

  function invalidMusicQualityPriority(settings) {
    return Object.values(settings).some((parserSettings) => {
      const values = parserSettings?.file_quality_priority;
      return Array.isArray(values)
        && (!values.length || values.length !== new Set(values).size);
    });
  }

  function updateMusicSettingsState() {
    const state = $("music-settings-state");
    if (!state) return;
    const invalid = invalidMusicQualityPriority(readMusicSettings());
    state.textContent = invalid ? "同一音质不能重复添加。"
      : musicSaveFailed ? "自动保存失败；修改选项可重试。"
      : musicDirty ? "正在保存音乐解析设置…" : "音乐解析设置已同步到磁盘。";
    state.dataset.error = String(invalid || musicSaveFailed);
  }

  function scheduleMusicSave() {
    clearTimeout(musicTimer);
    const settings = readMusicSettings();
    if (invalidMusicQualityPriority(settings)) {
      musicDirty = false;
      updateMusicSettingsState();
      return;
    }
    if (!nonce || !savedMusic || JSON.stringify(settings) === savedMusic) return;
    musicDirty = true;
    updateMusicSettingsState();
    musicTimer = setTimeout(saveMusicSettings, 350);
  }

  function saveMusicSettings() {
    clearTimeout(musicTimer);
    if (busy || !nonce || !savedMusic) return;
    const settings = readMusicSettings();
    if (invalidMusicQualityPriority(settings)) {
      musicDirty = false;
      updateMusicSettingsState();
      return;
    }
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
      force_chinese_language: $("setting-chinese-language").checked,
    };
  }
  function renderSettings(settings) {
    if (!settings) return;
    if (activeAction === "save-settings" || settingsDirty || changedSettings()) return;
    $("setting-backend").checked = Boolean(settings.backend_enabled);
    $("setting-return-audio").checked = Boolean(settings.return_audio);
    $("setting-cover").checked = settings.show_playlist_cover !== false;
    $("setting-url").checked = settings.show_playlist_url !== false;
    $("setting-chinese-language").checked = settings.force_chinese_language === true;
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
    // The Logo may have changed in AstrBot's plugin configuration while this page stayed open.
    void loadPluginLogo();
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
  $("refresh-groups")?.addEventListener("click", refreshGroupSettings);
  $("group-search")?.addEventListener("input", () => renderGroups());
  $("save-group-settings")?.addEventListener("click", () => saveGroupSettings());
  $("discard-group-settings")?.addEventListener("click", () => discardGroupChanges());
  window.addEventListener("focus", () => {
    if (!bridge) return;
    void loadPluginLogo();
    if (!groupDirty && !groupLoading && $("groups")?.hidden === false) {
      void loadGroupSettings();
    }
  });
  window.addEventListener("pageshow", () => {
    if (!bridge) return;
    if (!groupDirty && !groupLoading && $("groups")?.hidden === false) {
      void loadGroupSettings();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && bridge && !groupDirty && !groupLoading
      && $("groups")?.hidden === false) {
      void loadGroupSettings();
    }
  });
  const groupRefreshTimer = window.setInterval(() => {
    if (bridge && !groupDirty && !groupLoading && !groupSaving
      && document.visibilityState === "visible" && $("groups")?.hidden === false) {
      void loadGroupSettings();
    }
  }, 5000);
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
    clearInterval(groupRefreshTimer);
    $("password").value = "";
    $("code").value = "";
  });
  initializePageStyle();
  initializePageTheme();
  initializeNavigation();
  if (!bridge) {
    notice("请从 AstrBot 插件详情打开此页，不要直接打开 HTML 文件。", true);
    $("refresh").disabled = true;
    return;
  }
  const readyTimeout = setTimeout(() => notice("尚未连接后台页面桥接，请从支持 Plugin Pages 的 AstrBot 后台打开此页。", true), 8000);
  bridge.ready().then(() => { clearTimeout(readyTimeout); refresh(); });
})();

// Keep native selects as the single source of truth for the existing settings logic.
// The visible control is enhanced only when JavaScript is available.
(() => {
  const enhanced = new WeakMap();
  let opened = null;
  let menuId = 0;

  function closeMenu(restoreFocus = false) {
    if (!opened) return;
    const state = opened;
    opened = null;
    state.trigger.setAttribute("aria-expanded", "false");
    state.trigger.removeAttribute("aria-controls");
    state.menu?.remove();
    state.menu = null;
    if (restoreFocus && state.trigger.isConnected) state.trigger.focus({ preventScroll: true });
  }

  function syncSelect(state) {
    const selected = state.select.selectedOptions[0];
    const label = selected?.textContent.trim() || "请选择";
    if (state.value.textContent !== label) state.value.textContent = label;
    const accessibleName = `${state.name}，当前：${label}`;
    if (state.trigger.getAttribute("aria-label") !== accessibleName) {
      state.trigger.setAttribute("aria-label", accessibleName);
    }
    const disabled = state.select.matches(":disabled");
    if (state.trigger.disabled !== disabled) state.trigger.disabled = disabled;
    if (disabled && opened === state) closeMenu();
    if (opened === state) {
      state.menu.querySelectorAll("[role='option']").forEach((option, index) => {
        const isSelected = index === state.select.selectedIndex;
        option.setAttribute("aria-selected", String(isSelected));
      });
    }
  }

  function positionMenu(state) {
    if (!state.menu) return;
    const rect = state.trigger.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
      closeMenu();
      return;
    }
    const margin = 8;
    const width = Math.min(window.innerWidth - margin * 2, Math.max(rect.width, 224));
    const below = window.innerHeight - rect.bottom - margin - 6;
    const above = rect.top - margin - 6;
    const openAbove = below < 176 && above > below;
    state.menu.style.width = `${width}px`;
    state.menu.style.left = `${Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))}px`;
    state.menu.style.maxHeight = `${Math.max(48, Math.min(280, openAbove ? above : below))}px`;
    state.menu.style.top = `${openAbove ? rect.top - state.menu.offsetHeight - 6 : rect.bottom + 6}px`;
  }

  function focusOption(state, index) {
    if (!state.menu) return;
    const options = [...state.menu.querySelectorAll("[role='option']")];
    if (!options.length) return;
    const target = Math.max(0, Math.min(index, options.length - 1));
    const candidate = options[target].disabled
      ? options.find((option) => !option.disabled)
      : options[target];
    candidate?.focus({ preventScroll: true });
    candidate?.scrollIntoView({ block: "nearest" });
  }

  function commitOption(state, index) {
    const option = state.select.options[index];
    if (!option || option.disabled || state.select.matches(":disabled")) return;
    closeMenu(true);
    if (state.select.value !== option.value) {
      state.select.value = option.value;
      state.select.dispatchEvent(new Event("input", { bubbles: true }));
      state.select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    syncSelect(state);
  }

  function openMenu(state) {
    if (state.select.matches(":disabled")) return;
    if (opened) closeMenu();
    const menu = document.createElement("div");
    menu.id = `custom-select-menu-${++menuId}`;
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", state.name);
    [...state.select.options].forEach((option, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "custom-select-option";
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(index === state.select.selectedIndex));
      item.tabIndex = -1;
      item.disabled = option.disabled;
      item.textContent = option.textContent;
      item.addEventListener("click", () => commitOption(state, index));
      menu.append(item);
    });
    menu.addEventListener("keydown", (event) => {
      const options = [...menu.querySelectorAll("[role='option']")];
      const current = options.indexOf(document.activeElement);
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      } else if (event.key === "Tab") {
        closeMenu();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
        event.preventDefault();
        const step = event.key === "ArrowUp" ? -1 : 1;
        let next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : current;
        for (let count = 0; count < options.length; count++) {
          next = event.key === "Home" || event.key === "End" ? next : (next + step + options.length) % options.length;
          if (!options[next].disabled) break;
          if (event.key === "Home") next = Math.min(next + 1, options.length - 1);
          if (event.key === "End") next = Math.max(next - 1, 0);
        }
        focusOption(state, next);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        commitOption(state, current);
      } else if (event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
        const match = options.findIndex((option, index) => index > current && !option.disabled && option.textContent.trim().toLocaleLowerCase().startsWith(event.key.toLocaleLowerCase()));
        const first = match < 0 ? options.findIndex((option) => !option.disabled && option.textContent.trim().toLocaleLowerCase().startsWith(event.key.toLocaleLowerCase())) : match;
        if (first >= 0) {
          event.preventDefault();
          focusOption(state, first);
        }
      }
    });
    state.menu = menu;
    opened = state;
    document.body.append(menu);
    state.trigger.setAttribute("aria-controls", menu.id);
    state.trigger.setAttribute("aria-expanded", "true");
    positionMenu(state);
    focusOption(state, state.select.selectedIndex);
  }

  function enhanceSelect(select) {
    if (enhanced.has(select) || select.hidden) return;
    const wrapper = document.createElement("span");
    wrapper.className = "custom-select";
    select.before(wrapper);
    wrapper.append(select);
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "custom-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const value = document.createElement("span");
    value.className = "custom-select-value";
    const arrow = document.createElement("span");
    arrow.className = "custom-select-arrow";
    arrow.setAttribute("aria-hidden", "true");
    trigger.append(value, arrow);
    wrapper.append(trigger);
    const state = { select, trigger, value, menu: null, name: select.getAttribute("aria-label") || select.name || "选择" };
    enhanced.set(select, state);
    select.classList.add("custom-select-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.addEventListener("change", () => syncSelect(state));
    trigger.addEventListener("click", () => opened === state ? closeMenu() : openMenu(state));
    trigger.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        openMenu(state);
      }
    });
    syncSelect(state);
  }

  document.querySelectorAll("select").forEach(enhanceSelect);
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "childList") {
        for (const node of record.addedNodes) {
          if (!(node instanceof Element)) continue;
          if (node.matches("select")) enhanceSelect(node);
          node.querySelectorAll("select").forEach(enhanceSelect);
        }
      } else if (record.target instanceof HTMLSelectElement) {
        const state = enhanced.get(record.target);
        if (state) syncSelect(state);
      } else if (record.target instanceof HTMLFieldSetElement) {
        record.target.querySelectorAll("select").forEach((select) => {
          const state = enhanced.get(select);
          if (state) syncSelect(state);
        });
      } else if (record.target instanceof HTMLOptionElement) {
        const state = enhanced.get(record.target.closest("select"));
        if (state) syncSelect(state);
      }
    }
    if (opened && !opened.select.isConnected) closeMenu();
  }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "selected"] });
  document.addEventListener("pointerdown", (event) => {
    if (opened && !opened.menu.contains(event.target) && !opened.trigger.contains(event.target)) closeMenu();
  }, true);
  window.addEventListener("resize", () => { if (opened) positionMenu(opened); });
  window.addEventListener("scroll", () => { if (opened) positionMenu(opened); }, true);
})();
