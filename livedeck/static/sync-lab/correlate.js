"use strict";

function fft(real, imag, inverse) {
  const size = real.length;
  for (let i = 1, j = 0; i < size; i++) {
    let bit = size >> 1;
    while (j & bit) { j ^= bit; bit >>= 1; }
    j ^= bit;
    if (i < j) {
      [real[i], real[j]] = [real[j], real[i]];
      [imag[i], imag[j]] = [imag[j], imag[i]];
    }
  }
  for (let span = 2; span <= size; span *= 2) {
    const half = span / 2;
    const angle = (inverse ? 2 : -2) * Math.PI / span;
    const cos = Math.cos(angle), sin = Math.sin(angle);
    for (let start = 0; start < size; start += span) {
      let wr = 1, wi = 0;
      for (let j = start; j < start + half; j++) {
        const k = j + half;
        const re = real[k] * wr - imag[k] * wi;
        const im = real[k] * wi + imag[k] * wr;
        real[k] = real[j] - re;
        imag[k] = imag[j] - im;
        real[j] += re;
        imag[j] += im;
        const next = wr * cos - wi * sin;
        wi = wr * sin + wi * cos;
        wr = next;
      }
    }
  }
}

function correlator(tracks, radius, rate) {
  const length = tracks[0].length - 2 * radius;
  const floor = length * 1e-8;
  const positions = 2 * radius + 1;
  const exclusion = Math.ceil(rate * .09);
  let size = 1;
  while (size < tracks[0].length + length - 1) size *= 2;
  const templates = [], signals = [];
  const real = new Float64Array(size), imag = new Float64Array(size);
  const scores = new Float64Array(positions);

  function template(index) {
    if (templates[index]) return templates[index];
    const input = tracks[index];
    const re = new Float64Array(size), im = new Float64Array(size);
    let mean = 0, energy = 0;
    for (let i = 0; i < length; i++) mean += input[radius + i] / length;
    for (let i = 0; i < length; i++) {
      const centered = input[radius + i] - mean;
      re[length - 1 - i] = centered;
      energy += centered * centered;
    }
    if (energy >= floor) fft(re, im, false);
    return (templates[index] = { re, im, energy });
  }

  function signal(index) {
    if (signals[index]) return signals[index];
    const input = tracks[index];
    const re = new Float64Array(size), im = new Float64Array(size);
    const scales = new Float64Array(positions);
    let sum = 0, square = 0;
    for (let i = 0; i < length; i++) { sum += input[i]; square += input[i] ** 2; }
    for (let i = 0; i < positions; i++) {
      const variance = square - sum ** 2 / length;
      if (variance > floor) scales[i] = 1 / Math.sqrt(variance);
      const entering = input[i + length] || 0, leaving = input[i];
      sum += entering - leaving;
      square += entering ** 2 - leaving ** 2;
    }
    re.set(input);
    fft(re, im, false);
    return (signals[index] = { re, im, scales });
  }

  return (from, to) => {
    const a = template(from);
    if (a.energy < floor) return [];
    const b = signal(to);
    // 每路正向 FFT 及音量归一化数据在本窗口内复用，各配对只做逆变换。
    for (let i = 0; i < size; i++) {
      real[i] = a.re[i] * b.re[i] - a.im[i] * b.im[i];
      imag[i] = a.re[i] * b.im[i] + a.im[i] * b.re[i];
    }
    fft(real, imag, true);
    const scale = 1 / (size * Math.sqrt(a.energy));
    for (let i = 0; i < positions; i++) scores[i] = Math.abs(real[i + length - 1]) * scale * b.scales[i];
    const result = [];
    for (let k = 0; k < 3; k++) {
      let index = 0;
      for (let i = 1; i < positions; i++) if (scores[i] > scores[index]) index = i;
      if (scores[index] < .25) break;
      result.push({ lag: (index - radius) / rate, score: scores[index] });
      scores.fill(0, Math.max(0, index - exclusion), index + exclusion + 1);
    }
    return result;
  };
}

function clearPeak(peaks) {
  const [best, next] = peaks;
  return best && (!next || next.score <= best.score * .85) ? best : null;
}

// 两三路沿用直接匹配和三边校验。
function match(tracks, radius, rate) {
  const compare = correlator(tracks, radius, rate);
  const ab = compare(0, 1);
  if (tracks.length === 2) {
    const hit = clearPeak(ab);
    return hit ? [0, hit.lag] : null;
  }
  const ac = compare(0, 2);
  const bc = compare(1, 2);
  const candidates = [];
  for (const b of ab) for (const c of ac) for (const edge of bc) {
    const mismatch = c.lag - b.lag - edge.lag;
    if (Math.abs(mismatch) > .10) continue;
    candidates.push({ lag: [0, b.lag + mismatch / 3, c.lag - mismatch / 3],
      score: (b.score + c.score + edge.score) / 3 });
  }
  candidates.sort((a, b) => b.score - a.score);
  const best = candidates[0];
  if (!best || candidates.some(candidate => candidate.score > best.score * .88 &&
      candidate.lag.some((lag, i) => Math.abs(lag - best.lag[i]) > .15))) return null;
  return best.lag;
}

function project(roomCount, edges) {
  const lags = Array(roomCount).fill(null), tree = [];
  lags[0] = 0;
  while (tree.length < roomCount - 1) {
    // 用相关性最强的跨边界关系把下一间房接到参考时间轴。
    let best = null;
    for (const edge of edges) {
      if ((lags[edge.a] === null) === (lags[edge.b] === null)) continue;
      if (!best || edge.score > best.score) best = edge;
    }
    if (!best) break;
    if (lags[best.a] === null) lags[best.a] = lags[best.b] - best.lag;
    else lags[best.b] = lags[best.a] + best.lag;
    tree.push(best);
  }
  return { lags, tree };
}

