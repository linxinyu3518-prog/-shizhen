/* SPDX-License-Identifier: GPL-3.0-or-later */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { health: null, media: null, analyzing: false, creating: false, jobs: new Map(), polling: false, timer: null, toastTimer: null, douyinSession: null, sessionBusy: false, browserJob: null, browserTerminal: null, browserCancelling: false, upscale: null, upscaleBusy: false, upscaleError: "", compute: null, computeBusy: false, computeError: "" };
  const activeStatuses = new Set(["queued", "downloading", "converting"]);
  const statusLabels = { queued: "排队中", downloading: "正在下载", converting: "正在转换", completed: "已完成", failed: "提取失败", cancelled: "已取消" };
  const kindLabels = { video: "视频", audio: "音频", original: "原文件" };
  const qualityLabels = { balanced: "均衡", high: "高保真", maximum: "最高质量" };
  const frameModeLabels = { source: "跟随来源", fast: "快速转换", interpolate: "流畅插帧" };
  const processingLabels = { auto: "GPU 优先", gpu: "仅 GPU", cpu: "CPU 兼容" };
  const upscaleLabels = { none: "关闭 AI 超分", auto: "自动超分（RTX 优先）", rtx: "RTX AI 超分", comfyui: "ComfyUI 超分" };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showError(id, message) {
    $(id).textContent = message || "";
    $(id).hidden = !message;
  }

  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }

  function errorMessage(body, fallback) {
    if (!body || typeof body !== "object") return fallback;
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg || "参数不正确").join("；");
    if (body.detail && typeof body.detail.message === "string") return body.detail.message;
    if (typeof body.message === "string") return body.message;
    if (typeof body.error === "string") return body.error;
    return fallback;
  }

  function responseError(body, fallback) {
    const error = new Error(errorMessage(body, fallback));
    error.code = body?.code || body?.detail?.code;
    error.normalizedUrl = body?.normalized_url || body?.detail?.normalized_url;
    error.diagnostics = body?.diagnostics || body?.detail?.diagnostics;
    error.sourceId = body?.source_id || body?.detail?.source_id;
    return error;
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...options.headers };
    if (options.method === "POST") {
      if (!state.health?.token) await loadHealth();
      if (!state.health?.token) throw new Error("本地服务尚未连接，请检查服务后重试。");
      headers["Content-Type"] = "application/json";
      headers["X-App-Token"] = state.health.token;
    }
    let response;
    try { response = await fetch(path, { cache: "no-store", ...options, headers }); }
    catch { throw new Error("无法连接本地服务。请确认程序仍在运行，然后重试。"); }
    let body;
    try { body = await response.json(); }
    catch { throw new Error(`本地服务返回了无法读取的响应（${response.status}）。`); }
    if (!response.ok) {
      throw responseError(body, `操作失败（${response.status}），请稍后重试。`);
    }
    return body;
  }

  function updateSessionControls() {
    const file = $("douyin-cookie-file").files[0];
    $("douyin-cookie-file").disabled = state.sessionBusy;
    $("douyin-import-button").disabled = state.sessionBusy || !file || file.size === 0 || file.size > 256 * 1024;
    $("douyin-clear-button").disabled = state.sessionBusy || !state.douyinSession?.configured;
    $("douyin-dialog").setAttribute("aria-busy", String(state.sessionBusy));
  }

  function renderSession(session) {
    state.douyinSession = session;
    const count = Math.max(0, Number(session?.count) || 0);
    $("douyin-status").textContent = session?.configured ? `已导入 ${count} 条抖音 Cookie · 仅当前服务有效` : "未配置 · 使用公开访问";
    updateSessionControls();
  }

  async function openSessionSettings() {
    if (!$("douyin-dialog").open) $("douyin-dialog").showModal();
    if (state.sessionBusy) return;
    showError("douyin-error", "");
    state.sessionBusy = true;
    $("douyin-status").textContent = "正在读取本地会话状态…";
    updateSessionControls();
    try { renderSession(await api("/api/session/douyin")); }
    catch (error) { state.douyinSession = null; $("douyin-status").textContent = "状态暂不可用"; showError("douyin-error", error.message); }
    finally { state.sessionBusy = false; updateSessionControls(); }
  }

  async function importSession() {
    if (state.sessionBusy) return;
    const file = $("douyin-cookie-file").files[0];
    showError("douyin-error", "");
    if (!file || file.size === 0 || file.size > 256 * 1024) {
      showError("douyin-error", "请选择非空的 Netscape cookies.txt 文件，大小不超过 256 KiB。");
      return;
    }
    state.sessionBusy = true;
    updateSessionControls();
    try {
      // Read only on explicit import; cookie contents are never kept in app state or browser storage.
      renderSession(await api("/api/session/douyin", { method: "POST", body: JSON.stringify({ cookies: await file.text() }) }));
      toast("抖音会话已导入，请重新解析视频链接。");
    } catch (error) { showError("douyin-error", error.message); }
    finally { $("douyin-cookie-file").value = ""; state.sessionBusy = false; updateSessionControls(); }
  }

  async function clearSession() {
    if (state.sessionBusy) return;
    showError("douyin-error", "");
    state.sessionBusy = true;
    updateSessionControls();
    try {
      renderSession(await api("/api/session/douyin/clear", { method: "POST", body: "{}" }));
      $("douyin-cookie-file").value = "";
      toast("已清除本地服务中的抖音会话。");
    } catch (error) { showError("douyin-error", error.message); }
    finally { state.sessionBusy = false; updateSessionControls(); }
  }

  function upscaleProvider(id) {
    const providers = Array.isArray(state.upscale?.providers) ? state.upscale.providers : [];
    return providers.find((provider) => provider?.id === id);
  }

  function selectedUpscaleProvider() {
    const selected = $("video-upscale").value;
    if (selected === "auto") return [upscaleProvider("rtx"), upscaleProvider("comfyui")].find((provider) => provider?.available);
    return upscaleProvider(selected);
  }

  function updateUpscaleControls() {
    $("comfyui-url").disabled = state.upscaleBusy;
    $("comfyui-model").disabled = state.upscaleBusy;
    $("upscale-save-button").disabled = state.upscaleBusy;
    $("upscale-save-button").firstChild.textContent = state.upscaleBusy ? "正在检测… " : "检测并保存 ";
    $("upscale-dialog").setAttribute("aria-busy", String(state.upscaleBusy));
  }

  function renderUpscaleSettings(settings) {
    state.upscale = settings;
    state.upscaleError = "";
    $("comfyui-url").value = settings.comfyui_url || "http://127.0.0.1:8188";
    const model = String(settings.comfyui_model || "");
    const models = Array.isArray(settings.models) ? settings.models.filter((item) => typeof item === "string" && item) : [];
    const select = $("comfyui-model");
    select.replaceChildren(new Option(models.length ? "请选择超分模型" : "尚未检测到超分模型", ""));
    for (const name of new Set(models)) select.add(new Option(name, name));
    if (model && !models.includes(model)) select.add(new Option(`${model} · 已保存，尚未检测到`, model));
    select.value = model;
    renderUpscaleProviders();
    updateExport();
  }

  function renderUpscaleProviders() {
    const list = $("upscale-providers");
    list.replaceChildren();
    for (const id of ["rtx", "comfyui"]) {
      const provider = upscaleProvider(id);
      const row = element("div", "upscale-provider");
      const heading = element("p", "upscale-provider-heading");
      heading.append(element("strong", "", provider?.name || upscaleLabels[id]), element("span", provider?.available ? "provider-ready" : "provider-missing", provider?.available ? "已就绪 ✓" : "未就绪"));
      row.append(heading);
      row.append(element("p", "muted-text", provider?.reason || (state.upscaleError ? "未能获取接口状态，请重新检测。" : provider?.available ? "可以使用此服务进行 AI 超分。" : "尚未检测到可用的超分接口。")));
      list.append(row);
    }
  }

  async function loadUpscaleSettings() {
    if (state.upscaleBusy) return;
    state.upscaleBusy = true;
    updateUpscaleControls();
    try { renderUpscaleSettings(await api("/api/upscale")); }
    catch (error) {
      state.upscale = null;
      state.upscaleError = error.message;
      renderUpscaleProviders();
      if ($("upscale-dialog").open) showError("upscale-error", error.message);
    } finally { state.upscaleBusy = false; updateUpscaleControls(); updateExport(); }
  }

  async function openUpscaleSettings() {
    if (!$("upscale-dialog").open) $("upscale-dialog").showModal();
    showError("upscale-error", "");
    $("upscale-save-status").hidden = true;
    await loadUpscaleSettings();
  }

  async function saveUpscaleSettings() {
    if (state.upscaleBusy) return;
    showError("upscale-error", "");
    $("upscale-save-status").hidden = true;
    let url;
    try {
      url = new URL($("comfyui-url").value.trim());
      const local = url.hostname === "localhost" || url.hostname === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
      if (!local || !["http:", "https:"].includes(url.protocol) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error();
    } catch {
      showError("upscale-error", "请输入本机 ComfyUI 地址，例如 http://127.0.0.1:8188；不要填写网页路径、账号或外部地址。");
      $("comfyui-url").focus();
      return;
    }
    state.upscaleBusy = true;
    updateUpscaleControls();
    const payload = { comfyui_url: url.origin, comfyui_model: $("comfyui-model").value };
    try {
      renderUpscaleSettings(await api("/api/upscale/configure", { method: "POST", body: JSON.stringify(payload) }));
      const provider = upscaleProvider("comfyui");
      $("upscale-save-status").textContent = provider?.available ? "配置已保存，ComfyUI 超分已就绪。" : state.upscale.models?.length && !state.upscale.comfyui_model ? "已连接 ComfyUI。请选择一个超分模型，再点击检测并保存。" : "配置已保存。ComfyUI 超分暂未就绪，详情见上方状态；服务准备好后可再次检测。";
      $("upscale-save-status").hidden = false;
    } catch (error) { showError("upscale-error", error.message); }
    finally { state.upscaleBusy = false; updateUpscaleControls(); updateExport(); }
  }

  function showNormalizedUrl(value) {
    const note = $("analyze-normalized");
    note.replaceChildren();
    note.hidden = true;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol)) return;
      const link = element("a", "text-link", url.href);
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      note.append(document.createTextNode("已识别视频地址："), link);
      note.hidden = false;
    } catch { /* An absent or invalid canonical URL does not need a message. */ }
  }

  function renderDiagnostics(diagnostics, sourceId) {
    const details = $("analyze-diagnostics");
    const entries = Array.isArray(diagnostics) ? diagnostics.filter((item) => item && typeof item === "object") : [];
    const labels = { success: "已完成", completed: "已完成", failed: "未成功", error: "未成功", skipped: "已跳过", blocked: "访问受限", unavailable: "不可用", waiting: "等待操作", attempted: "已尝试", not_found: "未找到媒体" };
    $("diagnostics-list").replaceChildren();
    $("diagnostics-count").textContent = entries.length ? `${entries.length} 个步骤` : "";
    $("diagnostics-source").textContent = sourceId ? `视频 ID：${String(sourceId)}` : "";
    $("diagnostics-source").hidden = !sourceId;
    for (const item of entries) {
      const row = element("li", "diagnostics-step");
      const heading = element("div", "diagnostics-step-heading");
      heading.append(element("strong", "", String(item.label || item.strategy || "解析步骤")));
      const status = [labels[item.status] || String(item.status || "已尝试"), item.http_status ? `HTTP ${String(item.http_status)}` : ""].filter(Boolean).join(" · ");
      heading.append(element("span", "diagnostics-step-status", status));
      row.append(heading);
      if (item.message) row.append(element("p", "", String(item.message)));
      $("diagnostics-list").append(row);
    }
    details.hidden = entries.length === 0 && !sourceId;
    if (details.hidden) details.open = false;
  }

  function updateAnalyzeMode() {
    const browser = $("analyze-mode").value === "browser";
    $("analyze-mode-hint").textContent = browser
      ? state.health?.browser === false ? "浏览器解析组件尚未就绪，请查看本地运行环境，并按项目说明安装浏览器支持。" : "将打开独立浏览器。在其中登录并播放此视频，解析完成后自动返回。"
      : "先尝试页面和平台解析；需要登录时，可切换到浏览器解析。";
    $("analyze-browser-button").disabled = state.analyzing;
  }

  async function loadHealth() {
    try {
      const health = await api("/api/health");
      state.health = health;
      $("storage-location").textContent = health.storage ? `成品：${health.storage.downloads}；缓存：${health.storage.cache}；临时文件：${health.storage.temporary}。` : "服务尚未载入 E 盘存储配置。";
      const ready = health.ok;
      $("health-dot").className = `status-dot${ready ? "" : " warning"}`;
      $("health-label").textContent = ready ? "本地服务已就绪" : "本地服务 · 依赖待完善";
      $("app-version").textContent = `v${String(health.version || "1.0").replace(/^v/, "")}`;
      $("demo-button").hidden = !health.demo_available;
      $("download-limit-hint").textContent = `每个任务的来源文件上限：${Number(health.max_download_gib) || 10} GiB。`;
      showError("connection-alert", "");
      renderDependencies();
      updateAnalyzeMode();
      updateExport();
      return health;
    } catch (error) {
      state.health = null;
      $("health-dot").className = "status-dot error";
      $("health-label").textContent = "本地服务未连接";
      showError("connection-alert", error.message);
      renderDependencies();
      updateAnalyzeMode();
      updateExport();
      return null;
    }
  }

  function renderDependencies() {
    const list = $("dependency-list");
    list.replaceChildren();
    const checks = [["本地服务", !!state.health], ["FFmpeg · 媒体转换", state.health?.ffmpeg], ["FFprobe · 媒体信息", state.health?.ffprobe], ["H.264 / AAC 编码器", state.health?.h264 && state.health?.aac], ["GPU · RIFE 插帧", state.compute?.gpu_interpolation], ["GPU · NVENC 编码", state.compute?.gpu_encoding ?? state.health?.gpu_encoding], ["CPU · 运动补偿插帧", state.health?.interpolation], ["yt-dlp · 可选网站解析", state.health?.yt_dlp], ["独立浏览器 · 登录后解析", state.health?.browser]];
    for (const [label, available] of checks) {
      const row = element("div");
      row.append(element("dt", "", label), element("dd", available ? "" : "missing", available ? "已就绪 ✓" : "未就绪"));
      list.append(row);
    }
  }

  function duration(value) {
    const number = Number(value);
    if (value === null || value === undefined || !Number.isFinite(number) || number < 0) return "未知";
    const total = Math.round(number);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor(total % 3600 / 60);
    const seconds = total % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}` : `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function bytes(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const power = Math.min(Math.floor(Math.log(number) / Math.log(1024)), units.length - 1);
    return `${(number / (1024 ** power)).toFixed(power > 0 ? 1 : 0)} ${units[power]}`;
  }

  function currentKind() { return document.querySelector('input[name="kind"]:checked').value; }

  async function loadCompute() {
    if (state.computeBusy) return;
    state.computeBusy = true;
    state.computeError = "";
    updateExport();
    try { state.compute = await api("/api/compute"); }
    catch (error) { state.compute = null; state.computeError = error.message; }
    finally { state.computeBusy = false; renderDependencies(); updateExport(); }
  }

  function updateCompute() {
    const mode = $("processing-mode").value;
    const gpuEncoding = !!state.compute?.gpu_encoding;
    const gpuInterpolation = !!state.compute?.gpu_interpolation;
    const source = Number(state.media?.fps);
    const target = selectedFps();
    const needsInterpolation = target > 0 && $("frame-rate-mode").value === "interpolate" && (!(source > 0) || target > source + 0.001);
    const unavailable = mode === "gpu" && (!gpuEncoding || (needsInterpolation && !gpuInterpolation));
    let note;
    if (mode === "cpu") note = "CPU 执行运动补偿和 H.264 编码，适合兼容性处理；AI 超分由其独立设置决定。";
    else if (state.computeBusy) note = "正在检测 GPU 插帧与硬件编码能力…";
    else if (!state.compute) note = "GPU 状态暂不可用。GPU 优先模式会由后端确认可用能力；仅 GPU 模式需要完成检测。";
    else {
      const device = state.compute.device ? `${state.compute.device}。` : "";
      const parts = [gpuInterpolation ? "RIFE 插帧已就绪" : "RIFE 插帧未就绪", gpuEncoding ? "NVENC 编码已就绪" : "NVENC 编码未就绪"];
      note = `${device}${parts.join("；")}。`;
      note += mode === "gpu" ? "所需 GPU 能力不可用时明确报错，不切换 CPU。" : "优先调用 GPU；能力不足时使用 CPU 并在结果注明。";
      if ((!gpuEncoding || !gpuInterpolation) && state.compute.reason) note += ` ${state.compute.reason}`;
    }
    if (mode !== "cpu" && gpuInterpolation && needsInterpolation) note += " GPU 生成中间帧，CPU 协同解码、音频和读写；插帧画面直接送入编码器。";
    if (mode !== "cpu") note += " GPU 插帧单个视频最长支持 3 小时，最高 120 fps；时间戳按需读取，画面使用固定容量队列。";
    if ($("video-upscale").value !== "none") note += " AI 超分仍先逐帧完成，再进入插帧与编码，组合任务耗时会增加。";
    $("processing-hint").textContent = note;
    $("processing-hint").className = `muted-text setting-hint${unavailable ? " compute-unavailable" : ""}`;
    return currentKind() === "video" && unavailable;
  }

  function fpsLabel(value) {
    const number = Number(value);
    return number > 0 && Number.isFinite(number) ? `${number.toFixed(3).replace(/\.?0+$/, "")} fps` : "未知帧率";
  }

  function maxFps() {
    const maximum = Number(state.health?.max_fps);
    return Number.isInteger(maximum) && maximum > 0 ? Math.min(120, maximum) : 120;
  }

  function selectedFps() {
    const selected = $("video-fps").value;
    if (selected === "source") return null;
    const value = selected === "custom" ? $("custom-fps").value.trim() : selected;
    const number = Number(value);
    return value && Number.isInteger(number) && number >= 1 && number <= maxFps() ? number : NaN;
  }

  function updateVideoSettings() {
    const maximum = maxFps();
    const video = currentKind() === "video";
    const custom = $("video-fps").value === "custom";
    $("custom-fps-field").hidden = !custom;
    $("custom-fps").max = String(maximum);
    $("custom-fps-hint").textContent = `1–${maximum} 的整数`;
    for (const option of $("video-fps").options) {
      option.disabled = Number(option.value) > maximum;
    }
    const target = selectedFps();
    const invalid = video && Number.isNaN(target);
    $("custom-fps").setAttribute("aria-invalid", String(invalid && custom));
    showError("custom-fps-error", invalid && custom ? `请输入 1–${maximum} 的整数帧率。` : "");
    const processing = $("processing-mode").value;
    const interpolationMissing = !!state.health && (processing === "cpu" ? !state.health.interpolation : processing === "gpu" ? !!state.compute && !state.compute.gpu_interpolation : !!state.compute && !state.health.interpolation && !state.compute.gpu_interpolation);
    $("frame-rate-mode").querySelector('[value="interpolate"]').disabled = interpolationMissing && processing !== "gpu";
    if (interpolationMissing && processing !== "gpu" && $("frame-rate-mode").value === "interpolate") $("frame-rate-mode").value = "fast";
    const source = Number(state.media?.fps);
    const sourceKnown = source > 0 && Number.isFinite(source);
    const reducing = sourceKnown && target !== null && !invalid && target <= source + 0.001;
    $("frame-rate-mode").disabled = target === null || invalid || reducing;
    const qualities = {
      balanced: "在文件大小、速度与细节之间取平衡。",
      high: "高保真减少二次压缩损失，尽量保留画面细节。",
      maximum: "使用更高编码质量，处理更慢、文件通常更大；不会还原源文件没有的细节。"
    };
    $("video-quality-hint").textContent = (qualities[$("video-quality").value] || qualities.high) + (processing === "cpu" ? " CPU 使用 CRF 质量控制。" : " GPU 使用 NVENC CQ，CPU 使用 CRF；两者的数值不代表相同画质。") + ($("video-upscale").value === "none" ? " 未启用 AI 时，输出分辨率是上限，只缩小、不放大。" : " AI 超分结果将按此质量重新编码。");
    let note;
    if (invalid) note = "先填写有效帧率，再选择转换方式。";
    else if (target === null) note = sourceKnown ? `跟随来源 ${fpsLabel(source)}，无需插帧。` : "跟随来源帧率，无需插帧。";
    else if (reducing) note = `来源 ${fpsLabel(source)} → 输出 ${fpsLabel(target)}；降低或保持帧率无需插值。`;
    else {
      const prefix = sourceKnown ? `来源 ${fpsLabel(source)} → 输出 ${fpsLabel(target)}。` : `输出 ${fpsLabel(target)}；转码前会检查源帧率。`;
      note = $("frame-rate-mode").value === "interpolate"
        ? `${prefix}提升帧率时生成中间帧；GPU 模式使用 RIFE，CPU 模式使用运动补偿。快速运动、遮挡和切镜可能出现伪影。`
        : `${prefix}通过复制或丢弃帧快速转换，提升帧率不会补出新的运动细节。`;
      if (interpolationMissing) note += " 当前编码器不支持流畅插帧。";
    }
    $("frame-rate-hint").textContent = note;
    return invalid;
  }

  function updateUpscale() {
    const selected = $("video-upscale").value;
    const enabled = selected !== "none";
    const heightSelect = $("video-height");
    if (enabled && heightSelect.value === "best") heightSelect.value = "1440";
    for (const option of heightSelect.options) {
      option.disabled = enabled && option.value === "best";
      if (option.value === "best") continue;
      const suffix = option.value === "2160" ? " · 4K" : option.value === "1440" ? " · 2K" : "";
      option.textContent = `${enabled ? "目标" : "最高"} ${option.value}p${suffix}`;
    }
    const target = Number(heightSelect.value);
    const sourceHeight = Number(state.media?.height);
    const sourceWidth = Number(state.media?.width);
    const dimensionsKnown = sourceHeight > 0 && sourceWidth > 0;
    let note;
    if (!enabled) note = "1440p 和 2160p 均可选择，作为输出上限；较低分辨率来源保持原尺寸。开启 AI 后可提升到目标尺寸。";
    else {
      const sarParts = String(state.media?.sample_aspect_ratio || "1:1").split(":").map(Number);
      const ratio = sarParts.length === 2 && sarParts.every(value => Number.isFinite(value) && value > 0) ? sarParts[0] / sarParts[1] : 1;
      const expectedWidth = dimensionsKnown ? Math.max(2, Math.round(sourceWidth * ratio * target / sourceHeight / 2) * 2) : 0;
      const size = expectedWidth ? `${expectedWidth} × ${target}` : `${target}p 高度，宽度随来源比例调整`;
      note = dimensionsKnown && target <= sourceHeight ? `预计输出 ${size}；目标未超过源高度，此任务无需执行 AI 超分。` : `目标 ${size}。AI 超分会增加处理时间、显存和临时磁盘占用；效果取决于模型和素材。`;
    }
    $("upscale-hint").textContent = note;
    const provider = selectedUpscaleProvider();
    const needsAI = enabled && (!dimensionsKnown || target > sourceHeight);
    const unavailable = needsAI && !provider?.available;
    $("upscale-availability").hidden = !enabled;
    $("upscale-availability").className = `upscale-availability${unavailable ? " unavailable" : ""}`;
    $("upscale-availability").textContent = !enabled ? "" : !needsAI ? "目标不高于来源，此任务无需调用 AI 服务。" : provider?.available
      ? `当前使用：${provider.name || upscaleLabels[provider.id]}${provider.id === "comfyui" && state.upscale?.comfyui_model ? ` · ${state.upscale.comfyui_model}` : ""}`
      : state.upscaleBusy ? "正在检测 AI 接口，请稍候…" : state.upscaleError ? "AI 接口状态暂不可用；请打开本地接口设置重新检测。" : `${provider?.reason || "当前没有可用的 AI 超分服务。"} 请打开本地接口设置检查；也可关闭 AI 继续常规导出。`;
    return currentKind() === "video" && unavailable;
  }

  function updateExport() {
    const kind = currentKind();
    $("video-options").hidden = kind !== "video";
    $("audio-options").hidden = kind !== "audio";
    $("original-options").hidden = kind !== "original";
    const unavailableUpscale = updateUpscale();
    const invalidFps = updateVideoSettings();
    const unavailableCompute = updateCompute();
    if (kind === "original") {
      $("encoding-title").textContent = "保留来源编码与实际封装";
      $("encoding-detail").textContent = "不主动转码 · 文件后缀由来源决定";
      $("format-note").textContent = "部分网站音画分离，需要合并后才能保存完整视频。";
    } else if (kind === "audio") {
      $("encoding-title").textContent = "AAC 音频";
      $("encoding-detail").textContent = "48,000 Hz · 256 kbps";
      $("format-note").textContent = $("audio-container").value === "m4a" ? "M4A 封装的 AAC 音频，适合常见播放器和设备。" : "AAC 原始音频流，时长显示与拖动支持因播放器而异。";
    } else {
      $("encoding-title").textContent = state.media?.has_audio === false ? "H.264 视频 · 源文件没有音轨" : "H.264 视频 + AAC 音频";
      $("encoding-detail").textContent = state.media?.has_audio === false ? "导出无声视频" : "48,000 Hz · 256 kbps · 通用兼容";
      $("format-note").textContent = $("video-container").value === "avi" ? "AVI 的 H.264 / AAC 支持因播放器而异，优先推荐 MP4。" : $("video-upscale").value === "none" ? "按所选参数重新编码，保留来源已有细节。" : "先处理 AI 超分，再完成帧率与编码设置；任务结果显示实际使用的服务和尺寸。";
    }
    const unsupported = state.media && ((kind === "audio" && state.media.has_audio === false) || (kind === "video" && state.media.has_video === false));
    const runtimeMissing = !state.health || (kind !== "original" && !state.health.ok);
    $("create-job-button").disabled = !state.media || state.analyzing || state.creating || !!unsupported || runtimeMissing || invalidFps || unavailableUpscale || unavailableCompute;
    $("create-job-label").textContent = state.creating ? "正在创建任务…" : state.analyzing ? "正在解析链接…" : !state.media ? "解析后开始提取" : unsupported ? (kind === "audio" ? "源文件没有可提取的音轨" : "源文件不包含视频画面") : invalidFps ? "请填写有效帧率" : unavailableUpscale ? "请先配置可用的 AI 超分" : unavailableCompute ? "所需 GPU 能力尚未就绪" : kind === "original" ? "下载原文件" : kind === "audio" ? "开始提取音频" : "开始提取视频";
  }

  function renderMedia(media) {
    $("media-title").textContent = media.title || "未命名媒体";
    $("media-extractor").textContent = String(media.extractor || "媒体信息");
    $("media-quality").textContent = media.height ? `${media.height}p` : media.has_video === false ? "仅音频" : "未知";
    $("media-duration").textContent = duration(media.duration);
    $("media-codec").textContent = media.video_codec && media.video_codec !== "none" ? media.video_codec : media.has_video === false ? "无视频" : "未知";
    $("media-fps").textContent = media.fps ? fpsLabel(media.fps) : "未知";
    const sourceLink = $("media-source-link");
    try { const parsed = new URL(media.url); if (!["http:", "https:"].includes(parsed.protocol)) throw new Error(); sourceLink.href = parsed.href; sourceLink.hidden = false; }
    catch { sourceLink.removeAttribute("href"); sourceLink.hidden = true; }
    const extras = [];
    if (media.width && media.height) extras.push(`${media.width} × ${media.height}`);
    if (media.has_audio === false) extras.push("源文件没有音轨");
    else if (media.audio_codec && media.audio_codec !== "none") extras.push(`音频 ${media.audio_codec}`);
    if (media.source_size) extras.push(bytes(media.source_size));
    $("media-extra").textContent = extras.join(" · ");
    const select = $("original-format");
    select.replaceChildren(new Option("自动选择最佳画质", "best"));
    const formats = Array.isArray(media.formats) ? media.formats : [];
    for (const format of formats) {
      if (format.id === undefined || format.id === null || String(format.id) === "best") continue;
      select.add(new Option(format.label || `${format.height ? `${format.height}p` : "源格式"} · ${format.ext || "媒体"} · ${format.id}`, String(format.id)));
    }
    $("media-card").hidden = false;
    $("source-empty").hidden = true;
    updateExport();
  }

  async function analyzeInBrowser(value) {
    state.browserJob = null;
    state.browserTerminal = null;
    let job = await api("/api/browser/analyze", { method: "POST", body: JSON.stringify({ url: value }) });
    if (!job.id) throw new Error("未能创建浏览器解析任务，请重试。");
    state.browserJob = job.id;
    $("cancel-browser-button").disabled = false;
    while (true) {
      // A cancellation response is authoritative even if an older poll returns afterward.
      if (state.browserTerminal) job = state.browserTerminal;
      if (job.normalized_url) showNormalizedUrl(job.normalized_url);
      if (job.diagnostics) renderDiagnostics(job.diagnostics, job.source_id);
      if (job.status === "completed") return job.media;
      if (job.status === "cancelled") {
        const error = new Error("浏览器解析已取消。");
        error.code = "cancelled";
        throw error;
      }
      if (job.status === "failed") {
        const error = responseError(typeof job.error === "string" ? { error: job.error } : job.error, job.message || "浏览器解析失败，请重试。");
        error.normalizedUrl ||= job.normalized_url;
        error.diagnostics ||= job.diagnostics;
        error.sourceId ||= job.source_id;
        throw error;
      }
      $("source-loading-title").textContent = job.status === "starting" ? "正在打开独立浏览器" : "等待浏览器中的视频";
      $("source-loading-hint").textContent = job.message || "在打开的浏览器中登录并播放此视频；完成后自动返回。";
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (state.browserTerminal) continue;
      job = await api(`/api/browser/analyze/${encodeURIComponent(state.browserJob)}`);
    }
  }

  async function cancelBrowserAnalysis() {
    if (!state.browserJob || !state.analyzing || state.browserCancelling) return;
    const browserJob = state.browserJob;
    state.browserCancelling = true;
    $("cancel-browser-button").disabled = true;
    $("cancel-browser-button").textContent = "正在取消…";
    try {
      const job = await api(`/api/browser/analyze/${encodeURIComponent(browserJob)}/cancel`, { method: "POST", body: "{}" });
      if (state.browserJob !== browserJob) return;
      if (["cancelled", "failed", "completed"].includes(job.status)) state.browserTerminal = job;
      else toast("已请求取消浏览器解析。");
    } catch (error) { toast(error.message); }
    finally {
      if (state.browserJob === browserJob) {
        state.browserCancelling = false;
        $("cancel-browser-button").disabled = !!state.browserTerminal;
        $("cancel-browser-button").textContent = "取消浏览器解析";
      }
    }
  }

  async function analyze(urlOverride, modeOverride) {
    if (state.analyzing) return;
    const value = String(urlOverride || $("video-url").value).trim();
    const mode = modeOverride || $("analyze-mode").value;
    clearTimeout(state.toastTimer);
    $("toast").hidden = true;
    showError("analyze-error", "");
    showError("export-error", "");
    showNormalizedUrl(null);
    renderDiagnostics(null);
    $("analyze-session-button").hidden = true;
    $("analyze-browser-button").hidden = true;
    state.media = null;
    $("media-card").hidden = true;
    $("source-empty").hidden = false;
    updateExport();
    // The backend extracts one URL from share text and normalizes platform-specific links.
    if (!/https?:\/\//i.test(value)) {
      showError("analyze-error", "请粘贴 http:// 或 https:// 视频网址，或包含一个视频链接的分享文字。");
      $("video-url").focus();
      return;
    }
    if (mode === "browser" && state.health?.browser === false) {
      showError("analyze-error", "浏览器解析组件尚未就绪。请打开右上角的本地运行环境，并按项目说明安装浏览器支持后重启服务。");
      return;
    }
    $("video-url").value = value;
    state.analyzing = true;
    $("video-url").disabled = true;
    $("analyze-mode").disabled = true;
    state.media = null;
    $("analyze-button").disabled = true;
    $("analyze-button").firstElementChild.textContent = "解析中…";
    $("demo-button").disabled = true;
    $("media-card").hidden = true;
    $("source-empty").hidden = true;
    $("source-loading").hidden = false;
    $("source-loading-title").textContent = mode === "browser" ? "正在打开独立浏览器" : "正在读取媒体信息";
    $("source-loading-hint").textContent = mode === "browser" ? "在打开的浏览器中登录并播放此视频；完成后自动返回。" : "有些网站需要多一点时间，请稍候…";
    $("cancel-browser-button").hidden = mode !== "browser";
    $("cancel-browser-button").disabled = true;
    $("analyze-form").setAttribute("aria-busy", "true");
    updateExport();
    try {
      const media = mode === "browser" ? await analyzeInBrowser(value) : await api("/api/analyze", { method: "POST", body: JSON.stringify({ url: value, mode: "auto" }) });
      if (!media?.id) throw new Error("解析结果缺少媒体标识，请重试。");
      state.media = media;
      if ((media.normalized_url || media.url) !== value) showNormalizedUrl(media.normalized_url || media.url);
      renderDiagnostics(media.diagnostics, media.source_id);
      renderMedia(media);
      toast("解析完成，可以选择保存方式了。");
    } catch (error) {
      if (error.code === "cancelled") toast(error.message);
      else showError("analyze-error", error.message);
      showNormalizedUrl(error.normalizedUrl);
      renderDiagnostics(error.diagnostics, error.sourceId);
      $("analyze-session-button").hidden = !["session_required", "session_rejected"].includes(error.code);
      $("analyze-browser-button").hidden = mode === "browser" || !error.normalizedUrl;
      $("source-empty").hidden = false;
    } finally {
      state.analyzing = false;
      state.browserJob = null;
      state.browserTerminal = null;
      state.browserCancelling = false;
      $("video-url").disabled = false;
      $("analyze-mode").disabled = false;
      $("source-loading").hidden = true;
      $("cancel-browser-button").hidden = true;
      $("cancel-browser-button").textContent = "取消浏览器解析";
      $("analyze-button").disabled = false;
      $("analyze-button").firstElementChild.textContent = "解析链接";
      $("demo-button").disabled = false;
      $("analyze-form").setAttribute("aria-busy", "false");
      updateAnalyzeMode();
      updateExport();
    }
  }

  async function createJob() {
    if (!state.media || state.creating || state.analyzing) return;
    const kind = currentKind();
    const fps = selectedFps();
    if (kind === "video" && Number.isNaN(fps)) {
      updateExport();
      $("custom-fps").focus();
      return;
    }
    if (kind === "video" && (updateUpscale() || updateCompute())) { updateExport(); return; }
    state.creating = true;
    showError("export-error", "");
    updateExport();
    const height = $("video-height").value;
    const payload = { media_id: state.media.id, kind, container: kind === "audio" ? $("audio-container").value : $("video-container").value, height: kind === "video" && height !== "best" ? Number(height) : null, format_id: kind === "original" ? $("original-format").value : "best" };
    if (kind === "video") Object.assign(payload, { video_quality: $("video-quality").value, fps, frame_rate_mode: $("frame-rate-mode").value, upscale: $("video-upscale").value, processing_mode: $("processing-mode").value });
    try {
      const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      upsertJob(job);
      toast("任务已加入，正在准备提取。");
      schedulePoll(250);
      $("jobs-heading").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) { showError("export-error", error.message); }
    finally { state.creating = false; updateExport(); }
  }

  function jobMetadata(job) {
    const actualContainer = job.kind === "original" ? (job.filename?.split(".").pop() || "来源格式") : job.container;
    const parts = [kindLabels[job.kind] || "媒体", String(actualContainer || "").toUpperCase()];
    const result = job.result || {};
    if (result.width && result.height) parts.push(`${result.width} × ${result.height}`);
    else if (result.height) parts.push(`${result.height}p`);
    const codecLabels = { h264: "H.264", hevc: "H.265", aac: "AAC", av1: "AV1" };
    const codecs = [result.video_codec, result.audio_codec].filter(codec => codec && codec !== "none");
    if (codecs.length) parts.push(codecs.map(codec => codecLabels[codec] || String(codec).toUpperCase()).join(" / "));
    if (result.duration !== null && result.duration !== undefined) parts.push(duration(result.duration));
    if (result.sample_rate && job.kind === "audio") parts.push(`${Number(result.sample_rate).toLocaleString("en-US")} Hz`);
    if (result.size) parts.push(bytes(result.size));
    return parts.filter(Boolean);
  }

  function jobVideoSettings(job) {
    if (job.kind !== "video") return null;
    const result = job.result || {};
    const requested = [];
    if (processingLabels[job.processing_mode]) requested.push(processingLabels[job.processing_mode]);
    if (qualityLabels[job.video_quality]) requested.push(qualityLabels[job.video_quality]);
    if (Object.prototype.hasOwnProperty.call(job, "fps")) requested.push(job.fps === null ? "跟随来源帧率" : fpsLabel(job.fps));
    if (job.fps && frameModeLabels[job.frame_rate_mode]) requested.push(`升帧时${frameModeLabels[job.frame_rate_mode]}`);
    if (job.height) requested.push(`${job.upscale && job.upscale !== "none" ? "目标" : "最高"} ${job.height}p`);
    if (job.upscale && upscaleLabels[job.upscale]) requested.push(upscaleLabels[job.upscale]);
    const actual = [];
    if (result.source_fps > 0 && result.fps > 0) actual.push(`来源 ${fpsLabel(result.source_fps)} → 实测 ${fpsLabel(result.fps)}`);
    else if (result.fps > 0) actual.push(`实测 ${fpsLabel(result.fps)}`);
    else if (result.target_fps > 0) actual.push(`目标 ${fpsLabel(result.target_fps)}`);
    if (frameModeLabels[result.effective_frame_rate_mode]) actual.push(frameModeLabels[result.effective_frame_rate_mode]);
    if (qualityLabels[result.video_quality]) actual.push(qualityLabels[result.video_quality]);
    if (result.upscale_provider) actual.push(upscaleLabels[result.upscale_provider] || String(result.upscale_provider));
    if (result.upscale_model) actual.push(`模型 ${result.upscale_model}`);
    if (result.source_width && result.source_height && result.upscale_width && result.upscale_height) actual.push(`超分前 ${result.source_width} × ${result.source_height} → ${result.upscale_width} × ${result.upscale_height}`);
    const encoders = { h264_nvenc: "GPU · NVENC H.264", libx264: "CPU · x264 H.264" };
    const backends = { gpu: "GPU · RIFE CUDA 插帧", cpu: "CPU · 运动补偿", rife: "GPU · RIFE 插帧", rife_cuda: "GPU · RIFE CUDA 插帧", minterpolate: "CPU · 运动补偿" };
    if (result.video_encoder) actual.push(encoders[result.video_encoder] || String(result.video_encoder));
    if (result.interpolation_backend && result.interpolation_backend !== "none") actual.push(backends[result.interpolation_backend] || String(result.interpolation_backend));
    if (result.interpolation_model) actual.push(`插帧模型 ${result.interpolation_model}`);
    if (Number.isInteger(result.interpolation_generated_frames)) actual.push(`生成 ${result.interpolation_generated_frames} 个中间帧`);
    if (result.pipeline === "streaming") actual.push("流式插帧与编码");
    if (typeof result.processing_seconds === "number" && Number.isFinite(result.processing_seconds)) actual.push(`处理 ${result.processing_seconds.toFixed(1)} 秒`);
    if (typeof result.total_seconds === "number" && Number.isFinite(result.total_seconds)) actual.push(`总耗时 ${result.total_seconds.toFixed(1)} 秒`);
    if (!requested.length && !actual.length && !result.frame_rate_note && !result.upscale_note) return null;
    const details = element("div", "job-video-settings");
    for (const [label, parts] of [["设定", requested], ["结果", actual]]) {
      if (!parts.length) continue;
      const line = element("p");
      line.append(element("span", "job-settings-label", label), document.createTextNode(parts.join(" · ")));
      details.append(line);
    }
    if (result.frame_rate_note) details.append(element("p", "job-frame-note", String(result.frame_rate_note)));
    if (result.upscale_note) details.append(element("p", "job-frame-note", String(result.upscale_note)));
    if (result.compute_note) details.append(element("p", "job-frame-note", String(result.compute_note)));
    return details;
  }

  function makeJobCard(job) {
    const card = element("article", "job-card");
    card.dataset.jobId = job.id;
    const main = element("div", "job-main");
    const icon = element("div", "job-file-icon", job.kind === "original" ? "源文件" : (job.container || "媒体"));
    icon.setAttribute("aria-hidden", "true");
    const description = element("div", "job-description");
    const title = element("h3", "job-title", job.title || job.filename || "媒体提取任务");
    title.title = title.textContent;
    const meta = element("p", "job-meta");
    for (const text of jobMetadata(job)) meta.append(element("span", "", text));
    description.append(title, meta);
    const status = element("div", `job-state ${job.status}`);
    status.append(element("span", `status-dot${activeStatuses.has(job.status) ? " loading" : job.status === "failed" ? " error" : ""}`), element("span", "", statusLabels[job.status] || job.status || "准备中"));
    const actions = element("div", "job-actions");
    if (activeStatuses.has(job.status)) {
      const cancel = element("button", "job-cancel", "取消");
      cancel.type = "button";
      cancel.dataset.action = "cancel";
      cancel.setAttribute("aria-label", `取消任务：${title.textContent}`);
      actions.append(cancel);
    } else if (job.status === "completed") {
      const download = element("a", "job-download", "保存文件 ↓");
      // Downloads are restricted to this application's route, never to a URL from a website.
      download.href = `/api/jobs/${encodeURIComponent(job.id)}/download`;
      if (job.filename) download.download = job.filename;
      download.setAttribute("aria-label", `保存文件：${job.filename || title.textContent}`);
      actions.append(download);
    }
    main.append(icon, description, status, actions);
    card.append(main);
    const videoSettings = jobVideoSettings(job);
    if (videoSettings) card.append(videoSettings);
    if (activeStatuses.has(job.status)) {
      const value = Math.min(100, Math.max(0, Number(job.progress) || 0));
      const wrap = element("div", "job-progress-wrap");
      const label = element("div", "job-progress-label");
      label.append(element("span", "", job.stage || job.message || "等待处理"), element("span", "", `${Math.round(value)}%`));
      const track = element("div", "job-progress-track");
      track.setAttribute("role", "progressbar");
      track.setAttribute("aria-label", title.textContent);
      track.setAttribute("aria-valuemin", "0");
      track.setAttribute("aria-valuemax", "100");
      track.setAttribute("aria-valuenow", String(Math.round(value)));
      const fill = element("div", "job-progress-fill");
      fill.style.width = `${value}%`;
      track.append(fill);
      wrap.append(label, track);
      card.append(wrap);
    } else if (job.status === "failed" && job.message) {
      card.append(element("p", "job-message error", job.message));
    } else if (job.status === "completed" && job.filename) {
      card.append(element("p", "job-message", job.filename));
    }
    return card;
  }

  function upsertJob(job) {
    if (!job || !job.id) return;
    const previous = state.jobs.get(job.id);
    if (previous && JSON.stringify(previous) === JSON.stringify(job)) return;
    state.jobs.set(job.id, job);
    const oldCard = Array.from($("jobs-list").children).find((node) => node.dataset.jobId === String(job.id));
    const card = makeJobCard(job);
    if (oldCard) {
      const focusedAction = oldCard.contains(document.activeElement) ? document.activeElement.dataset.action : null;
      card.style.animation = "none";
      oldCard.replaceWith(card);
      if (focusedAction) card.querySelector(`[data-action="${focusedAction}"]`)?.focus({ preventScroll: true });
    } else { $("jobs-list").prepend(card); }
    $("job-count").textContent = String(state.jobs.size);
    $("jobs-empty").hidden = state.jobs.size > 0;
    if (previous && previous.status !== "completed" && job.status === "completed") toast(`「${job.title || "媒体"}」已完成，可以保存文件。`);
  }

  function schedulePoll(delay = 1400) {
    clearTimeout(state.timer);
    state.timer = setTimeout(pollJobs, delay);
  }

  async function pollJobs() {
    if (state.polling) { schedulePoll(); return; }
    state.polling = true;
    try {
      const data = await api("/api/jobs");
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      jobs.sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || ""))).forEach(upsertJob);
      const ids = new Set(jobs.map((job) => job.id));
      for (const id of state.jobs.keys()) {
        if (!ids.has(id)) {
          state.jobs.delete(id);
          Array.from($("jobs-list").children).find((node) => node.dataset.jobId === String(id))?.remove();
        }
      }
      $("job-count").textContent = String(state.jobs.size);
      $("jobs-empty").hidden = state.jobs.size > 0;
      showError("jobs-error", "");
    } catch (error) { showError("jobs-error", error.message); }
    finally {
      state.polling = false;
      const active = Array.from(state.jobs.values()).some((job) => activeStatuses.has(job.status));
      schedulePoll(document.hidden ? 10000 : active ? 1400 : 5000);
    }
  }

  $("analyze-form").addEventListener("submit", (event) => { event.preventDefault(); analyze(); });
  $("video-url").addEventListener("input", () => {
    showError("analyze-error", "");
    showNormalizedUrl(null);
    renderDiagnostics(null);
    $("analyze-session-button").hidden = true;
    $("analyze-browser-button").hidden = true;
    if (state.media && $("video-url").value.trim() !== state.media.url) {
      state.media = null;
      $("media-card").hidden = true;
      $("source-empty").hidden = false;
      updateExport();
    }
  });
  $("demo-button").addEventListener("click", async () => {
    $("demo-button").disabled = true;
    try { const demo = await api("/api/demo"); if (!demo.url) throw new Error("本地示例暂不可用。"); $("analyze-mode").value = "auto"; updateAnalyzeMode(); await analyze(demo.url, "auto"); }
    catch (error) { showError("analyze-error", error.message); }
    finally { $("demo-button").disabled = false; }
  });
  for (const radio of document.querySelectorAll('input[name="kind"]')) radio.addEventListener("change", () => { showError("export-error", ""); updateExport(); });
  for (const id of ["video-container", "video-height", "video-quality", "video-fps", "frame-rate-mode", "video-upscale", "processing-mode"]) $(id).addEventListener("change", () => { showError("export-error", ""); updateExport(); });
  $("video-upscale").addEventListener("change", () => {
    if ($("video-upscale").value !== "none" && !state.upscale && !state.upscaleBusy) loadUpscaleSettings();
  });
  $("custom-fps").addEventListener("input", () => { showError("export-error", ""); updateExport(); });
  $("audio-container").addEventListener("change", updateExport);
  $("create-job-button").addEventListener("click", createJob);
  $("jobs-list").addEventListener("click", async (event) => {
    const button = event.target.closest('[data-action="cancel"]');
    if (!button) return;
    const id = button.closest(".job-card").dataset.jobId;
    button.disabled = true;
    button.textContent = "取消中…";
    try { const job = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: "{}" }); upsertJob(job); toast("已请求取消该任务。"); schedulePoll(250); }
    catch (error) { toast(error.message); button.disabled = false; button.textContent = "取消"; }
  });
  $("help-button").addEventListener("click", () => $("help-dialog").showModal());
  $("douyin-settings-button").addEventListener("click", openSessionSettings);
  $("upscale-settings-button").addEventListener("click", openUpscaleSettings);
  $("upscale-save-button").addEventListener("click", saveUpscaleSettings);
  $("analyze-session-button").addEventListener("click", openSessionSettings);
  $("analyze-mode").addEventListener("change", updateAnalyzeMode);
  $("analyze-browser-button").addEventListener("click", () => { $("analyze-mode").value = "browser"; updateAnalyzeMode(); analyze(); });
  $("cancel-browser-button").addEventListener("click", cancelBrowserAnalysis);
  $("douyin-import-button").addEventListener("click", importSession);
  $("douyin-clear-button").addEventListener("click", clearSession);
  $("douyin-cookie-file").addEventListener("change", () => {
    const file = $("douyin-cookie-file").files[0];
    showError("douyin-error", file && (file.size === 0 || file.size > 256 * 1024) ? "请选择非空文件，大小不超过 256 KiB。" : "");
    updateSessionControls();
  });
  $("douyin-dialog").addEventListener("close", () => { $("douyin-cookie-file").value = ""; updateSessionControls(); });
  $("health-button").addEventListener("click", () => { renderDependencies(); $("health-dialog").showModal(); });
  $("refresh-health").addEventListener("click", async () => {
    $("refresh-health").disabled = true;
    try { await Promise.all([loadHealth(), loadCompute()]); }
    finally { $("refresh-health").disabled = false; }
  });
  for (const dialog of document.querySelectorAll("dialog")) {
    dialog.querySelector(".dialog-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedulePoll(100); });
  window.addEventListener("pagehide", () => clearTimeout(state.timer));
  loadHealth().then(() => pollJobs());
  loadCompute();
})();
