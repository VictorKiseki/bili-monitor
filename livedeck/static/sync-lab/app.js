"use strict";

const $ = id => document.getElementById(id);
const baseBuffer = 3;
const calibration = globalThis.LiveDeckCalibration;
const resetAnalysis = calibration.reset;
let session = null;

function status(text, error = false) {
  const node = $("status");
  if (node.textContent !== text) node.textContent = text;
  node.dataset.error = String(error);
}

function disposePlayer(room) {
  if (!room.player) return;
  room.player.destroy();
  room.player = null;
}

function fail(run, room) {
  if (session !== run || room.failed) return;
  room.failed = true;
  room.badge.dataset.error = "true";
  room.badge.textContent = "直播流中断";
  room.listen.checked = false;
  room.listen.disabled = true;
  listen();
  disposePlayer(room);
  resetAnalysis(run);
  status(`房间 ${room.id} 无法继续播放。可停止后重试，或调整测试房间。`, true);
}

function play(run, room) {
  if (!room.video.paused || room.playPending) return;
  room.playPending = true;
  room.video.play().catch(error => {
    if (session !== run) return;
    if (error.name === "NotAllowedError") {
      room.video.muted = true;
      room.badge.textContent = "请点击该画面继续";
    }
  }).finally(() => { room.playPending = false; });
}

function setDelays(run, delays, reset = true) {
  for (let i = 0; i < delays.length; i++) {
    const room = run.rooms[i];
    const delay = Math.round(delays[i] * 100) / 100;
    if (room.delay !== delay) room.needsSeek = true;
    room.delay = delay;
    room.field.value = delay.toFixed(2);
  }
  if (reset) resetAnalysis(run);
}

function follow(run, room, elapsed) {
  if (room.failed) return false;
  const video = room.video;
  const target = room.start + elapsed - room.delay;
  if (!calibration.contains(video, target)) {
    if (!video.paused) video.pause();
    room.needsSeek = true;
    room.badge.textContent = "等待缓冲";
    return false;
  }
  if (video.seeking) return false;
  const error = target - video.currentTime;
  if (room.needsSeek || Math.abs(error) > .55) {
    video.playbackRate = 1;
    video.currentTime = target;
    room.needsSeek = false;
    room.badge.textContent = "同步追位";
    play(run, room);
    return false;
  }
  video.playbackRate = Math.abs(error) < .11 ? 1 : 1 + Math.max(-.04, Math.min(.04, error * .15));
  play(run, room);
  const ready = !video.paused && video.readyState >= 3;
  room.badge.textContent = ready ? "播放中" : "等待缓冲";
  return ready && video.playbackRate === 1;
}

function tick(run) {
  if (session !== run) return;
  const now = performance.now();
  if (run.origin === null) {
    if (run.rooms.some(room => room.failed)) return;
    const times = run.rooms.map(room => calibration.livePosition(room.video, baseBuffer));
    if (times.includes(null)) {
      if (now - run.started > 25000) run.rooms.forEach((room, i) => { if (times[i] === null) fail(run, room); });
      return;
    }
    run.origin = now;
    run.rooms.forEach((room, i) => { room.start = times[i]; room.needsSeek = true; });
    if (run.audioError) status(run.audioError, true);
    else if ($("auto").checked) status("正在收集共通音频，画面已开始播放。");
    else status("手动模式。可调整各路额外延迟。");
  }
  const elapsed = (now - run.origin) / 1000;
  // 每一路都需要推进。没有可播画面的房间恢复后追上同一个播放时钟。
  const stable = run.rooms.map(room => follow(run, room, elapsed)).every(Boolean);
  if (!stable && run.collecting) resetAnalysis(run);
  run.stable = stable;
}

function measured(run, data) {
  if (session !== run || !$("auto").checked) return;
  calibration.measure(run, data, delays => setDelays(run, delays, false), status);
}

function useManual(run, text) {
  run.audioError = text;
  $("auto").checked = false;
  $("auto").disabled = true;
  calibration.closeAudio(run);
  resetAnalysis(run);
  status(text, true);
}

async function setupAnalysis(run) {
  const context = run.context;
  if (!context?.audioWorklet || typeof Worker !== "function") {
    useManual(run, "当前浏览器无法分析音频，可手动调整延迟。");
    return;
  }
  try {
    await context.audioWorklet.addModule("/static/sync-lab/capture.js");
    if (session !== run) return;
    calibration.bindAudio(run, () => session === run && $("auto").checked,
      data => measured(run, data),
      () => { if (session === run) useManual(run, "音频分析异常，可手动调整延迟。"); });
  } catch {
    if (session === run) useManual(run, "无法启动音频分析，可手动调整延迟。");
  }
}

function listen(event) {
  const slider = $("volume");
  const percent = `${slider.value}%`;
  $("volume-value").textContent = percent;
  slider.setAttribute("aria-valuetext", percent);
  if (!session) return;
  const enabled = session.rooms.filter(room => room.listen.checked).length;
  // 选中的音源平分总音量，避免多路满幅音频相加后削波。
  const volume = enabled ? slider.valueAsNumber / (100 * enabled) : 0;
  if (event && volume > 0) session.context?.resume().catch(() => {});
  for (const room of session.rooms) {
    const on = room.listen.checked;
    if (room.gain) {
      // 只调整试听输出。自动校准直接读音源，静音时也能匹配。
      room.gain.gain.value = on ? volume : 0;
      if (event && on && volume > 0) room.video.muted = false;
    } else {
      room.video.volume = volume;
      room.video.muted = !on || volume === 0;
    }
  }
}

