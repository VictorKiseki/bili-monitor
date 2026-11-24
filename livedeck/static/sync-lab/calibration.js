"use strict";

(function () {
  const maxDelay = 8;
  const trackingRange = .5;

  function reset(run) {
    run.generation++;
    run.captureAfter = performance.now() + 650;
    run.previous = null;
    run.collecting = false;
    run.worker?.postMessage({ reset: true, generation: run.generation, searchRange: maxDelay,
      trackingRange: run.autoAnchor ? trackingRange : 0 });
  }

  function span(values) {
    return Math.max(...values) - Math.min(...values);
  }

  function measure(run, { generation, lags, confirmed }, apply, notify) {
    if (generation !== run.generation) return;
    if (!lags) {
      run.previous = null;
      notify(run.rooms.length > 3
        ? "正在等待足够的跨房间音频匹配，保留当前延迟。"
        : "暂未找到足够清晰的共同声音，继续沿用当前延迟。");
      return;
    }
    const last = run.rooms.length > 3 && confirmed ? lags : run.previous;
    run.previous = lags;
    if (!last || lags.some((lag, i) => Math.abs(lag - last[i]) > .15)) {
      notify("找到共同声音，正在用下一段确认偏移。");
      return;
    }
    const current = run.rooms.map(room => room.delay);
    const correction = lags.map((lag, i) => -(lag + last[i]) / 2);
    const proposal = current.map((delay, i) => delay + correction[i]);
    const baseline = Math.min(...proposal) - Math.min(...current);
    const delays = proposal.map(value => value - baseline);
    const anchor = run.autoAnchor;
    // 连麦回声可能连续几次指向同一个错误方向，跟踪以首次确认的相对偏移为准。
    if (anchor && (span(correction) > trackingRange ||
        span(delays.map((delay, i) => delay - anchor[i])) > trackingRange)) {
      notify("新估算与已确认的偏移差距过大，保留当前延迟。可重新校准。");
      return;
    }
    if (delays.some(delay => delay > maxDelay)) {
      notify(`估算需要超过 ${maxDelay} 秒的额外延迟。当前补偿保持不变。`, true);
      return;
    }
    const changed = delays.some((delay, i) => Math.abs(delay - current[i]) >= .12);
    if (!anchor) run.autoAnchor = changed ? delays : current;
    if (changed) {
      apply(delays);
      reset(run);
      notify("已更新延迟，继续监听共同声音。");
    } else {
      if (!anchor) reset(run);
      notify("已对齐共同声音，继续检查偏移。");
    }
  }

  function contains(video, time) {
    const ranges = video.buffered;
    for (let i = 0; i < ranges.length; i++) {
      if (time >= ranges.start(i) + .05 && time <= ranges.end(i) - .12) return true;
    }
    return false;
  }

  function livePosition(video, buffer) {
    const ranges = video.buffered;
    if (!ranges.length || video.readyState < 2) return null;
    const index = ranges.length - 1;
    const time = ranges.end(index) - buffer;
    return time >= ranges.start(index) + .15 ? time : null;
  }

  function bindAudio(run, enabled, onMeasure, onFailure) {
    const worker = new Worker("/static/sync-lab/correlate.js");
    run.worker = worker;
    worker.onmessage = event => onMeasure(event.data);
    worker.onerror = onFailure;
    const capture = new AudioWorkletNode(run.context, "sync-lab-capture", {
      numberOfInputs: run.rooms.length, numberOfOutputs: 1, outputChannelCount: [1],
      channelCount: 1, channelCountMode: "explicit", processorOptions: { rooms: run.rooms.length }
    });
    run.capture = capture;
    capture.onprocessorerror = onFailure;
    run.rooms.forEach((room, i) => room.source.connect(capture, 0, i));
    capture.connect(run.context.destination);
    capture.port.onmessage = ({ data }) => {
      const mask = run.audioMask;
      if (!enabled() || (mask === undefined && !run.stable) || performance.now() < run.captureAfter) return;
      // 大组保留采样时钟，只剔除正在跳转、缓冲或调速的音轨。
      const validMask = mask === undefined ? undefined : run.rooms.reduce((valid, { video }, i) =>
        video.seeking || video.paused || video.playbackRate !== 1 || video.readyState < 3 ? valid & ~(1 << i) : valid, mask);
      run.collecting = true;
      worker.postMessage({ ...data, rooms: run.rooms.length, generation: run.generation, validMask }, [data.samples.buffer]);
    };
    reset(run);
  }

  function closeAudio(run) {
    run.worker?.terminate();
    run.worker = null;
    if (run.capture) {
      run.capture.port.close();
      run.capture.disconnect();
      run.capture = null;
    }
  }

  globalThis.LiveDeckCalibration = { maxDelay, reset, measure, contains, livePosition, bindAudio, closeAudio };
})();