function createGroupMatcher(roomCount) {
  const edges = [];
  for (let a = 0; a < roomCount; a++) for (let b = a + 1; b < roomCount; b++) {
    edges.push({ a, b, seen: -Infinity, tried: 0, confirmed: false });
  }
  const reference = edges.slice(0, roomCount - 1);
  const peers = edges.slice(roomCount - 1);
  const budget = 2 * (roomCount - 1);
  let round = 0;

  return (compare, validMask = (1 << roomCount) - 1) => {
    round++;
    let probes = 0;
    const fresh = edge => round - edge.seen <= 3;
    const available = edge => Boolean((validMask & (1 << edge.a)) && (validMask & (1 << edge.b)));
    const canProbe = edge => probes < budget && edge.tried !== round && available(edge);
    function probe(edge) {
      if (!canProbe(edge)) return;
      edge.tried = round;
      probes++;
      const hit = clearPeak(compare(edge.a, edge.b));
      if (!hit) return;
      edge.confirmed = fresh(edge) && Math.abs(edge.lag - hit.lag) <= .10;
      edge.lag = edge.confirmed ? (edge.lag + hit.lag) / 2 : hit.lag;
      edge.score = hit.score;
      edge.seen = round;
    }

    reference.forEach(probe);
    const network = () => project(roomCount, edges.filter(fresh));
    // 优先复查上一轮已找到的中转边，再把剩余次数用于扩展尚未接上的房间。
    let hint = network();
    if (!reference.every(fresh)) {
      hint.tree.forEach(probe);
      hint = network();
    }
    while (hint.tree.length < roomCount - 1) {
      const next = peers.filter(edge => canProbe(edge) &&
        (hint.lags[edge.a] === null) !== (hint.lags[edge.b] === null))
        .sort((a, b) => a.tried - b.tried)[0];
      if (!next) return null;
      probe(next);
      hint = network();
    }
    // 最近没查过的额外关系轮流用于校验闭环。普通窗口只查参考边和两条交叉边。
    peers.filter(edge => canProbe(edge) && !hint.tree.includes(edge))
      .sort((a, b) => a.tried - b.tried).slice(0, 2).forEach(probe);

    // 每条采纳的关系来自两个独立音频窗口。短暂无声时，仍可用近期关系接上其他房间。
    const confirmed = edges.filter(edge => fresh(edge) && edge.confirmed);
    const result = project(roomCount, confirmed);
    if (result.tree.length !== roomCount - 1 || !confirmed.some(edge => edge.seen === round)) return null;
    const checks = edges.filter(edge => fresh(edge) && (edge.confirmed || edge.seen === round));
    if (checks.some(edge => Math.abs(result.lags[edge.b] - result.lags[edge.a] - edge.lag) > .12)) return null;
    return result.lags;
  };
}

if (typeof module === "object") module.exports = { match, correlator, createGroupMatcher };

if (typeof self !== "undefined") {
  let chunks = [], count = 0, total = 0, lastTemplate = -Infinity, generation = 0;
  let searchRange = 0, trackingRange = 0, groupMatcher = null;

  self.onmessage = ({ data }) => {
    if (data.reset) {
      chunks = [];
      count = total = 0;
      lastTemplate = -Infinity;
      generation = data.generation;
      searchRange = data.searchRange;
      trackingRange = data.trackingRange;
      groupMatcher = null;
      return;
    }
    if (data.generation !== generation) return;
    const { samples, rate, rooms, validMask = (1 << rooms) - 1 } = data;
    const frames = samples.length / rooms;
    chunks.push({ samples, validMask });
    count += frames;
    total += frames;
    const windowSize = Math.round(3 * rate);
    // 锁定一组偏移后只搜索小范围残差，避免后续匹配到隔了几秒的重复声音。
    const maxRadius = Math.round((trackingRange || searchRange) * rate);
    while (count - chunks[0].samples.length / rooms >= windowSize + 2 * maxRadius) {
      count -= chunks.shift().samples.length / rooms;
    }
    const wideWindowReady = count >= windowSize + 2 * maxRadius;
    // 各种组规模都先用短窗口尽快校准，再在积累足够音频时搜索完整范围。
    const radius = trackingRange > 0 || wideWindowReady ? maxRadius : Math.round(2.5 * rate);
    const length = windowSize + 2 * radius;
    const template = total - radius - windowSize;
    if (count < length || template - lastTemplate < 3.25 * rate) return;
    lastTemplate = template;
    const tracks = Array.from({ length: rooms }, () => new Float32Array(length));
    let cursor = length - count;
    let commonMask = (1 << rooms) - 1;
    for (const chunk of chunks) {
      if (cursor + chunk.samples.length / rooms > 0) commonMask &= chunk.validMask;
      for (let j = 0; j < chunk.samples.length; j += rooms, cursor++) {
        if (cursor < 0) continue;
        for (let i = 0; i < rooms; i++) tracks[i][cursor] = chunk.samples[j + i];
      }
    }
    if (rooms <= 3) {
      self.postMessage({ generation, lags: match(tracks, radius, rate) });
      return;
    }
    if (!groupMatcher) groupMatcher = createGroupMatcher(rooms);
    let compare;
    const lags = groupMatcher((a, b) => {
      if (!compare) compare = correlator(tracks, radius, rate);
      return compare(a, b);
    }, commonMask);
    self.postMessage({ generation, lags, confirmed: Boolean(lags) });
  };
}