function createRoom(run, id) {
  const item = $("tile").content.firstElementChild.cloneNode(true);
  const video = item.querySelector("video");
  const field = item.querySelector("input");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = run.rooms.length === 0;
  const label = document.createElement("label");
  const labelText = document.createElement("span");
  labelText.textContent = `房间 ${id}`;
  label.append(checkbox, labelText);
  $("listen").append(label);
  const room = { id, video, field, listen: checkbox, badge: item.querySelector(".badge"), delay: 0 };
  item.querySelector(".room-name").textContent = `房间 ${id}`;
  $("stage").append(item);
  if (run.context) {
    room.source = run.context.createMediaElementSource(video);
    room.gain = run.context.createGain();
    room.gain.gain.value = 0;
    room.source.connect(room.gain).connect(run.context.destination);
  } else {
    video.muted = true;
  }
  video.addEventListener("click", () => {
    if (room.failed) return;
    if (run.context) video.muted = false;
    run.context?.resume().catch(() => {});
    play(run, room);
  });
  video.addEventListener("ended", () => fail(run, room));
  video.addEventListener("error", () => fail(run, room));
  field.addEventListener("change", () => {
    if (!field.validity.valid || !Number.isFinite(field.valueAsNumber)) {
      field.value = room.delay.toFixed(2);
      return;
    }
    $("auto").checked = false;
    const delays = run.rooms.map(item => item === room ? field.valueAsNumber : item.delay);
    run.autoAnchor = delays;
    setDelays(run, delays);
    status("手动模式。可调整各路额外延迟。");
  });
  return room;
}

function stop() {
  const run = session;
  if (!run) return;
  session = null;
  clearInterval(run.timer);
  calibration.closeAudio(run);
  for (const room of run.rooms) {
    if (!room.video.paused) room.video.pause();
    disposePlayer(room);
    room.source?.disconnect();
    room.gain?.disconnect();
  }
  run.context?.close().catch(() => {});
  $("stage").replaceChildren();
  $("listen").replaceChildren();
  $("controls").hidden = true;
  $("rooms").disabled = $("use-stage").disabled = $("start").disabled = false;
  $("stop").disabled = true;
  status("测试已停止。可重新填写房间并开始。");
}

$("setup").addEventListener("submit", event => {
  event.preventDefault();
  if (session) return;
  const ids = $("rooms").value.trim().split(/[\s/,，、]+/).filter(Boolean);
  if (ids.length < 2 || ids.length > 3 || new Set(ids).size !== ids.length || ids.some(id => !/^[1-9]\d{0,11}$/.test(id))) {
    status("请输入 2–3 个不同的直播间号。", true);
    return;
  }
  if (!window.mpegts?.isSupported()) {
    status("当前浏览器无法播放直播流，请使用监控室支持的 Edge 或 Chrome。", true);
    return;
  }
  let context;
  try { context = new AudioContext(); } catch { context = null; }
  const run = { context, rooms: [], origin: null, started: performance.now(), generation: 0 };
  session = run;
  $("rooms").disabled = $("use-stage").disabled = $("start").disabled = true;
  $("stop").disabled = false;
  $("controls").hidden = false;
  $("auto").disabled = false;
  status("正在加载直播流和播放缓存…");
  context?.resume().catch(() => {});
  try {
    for (const id of ids) {
      const room = createRoom(run, id);
      run.rooms.push(room);
      const url = `/api/sync-lab/flv?room=${encodeURIComponent(id)}`;
      const player = mpegts.createPlayer({ type: "flv", isLive: true, url }, {
        enableStashBuffer: false, lazyLoad: false, liveBufferLatencyChasing: false,
        autoCleanupSourceBuffer: true, autoCleanupMaxBackwardDuration: 45, autoCleanupMinBackwardDuration: 24
      });
      room.player = player;
      player.on(mpegts.Events.ERROR, () => fail(run, room));
      player.attachMediaElement(room.video);
      player.load();
      play(run, room);
    }
    listen();
    run.timer = setInterval(() => tick(run), 240);
    setupAnalysis(run);
  } catch {
    stop();
    status("无法初始化同步测试播放器。", true);
  }
});

$("use-stage").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/snapshot");
    const { active } = await response.json();
    if (session) return;
    if (!response.ok || !Array.isArray(active)) throw new Error("snapshot");
    if (active.length < 2) return status("舞台房间不足两路，可以手动填入房间号。", true);
    $("rooms").value = active.slice(0, 3).join(" / ");
    status(active.length > 3 ? "已填入舞台的前三路，可编辑后开始测试。" : "已填入舞台房间，可以开始测试。");
  } catch {
    if (!session) status("无法读取舞台，请手动输入房间号。", true);
  }
});
$("auto").addEventListener("change", () => {
  if (!session) return;
  resetAnalysis(session);
  status($("auto").checked ? "正在重新收集共通音频。" : "手动模式。可调整各路额外延迟。");
});
$("reset").addEventListener("click", () => {
  if (!session) return;
  session.autoAnchor = null;
  setDelays(session, session.rooms.map(() => 0));
  status($("auto").checked ? "已归零，正在重新收集共通音频。" : "已归零，当前为手动模式。");
});
$("listen").addEventListener("change", listen);
$("volume").addEventListener("input", listen);
$("stop").addEventListener("click", stop);
window.addEventListener("pagehide", stop);
