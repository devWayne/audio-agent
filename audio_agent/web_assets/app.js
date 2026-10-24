"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const state = {
    token: null, status: null, page: "document", view: "clips", query: "", filter: "all", offset: 0,
    limit: 50, items: [], total: 0, loading: true, listError: null, connected: false,
    selectedKey: null, clip: null, transcript: null, selectionVersion: 0,
    listVersion: 0, clipCache: new Map(), jobs: new Map(), importing: false,
    polling: false, language: "auto", transcriptLoading: false, initial: true,
    live: null, devices: [], devicesLoading: false, livePending: null,
    liveActionError: null, deviceError: null,
    lastPollAt: 0,
    gateSettings: null, gateDraftDb: -44.4, gateDirty: false, gateSaving: false,
    gateLoading: true, gatePending: false, gateError: null, gateRevision: 0, gateReadVersion: 0,
    vadEnabled: true, speakerStatus: null, speakerStatusError: null,
    speakerAnalysis: null, speakerLoading: false, speakerError: null,
    speakerJobs: new Map(), speakerSubmitting: false, speakerNumber: "auto",
    speakerConfirmId: null, speakerConfirmAnalysisId: null, speakerConfirmed: false, voiceprintBusy: false,
    voiceprintError: null, profileRevision: 0, speakerReadVersion: 0, transcriptReadVersion: 0,
    documentItems: [], documentRevision: null, documentSource: undefined,
    documentVersion: 0, documentLoading: false, documentReady: false,
    documentError: null, documentPending: null, documentNodes: new Map(),
    automaticSaving: false, automaticDraft: null, automaticError: null, automaticFeedback: null,
    speakerPreviews: new Set(), documentSpeakerOpening: null,
    profiles: [], profilesReady: false, profilesError: null, profilesReadVersion: 0,
    profileEditId: null, profileDraft: {name:"", note:""}, profileRemoveId: null,
    enrollKind: "self", enrollDraft: {name:"", note:""}, enrollReplaceId: null,
    enrollSelfMode: "append", selfSampleRemoveId: null,
    peopleClips: [], peopleTotal: 0, peopleOffset: 0, peopleLimit: 12, peopleQuery: "",
    peopleClipsLoading: false, peopleClipsError: null, peopleClipsVersion: 0,
    storage: null, storageDraft: null, storageLoading: false, storageSaving: false,
    storageError: null, storageReadError: null, storageSuccess: null, storageRevision: 0, storageReadVersion: 0,
    mcp: null, mcpLoading: false, mcpError: null, mcpReadVersion: 0, mcpSetupClient: "codex",
  };
  let searchTimer;
  let peopleSearchTimer;
  let dragDepth = 0;
  let stopSpeakerPreview = null;

  async function api(path, options = {}, retried = false) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") {
      if (state.storageSaving && path !== "/api/storage") throw new Error("正在复制资料并切换目录，请等待完成后再操作。");
      if (!state.token) throw new Error("本地服务尚未连接，请稍后重试。");
      headers.set("X-Audio-Agent-Token", state.token);
    }
    let response;
    try {
      response = await fetch(path, {...options, headers, credentials: "same-origin", cache: "no-store"});
    } catch (_) {
      throw new Error("无法连接本地服务，请确认 Audio Agent 仍在运行。");
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error("本地服务返回了无法读取的结果，请刷新后重试。"); }
    if (!response.ok) {
      if (response.status === 403 && data?.error?.code === "invalid_token" && !retried) {
        const session = await api("/api/session");
        state.token = session.token;
        return api(path, options, true);
      }
      const error = new Error(data?.error?.message || data?.error || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function toast(message, type = "success", timeout = 5000) {
    const element = document.createElement("div");
    element.className = `toast ${type === "error" ? "error" : ""}`;
    element.innerHTML = `${icon(type === "error" ? "alert" : "check")}<span></span><button aria-label="关闭提示">${icon("close")}</button>`;
    $("span", element).textContent = String(message).slice(0, 400);
    $("button", element).addEventListener("click", () => element.remove());
    $("#toast-region").append(element);
    setTimeout(() => element.remove(), timeout);
  }

  function formatTime(seconds) {
    const number = Math.max(0, Number(seconds) || 0);
    const h = Math.floor(number / 3600), m = Math.floor((number % 3600) / 60), s = Math.floor(number % 60);
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function formatDate(value, full = false) {
    if (value === undefined || value === null) return "时间未记录";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    if (!Number.isFinite(date.getTime())) return "时间未记录";
    return new Intl.DateTimeFormat("zh-CN", {
      ...(full ? {year: "numeric"} : {}), month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  }

  function clipName(clip) {
    return clip?.label || clip?.display_name || clip?.original_name || clip?.filename || `录音 ${String(clip?.id || "").slice(0, 8)}`;
  }

  function sourceName(source) {
    if (!source) return "本地录音";
    if (/^avfoundation:/.test(source)) return source.replace(/^avfoundation:/, "");
    if (/^live:/.test(source)) return "麦克风声音片段";
    if (/^(web[-_:]?import|import|file)(:|$)/i.test(source)) return "本地导入";
    return String(source).split(/[\\/]/).pop() || "本地录音";
  }

  function transcriptName(item) {
    const clip = state.clipCache.get(item.clip_id);
    return clip ? clipName(clip) : item.label || item.display_name || (item.clip_id ? `录音 ${String(item.clip_id).slice(0, 8)}` : "按需转写");
  }

  function activeJob(clipId) {
    return Array.from(state.jobs.values()).find(job => job.clip_id === clipId && ["queued", "running"].includes(job.status));
  }

  function latestJob(clipId) {
    return Array.from(state.jobs.values()).filter(job => job.clip_id === clipId).sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
  }

  function badge(clip) {
    const job = activeJob(clip.id);
    if (job) return `<span class="badge running">${job.status === "queued" ? "等待中" : "转写中"}</span>`;
    if (clip.transcribed) return `<span class="badge complete">${icon("check")}已转写</span>`;
    return `<span class="badge">待转写</span>`;
  }

  function setConnection(connected, message) {
    const changed = state.connected !== connected;
    state.connected = connected;
    $("#connection").innerHTML = `<span class="status-dot ${connected ? "" : "error"}"></span><span>${connected ? "本地服务已连接" : "连接已断开"}</span>`;
    $("#connection-error").hidden = connected;
    if (!connected) $("#connection-error-text").textContent = message || "无法连接本地服务，请确认 Audio Agent 仍在运行。";
    if (changed) { renderSpeakerSettings(); updateDocumentSpeakerStates(); if (state.page === "storage") renderStorage(); }
  }

  function renderStatus() {
    if (!state.status) return;
    const {storage = {}, capture = {}, models = [], transcript_count = 0} = state.status;
    const count = Number(storage.clip_count) || 0;
    const seconds = Number(storage.duration) || 0;
    $("#stat-clips").textContent = count.toLocaleString();
    $("#stat-transcripts").textContent = Number(transcript_count).toLocaleString();
    let duration = "0", unit = "秒";
    if (seconds < 60) duration = String(Math.round(seconds));
    else if (seconds < 3600) { duration = (seconds / 60).toFixed(1); unit = "分钟"; }
    else { duration = (seconds / 3600).toFixed(1); unit = "小时"; }
    $("#stat-duration").textContent = duration;
    $("#stat-duration-unit").textContent = unit;
    $("#nav-clip-count").textContent = count;
    $("#nav-transcript-count").textContent = transcript_count;
    $("#tab-clip-count").textContent = count;
    $("#tab-transcript-count").textContent = transcript_count;
    const recording = capture.state === "recording" && (capture.active || capture.running);
    const listening = recording && state.live?.activity_state === "listening";
    const captureLabel = listening ? "正在监听，等待声音" : recording ? "声音活动，正在录音" : capture.state === "starting" ? "监听启动中" : capture.state === "error" ? "监听出现错误" : "监听未启动";
    $("#capture-state").innerHTML = `<span class="status-dot ${recording ? listening ? "" : "live" : "muted"}"></span><span>${captureLabel}</span>`;
    $("#capture-detail").textContent = capture.last_error || (recording ? (capture.device || "已连接的录音设备") : "开始监听后，有声音的片段会进入资料库。");
    $("#model-indicator").classList.toggle("ready", models.length > 0);
    $("#model-name").textContent = models.length ? String(models[0].name || "本地模型已就绪").replace(/^ggml-/, "").replace(/\.bin$/, "") : "未找到识别模型";
    $("#model-name").title = models.length ? models[0].name : "需要在本机配置兼容的 Whisper 模型";
    $("#storage-note").textContent = storage.retention_hours ? `录音保留 ${storage.retention_hours} 小时 · 仅本地存储` : "WAV 录音 · 按需转写";
    $("#library-disclosure-summary").textContent = `${count} 个录音 · 收听、导入与说话人分析`;
    $("#document-retention").textContent = `完整展示当前保留的转写${storage.retention_hours ? `（${storage.retention_hours} 小时）` : ""}；需要长期保存的文字，请下载全文。`;
    renderLive();
  }

  function liveIsBusy() {
    return !!state.livePending || !!state.live?.active || ["starting", "recording", "stopping"].includes(state.live?.state);
  }

  function renderVad() {
    const live = state.live || {};
    const busy = liveIsBusy();
    const enabled = busy && state.livePending !== "start" && typeof live.vad_enabled === "boolean" ? live.vad_enabled : state.vadEnabled;
    $("#vad-enabled").checked = enabled;
    $("#vad-enabled").disabled = busy;
    const status = state.status?.vad;
    const probability = typeof live.vad_probability === "number" && Number.isFinite(live.vad_probability)
      ? ` · 当前语音概率 ${Math.round(Math.min(1, Math.max(0, live.vad_probability)) * 100)}%` : "";
    $("#vad-state").textContent = !enabled ? "仅声音门槛 · 未启用语音检测"
      : busy && live.vad_ready && live.state === "recording" ? `Silero 本地语音检测运行中${probability}`
      : busy && live.state === "stopping" ? "语音检测正在停止"
      : busy ? "正在准备本次语音检测…"
      : !status ? "正在检查本地语音模型…"
      : status.available ? "Silero 模型可用 · 待启动"
      : `模型暂不可用${status.reason ? `：${status.reason}` : ""}`;
  }

  function amplitudeDb(level) {
    return typeof level === "number" && Number.isFinite(level) && level > 0 ? 20 * Math.log10(level) : -Infinity;
  }

  function dbLabel(value) {
    return Number.isFinite(value) ? `${value.toFixed(1).replace("-", "−")} dBFS` : "−∞ dBFS";
  }

  function appliedTriggerDb() {
    if (typeof state.live?.trigger_db === "number" && Number.isFinite(state.live.trigger_db)) return state.live.trigger_db;
    if (typeof state.live?.trigger_threshold === "number" && state.live.trigger_threshold > 0) return amplitudeDb(state.live.trigger_threshold);
    return state.gateSettings?.trigger_db ?? -44.4;
  }

  function renderGate() {
    const configured = state.gateSettings?.trigger_db;
    const busy = liveIsBusy();
    const applied = appliedTriggerDb();
    const slider = $("#gate-range");
    // Preserve exact saved values, including legacy -44.43697 dBFS. The native
    // slider rounds its visual position only; loading never writes a setting.
    slider.value = String(state.gateDraftDb);
    slider.setAttribute("aria-valuetext", `触发门槛 ${dbLabel(state.gateDraftDb)}`);
    slider.disabled = state.gateLoading || state.gateSaving || !state.gateSettings;
    $("#gate-value").innerHTML = `${escapeHTML(state.gateDraftDb.toFixed(1).replace("-", "−"))} <span>dBFS</span>`;
    $("#gate-applied").textContent = state.gateSettings
      ? busy ? `当前生效 ${dbLabel(applied)} · 已保存 ${dbLabel(configured)}` : `已保存 ${dbLabel(configured)} · 下次监听使用`
      : state.gateLoading ? "正在读取当前设置…" : "尚未读取门槛设置";
    $$('[data-gate-db]').forEach(button => {
      const selected = Math.abs(Number(button.dataset.gateDb) - state.gateDraftDb) < .05;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
      button.disabled = slider.disabled;
    });
    const saveButton = $("#gate-save");
    const retryRead = !state.gateSettings && !!state.gateError;
    saveButton.disabled = state.gateLoading || state.gateSaving || !state.connected || (!retryRead && !state.gateDirty);
    saveButton.textContent = state.gateSaving ? "正在保存…" : retryRead ? "重新读取" : "保存门槛";
    $("#gate-feedback").textContent = state.gateLoading ? "正在读取门槛…"
      : state.gateSaving ? "正在保存…"
      : state.gateDirty ? "尚未保存，当前门槛保持不变"
      : state.gatePending ? busy ? "已保存，当前声段结束后生效" : "已保存，下次监听时生效"
      : state.gateSettings ? "已保存" : "设置暂不可用";
    $("#gate-error").hidden = !state.gateError;
    $("#gate-error").textContent = state.gateError || "";
  }

  async function loadGateSettings() {
    if (state.gateSaving) return;
    const revision = state.gateRevision, readVersion = ++state.gateReadVersion;
    state.gateLoading = true; state.gateError = null; renderGate();
    try {
      const result = await api("/api/live/settings");
      if (revision !== state.gateRevision || readVersion !== state.gateReadVersion || state.gateSaving) return;
      if (!validGateDb(result.settings?.trigger_db)) throw new Error("门槛设置返回格式无效，请重新读取。");
      state.gateSettings = result.settings;
      state.gatePending = !!result.pending;
      if (!state.gateDirty) state.gateDraftDb = result.settings.trigger_db;
    } catch (error) {
      if (revision === state.gateRevision && readVersion === state.gateReadVersion && !state.gateSaving) state.gateError = error.message;
    } finally {
      if (revision === state.gateRevision && readVersion === state.gateReadVersion) { state.gateLoading = false; renderGate(); }
    }
  }

  function validGateDb(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= -60 && value <= -18;
  }

  function changeGateDraft(value) {
    if (!Number.isFinite(value) || value < -60 || value > -18) return;
    state.gateDraftDb = value;
    state.gateDirty = !state.gateSettings || Math.abs(value - state.gateSettings.trigger_db) > .000001;
    state.gateError = null; renderGate();
  }

  async function saveGateSettings() {
    if (!state.gateSettings && state.gateError) { loadGateSettings(); return; }
    if (state.gateSaving || !state.gateDirty) return;
    const value = state.gateDraftDb;
    state.gateRevision++; state.gateLoading = false;
    state.gateSaving = true; state.gateError = null; renderGate();
    try {
      const result = await api("/api/live/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({trigger_db:value})});
      if (!validGateDb(result.settings?.trigger_db)) throw new Error("未能确认门槛保存结果，请重新读取设置。");
      state.gateSettings = result.settings; state.gateDraftDb = result.settings.trigger_db;
      state.gatePending = !!result.pending; state.gateDirty = false;
      if (state.live) {
        state.live.configured_trigger_db = result.settings.trigger_db;
        state.live.configured_trigger_threshold = result.settings.trigger_threshold;
        state.live.settings_pending = !!result.pending;
        if (!result.pending) Object.assign(state.live, result.settings);
      }
      toast(result.pending ? "门槛已保存，将在当前声段结束后生效。" : "门槛已保存。");
    } catch (error) { state.gateError = error.message; }
    finally { state.gateRevision++; state.gateSaving = false; renderGate(); }
  }

  async function loadDevices() {
    if (state.devicesLoading) return;
    state.devicesLoading = true; state.deviceError = null;
    $("#refresh-devices").classList.add("is-loading");
    renderLive();
    try {
      const response = await api("/api/devices");
      state.devices = response.items || [];
      const select = $("#live-device");
      const previous = select.value;
      select.replaceChildren();
      state.devices.forEach(device => {
        const option = document.createElement("option");
        option.value = String(device.name);
        option.textContent = device.name;
        select.append(option);
      });
      if (!state.devices.length) {
        const option = document.createElement("option"); option.value = ""; option.textContent = "未找到可用麦克风"; select.append(option);
        state.deviceError = "没有找到麦克风。请连接输入设备，并在系统设置中检查麦克风权限后刷新设备列表。";
      } else {
        const preferred = state.devices.find(device => /MacBook/i.test(device.name) && /麦克风|microphone|built.in|内置/i.test(device.name))
          || state.devices.find(device => /内置.*麦克风|built.in.*microphone/i.test(device.name));
        const current = state.devices.find(device => device.name === (liveIsBusy() ? state.live?.device : previous));
        select.value = String((current || preferred || state.devices[0]).name);
      }
    } catch (error) {
      state.deviceError = `${error.message} 请检查系统设置中的麦克风权限，然后刷新设备列表。`;
      if (!state.devices.length) $("#live-device").innerHTML = '<option value="">无法读取麦克风</option>';
    } finally { state.devicesLoading = false; $("#refresh-devices").classList.remove("is-loading"); renderLive(); }
  }

  function renderLive() {
    const live = state.live || {};
    const phase = state.livePending === "start" ? "starting" : state.livePending === "stop" ? "stopping" : live.state || "finished";
    const busy = liveIsBusy();
    const started = !!live.session_id;
    const running = phase === "recording";
    const listening = running && live.activity_state === "listening";
    const soundActive = running && !listening;
    const labels = {starting:"正在启动", recording:listening ? "监听中" : "录音中", stopping:"正在收尾", finished:started ? "监听已结束" : "尚未开始", error:"监听异常"};
    $("#live-card").classList.toggle("is-recording", soundActive);
    $("#live-status").innerHTML = `<span class="status-dot ${soundActive ? "live" : listening ? "" : phase === "error" ? "error" : "muted"}"></span><span>${labels[phase] || "尚未开始"}</span>`;
    const elapsed = typeof live.observed_seconds === "number" ? live.observed_seconds : busy && live.started_at && phase !== "stopping" ? Math.max(0, Date.now() / 1000 - live.started_at) : Number(live.audio_seconds) || 0;
    $("#live-timer").textContent = formatTime(elapsed);
    $("#live-timer").title = "本次监听时长";
    $("#live-device").disabled = busy || state.devicesLoading || !state.devices.length;
    $("#live-language").disabled = busy;
    $("#refresh-devices").disabled = busy || state.devicesLoading;
    if (busy && live.device && state.devices.some(device => device.name === live.device)) $("#live-device").value = live.device;
    if (busy && live.language) $("#live-language").value = live.language;
    $("#live-start").hidden = busy;
    $("#live-start").disabled = !state.connected || state.devicesLoading || !state.devices.length || !(state.status?.models || []).length || (state.vadEnabled && !state.status?.vad?.available);
    $("#live-stop").hidden = !busy;
    $("#live-stop").disabled = phase === "stopping" || !!state.livePending;
    $("#live-stop").innerHTML = `${phase === "stopping" ? '<span class="spinner" aria-hidden="true"></span>' : icon("stop")}<span>${phase === "stopping" ? "停止监听，正在收尾…" : "停止监听"}</span>`;
    const pending = Number(live.pending_count) || 0;
    const clips = Number(live.clips_count) || 0;
    const transcribed = Number(live.transcribed_count) || 0;
    $("#live-progress").textContent = started ? `${clips} 个声音片段 · ${transcribed} 条转写${pending ? ` · ${pending} 段等待识别` : ""}` : "尚未监听";
    const error = state.liveActionError || live.last_error || state.deviceError;
    $("#live-error").hidden = !error;
    $("#live-error").textContent = error ? String(error).slice(0, 700) : "";
    const freshLevel = typeof live.last_audio_at !== "number" || Date.now() / 1000 - live.last_audio_at <= 3;
    const hasLevel = typeof live.audio_level === "number" && Number.isFinite(live.audio_level) && phase === "recording" && freshLevel;
    $("#live-signal").hidden = !hasLevel;
    if (hasLevel) {
      const meanDb = amplitudeDb(live.audio_level);
      const hasPeak = typeof live.audio_peak_level === "number" && Number.isFinite(live.audio_peak_level);
      const peakDb = hasPeak ? amplitudeDb(live.audio_peak_level) : -Infinity;
      const triggerDb = appliedTriggerDb();
      const percent = db => `${Math.min(100, Math.max(0, (db + 80) / 80 * 100))}%`;
      $("#live-audio-level").value = Math.min(0, Math.max(-80, meanDb));
      $("#signal-average").textContent = dbLabel(meanDb);
      $("#signal-average-bar").style.width = percent(meanDb);
      $("#signal-peak-wrapper").hidden = !hasPeak;
      $("#signal-peak-bar").hidden = !hasPeak;
      if (hasPeak) {
        $("#signal-peak").textContent = dbLabel(peakDb);
        $("#signal-peak-bar").style.width = percent(peakDb);
      }
      $("#signal-threshold-marker").style.left = percent(triggerDb);
      $("#signal-threshold-marker").title = `当前生效门槛 ${dbLabel(triggerDb)}`;
      $("#signal-threshold-label").textContent = `当前生效 ${dbLabel(triggerDb)}`;
      $("#live-signal-label").textContent = hasPeak ? peakDb >= triggerDb ? "最近有声音达到门槛" : "最近声音低于触发门槛" : listening ? "等待声音" : "声音片段录制中";
    }
    const endSilence = typeof live.end_silence_seconds === "number" ? live.end_silence_seconds : .8;
    $("#live-footnote").textContent = phase === "stopping" ? "麦克风正在停止，已保存的声音片段会继续完成本地识别。"
      : busy ? `安静约 ${endSilence} 秒后结束声段并转写；静音不生成文件${pending ? "，已保存的片段仍会继续识别" : ""}。关闭网页不会停止监听，请使用「停止监听」。`
      : started ? "完整转写已保存在下方文档；录音可在底部展开收听。"
      : !(state.status?.models || []).length && state.status ? "需要先配置本地识别模型，才能开始监听并自动转写。" : "静音不会新建录音或转写任务。声音与文字均保存在本机。";
    renderGate();
    renderVad();
  }

  function documentSource() {
    return $("#document-scope").value === "session" ? (state.live?.session_id ? `live:${state.live.session_id}` : "") : null;
  }

  function documentSelectionActive() {
    const selection = window.getSelection();
    return selection && !selection.isCollapsed && selection.rangeCount
      && selection.getRangeAt(0).intersectsNode($("#document-body"));
  }

  function renderDocumentState() {
    const count = state.documentItems.length;
    const characters = state.documentItems.reduce((sum, item) => sum + item.text.length, 0);
    const complete = state.documentReady && !state.documentError && !state.documentPending;
    $("#document-summary").textContent = !state.documentReady ? "正在读取完整转写…"
      : state.documentSource === "" ? "当前没有监听会话，可切换到「全部记录」查看已保存的文字。"
      : `${count.toLocaleString()} 段 · ${characters.toLocaleString()} 字符 · 最新在前${state.documentError ? " · 更新失败" : state.documentPending ? " · 有更新待显示" : ""}`;
    $("#document-copy-all").disabled = !complete || !count;
    $("#document-download").disabled = !complete || !count;
    $("#document-refresh").disabled = state.documentLoading;
    $("#document-refresh").classList.toggle("is-loading", state.documentLoading);
    $("#document-loading").hidden = !state.documentLoading || state.documentReady;
    $("#document-error").hidden = !state.documentError;
    $("#document-error").textContent = state.documentError ? `${state.documentError} ${count ? "已显示的文字保留，全文复制将在成功读取后恢复。" : ""}` : "";
    $("#document-empty").hidden = !state.documentReady || !!count || !!state.documentError;
  }

  function makeDocumentEntry(item) {
    const entry = document.createElement("article");
    entry.className = "document-entry";
    entry.dataset.transcriptId = item.id;
    entry.innerHTML = '<div class="document-entry-meta"><time class="document-entry-time"></time><small></small></div><div class="document-entry-body"></div>';
    $("time", entry).textContent = AudioDocument.timeRange(item);
    const recordedAt = new Date(item.start_time * 1000);
    if (Number.isFinite(recordedAt.getTime())) $("time", entry).dateTime = recordedAt.toISOString();
    $("small", entry).textContent = ({live:"麦克风", microphone:"麦克风", import:"导入录音", recent:"最近音频"})[item.source_kind] || "本地录音";
    $("small", entry).title = item.display_name || "";
    const speakerState = document.createElement("span"); speakerState.className = "document-speaker-state";
    $(".document-entry-meta", entry).append(speakerState);
    const body = $(".document-entry-body", entry);
    if (!item.text.trim()) {
      const empty = document.createElement("p"); empty.className = "document-paragraph document-no-text";
      empty.textContent = "这段录音未识别到文字。"; body.append(empty);
    } else for (const paragraph of AudioDocument.paragraphs(item)) {
      if (paragraph.label) {
        const label = document.createElement("span"); label.className = "document-speaker";
        label.textContent = paragraph.label; body.append(label);
      }
      const text = document.createElement("p"); text.className = "document-paragraph";
      text.textContent = paragraph.text; body.append(text);
    }
    const actions = document.createElement("div"); actions.className = "document-entry-actions";
    const copy = document.createElement("button"); copy.type = "button";
    copy.innerHTML = `${icon("copy")}复制`; copy.title = "复制这段完整文字"; copy.disabled = !item.text;
    copy.addEventListener("click", () => copyText(item.text, "这段完整文字已复制")); actions.append(copy);
    if (item.clip_id && item.recording_available) {
      const recording = document.createElement("button"); recording.type = "button";
      recording.textContent = "录音"; recording.title = "查看并收听录音，不启动分析";
      recording.addEventListener("click", () => { openLibrary(); selectTranscript(item, false); $("#detail-panel").scrollIntoView({behavior:"smooth", block:"start"}); });
      actions.append(recording);
      const speakers = document.createElement("button"); speakers.type = "button"; speakers.className = "document-speaker-action";
      speakers.addEventListener("click", () => openDocumentSpeakers(item)); actions.append(speakers);
    }
    body.append(actions); updateDocumentSpeakerEntry(entry, item); return entry;
  }

  function updateDocumentSpeakerEntry(entry, item) {
    const job = item.clip_id ? speakerJob(item.clip_id) : null;
    const running = ["queued", "running"].includes(job?.status)
      || state.documentSpeakerOpening === item.id
      || (state.speakerSubmitting && selectedClipId() === item.clip_id);
    const analyzed = Number.isInteger(item.speaker_count) && item.speaker_count >= 0;
    const labels = [...new Set((item.segments || []).flatMap(segment => Array.isArray(segment.speaker_labels)
      ? segment.speaker_labels.filter(label => typeof label === "string") : []))];
    const marker = $(".document-speaker-state", entry);
    marker.textContent = running ? job?.status === "queued" ? "等待区分…" : state.documentSpeakerOpening === item.id && !job ? "正在打开…" : "分析中…"
      : job?.status === "failed" ? analyzed ? "更新失败 · 保留原标注" : "区分失败"
      : analyzed ? item.speaker_count === 0 ? "未检测到说话人"
        : item.speaker_count === 1 && labels.length === 1 ? labels[0] : `已区分 ${item.speaker_count} 人`
      : "未区分";
    marker.classList.toggle("running", running);
    marker.classList.toggle("analyzed", analyzed && !running);
    marker.classList.toggle("failed", job?.status === "failed" && !running);
    marker.title = job?.status === "failed" ? String(job.error || "请重试说话人分析。")
      : analyzed ? "匿名编号只在这段录音内有效；声纹匹配的人物仍需核对。"
      : !item.clip_id || !item.recording_available ? "没有可用的独立录音，暂不能区分说话人。"
      : "这段文字尚未获得说话人标注，可点击「区分说话人」使用本地模型分析。";
    const button = $(".document-speaker-action", entry);
    if (button) {
      button.textContent = running ? "查看进度" : analyzed ? "说话人" : "区分说话人";
      button.disabled = !state.connected || state.documentSpeakerOpening !== null;
      button.title = analyzed ? "在声纹与人物页查看、试听并主动登记人物" : "在声纹与人物页运行本地说话人分析；不会自动登记任何人物";
    }
  }

  function updateDocumentSpeakerStates() {
    for (const item of state.documentItems) {
      const key = item.clip_id ? `clip:${item.clip_id}` : `text:${item.id}`;
      const entry = state.documentNodes.get(key)?.node;
      if (entry) updateDocumentSpeakerEntry(entry, item);
    }
  }

  async function openDocumentSpeakers(item) {
    if (!item.clip_id || !item.recording_available || state.documentSpeakerOpening !== null) return;
    state.documentSpeakerOpening = item.id; updateDocumentSpeakerStates();
    try {
      openPeople();
      const selection = selectTranscript(item, false);
      const version = state.selectionVersion;
      await selection;
      if (version !== state.selectionVersion || selectedClipId() !== item.clip_id) return;
      await loadSpeakerAnalysis(item.clip_id, version);
      if (version !== state.selectionVersion || selectedClipId() !== item.clip_id) return;
      $("#voiceprint-select-hint").hidden = true;
      $("#speaker-panel")?.scrollIntoView({behavior:"smooth", block:"start"});
      const active = ["queued", "running"].includes(speakerJob(item.clip_id)?.status);
      if (!state.speakerAnalysis && !state.speakerError && !active && state.speakerStatus?.available) await analyzeSpeakers(item.clip_id);
    } catch (error) { toast(error.message || "无法打开说话人分析，请重试。", "error"); }
    finally { state.documentSpeakerOpening = null; updateDocumentSpeakerStates(); }
  }

  function commitDocument(snapshot) {
    // Preserve every unchanged paragraph node, scroll position and text selection.
    // If existing text needs replacement while selected, defer the whole commit.
    const changed = state.documentReady && snapshot.revision !== state.documentRevision;
    if (changed && documentSelectionActive()) {
      state.documentPending = snapshot;
      $("#document-new").hidden = false;
      renderDocumentState(); return;
    }
    const body = $("#document-body"), desired = [], retained = new Set();
    // The API paginates oldest first. Reverse only the complete snapshot so
    // display, copy and download share one newest-first sequence across pages.
    const items = snapshot.items.slice().reverse();
    let day = null;
    for (const item of items) {
      const date = AudioDocument.stamp(item.start_time, true);
      if (date !== day) {
        day = date;
        const key = `day:${date}`;
        let record = state.documentNodes.get(key);
        if (!record) { const node = document.createElement("h3"); node.className = "document-day"; node.textContent = date; record = {node}; state.documentNodes.set(key, record); }
        desired.push(record.node); retained.add(key);
      }
      const key = item.clip_id ? `clip:${item.clip_id}` : `text:${item.id}`;
      const fingerprint = JSON.stringify(item);
      let record = state.documentNodes.get(key);
      if (!record || record.fingerprint !== fingerprint) {
        const node = makeDocumentEntry(item);
        if (record) record.node.replaceWith(node);
        record = {node, fingerprint}; state.documentNodes.set(key, record);
      }
      desired.push(record.node); retained.add(key);
    }
    for (const [key, record] of state.documentNodes) if (!retained.has(key)) { record.node.remove(); state.documentNodes.delete(key); }
    desired.forEach((node, index) => { if (body.children[index] !== node) body.insertBefore(node, body.children[index] || null); });
    state.documentItems = items; state.documentRevision = snapshot.revision;
    state.documentReady = true; state.documentError = null; state.documentPending = null;
    if (changed) $("#document-new").hidden = false;
    renderDocumentState();
  }

  async function loadDocument({force = false} = {}) {
    const source = documentSource();
    if (source !== state.documentSource) {
      state.documentVersion++; state.documentSource = source;
      state.documentItems = []; state.documentNodes.clear(); $("#document-body").replaceChildren();
      state.documentRevision = null; state.documentReady = false; state.documentPending = null;
      state.documentError = null; state.documentLoading = false; $("#document-new").hidden = true;
    }
    if (state.documentLoading) return;
    const version = ++state.documentVersion;
    state.documentLoading = true; renderDocumentState();
    try {
      const result = source === "" ? {items:[], revision:"no-session"} : await AudioDocument.readSnapshot(
        ({source: selectedSource, cursor}) => {
          const params = new URLSearchParams({limit:"200"});
          if (selectedSource !== null) params.set("source", selectedSource);
          if (cursor) params.set("cursor", cursor);
          return api(`/api/document?${params}`);
        }, source, force ? null : state.documentPending?.revision || state.documentRevision);
      if (version !== state.documentVersion || source !== documentSource()) return;
      state.documentError = null;
      if (result) commitDocument(result);
    } catch (error) {
      if (version === state.documentVersion) state.documentError = error.message;
    } finally {
      if (version === state.documentVersion) {
        state.documentLoading = false; renderDocumentState();
        if (source !== documentSource()) loadDocument({force:true});
      }
    }
  }

  async function copyText(text, message) {
    try { await navigator.clipboard.writeText(text); toast(message); }
    catch (_) { toast("无法访问剪贴板，可以下载全文或手动选中文字。", "error"); }
  }

  function openLibrary(view = state.view) {
    showWorkspace("document");
    $("#library-disclosure").open = true;
    $("#nav-document").classList.remove("active"); $("#nav-document").removeAttribute("aria-current");
    $$(".sidebar-nav [data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  }

  function showWorkspace(page) {
    state.page = page;
    const people = page === "people", storage = page === "storage", mcp = page === "mcp";
    const title = people ? "声纹与人物" : storage ? "存储位置" : mcp ? "MCP 接入" : "转写文档";
    for (const selector of ["#live-card", ".people-entry", "#speaker-settings", "#document-panel", "#library-disclosure"]) $(selector).hidden = people || storage || mcp;
    $("#people-page").hidden = !people;
    $("#storage-page").hidden = !storage;
    $("#mcp-page").hidden = !mcp;
    $("#page-title").textContent = title;
    document.title = `${title} · Audio Agent`;
    $(".skip-link").href = people ? "#people-page" : storage ? "#storage-page" : mcp ? "#mcp-page" : "#document-panel";
    $(".skip-link").textContent = `跳到${title}`;
    const hash = storage ? "#storage" : mcp ? "#mcp" : "";
    if (hash && location.hash !== hash) history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
    else if (!hash && ["#storage", "#mcp"].includes(location.hash)) history.replaceState(null, "", `${location.pathname}${location.search}`);
    for (const button of $$(".sidebar-nav .nav-item")) {
      const active = button.id === (people ? "nav-people" : storage ? "nav-storage" : mcp ? "nav-mcp" : "nav-document");
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    }
    const host = $(people ? "#people-detail-host" : "#library-detail-host");
    if ($("#detail-panel").parentElement !== host) host.replaceChildren($("#detail-panel"));
    renderSpeakers();
  }

  function openPeople() {
    showWorkspace("people");
    loadProfiles(); loadPeopleClips();
    $("#people-page").focus({preventScroll:true});
    $("#page-title").scrollIntoView({block:"start"});
  }

  function showDocument() {
    showWorkspace("document");
    $("#document-panel").scrollIntoView({behavior:"smooth", block:"start"});
  }

  function openStorage() {
    showWorkspace("storage");
    renderStorage();
    if (state.token) loadStorage();
    $("#storage-page").focus({preventScroll:true});
    $("#page-title").scrollIntoView({block:"start"});
  }

  function openMcp() {
    showWorkspace("mcp");
    renderMcp();
    if (state.token) loadMcp();
    $("#mcp-page").focus({preventScroll:true});
    $("#page-title").scrollIntoView({block:"start"});
  }

  function validateMcp(data) {
    if (!data || data.transport !== "stdio" || typeof data.python_path !== "string" || typeof data.data_dir !== "string"
        || !Array.isArray(data.clients) || !data.clients.length || !Array.isArray(data.tools) || !Array.isArray(data.examples)
        || data.clients.some(client => !client || !["claude", "codex"].includes(client.id)
          || ["name", "config_file", "language", "config", "check_command"].some(key => typeof client[key] !== "string"))
        || data.tools.some(tool => !tool || typeof tool.name !== "string" || typeof tool.description !== "string" || typeof tool.read_only !== "boolean")
        || data.examples.some(example => !example || ["id", "title", "tool", "arguments", "note"].some(key => typeof example[key] !== "string"))) {
      throw new Error("MCP 接入信息返回不完整，请刷新后再复制配置。");
    }
    return data;
  }

  async function copyMcpText(text, message) {
    try { await navigator.clipboard.writeText(text); toast(message); }
    catch (_) { toast("无法访问剪贴板，请直接选中配置或命令文本复制。", "error"); }
  }

  function renderMcpSetup(copyDisabled) {
    const scope = $("#mcp-setup-scope").value;
    $("#mcp-setup-scope").disabled = copyDisabled;
    $("#mcp-setup-note").textContent = scope === "user"
      ? "Agent 会合并用户级 MCP 配置并安装个人 audio-agent Skill，供这台电脑的其他项目使用；保留已有服务和权限设置。"
      : "Agent 会检查环境、合并 MCP 配置、安装 audio-agent Skill，并验证连接。当前项目指你在目标客户端打开的工作区。";
    let prompt = "正在读取接入信息…", unavailable = copyDisabled;
    if (state.mcp) {
      try { prompt = AudioAgentSetup.installationRequest(state.mcp, state.mcpSetupClient, scope); }
      catch (error) { prompt = error.message; unavailable = true; }
    }
    $("#mcp-setup-preview-client").textContent = state.mcpSetupClient === "codex" ? "Codex" : "Claude Code";
    const preview = $("#mcp-setup-preview");
    if (preview.textContent !== prompt) preview.textContent = prompt;
    for (const selector of ["#mcp-setup-codex", "#mcp-setup-claude"]) $(selector).disabled = unavailable;
  }

  function copyMcpSetup(client) {
    if (!state.mcp || state.mcpLoading || state.mcpError || state.storageSaving) return;
    state.mcpSetupClient = client;
    renderMcpSetup(false);
    try {
      const text = AudioAgentSetup.installationRequest(state.mcp, client, $("#mcp-setup-scope").value);
      copyMcpText(text, `已复制 ${client === "codex" ? "Codex" : "Claude Code"} 完整安装指令；粘贴给对应客户端执行。`);
    } catch (error) { toast(error.message, "error"); }
  }

  function renderMcp() {
    const data = state.mcp;
    $("#mcp-loading").hidden = !state.mcpLoading && !!data;
    $("#mcp-loading").textContent = state.mcpLoading ? "正在读取本机接入配置…" : "尚未读取接入配置。";
    $("#mcp-error").hidden = !state.mcpError;
    $("#mcp-error").textContent = state.mcpError ? `${state.mcpError} 请点击右上角「刷新 MCP 接入信息」重试。${data ? " 已显示的旧配置保留，仅供查看；刷新成功后恢复复制。" : ""}` : "";
    $("#mcp-refresh").disabled = state.mcpLoading || state.storageSaving;
    $("#mcp-command").textContent = data?.python_path || "—";
    $("#mcp-data-dir").textContent = data?.data_dir || "—";
    $("#mcp-connection-note").textContent = "未探测客户端连接状态";
    $("#mcp-storage-note").textContent = data?.configuration_note || "默认使用项目 .data；手动更改存储位置后，请在客户端重连 MCP 并重建增量游标。";
    const copyDisabled = !data || state.mcpLoading || !!state.mcpError || state.storageSaving;
    renderMcpSetup(copyDisabled);
    const clients = $("#mcp-clients");
    // Keep selectable configuration nodes intact across loading/status changes.
    const key = JSON.stringify(data);
    if (clients.dataset.renderKey !== key) {
      clients.dataset.renderKey = key; clients.replaceChildren();
      $("#mcp-tools").replaceChildren(); $("#mcp-examples").replaceChildren();
      for (const client of data?.clients || []) {
        const card = document.createElement("section"); card.className = "mcp-card mcp-client";
        card.setAttribute("aria-label", `${client.name} 配置`);
        card.innerHTML = '<header class="mcp-heading"><div><h2></h2><code></code></div><button class="button small mcp-copy-config">复制配置</button></header><p class="mcp-note"></p><pre class="mcp-config" tabindex="0"></pre><div class="mcp-check"><code></code><button class="text-button mcp-copy-check">复制检查命令</button></div>';
        $("h2", card).textContent = client.name;
        $(".mcp-heading code", card).textContent = client.config_file;
        $(".mcp-note", card).textContent = "将此项合并到客户端配置，保留其他已有服务；然后在客户端确认信任并重新加载。";
        $(".mcp-config", card).textContent = client.config;
        $(".mcp-check code", card).textContent = client.check_command;
        $(".mcp-copy-config", card).addEventListener("click", () => { if (!state.mcpLoading && !state.mcpError && !state.storageSaving) copyMcpText(client.config, `已复制 ${client.name} 配置；尚未修改客户端。`); });
        $(".mcp-copy-check", card).addEventListener("click", () => { if (!state.mcpLoading && !state.mcpError && !state.storageSaving) copyMcpText(client.check_command, "已复制检查命令；请在对应客户端自行执行。"); });
        clients.append(card);
      }
      for (const tool of data?.tools || []) {
        const row = document.createElement("tr");
        row.innerHTML = '<td><code></code></td><td class="mcp-tool-description"></td><td><span class="mcp-tool-mode"></span></td>';
        $("code", row).textContent = tool.name;
        $(".mcp-tool-description", row).textContent = tool.description;
        $(".mcp-tool-mode", row).textContent = tool.read_only ? "只读" : "按需本地转写";
        $(".mcp-tool-mode", row).classList.toggle("inference", !tool.read_only);
        $("#mcp-tools").append(row);
      }
      for (const example of data?.examples || []) {
        const card = document.createElement("article"); card.className = "mcp-example";
        card.innerHTML = '<div class="mcp-example-heading"><strong></strong><code></code><button class="button small">复制调用参数</button></div><pre tabindex="0"></pre><p class="mcp-note"></p>';
        $("strong", card).textContent = example.title;
        $("code", card).textContent = example.tool;
        $("pre", card).textContent = example.arguments;
        $(".mcp-note", card).textContent = example.note;
        $("button", card).addEventListener("click", () => { if (!state.mcpLoading && !state.mcpError && !state.storageSaving) copyMcpText(example.arguments, `已复制 ${example.tool} 调用参数；未执行工具。`); });
        $("#mcp-examples").append(card);
      }
    }
    for (const button of $$("#mcp-clients button, #mcp-examples button")) button.disabled = copyDisabled;
  }

  async function loadMcp() {
    if (state.storageSaving) return;
    const version = ++state.mcpReadVersion, revision = state.storageRevision;
    state.mcpLoading = true; renderMcp();
    try {
      const response = await api("/api/mcp");
      if (version !== state.mcpReadVersion || revision !== state.storageRevision || state.storageSaving) return;
      state.mcp = validateMcp(response); state.mcpError = null;
    } catch (error) {
      if (version !== state.mcpReadVersion || revision !== state.storageRevision || state.storageSaving) return;
      state.mcpError = error.message;
    } finally {
      if (version === state.mcpReadVersion && revision === state.storageRevision && !state.storageSaving) { state.mcpLoading = false; renderMcp(); }
    }
  }

  function storageBusyReasons() {
    const reasons = Array.isArray(state.storage?.busy_reasons) ? state.storage.busy_reasons.filter(reason => typeof reason === "string" && reason) : [];
    if (liveIsBusy() || Number(state.live?.pending_count) > 0) reasons.push("请先停止监听，并等待已录片段处理完成。");
    if ([...state.jobs.values(), ...state.speakerJobs.values()].some(job => ["queued", "running"].includes(job.status))) reasons.push("本地转写或说话人分析尚未完成。");
    if (state.importing) reasons.push("正在导入录音，请等待导入完成。");
    if (state.voiceprintBusy || state.automaticSaving || state.gateSaving || state.speakerSubmitting || state.livePending) reasons.push("正在保存设置或提交任务，请等待完成。");
    return [...new Set(reasons)];
  }

  function storageDirectoryValid() {
    const directory = (state.storageDraft || "").trim();
    return directory.startsWith("/") && !/[\0\r\n]/.test(directory);
  }

  function renderStorage() {
    const storage = state.storage;
    const pathText = (selector, value) => { $(selector).textContent = typeof value === "string" && value ? value : "—"; };
    pathText("#storage-current-dir", storage?.current_dir);
    pathText("#storage-default-dir", storage?.default_dir);
    pathText("#storage-recordings-dir", storage?.paths?.recordings);
    pathText("#storage-transcripts-dir", storage?.paths?.transcripts);
    pathText("#storage-configured-dir", storage ? storage.configured_dir || "未单独配置，使用默认或启动指定的目录" : null);
    pathText("#storage-config-path", storage?.config_path);
    $("#storage-source").textContent = !storage ? "正在读取…" : storage.current_dir === storage.default_dir ? "当前使用默认目录" : "当前使用自定义目录";
    const retention = typeof storage?.retention_hours === "number" && storage.retention_hours > 0 ? `${storage.retention_hours} 小时` : null;
    $("#storage-retention-note").textContent = retention ? `录音保留设置：${retention}；切换位置不会延长保留时间。需要长期保存的录音，请另行备份。` : "录音默认保留 24 小时，具体按当前保留设置管理；切换位置不会延长保留时间。";
    const input = $("#storage-directory");
    // Polls update only status, never a path the user is editing.
    if (input.value !== (state.storageDraft ?? "")) input.value = state.storageDraft ?? "";
    input.disabled = state.storageSaving;
    $("#storage-use-default").disabled = state.storageSaving || !storage?.default_dir;
    $("#storage-refresh").disabled = state.storageSaving || state.storageLoading;
    const reasons = storageBusyReasons();
    const unchanged = storage && (state.storageDraft || "").trim().replace(/\/+$/, "") === storage.current_dir.replace(/\/+$/, "");
    const available = storage?.can_change === true && !reasons.length;
    $("#storage-save").disabled = state.storageSaving || state.storageLoading || !state.connected || !!state.storageReadError || !available || !storageDirectoryValid() || unchanged;
    $("#storage-save").innerHTML = state.storageSaving ? '<span class="spinner" aria-hidden="true"></span><span>正在复制并切换，请稍候…</span>' : `${icon("folder")}<span>复制现有资料并切换</span>`;
    $("#storage-busy").textContent = state.storageSaving ? "复制完成前请勿关闭本地服务；当前输入已锁定。"
      : !state.connected ? "本地服务未连接。"
      : state.storageLoading && !storage ? "正在检查是否可切换…"
      : !storage ? "请先读取当前目录。"
      : !available ? `暂不能切换。${reasons.join(" ") || "请先停止监听，并等待转写与说话人分析任务完成。"}`
      : unchanged ? "填写新的空文件夹路径后即可切换。"
      : !storageDirectoryValid() ? "请输入以 / 开头的绝对路径。"
      : "当前可切换；仅在点击按钮后复制资料。";
    $("#storage-error").hidden = !state.storageError && !state.storageReadError;
    $("#storage-error").textContent = state.storageError || state.storageReadError || "";
    $("#storage-success").hidden = !state.storageSuccess;
    $("#storage-success").textContent = state.storageSuccess || "";
  }

  function applyStorage(storage) {
    if (!storage || typeof storage.current_dir !== "string" || typeof storage.default_dir !== "string" || typeof storage.can_change !== "boolean" || !storage.paths) throw new Error("存储状态返回不完整，请刷新后重试。");
    state.storage = storage;
    if (state.storageDraft === null) state.storageDraft = storage.current_dir;
  }

  async function loadStorage() {
    if (state.storageSaving) return;
    const version = ++state.storageReadVersion, revision = state.storageRevision;
    state.storageLoading = true; renderStorage();
    try {
      const response = await api("/api/storage");
      if (revision !== state.storageRevision || version !== state.storageReadVersion || state.storageSaving) return;
      applyStorage(response); state.storageReadError = null;
    } catch (error) {
      if (revision !== state.storageRevision || version !== state.storageReadVersion || state.storageSaving) return;
      state.storageReadError = error.message;
    } finally {
      if (revision === state.storageRevision && version === state.storageReadVersion && !state.storageSaving) { state.storageLoading = false; renderStorage(); }
    }
  }

  async function saveStorage() {
    if (state.storageSaving || $("#storage-save").disabled) return;
    const directory = (state.storageDraft || "").trim();
    if (!storageDirectoryValid()) { state.storageError = "请输入以 / 开头的本机绝对路径。"; renderStorage(); return; }
    state.storageSaving = true; state.storageRevision++; state.storageReadVersion++;
    state.mcpReadVersion++; state.mcpLoading = false;
    state.storageLoading = false; state.storageError = null; state.storageSuccess = null; renderStorage();
    let switched = false;
    try {
      const response = await api("/api/storage", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({directory, copy_existing:true})});
      applyStorage(response.storage); switched = true;
      state.mcp = null; state.mcpError = null;
      state.storageDraft = response.storage.current_dir;
      const files = Number(response.copied?.files) || 0, bytes = Number(response.copied?.bytes) || 0;
      const size = bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
      state.storageSuccess = `已切换到 ${response.storage.current_dir}。复制 ${files} 个文件（${size}）；旧目录 ${response.previous_dir || ""} 保留，未自动删除。`;
      // Invalidate pre-switch requests and reference previews before reloading.
      state.listVersion++; state.peopleClipsVersion++; state.documentVersion++;
      state.profileRevision++; state.selectionVersion++;
      state.clipCache.clear(); state.jobs.clear(); state.speakerJobs.clear(); state.speakerPreviews.clear();
      state.documentLoading = false; state.documentPending = null; state.documentRevision = null;
      state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
      $("#audio-player")?.pause();
      renderStorage();
      await refreshAll({quiet:true});
      if (!state.connected) throw new Error("本地服务暂未重新连接。");
      await Promise.all([reloadSpeakerDetails(), loadGateSettings()]);
      toast("资料已复制，后续录音和转写将保存到新目录。");
    } catch (error) {
      state.storageError = switched ? `目录已切换，但刷新资料失败：${error.message} 请刷新查看。` : `${error.message} 输入已保留，可修改后重试。`;
    } finally {
      state.storageSaving = false; state.storageLoading = false; renderStorage();
      if (state.page === "mcp") loadMcp();
    }
  }

  function renderPeopleClips() {
    const list = $("#people-recording-list");
    list.replaceChildren();
    if (state.peopleClipsLoading || state.peopleClipsError || !state.peopleClips.length) {
      const info = document.createElement("p"); info.className = "people-help";
      info.textContent = state.peopleClipsLoading ? "正在读取录音…" : state.peopleClipsError || (state.peopleQuery ? "没有找到这段录音，试试其他名称。" : "暂无可用录音。可导入 WAV，或在转写文档中开始监听。");
      list.append(info);
    } else for (const clip of state.peopleClips) {
      const button = document.createElement("button"); button.type = "button";
      button.className = "people-recording"; button.classList.toggle("selected", clip.id === selectedClipId());
      button.disabled = state.voiceprintBusy;
      button.innerHTML = `<strong>${escapeHTML(clipName(clip))}</strong><span>${escapeHTML(formatDate(clip.start_time || clip.created_time))} · ${formatTime(clip.duration)}</span>`;
      button.addEventListener("click", () => { selectClip(clip, false); renderPeopleClips(); });
      list.append(button);
    }
    $("#people-recording-summary").textContent = state.peopleClipsError ? "读取失败" : `共 ${state.peopleTotal} 段 · 第 ${Math.floor(state.peopleOffset / state.peopleLimit) + 1} 页`;
    $("#people-recording-previous").disabled = state.peopleClipsLoading || state.peopleOffset === 0;
    $("#people-recording-next").disabled = state.peopleClipsLoading || state.peopleOffset + state.peopleLimit >= state.peopleTotal;
  }

  async function loadPeopleClips() {
    const version = ++state.peopleClipsVersion;
    state.peopleClipsLoading = true; state.peopleClipsError = null; renderPeopleClips();
    try {
      const params = new URLSearchParams({q:state.peopleQuery, status:"all", offset:String(state.peopleOffset), limit:String(state.peopleLimit)});
      const response = await api(`/api/clips?${params}`);
      if (version !== state.peopleClipsVersion) return;
      state.peopleClips = response.items || []; state.peopleTotal = response.total || 0;
      for (const clip of state.peopleClips) state.clipCache.set(clip.id, clip);
    } catch (error) { if (version === state.peopleClipsVersion) state.peopleClipsError = error.message; }
    finally { if (version === state.peopleClipsVersion) { state.peopleClipsLoading = false; renderPeopleClips(); } }
  }

  async function startLive() {
    const device = $("#live-device").value;
    if (!device || state.livePending || liveIsBusy()) return;
    state.livePending = "start"; state.liveActionError = null; renderLive();
    try {
      const response = await api("/api/live/start", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({device, language:$("#live-language").value, vad_enabled:state.vadEnabled})});
      state.live = response.live; state.livePending = null; renderLive();
      toast("已开始连接麦克风，有声音时录音，安静后自动转写。");
      loadDocument({force:true});
      poll();
    } catch (error) { state.livePending = null; state.liveActionError = error.message; renderLive(); toast(error.message, "error", 8000); }
  }

  async function stopLive() {
    if (state.livePending) return;
    state.livePending = "stop"; state.liveActionError = null; renderLive();
    try {
      const response = await api("/api/live/stop", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      state.live = response.live; state.livePending = null; renderLive();
      toast("正在停止监听，已保存的声音片段会继续完成识别。");
      poll();
    } catch (error) { state.livePending = null; state.liveActionError = error.message; renderLive(); toast(error.message, "error", 8000); }
  }

  function renderList() {
    const isClips = state.view === "clips";
    $("#library-content").setAttribute("aria-labelledby", isClips ? "tab-clips" : "tab-transcripts");
    $("#library-content").setAttribute("aria-busy", String(state.loading));
    const head = $("#table-head"), body = $("#table-body"), listState = $("#list-state");
    head.innerHTML = `<tr><th class="column-name" scope="col">${isClips ? "录音名称" : "转写内容"}</th><th class="column-duration" scope="col">时长</th><th class="column-status" scope="col">${isClips ? "状态" : "语言"}</th><th class="column-action" scope="col"><span class="sr-only">查看</span></th></tr>`;
    body.replaceChildren();
    $("#table-wrap").hidden = state.loading || !!state.listError || state.items.length === 0;
    listState.hidden = !state.loading && !state.listError && state.items.length > 0;
    if (state.loading) {
      listState.innerHTML = `<span class="spinner" aria-hidden="true"></span><p>正在读取本地资料库…</p>`;
    } else if (state.listError) {
      listState.innerHTML = `<span class="empty-icon">${icon("alert")}</span><h2>暂时无法读取资料库</h2><p></p><button class="button small" id="retry-list">重新加载</button>`;
      $("p", listState).textContent = state.listError;
      $("#retry-list").addEventListener("click", () => loadList());
    } else if (!state.items.length) {
      const filtered = state.query || (isClips && state.filter !== "all");
      listState.innerHTML = `<span class="empty-icon">${icon(filtered ? "search" : isClips ? "headphones" : "text")}</span><h2>${filtered ? "没有找到匹配的内容" : isClips ? "让第一段声音，从这里开始" : "还没有转写记录"}</h2><p>${filtered ? "试试其他关键词，或清除筛选条件。" : isClips ? "开始采音或导入 WAV 后，录音会出现在这里。" : "在录音文件中选择一段音频，点击「本地转写」，即可在这里查看文字。"}</p><button class="button small ${filtered ? "" : "primary"}" id="empty-action">${filtered ? "清除筛选" : isClips ? `${icon("upload")}导入 WAV 文件` : "查看录音文件"}</button>`;
      $("#empty-action").addEventListener("click", () => {
        if (filtered) { state.query = ""; state.filter = "all"; $("#search-input").value = ""; $("#status-filter").value = "all"; state.offset = 0; loadList(); }
        else if (isClips) $("#file-input").click();
        else switchView("clips");
      });
    } else {
      state.items.forEach(item => {
        const row = document.createElement("tr");
        const key = `${state.view}:${item.id}`;
        row.classList.toggle("selected", state.selectedKey === key);
        const name = isClips ? clipName(item) : transcriptName(item);
        const subline = `${formatDate(isClips ? item.start_time || item.created_time : item.created_at)} · ${sourceName(item.source)}`;
        row.innerHTML = `<td><div class="file-cell"><span class="file-icon">${icon(isClips ? "wave" : "text")}</span><div class="file-info"><button class="file-name" title="${escapeHTML(name)}">${escapeHTML(name)}</button>${!isClips ? `<span class="transcript-preview">${escapeHTML(item.text || "转写结果为空")}</span>` : ""}<span class="file-meta" title="${escapeHTML(subline)}">${escapeHTML(subline)}</span></div></div></td><td class="time-cell">${formatTime(isClips ? item.duration : item.duration_seconds)}</td><td>${isClips ? badge(item) : `<span class="badge complete">${escapeHTML(languageLabel(item.language))}</span>`}</td><td><button class="row-arrow" aria-label="查看 ${escapeHTML(name)}">${icon("chevron")}</button></td>`;
        const select = () => isClips ? selectClip(item) : selectTranscript(item);
        $(".file-name", row).addEventListener("click", select);
        $(".row-arrow", row).addEventListener("click", select);
        row.addEventListener("click", event => { if (!event.target.closest("button")) select(); });
        body.append(row);
      });
    }
    $("#list-summary").textContent = state.loading ? "正在读取资料库…" : state.listError ? "读取失败" : state.total ? `共 ${state.total} ${isClips ? "个录音" : "条转写"} · 显示 ${state.offset + 1}–${Math.min(state.offset + state.items.length, state.total)}` : `共 0 ${isClips ? "个录音" : "条转写"}`;
    $("#page-number").textContent = Math.floor(state.offset / state.limit) + 1;
    $("#previous-button").disabled = state.loading || state.offset <= 0;
    $("#next-button").disabled = state.loading || state.offset + state.limit >= state.total;
  }

  async function loadList({quiet = false, selectFirst = false} = {}) {
    const version = ++state.listVersion;
    if (!quiet) { state.loading = true; state.listError = null; renderList(); }
    const params = new URLSearchParams({q: state.query, limit: String(state.limit), offset: String(state.offset)});
    if (state.view === "clips") params.set("status", state.filter);
    try {
      const result = await api(`/api/${state.view}?${params}`);
      if (version !== state.listVersion) return;
      state.items = result.items || [];
      state.total = result.total || 0;
      state.loading = false;
      state.listError = null;
      if (state.view === "clips") state.items.forEach(clip => state.clipCache.set(clip.id, clip));
      renderList();
      if (selectFirst && state.items.length && !state.selectedKey) {
        if (state.view === "clips") selectClip(state.items[0], false);
        else selectTranscript(state.items[0], false);
      }
    } catch (error) {
      if (version !== state.listVersion) return;
      state.loading = false;
      state.listError = error.message;
      renderList();
    }
  }

  function languageLabel(language) {
    return ({auto:"自动识别", zh:"中文", en:"English", ja:"日语", ko:"韩语", yue:"粤语"})[language] || language || "自动识别";
  }

  function selectedClipId() {
    return state.clip?.id || state.transcript?.clip_id || null;
  }

  function resetSpeakerSelection() {
    state.speakerAnalysis = null; state.speakerLoading = false; state.speakerError = null;
    state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
    state.speakerNumber = "auto";
    state.enrollKind = "self"; state.enrollDraft = {name:"", note:""}; state.enrollReplaceId = null;
  }

  function speakerJob(clipId) {
    const jobs = Array.from(state.speakerJobs.values()).filter(job => job.clip_id === clipId);
    return jobs.find(job => ["queued", "running"].includes(job.status))
      || jobs.sort((a, b) => (b.created_at || 0) - (a.created_at || 0))[0];
  }

  function personFieldsError(name, note) {
    if (!name || Array.from(name).length > 80 || /[\r\n]/.test(name)) return "姓名须为 1–80 字单行文字。";
    if (name === "我") return "「我」用于本人声纹，请使用「登记为我」。";
    if (Array.from(note).length > 200 || /[\r\n]/.test(note)) return "备注最多 200 字，使用单行文字。";
    return null;
  }

  function applyProfiles(response) {
    if (!Array.isArray(response.items)) throw new Error("人物列表返回不完整，请刷新后重试。");
    state.profiles = response.items; state.profilesReady = true; state.profilesError = null;
    if (response.voiceprint) state.speakerStatus = {...state.speakerStatus, voiceprint:response.voiceprint};
  }

  async function loadProfiles() {
    if (state.voiceprintBusy) return;
    const version = ++state.profilesReadVersion, revision = state.profileRevision;
    try {
      const response = await api("/api/speaker-profiles");
      if (version !== state.profilesReadVersion || revision !== state.profileRevision || state.voiceprintBusy) return;
      const changed = state.profilesReady && JSON.stringify(response.items) !== JSON.stringify(state.profiles);
      applyProfiles(response);
      if (state.profileEditId && !state.profiles.some(profile => profile.id === state.profileEditId)) state.profileEditId = null;
      renderProfiles(); renderVoiceprint();
      if (changed) { state.profileRevision++; await reloadSpeakerDetails(); }
    } catch (error) {
      if (version !== state.profilesReadVersion || revision !== state.profileRevision || state.voiceprintBusy) return;
      state.profilesError = error.message; renderProfiles();
    }
  }

  function renderProfiles() {
    $("#people-profile-count").textContent = state.profilesReady ? `${state.profiles.length} 位 · 仅本机` : "尚未读取";
    $("#people-entry-status").textContent = `${state.speakerStatus?.voiceprint?.enrolled ? "本人已登记" : "本人未登记"}${state.profilesReady ? ` · 其他人物 ${state.profiles.length} 位` : ""}`;
    $("#people-error").hidden = !state.profilesError;
    $("#people-error").textContent = state.profilesError || "";
    $("#people-refresh").disabled = state.voiceprintBusy;
    const list = $("#people-profile-list");
    const key = JSON.stringify([state.profiles, state.profilesReady, state.profileEditId, state.profileRemoveId, state.voiceprintBusy, state.connected]);
    if (list.dataset.renderKey === key) return;
    const active = list.contains(document.activeElement) ? document.activeElement : null;
    const focus = active?.id ? {id:active.id, start:active.selectionStart, end:active.selectionEnd} : null;
    list.dataset.renderKey = key; list.replaceChildren();
    if (!state.profiles.length) {
      const empty = document.createElement("p"); empty.className = "people-empty";
      empty.textContent = state.profilesReady ? "还没有登记其他人物。下方试听后，选择「登记其他人物」。" : "正在读取本地人物…";
      list.append(empty);
    }
    for (const profile of state.profiles) {
      const card = document.createElement("article"); card.className = "person-profile";
      const disabled = state.voiceprintBusy || !state.connected;
      card.innerHTML = `<div class="person-profile-heading"><strong>${escapeHTML(profile.name)}</strong><span>${Number(profile.reference_seconds || 0).toFixed(1)} 秒参考</span><div><button class="text-button person-edit" ${disabled ? "disabled" : ""}>编辑备注</button><button class="text-button person-remove" ${disabled ? "disabled" : ""}>移除</button></div></div><p class="person-note">${escapeHTML(profile.note || "未填写备注")}</p>`;
      $(".person-edit", card).addEventListener("click", () => {
        state.profileEditId = state.profileEditId === profile.id ? null : profile.id;
        state.profileRemoveId = null; state.profileDraft = {name:profile.name, note:profile.note || ""};
        renderProfiles(); $("#person-edit-name")?.focus();
      });
      $(".person-remove", card).addEventListener("click", () => {
        state.profileRemoveId = state.profileRemoveId === profile.id ? null : profile.id;
        state.profileEditId = null; renderProfiles();
      });
      if (state.profileEditId === profile.id) {
        const form = document.createElement("form"); form.className = "person-form";
        form.innerHTML = `<label>姓名<input id="person-edit-name" name="name" maxlength="80" required value="${escapeHTML(state.profileDraft.name)}" ${disabled ? "disabled" : ""}></label><label>备注（可选）<input id="person-edit-note" name="note" maxlength="200" value="${escapeHTML(state.profileDraft.note)}" placeholder="例如：同事、项目负责人" ${disabled ? "disabled" : ""}></label><div class="person-form-actions"><button type="submit" class="button primary small" ${disabled ? "disabled" : ""}>${state.voiceprintBusy ? "正在保存…" : "保存姓名与备注"}</button><button type="button" class="button small person-cancel" ${disabled ? "disabled" : ""}>取消</button></div>`;
        $("[name=name]", form).addEventListener("input", event => { state.profileDraft.name = event.target.value; });
        $("[name=note]", form).addEventListener("input", event => { state.profileDraft.note = event.target.value; });
        $(".person-cancel", form).addEventListener("click", () => { state.profileEditId = null; renderProfiles(); });
        form.addEventListener("submit", event => { event.preventDefault(); updateProfile(profile.id); });
        card.append(form);
      }
      if (state.profileRemoveId === profile.id) {
        const confirm = document.createElement("div"); confirm.className = "person-remove-confirm";
        confirm.innerHTML = `<span>移除「${escapeHTML(profile.name)}」的本地声纹和备注？录音与原文会保留。</span><button class="button small" ${disabled ? "disabled" : ""}>确认移除</button><button class="text-button" ${disabled ? "disabled" : ""}>取消</button>`;
        $(".button", confirm).addEventListener("click", () => mutateProfile("/api/speaker-profiles/remove", {profile_id:profile.id}, "已移除人物声纹与备注，录音和原文仍保留。"));
        $(".text-button", confirm).addEventListener("click", () => { state.profileRemoveId = null; renderProfiles(); });
        card.append(confirm);
      }
      list.append(card);
    }
    if (focus) { const input = $(`#${focus.id}`); input?.focus({preventScroll:true}); if (typeof focus.start === "number") input?.setSelectionRange(focus.start, focus.end); }
  }

  async function updateProfile(id) {
    const name = state.profileDraft.name.trim(), note = state.profileDraft.note.trim();
    const error = personFieldsError(name, note);
    if (error) { state.profilesError = error; renderProfiles(); return; }
    await mutateProfile("/api/speaker-profiles/update", {profile_id:id, name, note}, "人物姓名与备注已更新。");
  }

  async function mutateProfile(path, payload, success) {
    if (state.voiceprintBusy || state.automaticSaving) return;
    state.voiceprintBusy = true; state.profileRevision++; state.profilesError = null; state.voiceprintError = null;
    renderProfiles(); renderVoiceprint(); renderSpeakers(); renderPeopleClips();
    try {
      const response = await api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      applyProfiles(response);
      state.profileEditId = null; state.profileRemoveId = null;
      state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
      await reloadSpeakerDetails(); toast(success);
    } catch (error) {
      state.profilesError = error.message; state.voiceprintError = error.message;
      if (payload.analysis_id && [400,409].includes(error.status)) {
        state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
        state.speakerPreviews.delete(`${payload.clip_id}:${payload.analysis_id}:${payload.speaker_id}`);
        await reloadSpeakerDetails();
      }
      toast(error.message, "error", 8000);
    } finally { state.voiceprintBusy = false; renderProfiles(); renderVoiceprint(); renderSpeakers(); renderPeopleClips(); }
  }

  function renderVoiceprint() {
    const profile = state.speakerStatus?.voiceprint;
    const enrolled = !!profile?.enrolled;
    const multi = state.speakerStatus?.voiceprint_multisample === true;
    const count = multi ? Number(profile?.sample_count || 0) : enrolled ? 1 : 0;
    const samples = multi && Array.isArray(profile?.samples) ? profile.samples : [];
    $("#voiceprint-state").textContent = state.voiceprintBusy ? "正在更新…" : enrolled ? `已登记 · ${count} 段样本 · 仅本机` : state.speakerStatus ? "尚未登记" : state.speakerStatusError ? "暂时无法读取" : "正在检查…";
    $("#voiceprint-description").textContent = enrolled
      ? `有效参考共 ${Number(profile.total_reference_seconds ?? profile.reference_seconds ?? 0).toFixed(1)} 秒。${multi ? `优先从不同录音追加清晰样本，最多 ${state.speakerStatus.voiceprint_max_samples || 10} 段；无需反复分析同一录音来追加。综合参考进行匹配，不会自动训练或加入新声音。` : "当前后台只保存一段参考，再次登记会替换旧参考。"}结果仍需核对。${(samples.length ? Math.max(...samples.map(item => Number(item.reference_seconds) || 0)) : Number(profile.reference_seconds || 0)) < 2 ? " 当前参考较短，建议补充清晰、较长的本人声音；登记成功不保证后续匹配稳定。" : ""}`
      : "导入或选择录音，分析说话人后主动登记为「我」。不设最低秒数，1–2 秒也可登记；短片段匹配可能不稳定，清晰独白 10–20 秒更稳。声纹仅保存在本机。";
    $("#voiceprint-clear").hidden = !enrolled;
    $("#voiceprint-clear").textContent = multi ? "清空本人样本" : "取消登记";
    $("#voiceprint-clear").disabled = state.voiceprintBusy || state.automaticSaving || !state.connected;
    const error = state.voiceprintError || state.speakerStatusError || profile?.error;
    $("#voiceprint-error").hidden = !error;
    $("#voiceprint-error").textContent = error || "";
    $("#voiceprint-setup-button").textContent = enrolled ? multi ? "添加我的声音样本" : "重新设置我的声纹" : "设置我的声纹";
    $("#voiceprint-setup-button").disabled = state.voiceprintBusy;
    renderSelfSamples(samples, multi);
    renderProfiles();
    renderSpeakerSettings();
  }

  function renderSelfSamples(samples, supported) {
    const list = $("#voiceprint-samples");
    list.hidden = !supported || !samples.length;
    const key = JSON.stringify([samples, state.selfSampleRemoveId, state.voiceprintBusy, state.automaticSaving, state.connected]);
    if (list.dataset.renderKey === key) return;
    list.dataset.renderKey = key; list.replaceChildren();
    samples.forEach((sample, index) => {
      const row = document.createElement("div"); row.className = "voiceprint-sample";
      const busy = state.voiceprintBusy || state.automaticSaving || !state.connected;
      row.innerHTML = '<div><strong></strong><span class="voiceprint-sample-meta"></span></div><button class="text-button voiceprint-sample-open">查看录音</button><button class="text-button voiceprint-sample-remove">移除</button>';
      $("strong", row).textContent = `样本 ${index + 1} · 有效 ${Number(sample.reference_seconds || 0).toFixed(1)} 秒`;
      $(".voiceprint-sample-meta", row).textContent = `${new Date(Number(sample.created_at) * 1000).toLocaleString()}${sample.model_compatible === false ? " · 模型不兼容，暂不参与匹配" : Number(sample.reference_seconds) < 2 ? " · 短样本，建议补充更清晰语音" : ""}`;
      $(".voiceprint-sample-open", row).disabled = busy;
      $(".voiceprint-sample-open", row).addEventListener("click", async () => {
        try {
          const response = await api(`/api/clips?q=${encodeURIComponent(sample.clip_id)}&limit=1&offset=0`);
          const clip = response.items?.find(item => item.id === sample.clip_id);
          if (!clip) throw new Error("参考录音不可用");
          await selectClip(clip, true);
        }
        catch (error) { toast("原录音可能已过期或不可用；已保存的声纹样本仍保留。", "error"); }
      });
      $(".voiceprint-sample-remove", row).disabled = busy;
      $(".voiceprint-sample-remove", row).addEventListener("click", () => { state.selfSampleRemoveId = sample.id; renderVoiceprint(); });
      if (state.selfSampleRemoveId === sample.id) {
        const confirm = document.createElement("div"); confirm.className = "voiceprint-sample-confirm";
        confirm.innerHTML = '<span></span><button class="button small">确认移除</button><button class="text-button">取消</button>';
        $("span", confirm).textContent = samples.length === 1 ? "这是最后一个样本，移除后将取消本人登记。" : "移除此样本，保留其他样本和原录音。";
        $(".button", confirm).disabled = busy;
        $(".button", confirm).addEventListener("click", () => removeSelfSample(sample.id));
        $(".text-button", confirm).disabled = busy;
        $(".text-button", confirm).addEventListener("click", () => { state.selfSampleRemoveId = null; renderVoiceprint(); });
        row.append(confirm);
      }
      list.append(row);
    });
  }

  async function removeSelfSample(sampleId) {
    if (state.voiceprintBusy || state.automaticSaving || !state.connected || !state.speakerStatus?.voiceprint_multisample) return;
    state.voiceprintBusy = true; state.profileRevision++; state.voiceprintError = null; renderVoiceprint(); renderSpeakers();
    try {
      const response = await api("/api/voiceprint/samples/remove", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sample_id:sampleId})});
      state.speakerStatus = {...state.speakerStatus, voiceprint:response.voiceprint}; state.selfSampleRemoveId = null;
      await reloadSpeakerDetails(); toast("已移除本人样本；原录音不受影响。");
    } catch (error) { state.voiceprintError = error.message; toast(error.message, "error"); }
    finally { state.voiceprintBusy = false; renderVoiceprint(); renderSpeakers(); }
  }

  function renderSpeakerSettings() {
    const status = state.speakerStatus;
    const known = typeof status?.automatic_enabled === "boolean";
    const enabled = state.automaticDraft ?? status?.automatic_enabled ?? false;
    const person = status?.voiceprint?.enrolled ? "本人已登记" : status ? "本人未登记" : "本人状态待读取";
    const automatic = state.automaticSaving ? "正在保存自动设置…" : known ? status.automatic_enabled ? "新录音自动区分已开" : "新录音自动区分已关" : "自动设置待读取";
    $("#speaker-settings-summary").textContent = `${person} · ${automatic}${status?.automatic_error ? " · 最近分析失败" : ""}`;
    $("#speaker-automatic").checked = enabled;
    $("#speaker-automatic").disabled = state.automaticSaving || state.voiceprintBusy || !known || !state.connected || (!status?.available && !enabled);
    $("#speaker-settings-feedback").textContent = state.automaticSaving ? "正在保存…" : state.automaticFeedback
      || (!known ? status ? "服务暂未提供自动设置" : "正在读取设置…" : !status.available ? "本地模型暂不可用" : enabled ? "已启用 · 仅本机处理" : "已关闭 · 可按段手动区分");
    const error = state.automaticError || state.speakerStatusError || status?.automatic_error
      || (status && !status.available ? status.reason || "请先准备本地说话人分析模型。" : null);
    $("#speaker-settings-error").hidden = !error;
    $("#speaker-settings-error").textContent = error ? String(error).slice(0, 700) : "";
  }

  async function saveAutomaticSpeakers(enabled) {
    if (state.automaticSaving || state.voiceprintBusy || typeof state.speakerStatus?.automatic_enabled !== "boolean") return;
    state.automaticSaving = true; state.automaticDraft = enabled;
    state.automaticError = null; state.automaticFeedback = null; state.profileRevision++;
    renderVoiceprint(); renderSpeakers();
    try {
      await api("/api/speakers/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({automatic_enabled:enabled})});
      const status = await loadSpeakerStatus({forSettings:true});
      if (!status || status.automatic_enabled !== enabled) throw new Error("设置已提交，但暂未确认保存结果。请刷新后检查自动区分状态。");
      state.automaticFeedback = enabled ? "已保存 · 新录音自动区分" : "已保存 · 后续可手动区分";
      toast(enabled ? "已开启新录音自动区分说话人；本人声纹仍需主动登记。" : "已关闭新录音自动区分说话人。");
    } catch (error) { state.automaticError = error.message; }
    finally { state.automaticSaving = false; state.automaticDraft = null; renderVoiceprint(); renderSpeakers(); }
  }

  async function loadSpeakerStatus({forSettings = false} = {}) {
    if (state.automaticSaving && !forSettings) return null;
    const revision = state.profileRevision;
    try {
      const response = await api("/api/speakers/status");
      if (revision !== state.profileRevision || state.voiceprintBusy) return;
      const changed = JSON.stringify(response) !== JSON.stringify(state.speakerStatus);
      if (!state.automaticSaving && response.automatic_enabled !== state.speakerStatus?.automatic_enabled) state.automaticFeedback = null;
      state.speakerStatus = response; state.speakerStatusError = null;
      renderVoiceprint();
      if (changed) { state.profileRevision++; renderSpeakers(); await refreshSelectedTranscript(); }
      updateDocumentSpeakerStates();
      return response;
    } catch (error) {
      if (revision !== state.profileRevision || state.voiceprintBusy) return null;
      state.speakerStatusError = error.message;
      renderVoiceprint(); renderSpeakers();
      return null;
    }
  }

  function playFrom(time, end = null, onStarted = null) {
    const audio = $("#audio-player");
    if (!audio) return;
    if (stopSpeakerPreview) { stopSpeakerPreview(); stopSpeakerPreview = null; }
    const seekAndPlay = () => {
      if ($("#audio-player") !== audio) return;
      if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(Math.max(0, Number(time) || 0), Math.max(0, audio.duration - .05));
      if (typeof end === "number" && Number.isFinite(end) && end > time) {
        const stop = () => { if (audio.currentTime >= end) { audio.pause(); audio.removeEventListener("timeupdate", stop); stopSpeakerPreview = null; } };
        audio.addEventListener("timeupdate", stop);
        stopSpeakerPreview = () => audio.removeEventListener("timeupdate", stop);
      }
      audio.play().then(() => { if ($("#audio-player") === audio && onStarted) onStarted(); })
        .catch(() => toast("无法播放这段录音，请检查音频播放器。", "error"));
    };
    if (audio.readyState >= 1) seekAndPlay();
    else { audio.addEventListener("loadedmetadata", seekAndPlay, {once:true}); audio.load(); }
  }

  function renderSpeakers() {
    const panel = $("#speaker-panel"), clipId = selectedClipId();
    if (!panel || !clipId) return;
    const analysis = state.speakerAnalysis?.clip_id === clipId ? state.speakerAnalysis : null;
    const job = speakerJob(clipId);
    const running = state.speakerSubmitting || ["queued", "running"].includes(job?.status);
    const available = !!state.speakerStatus?.available;
    const key = JSON.stringify([clipId, analysis, job?.status, job?.error, running, available, state.connected,
      state.page, state.speakerLoading, state.speakerError, state.speakerStatusError, state.voiceprintError,
      state.speakerStatus?.reason, state.speakerStatus?.voiceprint?.enrolled, state.voiceprintBusy, state.automaticSaving,
      state.speakerConfirmId, state.speakerConfirmAnalysisId, state.enrollKind, state.enrollReplaceId,
      state.enrollSelfMode, state.speakerStatus?.voiceprint_multisample, state.speakerStatus?.voiceprint?.samples,
      state.profiles, Array.from(state.speakerPreviews)]);
    if (panel.dataset.renderKey === key) return;
    const active = panel.contains(document.activeElement) ? document.activeElement : null;
    const focus = active?.id ? {id:active.id, start:active.selectionStart, end:active.selectionEnd} : null;
    panel.dataset.renderKey = key;
    const unavailable = state.speakerStatusError || (state.speakerStatus ? state.speakerStatus.reason || "请先准备本地说话人分析模型。" : "正在检查本地模型…");
    const countLabel = analysis ? Number.isInteger(analysis.requested_speakers) && analysis.requested_speakers > 0
      ? `按指定 ${analysis.requested_speakers} 人分析 · 仅此录音`
      : `估计 ${Number(analysis.speaker_count) || 0} 位说话人 · 仅此录音` : "按声音区分 · 手动分析";
    panel.innerHTML = `<div class="speaker-panel-head"><h3>说话人分析</h3><span>${escapeHTML(countLabel)}</span></div><div class="speaker-controls"><select id="speaker-number" aria-label="这段录音的说话人数" ${running || state.voiceprintBusy ? "disabled" : ""}><option value="auto">人数：自动</option>${Array.from({length:10}, (_, index) => `<option value="${index + 1}">${index + 1} 位说话人</option>`).join("")}</select><button class="button" id="analyze-speakers" ${running || !available || !state.connected || state.voiceprintBusy ? "disabled" : ""}>${running ? '<span class="spinner" aria-hidden="true"></span>正在分析…' : analysis ? "重新分析说话人" : "分析说话人"}</button>${state.page !== "people" ? '<button class="button" id="speaker-open-people">登记 / 管理人物</button>' : ""}</div><p class="speaker-note">匿名编号只属于这段录音。先试听，再主动登记为「我」或其他人物；打开页面不会启动模型或录音。</p><div id="speaker-message" role="status"></div><div class="speaker-list" id="speaker-list"></div>`;
    $("#speaker-number").value = state.speakerNumber;
    $("#speaker-number").addEventListener("change", event => { state.speakerNumber = event.target.value; });
    $("#analyze-speakers").addEventListener("click", () => analyzeSpeakers(clipId));
    $("#speaker-open-people")?.addEventListener("click", openPeople);
    const message = $("#speaker-message");
    const error = state.speakerError || state.voiceprintError || (!running && job?.status === "failed" ? job.error || "分析未完成，请重试。" : null);
    const info = error || (running ? job?.status === "queued" ? "分析任务已提交，正在等待本地引擎。" : "本地模型正在区分说话人，可以继续浏览或收听录音。"
      : state.speakerLoading ? "正在读取已有分析…" : !available ? unavailable : null);
    if (info) { const paragraph = document.createElement("p"); paragraph.className = `speaker-notice${error ? " error" : ""}`; paragraph.textContent = String(info).slice(0, 700); message.append(paragraph); }
    if (!analysis) return;
    for (const speaker of analysis.speakers || []) {
      const card = document.createElement("div"); card.className = "speaker-card";
      const identity = speaker.identity;
      const label = identity?.display_label || identity?.label || speaker.label || `说话人 ${String(speaker.id).replace(/^speaker_/, "")}`;
      const reference = Number(speaker.reference_seconds) || 0;
      const canEnroll = !!speaker.can_enroll;
      const previewKey = `${clipId}:${analysis.id}:${speaker.id}`;
      const previewed = state.speakerPreviews.has(previewKey);
      const identityNote = identity ? identity.source === "user_reference" ? "已主动登记" : "声纹匹配 · 请核对" : "匿名说话人";
      const turn = (analysis.turns || []).find(item => item.speaker === speaker.id);
      const disabled = !canEnroll || !state.connected || state.voiceprintBusy || state.automaticSaving || running;
      const bound = state.profiles.find(profile => profile.clip_id === clipId && profile.speaker_id === speaker.id && profile.analysis_id === analysis.id);
      card.innerHTML = `<div class="speaker-card-heading"><i class="speaker-color-dot" aria-hidden="true"></i><strong>${escapeHTML(label)}</strong><span>${escapeHTML(identityNote)}</span></div><p class="speaker-card-meta">发言 ${Number(speaker.speech_seconds || 0).toFixed(1)} 秒 · 可用参考 ${reference.toFixed(1)} 秒</p><div class="speaker-card-actions"><button class="button speaker-listen" ${turn && !state.voiceprintBusy ? "" : "disabled"}>${icon("play")}试听此人</button>${state.page === "people" ? `<button class="button speaker-enroll" ${disabled ? "disabled" : ""}>登记为我</button><button class="button speaker-enroll-person" ${disabled ? "disabled" : ""}>${bound ? "更新此人物声纹" : "登记其他人物"}</button>` : '<button class="button speaker-manage-person">登记 / 管理人物</button>'}</div>${canEnroll && reference < 5 ? '<p class="speaker-card-meta">短片段可以登记；后续声纹匹配可能不够稳定。</p>' : ""}${!canEnroll ? '<p class="speaker-card-meta">尚未提取到有效声纹，请重新分析或选择更清晰的单人录音。</p>' : !previewed ? '<p class="speaker-card-meta">点击登记可打开确认表单；保存前请试听并核对身份。</p>' : ""}`;
      $(".speaker-listen", card).addEventListener("click", () => playFrom(turn?.start, turn?.end, () => {
        if (selectedClipId() !== clipId || state.speakerAnalysis?.id !== analysis.id) return;
        state.speakerPreviews.add(previewKey); renderSpeakers();
      }));
      $(".speaker-manage-person", card)?.addEventListener("click", openPeople);
      const beginEnrollment = kind => {
        if (disabled) return;
        const collapse = state.speakerConfirmId === speaker.id && state.enrollKind === kind;
        state.speakerConfirmId = collapse ? null : speaker.id;
        state.speakerConfirmAnalysisId = collapse ? null : analysis.id;
        state.enrollKind = kind; state.enrollReplaceId = kind === "person" ? bound?.id || null : null;
        state.enrollSelfMode = "append";
        state.enrollDraft = {name:bound?.name || "", note:bound?.note || ""};
        state.speakerConfirmed = false; state.voiceprintError = null; renderSpeakers();
        $(kind === "person" ? "#speaker-person-name" : "#speaker-consent-check")?.focus({preventScroll:true});
      };
      $(".speaker-enroll", card)?.addEventListener("click", () => beginEnrollment("self"));
      $(".speaker-enroll-person", card)?.addEventListener("click", () => beginEnrollment("person"));
      if (state.page === "people" && state.speakerConfirmId === speaker.id && state.speakerConfirmAnalysisId === analysis.id) {
        const person = state.enrollKind === "person", busy = state.voiceprintBusy;
        const consent = document.createElement("form"); consent.className = "speaker-consent person-form";
        const fields = person ? `<label>登记对象<select id="speaker-profile-target" ${busy ? "disabled" : ""}><option value="">新人物</option>${state.profiles.map(profile => `<option value="${escapeHTML(profile.id)}" ${profile.id === state.enrollReplaceId ? "selected" : ""}>替换 ${escapeHTML(profile.display_label || profile.name)} 的参考声纹</option>`).join("")}</select></label><label>姓名<input id="speaker-person-name" maxlength="80" required value="${escapeHTML(state.enrollDraft.name)}" placeholder="例如：小王" ${busy ? "disabled" : ""}></label><label>备注（可选）<input id="speaker-person-note" maxlength="200" value="${escapeHTML(state.enrollDraft.note)}" placeholder="例如：同事、项目负责人" ${busy ? "disabled" : ""}></label>` : "";
        const multi = !person && state.speakerStatus?.voiceprint_multisample === true;
        const enrolled = !!state.speakerStatus?.voiceprint?.enrolled;
        const ownSamples = state.speakerStatus?.voiceprint?.samples || [];
        const alreadyAdded = ownSamples.some(item => item.clip_id === clipId && item.analysis_id === analysis.id && item.speaker_id === speaker.id);
        const atLimit = multi && state.enrollSelfMode === "append" && !alreadyAdded && ownSamples.length >= (state.speakerStatus.voiceprint_max_samples || 10);
        const modeField = multi && enrolled ? `<label>保存方式<select id="speaker-self-mode" ${busy ? "disabled" : ""}><option value="append" ${state.enrollSelfMode === "append" ? "selected" : ""}>追加到我的样本（保留已有 ${ownSamples.length} 段）</option><option value="replace" ${state.enrollSelfMode === "replace" ? "selected" : ""}>替换全部本人样本，仅保留这一段</option></select></label>` : "";
        const confirmText = person ? state.enrollReplaceId ? "确认替换此人物声纹" : "确认登记人物" : multi ? state.enrollSelfMode === "replace" && enrolled ? "确认替换全部本人样本" : alreadyAdded ? "确认已添加的本人样本" : "确认添加本人样本" : enrolled ? "确认替换本人声纹" : "确认登记为我";
        consent.innerHTML = `${fields}${modeField}${atLimit ? '<p class="speaker-notice">本人样本已达上限，请先移除不需要的样本，或明确选择替换全部。</p>' : ""}<label class="speaker-consent-label"><input type="checkbox" id="speaker-consent-check" ${state.speakerConfirmed ? "checked" : ""} ${busy ? "disabled" : ""}><span>${person ? "我已试听并确认这是此人物的单人声音，同意仅本机保存声纹与备注" : "我已试听并确认这是我的清晰单人声音，同意按所选方式在本机保存声纹"}</span></label><button type="submit" class="button primary" id="confirm-voiceprint" ${!state.speakerConfirmed || busy || !state.connected ? "disabled" : ""}>${busy ? "正在保存…" : confirmText}</button>`;
        const updateButton = () => { $("button[type=submit]", consent).disabled = atLimit || !state.speakerConfirmed || state.voiceprintBusy || !state.connected || (person && !!personFieldsError(state.enrollDraft.name.trim(), state.enrollDraft.note.trim())); };
        $("#speaker-consent-check", consent).addEventListener("change", event => { state.speakerConfirmed = event.target.checked; updateButton(); });
        $("#speaker-self-mode", consent)?.addEventListener("change", event => { state.enrollSelfMode = event.target.value; state.speakerConfirmed = false; renderSpeakers(); });
        if (person) {
          $("#speaker-profile-target", consent).addEventListener("change", event => {
            state.enrollReplaceId = event.target.value || null;
            const target = state.profiles.find(profile => profile.id === state.enrollReplaceId);
            state.enrollDraft = {name:target?.name || "", note:target?.note || ""};
            state.speakerConfirmed = false; renderSpeakers();
          });
          $("#speaker-person-name", consent).addEventListener("input", event => { state.enrollDraft.name = event.target.value; updateButton(); });
          $("#speaker-person-note", consent).addEventListener("input", event => { state.enrollDraft.note = event.target.value; updateButton(); });
        }
        consent.addEventListener("submit", event => { event.preventDefault(); if (person) enrollPerson(clipId, speaker.id, analysis.id); else enrollVoiceprint(clipId, speaker.id, analysis.id); });
        card.append(consent); updateButton();
      }
      $("#speaker-list").append(card);
    }
    for (const note of analysis.notes || []) {
      const paragraph = document.createElement("p"); paragraph.className = "speaker-note"; paragraph.textContent = String(note); panel.append(paragraph);
    }
    if (focus) { const input = $(`#${focus.id}`); input?.focus({preventScroll:true}); if (typeof focus.start === "number") input?.setSelectionRange(focus.start, focus.end); }
  }

  async function enrollPerson(clipId, speakerId, analysisId) {
    if (state.voiceprintBusy || state.automaticSaving || !state.speakerConfirmed || state.enrollKind !== "person" || state.speakerConfirmId !== speakerId || selectedClipId() !== clipId) return;
    if (!analysisId || state.speakerAnalysis?.id !== analysisId || state.speakerConfirmAnalysisId !== analysisId) {
      state.speakerConfirmId = null; state.speakerConfirmed = false;
      toast("参考声音已变化，请重新试听再登记。", "error"); renderSpeakers(); return;
    }
    const name = state.enrollDraft.name.trim(), note = state.enrollDraft.note.trim();
    const error = personFieldsError(name, note);
    if (error) { state.voiceprintError = error; renderSpeakers(); return; }
    const payload = {clip_id:clipId, speaker_id:speakerId, analysis_id:analysisId, confirm:true, name, note};
    if (state.enrollReplaceId) payload.replace_profile_id = state.enrollReplaceId;
    await mutateProfile("/api/speaker-profiles/enroll", payload, `「${name}」的声纹与备注已保存在本机。`);
  }

  async function loadSpeakerAnalysis(clipId, version = state.selectionVersion) {
    if (!clipId) return;
    const readVersion = ++state.speakerReadVersion;
    state.speakerLoading = true; renderSpeakers();
    try {
      const response = await api(`/api/clips/${encodeURIComponent(clipId)}/speakers`);
      if (version !== state.selectionVersion || selectedClipId() !== clipId || readVersion !== state.speakerReadVersion) return;
      if (state.speakerConfirmAnalysisId && response.analysis?.id !== state.speakerConfirmAnalysisId) {
        state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
        toast("说话人分析已更新，请重新试听并选择要登记的声音。", "error");
      }
      state.speakerAnalysis = response.analysis; state.speakerError = null;
    } catch (error) {
      if (version !== state.selectionVersion || selectedClipId() !== clipId || readVersion !== state.speakerReadVersion) return;
      state.speakerError = error.message;
    } finally {
      if (version === state.selectionVersion && selectedClipId() === clipId && readVersion === state.speakerReadVersion) { state.speakerLoading = false; renderSpeakers(); }
    }
  }

  async function refreshSelectedTranscript(version = state.selectionVersion) {
    const id = state.transcript?.id;
    if (!id) return;
    const readVersion = ++state.transcriptReadVersion;
    try {
      const response = await api(`/api/transcripts/${encodeURIComponent(id)}`);
      if (version !== state.selectionVersion || state.transcript?.id !== id || readVersion !== state.transcriptReadVersion) return;
      state.transcript = response.transcript || response; renderTranscriptContent();
    } catch (error) { if (version === state.selectionVersion) toast(error.message, "error"); }
  }

  async function reloadSpeakerDetails() {
    const version = state.selectionVersion;
    await Promise.all([loadSpeakerAnalysis(selectedClipId(), version), refreshSelectedTranscript(version), loadDocument({force:true})]);
  }

  async function analyzeSpeakers(clipId) {
    if (state.speakerSubmitting || !state.speakerStatus?.available) return;
    const version = state.selectionVersion;
    state.speakerSubmitting = true; state.speakerError = null;
    state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false; renderSpeakers(); updateDocumentSpeakerStates();
    try {
      const response = await api("/api/speakers/analyze", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({clip_id:clipId, num_speakers:state.speakerNumber === "auto" ? null : Number(state.speakerNumber)})});
      state.speakerJobs.set(response.job.id, response.job); toast("说话人分析已提交，将在本机处理。");
    } catch (error) { if (version === state.selectionVersion) state.speakerError = error.message; toast(error.message, "error", 8000); }
    finally { state.speakerSubmitting = false; renderSpeakers(); updateDocumentSpeakerStates(); }
  }

  async function enrollVoiceprint(clipId, speakerId, analysisId) {
    if (state.voiceprintBusy || state.automaticSaving || !state.speakerConfirmed || state.enrollKind !== "self" || state.speakerConfirmId !== speakerId || selectedClipId() !== clipId) return;
    if (!analysisId || state.speakerAnalysis?.id !== analysisId || state.speakerConfirmAnalysisId !== analysisId) {
      state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
      toast("说话人分析已更新，请重新试听并选择本人声音。", "error"); renderSpeakers(); return;
    }
    const payload = {clip_id:clipId, speaker_id:speakerId, analysis_id:analysisId, confirm:true};
    if (state.speakerStatus?.voiceprint_multisample === true) payload.mode = state.enrollSelfMode;
    state.voiceprintBusy = true; state.profileRevision++; state.voiceprintError = null; renderVoiceprint(); renderSpeakers();
    try {
      const response = await api("/api/voiceprint/enroll", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      state.speakerStatus = {...state.speakerStatus, voiceprint:response.voiceprint};
      state.selfSampleRemoveId = null;
      state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
      await reloadSpeakerDetails(); toast(payload.mode === "append" ? "已保存本人样本，已有样本保留；同一分析中的同一片段不会重复添加。" : "本人声纹已保存在本机，已将所选声音登记为「我」。");
    } catch (error) {
      state.voiceprintError = error.message;
      if (error.status === 400 || error.status === 409) {
        state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
        state.voiceprintError = `${error.message} 请重新试听并选择本人声音。`;
        await reloadSpeakerDetails();
      }
      toast(state.voiceprintError, "error", 8000);
    }
    finally { state.voiceprintBusy = false; renderVoiceprint(); renderSpeakers(); }
  }

  async function clearVoiceprint() {
    if (state.voiceprintBusy || state.automaticSaving || !state.speakerStatus?.voiceprint?.enrolled) return;
    state.voiceprintBusy = true; state.profileRevision++; state.voiceprintError = null; renderVoiceprint(); renderSpeakers();
    try {
      const response = await api("/api/voiceprint/clear", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      state.speakerStatus = {...state.speakerStatus, voiceprint:response.voiceprint};
      state.selfSampleRemoveId = null;
      state.speakerConfirmId = null; state.speakerConfirmAnalysisId = null; state.speakerConfirmed = false;
      await reloadSpeakerDetails(); toast("已取消本人声纹登记，匿名说话人分析仍保留。");
    } catch (error) { state.voiceprintError = error.message; toast(error.message, "error", 8000); }
    finally { state.voiceprintBusy = false; renderVoiceprint(); renderSpeakers(); }
  }

  async function refreshSpeakerJobs(initial = false) {
    try {
      const response = await api("/api/speaker-jobs");
      for (const job of response.items || []) {
        if (job.kind && job.kind !== "diarization") continue;
        const previous = state.speakerJobs.get(job.id);
        state.speakerJobs.set(job.id, job);
        if (previous?.status === job.status) continue;
        if (!initial && previous && job.status === "completed") {
          if (!job.automatic) toast("说话人分析已完成。可试听并主动登记人物。");
          if (selectedClipId() === job.clip_id) await reloadSpeakerDetails();
        } else if (!initial && previous && job.status === "failed") toast(job.error || "说话人分析失败，请查看详情后重试。", "error", 8000);
        if (selectedClipId() === job.clip_id) renderSpeakers();
      }
    } catch (error) { state.speakerError = error.message; renderSpeakers(); }
    finally { updateDocumentSpeakerStates(); }
  }

  function renderTranscriptContent() {
    const content = $("#transcript-content");
    if (!content) return;
    const transcript = state.transcript;
    content.replaceChildren(); content.className = "";
    if (state.transcriptLoading) content.innerHTML = `<div class="transcription-empty"><span class="spinner" aria-hidden="true"></span><p>正在读取转写…</p></div>`;
    else if (!transcript) content.innerHTML = `<div class="transcription-empty">${icon("text")}<p>这段声音，还没有文字。<br>选择语言，开始第一次本地转写。</p></div>`;
    else {
      content.className = "transcript-body";
      const segments = Array.isArray(transcript.segments) ? transcript.segments.filter(segment => typeof segment.text === "string" && segment.text.trim()) : [];
      if (segments.length) segments.forEach(segment => {
        const element = document.createElement("div"); element.className = "segment";
        const time = Number(segment.start) || 0;
        element.innerHTML = `<button class="segment-time" title="播放到 ${formatTime(time)}" aria-label="从 ${formatTime(time)} 开始播放" ${selectedClipId() ? "" : "disabled"}>${formatTime(time)}</button><div class="segment-content"><p></p></div>`;
        const labels = Array.isArray(segment.speaker_labels) ? [...new Set(segment.speaker_labels.filter(label => typeof label === "string"))] : [];
        const speakerIds = Array.isArray(segment.speaker_ids) ? new Set(segment.speaker_ids) : new Set();
        if (labels.length) {
          const tags = document.createElement("div"); tags.className = "speaker-segment-labels";
          for (const label of labels) { const tag = document.createElement("span"); tag.className = `speaker-segment-label${label === "我" ? " mine" : ""}`; tag.textContent = label; tags.append(tag); }
          if (Math.max(labels.length, speakerIds.size) > 1) { const mixed = document.createElement("span"); mixed.className = "speaker-segment-mixed"; mixed.textContent = "混合发言"; mixed.title = "这一段覆盖多位说话人，文字尚未按人拆分。"; tags.append(mixed); }
          $(".segment-content", element).prepend(tags);
        }
        $("p", element).textContent = segment.text;
        $("button", element).addEventListener("click", () => playFrom(time)); content.append(element);
      });
      else { const paragraph = document.createElement("p"); paragraph.className = "transcript-paragraph"; paragraph.textContent = transcript.text || "识别已完成，这段音频中未获得可转写的文字。"; content.append(paragraph); }
    }
  }

  function renderDetail() {
    const panel = $("#detail-panel");
    if (!state.clip && !state.transcript) return;
    const active = panel.contains(document.activeElement) ? document.activeElement : null;
    const focus = active?.id ? {id:active.id, start:active.selectionStart, end:active.selectionEnd} : null;
    const oldPlayer = $("#audio-player");
    const clip = state.clip;
    const transcript = state.transcript;
    const clipId = clip?.id || transcript?.clip_id;
    const oldPlayback = oldPlayer?.dataset.clipId === clipId ? {time: oldPlayer.currentTime, paused: oldPlayer.paused} : null;
    const name = clip ? clipName(clip) : transcriptName(transcript);
    const job = clipId ? latestJob(clipId) : null;
    const running = job && ["queued", "running"].includes(job.status);
    const hasModel = (state.status?.models || []).length > 0;
    const date = clip ? clip.start_time || clip.created_time : transcript?.created_at;
    const duration = clip?.duration ?? transcript?.duration_seconds ?? 0;
    const subtitle = `${formatDate(date, true)} · ${sourceName(clip?.source || transcript?.source)}`;
    panel.innerHTML = `<div class="detail-heading"><div class="detail-eyebrow"><span>${icon("wave")} ${clipId ? "录音详情" : "转写详情"}</span><span>${icon("lock")} 本地文件</span></div><h2>${escapeHTML(name)}</h2><p class="detail-subtitle">${escapeHTML(subtitle)}</p></div>
      ${clipId ? `<section class="audio-section" aria-label="播放录音"><div class="audio-label"><strong>${icon("headphones")}音频预览</strong><span>${formatTime(duration)} · WAV</span></div><audio id="audio-player" controls preload="metadata" data-clip-id="${escapeHTML(clipId)}" src="/api/clips/${encodeURIComponent(clipId)}/audio" aria-label="播放 ${escapeHTML(name)}"></audio><p class="audio-error" id="audio-error" hidden>此音频暂时无法播放，文件可能已超过保留时间。转写文字仍可查看。</p></section>` : ""}
      ${clipId ? '<section id="speaker-panel" class="speaker-panel" aria-label="说话人分析"></section>' : ""}
      <section class="detail-body" aria-label="转写内容"><div class="transcription-toolbar"><h3>${icon("text")}转写内容</h3><div class="transcript-actions"><button class="icon-button" id="copy-transcript" aria-label="复制转写文字" title="复制文字" ${transcript?.text ? "" : "disabled"}>${icon("copy")}</button><button class="icon-button" id="download-transcript" aria-label="下载转写文本" title="下载 TXT" ${transcript?.text ? "" : "disabled"}>${icon("download")}</button></div></div>
      <div id="job-message" role="status" aria-live="polite"></div><div id="transcript-content"></div>
      ${clipId ? `<div class="transcription-controls"><label class="language-control"><span class="sr-only">识别语言</span><select id="language-select" ${running ? "disabled" : ""}><option value="auto">自动识别</option><option value="zh">中文</option><option value="en">English</option></select></label><button class="button primary transcribe-button" id="transcribe-button" ${running || !hasModel ? "disabled" : ""}>${running ? '<span class="spinner" aria-hidden="true"></span>' : icon("wave")}<span>${running ? "正在本地转写" : transcript ? "重新转写" : "本地转写"}</span></button></div><p class="detail-help">${running ? "模型正在处理音频，可以继续浏览其他录音。" : "仅在点击后开始识别，音频不会上传到云端。"}</p>${!hasModel ? '<p class="model-warning">尚未找到本地识别模型。请先在本机配置兼容的 Whisper 模型，再开始转写。</p>' : ""}` : ""}
      ${transcript ? `<p class="transcript-footnote">${escapeHTML(languageLabel(transcript.language))} · ${transcript.elapsed_seconds !== undefined ? `识别耗时 ${Number(transcript.elapsed_seconds).toFixed(1)} 秒 · ` : ""}${escapeHTML(formatDate(transcript.created_at))}</p>` : ""}</section>`;
    const player = $("#audio-player");
    if (player) {
      player.addEventListener("error", () => { $("#audio-error").hidden = false; });
      if (oldPlayback && oldPlayback.time > 0) player.addEventListener("loadedmetadata", () => {
        player.currentTime = Math.min(oldPlayback.time, Math.max(0, player.duration - .05));
        if (!oldPlayback.paused) player.play().catch(() => {});
      }, {once:true});
    }
    if (running) $("#job-message").innerHTML = `<div class="job-status"><span class="spinner" aria-hidden="true"></span><span>${job.status === "queued" ? "任务已提交，正在等待本地引擎。" : "本地模型正在识别，请稍候…"}</span></div>`;
    else if (job?.status === "failed") {
      $("#job-message").innerHTML = `<div class="job-status failed">${icon("alert")}<span></span></div>`;
      $("#job-message span").textContent = String(job.error || "转写未完成，可以重试。").slice(0, 600);
    }
    renderTranscriptContent();
    renderSpeakers();
    $("#copy-transcript").addEventListener("click", () => copyTranscript());
    $("#download-transcript").addEventListener("click", () => downloadTranscript());
    if (clipId) {
      $("#language-select").value = state.language;
      $("#language-select").addEventListener("change", event => { state.language = event.target.value; });
      $("#transcribe-button").addEventListener("click", () => startTranscription(clipId));
    }
    if (focus) { const input = $(`#${focus.id}`); input?.focus({preventScroll:true}); if (typeof focus.start === "number") input?.setSelectionRange(focus.start, focus.end); }
  }

  function scrollToDetail(shouldScroll) {
    if (shouldScroll && matchMedia("(max-width: 1000px)").matches) $("#detail-panel").scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start"});
  }

  async function selectClip(clip, shouldScroll = true) {
    const version = ++state.selectionVersion;
    const profileRevision = state.profileRevision;
    state.selectedKey = `clips:${clip.id}`; state.clip = clip; state.transcript = null;
    state.transcriptLoading = !!clip.transcribed;
    resetSpeakerSelection();
    renderList(); renderDetail(); scrollToDetail(shouldScroll);
    loadSpeakerAnalysis(clip.id, version);
    try {
      let transcript = null;
      if (clip.latest_transcript?.id) transcript = await api(`/api/transcripts/${encodeURIComponent(clip.latest_transcript.id)}`);
      else {
        const results = await api(`/api/transcripts?clip_id=${encodeURIComponent(clip.id)}&limit=1&offset=0`);
        transcript = results.items?.[0] || null;
      }
      if (version !== state.selectionVersion) return;
      const transcriptId = (transcript?.transcript || transcript)?.id;
      if (transcriptId && profileRevision !== state.profileRevision) transcript = await api(`/api/transcripts/${encodeURIComponent(transcriptId)}`);
      if (version !== state.selectionVersion) return;
      state.transcript = transcript?.transcript || transcript;
      state.transcriptLoading = false; renderDetail();
    } catch (error) {
      if (version !== state.selectionVersion) return;
      state.transcriptLoading = false; renderDetail(); toast(error.message, "error");
    }
  }

  async function selectTranscript(item, shouldScroll = true) {
    const version = ++state.selectionVersion;
    const profileRevision = state.profileRevision;
    state.selectedKey = `transcripts:${item.id}`;
    state.transcript = item;
    state.clip = state.clipCache.get(item.clip_id) || null;
    state.transcriptLoading = true;
    resetSpeakerSelection();
    renderList(); renderDetail(); scrollToDetail(shouldScroll);
    loadSpeakerAnalysis(item.clip_id, version);
    try {
      let result = await api(`/api/transcripts/${encodeURIComponent(item.id)}`);
      if (version !== state.selectionVersion) return;
      if (profileRevision !== state.profileRevision) result = await api(`/api/transcripts/${encodeURIComponent(item.id)}`);
      if (version !== state.selectionVersion) return;
      state.transcript = result.transcript || result;
      state.transcriptLoading = false; renderDetail();
    } catch (error) {
      if (version !== state.selectionVersion) return;
      state.transcriptLoading = false; renderDetail(); toast(error.message, "error");
    }
  }

  async function startTranscription(clipId) {
    const button = $("#transcribe-button");
    if (button) { button.disabled = true; $("span:last-child", button).textContent = "正在提交…"; }
    try {
      const response = await api("/api/transcribe", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({clip_id:clipId, language:state.language})});
      state.jobs.set(response.job.id, response.job);
      if (state.clip?.id === clipId || state.transcript?.clip_id === clipId) renderDetail();
      renderList(); toast("转写任务已提交，正在使用本地模型。");
    } catch (error) { renderDetail(); toast(error.message, "error", 7000); }
  }

  async function copyTranscript() {
    const text = state.transcript?.text;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast("转写文字已复制");
    } catch (_) { toast("无法访问剪贴板，可以下载 TXT 文件或手动选中文字。", "error"); }
  }

  function downloadTranscript() {
    if (!state.transcript?.text) return;
    const blob = new Blob([state.transcript.text], {type:"text/plain;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${(state.clip ? clipName(state.clip) : transcriptName(state.transcript)).replace(/[\\/:*?"<>|]/g, "_").replace(/\.wav$/i, "")}.txt`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function refreshStatus() {
    const oldCount = state.status?.storage?.clip_count;
    const oldTranscripts = state.status?.transcript_count;
    const gateRevision = state.gateRevision, gateWasSaving = state.gateSaving;
    state.status = await api("/api/status");
    const gateFresh = gateRevision === state.gateRevision && !gateWasSaving && !state.gateSaving;
    if (state.status.live) {
      // Other live counters can refresh while a threshold POST is pending, but
      // a pre-save response must not replace the newly saved/applied values.
      if (!gateFresh && state.live) for (const key of ["trigger_db", "trigger_threshold", "release_threshold", "configured_trigger_db", "configured_trigger_threshold", "settings_pending", "settings_error"]) {
        if (key in state.live) state.status.live[key] = state.live[key];
        else delete state.status.live[key];
      }
      state.live = state.status.live;
    }
    if (gateFresh && !state.gateLoading && state.live) {
      const configured = state.live.configured_trigger_db;
      if (validGateDb(configured)) {
        const threshold = 10 ** (configured / 20);
        state.gateSettings = {trigger_db:configured, trigger_threshold:threshold, release_threshold:threshold * (2 / 3)};
        if (!state.gateDirty) state.gateDraftDb = configured;
        state.gateDirty = Math.abs(state.gateDraftDb - configured) > .000001;
        state.gatePending = typeof state.live.settings_pending === "boolean" ? state.live.settings_pending
          : Math.abs(appliedTriggerDb() - configured) > .00001;
      }
    }
    setConnection(true); renderStatus();
    return oldCount !== state.status.storage?.clip_count || oldTranscripts !== state.status.transcript_count;
  }

  async function refreshJobs(initial = false) {
    const response = await api("/api/jobs");
    let changed = false, updatedLibrary = false;
    for (const job of response.items || []) {
      const previous = state.jobs.get(job.id);
      state.jobs.set(job.id, job);
      if (previous?.status === job.status) continue;
      changed = true;
      if (!initial && previous && job.status === "completed") {
        updatedLibrary = true;
        toast("转写已完成，文字已保存到本地。");
        if (state.clip?.id === job.clip_id || state.transcript?.clip_id === job.clip_id) {
          const selectionVersion = state.selectionVersion;
          let result = job.result;
          if (result?.id) { const full = await api(`/api/transcripts/${encodeURIComponent(result.id)}`); result = full.transcript || full; }
          if (selectionVersion !== state.selectionVersion || selectedClipId() !== job.clip_id) continue;
          if (result) state.transcript = result;
          if (state.clip) state.clip = {...state.clip, transcribed:true, latest_transcript:result};
          state.transcriptLoading = false; renderDetail();
        }
      } else if (!initial && previous && job.status === "failed") {
        toast(job.error || "本地转写失败，请查看详情后重试。", "error", 8000);
        if (state.clip?.id === job.clip_id || state.transcript?.clip_id === job.clip_id) renderDetail();
      } else if (!initial && (state.clip?.id === job.clip_id || state.transcript?.clip_id === job.clip_id)) renderDetail();
    }
    if (changed) renderList();
    return updatedLibrary;
  }

  async function refreshAll({quiet = false} = {}) {
    $("#refresh-button").classList.add("is-loading");
    try {
      await refreshStatus();
      await refreshJobs(state.initial);
      await loadSpeakerStatus();
      await loadProfiles();
      await refreshSpeakerJobs(state.initial);
      await Promise.all([loadList({quiet}), loadDocument({force:true})]);
      state.initial = false;
    } catch (error) {
      setConnection(false, error.message);
      state.documentError = error.message; renderDocumentState();
      if (state.loading) { state.loading = false; state.listError = error.message; renderList(); }
      if (!quiet) toast(error.message, "error");
    }
    finally { $("#refresh-button").classList.remove("is-loading"); }
  }

  async function connect() {
    try {
      const session = await api("/api/session"); state.token = session.token;
      await refreshAll();
      loadDevices();
      loadGateSettings();
      if (state.page === "storage") loadStorage();
      if (state.page === "mcp") loadMcp();
    } catch (error) {
      state.loading = false; state.listError = error.message; renderList(); setConnection(false, error.message);
    }
  }

  function switchView(view) {
    openLibrary(view);
    if (state.view === view) return;
    clearTimeout(searchTimer);
    state.view = view; state.offset = 0; state.query = "";
    $("#search-input").value = "";
    $("#search-input").placeholder = view === "clips" ? "搜索录音名称或来源" : "搜索转写文字";
    $(".filter-field").hidden = view !== "clips";
    $$('[data-view]').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      if (button.getAttribute("role") === "tab") { button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; }
    });
    loadList();
  }

  async function importFiles(files) {
    if (state.storageSaving) { toast("正在复制资料并切换目录，请等待完成后再导入。", "error"); return; }
    if (state.importing) { toast("正在导入文件，请稍候。", "error"); return; }
    const accepted = [], importToPeople = state.page === "people";
    for (const file of files) {
      if (!/\.wav$/i.test(file.name)) { toast(`「${file.name}」不是 WAV 文件，目前只支持导入 WAV。`, "error"); continue; }
      if (file.size > 512 * 1024 * 1024) { toast(`「${file.name}」超过单个文件 512 MB 的上限。`, "error"); continue; }
      if (!file.size) { toast(`「${file.name}」是空文件。`, "error"); continue; }
      accepted.push(file);
    }
    if (!accepted.length) return;
    state.importing = true;
    const button = $("#import-button"); button.disabled = true;
    let lastClip = null, count = 0;
    try {
      for (let index = 0; index < accepted.length; index++) {
        $("span", button).textContent = accepted.length > 1 ? `导入 ${index + 1}/${accepted.length}` : "导入中…";
        const file = accepted[index];
        const response = await api("/api/import", {method:"POST", headers:{"Content-Type":"audio/wav", "X-Filename":encodeURIComponent(file.name)}, body:file});
        lastClip = response.clip; count++;
      }
      toast(count === 1 && lastClip?.deduplicated ? "这段录音已在资料库中，已为你打开。" : `已将 ${count} 个录音导入本地资料库。`);
    } catch (error) { toast(`${count ? `已导入 ${count} 个录音。` : ""}${error.message}`, "error", 8000); }
    finally {
      state.importing = false; button.disabled = false; $("span", button).textContent = "导入录音"; $("#file-input").value = "";
      if (lastClip) {
        if (!importToPeople) openLibrary("clips");
        clearTimeout(searchTimer);
        state.clipCache.set(lastClip.id, lastClip); state.offset = 0; state.query = ""; state.filter = "all";
        $("#search-input").value = ""; $("#status-filter").value = "all";
        if (!importToPeople && state.view !== "clips") switchView("clips");
        await refreshAll({quiet:true}); selectClip(lastClip, !importToPeople);
        if (importToPeople) { showWorkspace("people"); loadPeopleClips(); }
      }
    }
  }

  async function poll() {
    if (state.polling || state.storageSaving || !state.token || (document.hidden && !liveIsBusy())) return;
    state.polling = true;
    state.lastPollAt = Date.now();
    try {
      const changed = await refreshStatus();
      const completed = await refreshJobs();
      await refreshSpeakerJobs();
      await loadSpeakerStatus();
      await loadProfiles();
      await loadDocument();
      if (changed || completed) await loadList({quiet:true});
      if (changed && state.page === "people") await loadPeopleClips();
      if (state.page === "storage") await loadStorage();
    } catch (error) { setConnection(false, error.message); state.documentError = error.message; renderDocumentState(); }
    finally { state.polling = false; }
  }

  $$('[data-view]').forEach(button => button.addEventListener("click", () => {
    switchView(button.dataset.view);
    if (button.closest(".sidebar-nav")) $("#library-disclosure").scrollIntoView({behavior:"smooth", block:"start"});
  }));
  $("#nav-document").addEventListener("click", showDocument);
  $("#nav-people").addEventListener("click", openPeople);
  $("#nav-storage").addEventListener("click", openStorage);
  $("#nav-mcp").addEventListener("click", openMcp);
  $("#mcp-back-document").addEventListener("click", showDocument);
  $("#mcp-refresh").addEventListener("click", loadMcp);
  $("#mcp-setup-scope").addEventListener("change", () => renderMcp());
  $("#mcp-setup-codex").addEventListener("click", () => copyMcpSetup("codex"));
  $("#mcp-setup-claude").addEventListener("click", () => copyMcpSetup("claude"));
  $("#storage-back-document").addEventListener("click", showDocument);
  $("#storage-refresh").addEventListener("click", loadStorage);
  $("#storage-directory").addEventListener("input", event => {
    state.storageDraft = event.target.value; state.storageError = null; state.storageSuccess = null; renderStorage();
  });
  $("#storage-use-default").addEventListener("click", () => {
    if (state.storageSaving || !state.storage?.default_dir) return;
    state.storageDraft = state.storage.default_dir; state.storageError = null; state.storageSuccess = null; renderStorage();
    $("#storage-directory").focus({preventScroll:true});
  });
  $("#storage-form").addEventListener("submit", event => { event.preventDefault(); saveStorage(); });
  $("#open-people").addEventListener("click", openPeople);
  $("#people-back-document").addEventListener("click", showDocument);
  $("#people-refresh").addEventListener("click", () => { loadProfiles(); loadSpeakerStatus(); loadPeopleClips(); });
  $("#people-import").addEventListener("click", () => $("#file-input").click());
  $("#people-recording-search").addEventListener("input", event => {
    clearTimeout(peopleSearchTimer);
    const query = event.target.value.trim();
    peopleSearchTimer = setTimeout(() => { state.peopleQuery = query; state.peopleOffset = 0; loadPeopleClips(); }, 250);
  });
  $("#people-recording-previous").addEventListener("click", () => { state.peopleOffset = Math.max(0, state.peopleOffset - state.peopleLimit); loadPeopleClips(); });
  $("#people-recording-next").addEventListener("click", () => { state.peopleOffset += state.peopleLimit; loadPeopleClips(); });
  $("#document-scope").addEventListener("change", () => loadDocument({force:true}));
  $("#document-refresh").addEventListener("click", () => loadDocument({force:true}));
  $("#document-copy-all").addEventListener("click", () => {
    if (!$("#document-copy-all").disabled) copyText(AudioDocument.plainText(state.documentItems), `已复制全部 ${state.documentItems.length} 段完整转写（含时间）`);
  });
  $("#document-download").addEventListener("click", () => {
    if ($("#document-download").disabled) return;
    const url = URL.createObjectURL(new Blob([AudioDocument.plainText(state.documentItems)], {type:"text/plain;charset=utf-8"}));
    const link = document.createElement("a"); link.href = url;
    link.download = `完整转写-${AudioDocument.stamp(Date.now() / 1000, true).replace(/[\\/]/g, "-")}.txt`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("#document-new").addEventListener("click", () => {
    if (state.documentPending) { window.getSelection()?.removeAllRanges(); commitDocument(state.documentPending); }
    $("#document-body .document-entry")?.scrollIntoView({behavior:"smooth", block:"start"});
    $("#document-new").hidden = true;
  });
  document.addEventListener("selectionchange", () => {
    if (state.documentPending && !documentSelectionActive()) commitDocument(state.documentPending);
  });
  $(".view-tabs").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const view = event.key === "Home" ? "clips" : event.key === "End" ? "transcripts" : state.view === "clips" ? "transcripts" : "clips";
    switchView(view); $(`#tab-${view}`).focus();
  });
  $("#search-input").addEventListener("input", event => {
    clearTimeout(searchTimer);
    const query = event.target.value.trim();
    searchTimer = setTimeout(() => { state.query = query; state.offset = 0; loadList(); }, 250);
  });
  $("#status-filter").addEventListener("change", event => { state.filter = event.target.value; state.offset = 0; loadList(); });
  $("#refresh-button").addEventListener("click", () => refreshAll());
  $("#reconnect-button").addEventListener("click", connect);
  $("#previous-button").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadList(); });
  $("#next-button").addEventListener("click", () => { state.offset += state.limit; loadList(); });
  $("#import-button").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", event => importFiles(Array.from(event.target.files || [])));
  $("#refresh-devices").addEventListener("click", loadDevices);
  $("#live-start").addEventListener("click", startLive);
  $("#live-stop").addEventListener("click", stopLive);
  $("#vad-enabled").addEventListener("change", event => { state.vadEnabled = event.target.checked; renderLive(); });
  $("#voiceprint-clear").addEventListener("click", clearVoiceprint);
  $("#speaker-automatic").addEventListener("change", event => saveAutomaticSpeakers(event.target.checked));
  $("#voiceprint-setup-button").addEventListener("click", () => {
    const steps = $("#voiceprint-setup-steps");
    steps.hidden = !steps.hidden;
    $("#voiceprint-setup-button").setAttribute("aria-expanded", String(!steps.hidden));
  });
  $("#voiceprint-choose-recording").addEventListener("click", () => {
    $(".people-enrollment").scrollIntoView({behavior:"smooth", block:"start"});
    $("#people-recording-search").focus({preventScroll:true});
  });
  $("#voiceprint-import").addEventListener("click", () => $("#file-input").click());
  $("#voiceprint-select-done").addEventListener("click", () => { $("#voiceprint-select-hint").hidden = true; });
  $("#gate-range").addEventListener("input", event => changeGateDraft(Number(event.target.value)));
  $$('[data-gate-db]').forEach(button => button.addEventListener("click", () => changeGateDraft(Number(button.dataset.gateDb))));
  $("#gate-save").addEventListener("click", saveGateSettings);
  window.addEventListener("dragenter", event => {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault(); dragDepth++; $("#drop-overlay").hidden = false;
  });
  window.addEventListener("dragover", event => { if (Array.from(event.dataTransfer?.types || []).includes("Files")) event.preventDefault(); });
  window.addEventListener("dragleave", event => { event.preventDefault(); dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#drop-overlay").hidden = true; });
  window.addEventListener("drop", event => { event.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true; importFiles(Array.from(event.dataTransfer?.files || [])); });
  window.addEventListener("keydown", event => { if (event.key === "Escape") { dragDepth = 0; $("#drop-overlay").hidden = true; } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#storage") openStorage();
    else if (location.hash === "#mcp") openMcp();
    else if (["storage", "mcp"].includes(state.page)) showDocument();
  });
  if (["#storage", "#mcp"].includes(location.hash)) showWorkspace(location.hash.slice(1));
  renderStorage();
  renderMcp();
  renderList(); connect();
  setInterval(() => { if (liveIsBusy() || Date.now() - state.lastPollAt >= 3500) poll(); }, 1000);
})();
