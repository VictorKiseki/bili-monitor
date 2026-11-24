"use strict";

(function () {
  globalThis.createLiveDeckStageSync = function (stage) {
    const calibration = globalThis.LiveDeckCalibration;
    const $ = id => document.getElementById(id);
    const button = $("stage-sync-btn"), dialog = $("stage-sync-dialog"), list = $("stage-sync-rooms");
    const auto = $("stage-sync-auto"), apply = $("stage-sync-apply"), info = $("stage-sync-status");
    const maxRooms = 9;
    let settings = null, run = null, context = null, timer = null, choices = [];

    function status(text, error = false) {
      if (info.textContent !== text) info.textContent = text;
      info.classList.toggle("fail", error);
      button.title = text || "手动选择舞台上的联动房间";
    }

    function paint() {
      const ids = settings?.ids || [];
      button.textContent = ids.length ? `联动同步 · ${ids.length}` : "联动同步";
      button.classList.toggle("on", ids.length > 0);
      for (const slot of stage.rooms()) {
        const badge = slot.tile.querySelector(".sync-badge");
        const chosen = ids.includes(slot.id);
        badge.classList.toggle("hide", !chosen);
        const room = run?.rooms.find(room => room.id === slot.id);
        badge.title = room ? `联动同步，额外延迟 ${room.delay.toFixed(2)} 秒` : "已选入联动组，等待同步";
      }
      if (dialog.open && auto.checked) {
        for (const choice of choices) {
          const room = run?.rooms.find(room => room.id === choice.id);
          if (room) choice.delay.value = room.delay.toFixed(2);
        }
      }
    }

    function closeAudio(active) {
      calibration.closeAudio(active);
      for (const room of active.rooms) {
        room.source?.disconnect();
        room.captureStream?.getTracks().forEach(track => track.stop());
        room.source = room.captureStream = null;
      }
    }

    function dropRun(restore) {
      const active = run;
      if (!active) return;
      run = null;
      closeAudio(active);
      for (const room of active.rooms) {
        room.video.playbackRate = 1;
        if (room.hlsConfig) room.hlsConfig.liveMaxLatencyDurationCount = room.hlsLimit;
        if (restore) stage.restore(room.video, room.stream);
      }
    }

    function end(restore = true) {
      settings = null;
      clearInterval(timer);
      timer = null;
      dropRun(restore);
      context?.close().catch(() => {});
      context = null;
      status("");
      paint();
    }

    function owns(video) {
      return Boolean(run?.rooms.some(room => room.video === video));
    }

    function invalidate(id) {
      if (!settings?.ids.includes(id)) return;
      if (!stage.isOnStage(id)) return refresh();
      if (!run?.rooms.some(room => room.id === id)) return;
      dropRun(false);
      status("所选房间正在恢复播放，画面就绪后会重新同步。");
      paint();
    }

    function refresh() {
      if (settings?.ids.some(id => !stage.isOnStage(id))) {
        end();
        stage.notify("联动房间已移出舞台，同步已停止。", "warn");
      } else {
        paint();
      }
    }

    function applyDelays(active, delays) {
      active.rooms.forEach((room, i) => {
        const next = Math.round(delays[i] * 100) / 100;
        if (room.delay !== next) room.needsSeek = true;
        room.delay = next;
      });
      paint();
    }

    function audioFailure(active) {
      if (run !== active) return;
      closeAudio(active);
      active.audioFailed = true;
      status("这组房间的音频无法用于自动校准，可关闭自动校准后手动填写延迟。", true);
    }

    async function setupAudio(active) {
      if (!active.context?.audioWorklet || typeof Worker !== "function") return audioFailure(active);
      try {
        await active.context.audioWorklet.addModule("/static/sync-lab/capture.js");
        if (run !== active) return;
        // 捕获现有播放器的音轨。每格照常用自己的音量和静音控制。
        for (const room of active.rooms) {
          const media = room.video.captureStream();
          room.captureStream = media;
          media.getVideoTracks().forEach(track => track.stop());
          const track = media.getAudioTracks()[0];
          if (!track || track.readyState !== "live") return audioFailure(active);
          room.source = active.context.createMediaStreamSource(new MediaStream([track]));
          track.addEventListener("ended", () => { if (run === active) invalidate(room.id); }, { once: true });
        }
        calibration.bindAudio(active, () => run === active, data => {
          if (run === active && (active.stable || data.confirmed)) {
            calibration.measure(active, data, delays => applyDelays(active, delays), status);
          }
        }, () => audioFailure(active));
      } catch {
        audioFailure(active);
      }
    }

    function follow(room, elapsed) {
      const video = room.video, target = room.start + elapsed - room.delay;
      if (video.seeking) return false;
      if (!calibration.contains(video, target)) {
        video.playbackRate = 1;
        room.needsSeek = true;
        return false;
      }
      const error = target - video.currentTime;
      if (room.needsSeek || Math.abs(error) > .55) {
        room.needsSeek = false;
        if (Math.abs(error) > .06) {
          video.playbackRate = 1;
          video.currentTime = target;
          if (video.paused) stage.play(video);
          return false;
        }
      }
      const rate = Math.abs(error) < .11 ? 1 : 1 + Math.max(-.04, Math.min(.04, error * .15));
      if (video.playbackRate !== rate) video.playbackRate = rate;
      if (video.paused) stage.play(video);
      return !video.paused && video.readyState >= 3 && rate === 1;
    }

    function tick() {
      if (!settings) return;
      const slots = settings.ids.map(id => stage.room(id));
      if (run && slots.some((slot, i) => !slot || slot.stream !== run.rooms[i].stream || !slot.stream?.player || slot.video.ended)) {
        dropRun(false);
      }
      if (!run) {
        if (slots.some(slot => !slot)) return status("等待所选房间回到多屏舞台。");
        const times = slots.map(({ video, stream }) => stream?.player && video.readyState >= 3 && !video.paused && !video.seeking && !video.ended
          ? calibration.livePosition(video, Math.max(3, stage.buffer())) : null);
        if (times.includes(null)) return status("等待所选房间播放并积累缓冲。");
        const active = { context, generation: 0, origin: performance.now(), rooms: slots.map((slot, i) => {
          const hlsConfig = slot.stream.kind === "hls" ? slot.stream.player.config : null;
          const hlsLimit = hlsConfig?.liveMaxLatencyDurationCount;
          if (hlsConfig) hlsConfig.liveMaxLatencyDurationCount = Infinity;
          return { ...slot, start: times[i], delay: settings.delays[slot.id] || 0, needsSeek: true, hlsConfig, hlsLimit };
        }) };
        run = active;
        paint();
        status(settings.auto ? "正在收集共同音频。" : "正在按手动延迟同步选中的房间。");
        if (settings.auto) setupAudio(active);
      }
      const now = performance.now(), elapsed = (now - run.origin) / 1000;
      const ready = run.rooms.map(room => follow(room, elapsed));
      run.stable = ready.every(Boolean);
      if (run.rooms.length > 3) {
        run.audioMask = 0;
        run.rooms.forEach((room, i) => {
          if (!ready[i]) room.audioReadyAt = now + 500;
          else if (now >= (room.audioReadyAt || 0)) run.audioMask |= 1 << i;
        });
      } else if (!run.stable && run.collecting) calibration.reset(run);
    }

    function start(next) {
      end();
      settings = next;
      if (settings.auto && typeof AudioContext === "function") {
        try { context = new AudioContext(); context.resume().catch(() => {}); } catch { context = null; }
      }
      status("等待所选房间播放并积累缓冲。");
      paint();
      timer = setInterval(tick, 240);
      tick();
    }

    function validate() {
      const total = choices.filter(choice => choice.check.checked).length;
      for (const choice of choices) {
        choice.check.disabled = total >= maxRooms && !choice.check.checked;
        choice.delay.disabled = auto.checked || !choice.check.checked;
      }
      apply.disabled = total < 2 || total > maxRooms;
    }

    function open() {
      auto.checked = settings?.auto ?? true;
      choices = stage.rooms().map(slot => {
        const row = document.createElement("div"), label = document.createElement("label");
        const check = document.createElement("input"), delay = document.createElement("input");
        const name = document.createElement("span"), seconds = document.createElement("label");
        row.className = "stage-sync-room";
        check.type = "checkbox";
        check.checked = settings?.ids.includes(slot.id) || false;
        name.textContent = slot.name;
        label.className = "stage-sync-choice";
        label.append(check, name);
        if (slot.name !== `房间 ${slot.id}`) {
          const number = document.createElement("small");
          number.textContent = `#${slot.id}`;
          label.append(number);
        }
        delay.type = "number";
        delay.min = 0;
        delay.max = calibration.maxDelay;
        delay.step = .01;
        delay.value = (run?.rooms.find(room => room.id === slot.id)?.delay ?? settings?.delays[slot.id] ?? 0).toFixed(2);
        delay.required = true;
        delay.setAttribute("aria-label", `${slot.name}的额外延迟（秒）`);
        seconds.className = "stage-sync-delay";
        seconds.append(delay, " 秒");
        row.append(label, seconds);
        return { id: slot.id, row, check, delay };
      });
      list.replaceChildren(...choices.map(choice => choice.row));
      $("stage-sync-stop").hidden = !settings;
      $("stage-sync-reset").hidden = !settings?.auto;
      apply.textContent = settings ? "应用" : "开启同步";
      $("stage-sync-empty").hidden = choices.length >= 2;
      validate();
      dialog.showModal();
    }

    button.addEventListener("click", open);
    dialog.addEventListener("keydown", event => { if (event.key === "Escape") event.stopPropagation(); });
    list.addEventListener("change", validate);
    auto.addEventListener("change", validate);
    $("stage-sync-cancel").addEventListener("click", () => dialog.close());
    $("stage-sync-stop").addEventListener("click", () => { end(); dialog.close(); });
    $("stage-sync-reset").addEventListener("click", () => {
      dropRun(false);
      status("正在重新校准所选房间。");
      paint();
      dialog.close();
    });
    $("stage-sync-form").addEventListener("submit", event => {
      event.preventDefault();
      const selected = choices.filter(choice => choice.check.checked && stage.room(choice.id));
      if (selected.length < 2 || selected.length > maxRooms) return status(`请从当前舞台选择 2–${maxRooms} 个房间。`, true);
      const ids = selected.map(choice => choice.id);
      const delays = auto.checked ? {} : Object.fromEntries(selected.map(choice => [choice.id, choice.delay.valueAsNumber]));
      const same = settings && ids.length === settings.ids.length && ids.every(id => settings.ids.includes(id)) &&
        settings.auto === auto.checked && (auto.checked || ids.every(id => delays[id] === settings.delays[id]));
      if (!same) start({ ids, auto: auto.checked, delays });
      dialog.close();
    });
    window.addEventListener("pagehide", () => end(false));
    return { owns, invalidate, refresh };
  };
})();
