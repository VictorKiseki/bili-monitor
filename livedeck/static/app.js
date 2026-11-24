/* DD全能工作间 · 监控室前端：多屏舞台 + 房间名单 + 合并弹幕流 */
"use strict";
(function () {
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var stageSync = null;

  var st = {
    snap: null, rooms: [], info: {}, active: [], listening: [], msgs: [], seen: Object.create(null), seenBase: 0,
    q: [], qTimer: null, railSig: "", railCompact: false,
    filterRoom: 0, preset: "auto", solo: 0,
    sse: null, sseTries: 0, sseRetryAt: 0, stall: {}, tiles: {}, players: {}, login: null, streamPort: 0,
    qrTimer: null, qrKey: "", qrDeadline: 0,
    received: 0, stamps: [], sendAt: {}, sendTo: 0, autoScroll: true,
    grpOpen: {}, quality: {}, qualities: {}, volume: {}, qUser: {}, grpRoom: 0, delRoom: 0, net: null, dragId: 0,
    stallHist: {}, degraded: {}, zoom: {},
    mode: "low",
    bgKeep: false,
    emo: {}, emoTab: 0, emoRoom: 0,
    combos: [], filterCombo: 0,
    chatSplit: false, subSource: "", chatShowTime: true,
    mainPane: { kind: "all", clearedThrough: -1 },
    subPane: { kind: "all", clearedThrough: -1 },
    msgArrival: new WeakMap(),
    colors: {}, colRoom: 0,
    groupOrder: [], dragGrp: "", dropAfter: false
  };

  var KIND_LABEL = { danmaku: "弹幕", gift: "礼物", sc: "醒目留言", guard: "舰长", live: "开播", sys: "系统" };

  /* ---------------- 基础工具 ---------------- */
  function api(path, body) {
    var opt = { headers: {} };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (r) {
      return r.text().then(function (t) {
        var j = {};
        try { j = JSON.parse(t); } catch (e) { j = { ok: false, error: "返回不是 JSON" }; }
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        if (j && j.ok === false && j.error) throw new Error(j.error);
        return j;
      });
    });
  }

  function toast(text, kind) {
    var box = $("#toasts");
    var d = document.createElement("div");
    d.className = "toast" + (kind ? " " + kind : "");
    d.textContent = text;
    box.appendChild(d);
    setTimeout(function () { d.remove(); }, kind === "err" ? 4200 : 2600);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
    });
  }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function clock(ts) {
    var d = new Date((ts || 0) * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function hue(id) { var h = 0; var s = String(id); for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360; return h; }
  // LIVE 旁边那串数字展示在线人气，用中文习惯的「万/亿」
  function fmtOnline(n) {
    n = Number(n) || 0;
    if (n >= 100000000) return (n / 100000000).toFixed(1) + "亿";
    if (n >= 10000) return (n / 10000).toFixed(1) + "万";
    return String(n);
  }

  function roomName(id) {
    var inf = (st.info && st.info[String(id)]) || {};
    if (inf.uname) return inf.uname;
    for (var i = 0; i < st.rooms.length; i++) if (st.rooms[i].room_id === id) return st.rooms[i].name || ("房间 " + id);
    return "房间 " + id;
  }
  function shortName(id) {
    var n = roomName(id).replace(/\s+/g, "");
    return n.length > 7 ? n.slice(0, 7) : n;
  }
  // 「在不在播」只认这一个函数。以前顶栏数的是弹幕桥那份、格子角标优先房间信息、
  // 左栏名单又用监控台写进名单的状态，三份不同步时会出现「顶栏在播 0、名单却亮 LIVE、
  // 画面还在播」这种自相矛盾 —— 现在统一按：房间信息（getInfoByRoom，20 秒刷）
  // → 名单里的开播状态（监控台 10 秒写一次）→ 都没有就是「未知」，不再装成离线。
  function liveStatusOf(id) {
    var inf = (st.info && st.info[String(id)]) || {};
    if (inf.known) return { status: Number(inf.live_status) || 0, known: true };
    for (var i = 0; i < st.rooms.length; i++) {
      if (st.rooms[i].room_id === id && st.rooms[i].live_status != null) {
        return { status: Number(st.rooms[i].live_status) || 0, known: true };
      }
    }
    return { status: -1, known: false };
  }
  function liveBadge(v) {
    if (v === 1) return '<span class="badge live">LIVE</span>';
    if (v === 2) return '<span class="badge round">轮播</span>';
    if (v === 0) return '<span class="badge off">离线</span>';
    return '<span class="badge off" title="还没拿到开播状态，下一轮刷新后自动补上">未知</span>';
  }
  function bridgeState() {
    var b = st.snap && st.snap.bridge || {};
    var counts = Object.keys(b.rooms || {}).length;
    var open = 0;
    Object.keys(b.rooms || {}).forEach(function (k) {
      var s = b.rooms[k].state;
      if (s === "open" || s === "auth") open++;
    });
    return { status: b.status || "offline", state: b.state || "", error: b.error || "",
             open: open, rooms: counts };
  }

  /* ---------------- 登录态 ---------------- */
  function loginChip() {
    var l = (st.snap && st.snap.login) || {};
    var chip = $("#login-chip");
    var label, cls, key;
    if (!l.sessdata_masked && !l.sessdata) { label = "登录 B 站账号"; cls = ""; key = "未登录"; }
    else if (l.state === "network" || l.state === "risk") {
      // 网络不通 / 被风控拦时 Cookie 往往还是好的（2026-11-20 用户反馈的场景），
      // 不能当成「登录已失效」吓人；后端会在 15 秒后自己重试。
      label = "登录态未校验"; cls = "warn";
      key = l.state === "risk" ? "被风控拦截 · 稍后重试" : "网络异常 · 稍后重试";
    }
    else if (!l.verified) { label = "登录已失效"; cls = "warn"; key = "需重新登录"; }
    else if (!l.can_send) { label = (l.uname || "已登录") + " · 无法发弹幕"; cls = "warn"; key = "缺少 bili_jct"; }
    else { label = l.uname || "已登录"; cls = "ok"; key = "可发弹幕"; }
    chip.className = "chip " + cls;
    chip.innerHTML = '<span class="dot ' + (cls === "ok" ? "on" : cls === "warn" ? "warn" : "") + '"></span>' +
      '<span class="nm-txt" style="max-width:118px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(label) + "</span>" +
      '<span class="k">' + esc(key) + "</span>";
  }

  /* ---------------- 状态快照 ---------------- */
  function applySnapshot(snp) {
    var prevPort = st.streamPort;
    var prevChatSources = JSON.stringify([watchIds().sort(), st.combos]);
    st.snap = snp;
    st.rooms = snp.rooms || [];
    st.info = snp.info || {};
    st.streamPort = snp.stream_port || 0;
    var prevActive = st.active.slice();
    st.active = (snp.active || []).slice();
    syncZoom(prevActive, st.active);
    st.listening = (snp.listening || []).filter(function (r) { return st.active.indexOf(r) < 0; });
    st.combos = snp.combos || [];
    st.colors = snp.colors || {};
    st.groupOrder = snp.group_order || [];
    renderHeader(); loginChip(); renderRail(); renderGrid(); renderChatFilter();
    if (prevChatSources !== JSON.stringify([watchIds().sort(), st.combos])) rebuildFeed();
    // 服务重启后 WS 中继端口会变（stream_port），旧播放器连的是已经没人监听的端口
    if (prevPort && st.streamPort && prevPort !== st.streamPort) rebuildStreams();
    else syncStreams();
  }

  function refresh() {
    return api("/api/snapshot").then(applySnapshot).catch(function (e) { toast(e.message, "err"); });
  }

  // 正在收弹幕的房间：舞台 + 只听弹幕（去重，舞台优先）
  function watchIds() {
    var out = st.active.slice();
    st.listening.forEach(function (r) { if (out.indexOf(r) < 0) out.push(r); });
    return out;
  }
  function watching(id) {
    return st.active.indexOf(id) >= 0 || st.listening.indexOf(id) >= 0;
  }

  /* ---------------- 顶栏 ---------------- */
  function renderHeader() {
    var act = st.active.length;
    var watched = watchIds().length;
    var live = st.active.filter(function (r) { return liveStatusOf(r).status === 1; }).length;
    var b = bridgeState();
    $("#m-rooms").textContent = act + " / " + (st.snap ? st.snap.max_rooms : 16);
    $("#m-live").textContent = live;
    paintRate();
    $("#m-total").textContent = st.received;
    var pill = $("#bridge-pill");
    // waiting = 舞台为空、还没有要收弹幕的房间：这是「等待选台」，不是故障，别显示成离线
    var cls = b.status === "online" ? "on"
      : b.status === "starting" || b.status === "stale" ? "warn"
      : b.status === "waiting" ? "" : "off";
    pill.className = "dot " + cls;
    pill.title = "弹幕引擎：" + b.status + (b.error ? " / " + b.error : "") + " · 已连 " + b.open + " 个房间";
    $("#board-status").textContent = b.status === "online" ? ("引擎就绪 · " + b.open + "/" + watched + " 路已连通") :
      b.status === "starting" ? "引擎启动中…" :
      b.status === "waiting" ? "等待选台" : "引擎离线";
  }

  function rate() {
    var now = Date.now() / 1000;
    st.stamps = st.stamps.filter(function (t) { return now - t < 60; });
    return st.stamps.length;
  }

  // 弹幕栏底部那根细条 = 最近 60 秒的弹幕条数（和顶栏「弹幕/分」同一个数），
  // 30 条/分算满格；数字与条子分开更新过，会看到条子滞后十多秒，所以统一由这里画。
  function paintRate() {
    var r = rate();
    var num = $("#m-rate");
    if (num) num.textContent = r;
    var bar = $("#rate-bar");
    if (bar) bar.style.width = Math.min(100, Math.round(r / 30 * 100)) + "%";
  }

  /* ---------------- 左栏：按监控台分组 ---------------- */
  // 播放模式：low = 低延迟（缓冲 2.5s，定时裁掉旧数据）；smooth = 流畅优先（缓冲 4s，不裁数据，每 20 分钟软重连回收）
  function latTarget() { return st.mode === "smooth" ? 4.0 : 2.5; }
  // 默认「流畅优先」：实测 1080p60 房间在 low 模式下每 8~10 秒会被 SourceBuffer 裁剪抖一次，
  // smooth 不裁缓冲 + 4 秒水位，长时间 0 抖动；要更低延迟可点舞台右上角切「低延迟」。
  st.mode = (function () {
    try { return localStorage.getItem("ld.mode") === "low" ? "low" : "smooth"; } catch (e) { return "smooth"; }
  })();

  var QUALITY_FALLBACK = [
    { qn: 10000, label: "原画" }, { qn: 400, label: "蓝光" }, { qn: 250, label: "超清" },
    { qn: 150, label: "高清" }, { qn: 80, label: "流畅" }
  ];

  // 同时看的画面越多，默认画质越低：16 路 1080p 解码会吃掉一两个 G 内存
  function defaultQn() {
    var n = st.active.length;
    if (n > 9) return 150;
    if (n > 4) return 400;
    return 10000;
  }

  // 记住每个房间上次用的画质（自动降档或手动选过），刷新后不用再卡一轮
  function qnBook() {
    try { return JSON.parse(localStorage.getItem("ld.qn") || "{}") || {}; } catch (e) { return {}; }
  }
  function savedQn(id) {
    var qn = parseInt(qnBook()[id], 10);
    return qn > 0 ? qn : 0;
  }
  function saveQn(id, qn) {
    try {
      var m = qnBook();
      if (qn > 0) m[id] = qn; else delete m[id];
      localStorage.setItem("ld.qn", JSON.stringify(m));
    } catch (e) {}
  }

  function fillQuality(id, sel, cur) {
    var list = st.qualities[id] || QUALITY_FALLBACK;
    sel.innerHTML = list.map(function (q) {
      return '<option value="' + q.qn + '">' + esc(q.label) + "</option>";
    }).join("");
    // 这个房间不一定提供默认档（比如 150 流畅），退化到不高于它的最高档，并记下来与实际拉流一致
    var want = parseInt(cur, 10) || list[0].qn;
    var has = list.some(function (q) { return q.qn === want; });
    if (!has) {
      var lower = list.filter(function (q) { return q.qn <= want; })
        .sort(function (a, b) { return b.qn - a.qn; })[0];
      want = lower ? lower.qn : list[list.length - 1].qn;
    }
    sel.value = String(want);
    st.quality[id] = want;
  }

  // 头像还没拉到时用首字母占位：<img src=""> 会让行里空一块，破图后更是什么都看不到
  function initialOf(name) {
    var s = String(name || "").replace(/^\s+/, "");
    return s ? s.slice(0, 1).toUpperCase() : "?";
  }

  // 破图（头像 403 / CDN 抽风）时换成首字母占位，别让行里留一块黑
  window.avFallback = function (img) {
    var span = document.createElement("span");
    span.className = "av ph";
    span.setAttribute("aria-hidden", "true");
    span.textContent = (img && img.getAttribute && img.getAttribute("data-init")) || "?";
    if (img && img.parentNode) img.parentNode.replaceChild(span, img);
  };

  function avTag(r) {
    if (r.face) {
      return '<img class="av" loading="lazy" referrerpolicy="no-referrer" data-init="' + esc(initialOf(r.name)) +
        '" src="' + esc(r.face) + '" onerror="avFallback(this)" alt="">';
    }
    return '<span class="av ph" aria-hidden="true">' + esc(initialOf(r.name)) + "</span>";
  }

  // 头像是一张张异步补上来的，统一节流重绘左栏（SSE 一次可能连来十几条）
  var railSoon = 0;
  function renderRailSoon() {
    if (railSoon) return;
    railSoon = setTimeout(function () { railSoon = 0; renderRail(); }, 180);
  }

  function railRow(r) {
    var on = st.active.indexOf(r.room_id) >= 0;
    var listening = st.listening.indexOf(r.room_id) >= 0;
    var status = liveStatusOf(r.room_id).status;
    var name = esc(r.name || ("房间 " + r.room_id));
    var attrs = ' class="room-row' + (on ? " on" : "") + (listening ? " listening" : "") +
      (r.temp ? " temp" : "") + '" data-room="' + r.room_id + '"';
    if (st.railCompact) {
      return '<button type="button"' + attrs + ' title="' + name + '" aria-pressed="' + on + '">' +
        avTag(r) + '<span class="rail-room-name">' + name + "</span>" + liveBadge(status) + "</button>";
    }
    var title = status === 1 ? esc(String(r.title || "").trim()) : "";
    // 行内只留一个三横线入口，加入/只听/分组/移除都收进二次菜单，避免一排小图标难认
    var acts = '<span class="acts"><button data-act="menu" class="more" title="更多操作" aria-label="更多操作">' +
      '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M3 8h10M3 11.5h10"/></svg></button></span>';
    return "<div" + attrs + ">" + avTag(r) +
      '<div class="meta"><div class="nm">' + liveBadge(status) + "<span>" + name + "</span></div>" +
      (title ? '<div class="live-title" title="' + title + '">' + title + "</div>" : "") +
      '<div class="sub">' + esc(String(r.room_id)) + (r.grp ? " · " + esc(r.grp) : "") +
      (listening ? " · 只听弹幕" : "") + "</div></div>" + acts + "</div>";
  }

  function railHit(r, q) {
    if (!q) return true;
    return (r.name || "").toLowerCase().indexOf(q) >= 0 || String(r.room_id).indexOf(q) >= 0 ||
      (r.grp || "").toLowerCase().indexOf(q) >= 0 || (r.title || "").toLowerCase().indexOf(q) >= 0;
  }

  /* ---------------- 分组顺序（拖动排序，存服务端） ---------------- */
  // 排序键：先按用户排过的 group_order，再按首次出现顺序；「临时」永远最后
  function sortGroups(names) {
    var idx = {}, known = st.groupOrder || [];
    known.forEach(function (g, i) { if (!(g in idx)) idx[g] = i; });
    var list = names.filter(function (g) { return g !== "临时"; })
      .map(function (g, i) { return { g: g, i: (g in idx ? idx[g] : 1e6 + i) }; })
      .sort(function (a, b) { return a.i - b.i; })
      .map(function (x) { return x.g; });
    if (names.indexOf("临时") >= 0) list.push("临时");
    return list;
  }

  // 已保存顺序 + 当前名单里新冒出来的分组，凑成一份完整顺序再提交
  function fullGroupOrder() {
    var out = [], seen = Object.create(null);
    (st.groupOrder || []).forEach(function (g) { if (g && !seen[g]) { seen[g] = 1; out.push(g); } });
    st.rooms.forEach(function (r) {
      var g = r.grp || "未分组";
      if (g !== "临时" && !seen[g]) { seen[g] = 1; out.push(g); }
    });
    return out;
  }

  function clearDropMarks(root) {
    $$(".grp.drop-before, .grp.drop-after", root).forEach(function (el) {
      el.classList.remove("drop-before", "drop-after");
    });
  }

  function moveGroup(src, dst, after) {
    if (!src || !dst || src === dst) return;
    var list = fullGroupOrder();
    if (list.indexOf(src) < 0) list.push(src);
    if (list.indexOf(dst) < 0) list.push(dst);
    list.splice(list.indexOf(src), 1);
    var at = list.indexOf(dst);
    list.splice(after ? at + 1 : at, 0, src);
    api("/api/groups/order", { order: list }).then(function (j) {
      st.groupOrder = j.order || list;
      st.railSig = "";
      renderRail();
      toast("分组顺序已保存", "ok");
    }).catch(function (e) { toast("保存分组顺序失败：" + e.message, "err"); });
  }

  function bindGrpDrag(h) {
    var box = h.parentNode;
    var g = box.dataset.grp;
    h.addEventListener("dragstart", function (ev) {
      st.dragGrp = g;
      h.classList.add("dragging");
      if (ev.dataTransfer) {
        try { ev.dataTransfer.effectAllowed = "move"; } catch (e) {}
        try { ev.dataTransfer.setData("text/plain", g); } catch (e) {}
      }
    });
    h.addEventListener("dragend", function () {
      st.dragGrp = "";
      st.dropAfter = false;
      h.classList.remove("dragging");
      clearDropMarks(document);
    });
    h.addEventListener("dragover", function (ev) {
      if (!st.dragGrp || st.dragGrp === g) return;
      ev.preventDefault();
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
      // 光标过中线就算「放到它后面」，给一条明显的插入提示线
      st.dropAfter = (ev.clientY - h.getBoundingClientRect().top) > h.offsetHeight / 2;
      clearDropMarks(document);
      box.classList.add(st.dropAfter ? "drop-after" : "drop-before");
    });
    h.addEventListener("drop", function (ev) {
      ev.preventDefault();
      var src = st.dragGrp || (ev.dataTransfer ? ev.dataTransfer.getData("text/plain") : "");
      var after = st.dropAfter;
      st.dragGrp = "";
      clearDropMarks(document);
      st.suppressGrpClick = true;
      setTimeout(function () { st.suppressGrpClick = false; }, 350);
      moveGroup(src, g, after);
    });
  }

  function renderRail() {
    $("#rail-count").textContent = st.active.length + "/" + (st.snap ? st.snap.max_rooms : 16);
    var listenN = st.listening.length;
    var listenPill = $("#rail-listen");
    listenPill.textContent = "听 " + listenN;
    listenPill.classList.toggle("hide", !listenN);
    var hidden = (st.snap && st.snap.hidden) || 0;
    $("#rail-sub-text").textContent = hidden ? ("名单来自 data/bot.db · 已隐藏 " + hidden + " 个") : "名单来自本地监控台 data/bot.db";
    $("#rail-unhide").classList.toggle("hide", !hidden);

    var body = $("#rail-body");
    // 半收起时暂停搜索，两种宽度共用分组展开状态。
    var q = st.railCompact ? "" : ($("#rail-search").value || "").trim().toLowerCase();
    var rows = st.rooms.filter(function (r) { return railHit(r, q); });
    // 舞台上不在监控名单里的临时房间，单独归到「临时」组（与后端 set_active 的 grp 一致）
    var temps = st.active.filter(function (id) {
      return !st.rooms.some(function (x) { return x.room_id === id; });
    }).map(function (id) {
      var inf = (st.info && st.info[String(id)]) || {};
      return { room_id: id, name: inf.uname || ("房间 " + id),
        title: inf.title, grp: "临时", temp: true, in_db: false, face: "" };
    }).filter(function (r) { return railHit(r, q); });
    // 名单/搜索/分组状态都没变就别重建：44 个头像每 12 秒重来一次会让页面周期性一卡
    var sig = JSON.stringify([st.railCompact, q, st.active, st.listening, st.grpOpen, hidden, st.groupOrder,
      rows.map(function (r) { return [r.room_id, r.name, r.grp, liveStatusOf(r.room_id).status, r.face, r.temp, r.in_db, r.title]; }),
      temps.map(function (r) { return [r.room_id, r.name, liveStatusOf(r.room_id).status, r.title]; })]);
    if (sig === st.railSig) { return; }
    st.railSig = sig;
    if (!rows.length && !temps.length) {
      closeRoomMenu();
      body.innerHTML = st.railCompact ? '<div class="rail-empty">暂无房间</div>' :
        '<div class="rail-empty">没有匹配的直播间<br>可在底部直接填房间号临时加入</div>';
      return;
    }
    var groups = {}, order = [];
    rows.concat(temps).forEach(function (r) {
      var g = r.grp || "未分组";
      if (!groups[g]) { groups[g] = []; order.push(g); }
      groups[g].push(r);
    });
    order = sortGroups(order);
    var focused = document.activeElement;
    var focusedRoom = st.railCompact && body.contains(focused) && focused.dataset.room;
    body.innerHTML = order.map(function (g) {
      var list = groups[g];
      var label = esc(g);
      var onN = list.filter(function (r) { return st.active.indexOf(r.room_id) >= 0; }).length;
      var open = st.grpOpen[g] !== false;
      return '<div class="grp" data-grp="' + label + '">' +
        '<div class="grp-head" draggable="true" title="拖动可调整分组顺序"><span class="caret">' + (open ? "▾" : "▸") + '</span>' +
        '<span class="gn" title="' + label + '">' + label + '</span><span class="gc">' + list.length + "</span>" +
        (onN ? '<span class="go">' + onN + " 在台</span>" : "") + "</div>" +
        (open ? '<div class="grp-body">' + list.map(railRow).join("") + "</div>" : "") +
        "</div>";
    }).join("");

    $$(".grp-head", body).forEach(function (h) {
      h.addEventListener("click", function () {
        // 刚拖完那一下会先落到 click 上，别顺手把分组收起/展开
        if (st.suppressGrpClick) { st.suppressGrpClick = false; return; }
        var g = h.parentNode.dataset.grp;
        st.grpOpen[g] = st.grpOpen[g] === false;
        renderRail();
      });
      bindGrpDrag(h);
    });
    $$(".room-row", body).forEach(function (el) {
      el.addEventListener("click", function () { toggleRoom(parseInt(el.dataset.room, 10)); });
      $$("[data-act]", el).forEach(function (btn) {
        btn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var id = parseInt(el.dataset.room, 10);
          if (btn.dataset.act === "menu") openRoomMenu(btn, id);
          else if (btn.dataset.act === "stage") toggleRoom(id);
          else if (btn.dataset.act === "listen") toggleListen(id);
          else if (btn.dataset.act === "grp") openGrp(id);
          else if (btn.dataset.act === "del") openDel(id);
        });
      });
    });
    if (focusedRoom) {
      var roomButton = $('.room-row[data-room="' + focusedRoom + '"]', body);
      if (roomButton) roomButton.focus({ preventScroll: true });
    }
    if (roomMenu) {
      var id = parseInt(roomMenu.dataset.room, 10);
      var btn = $('.room-row[data-room="' + id + '"] [data-act="menu"]', body);
      if (btn) openRoomMenu(btn, id);
      else closeRoomMenu();
    }
  }

  /* ---------------- 房间行二次操作菜单 ---------------- */
  var roomMenu = null;
  var roomMenuScrollTop = 0;

  function closeRoomMenu() {
    if (roomMenu && roomMenu.parentNode) roomMenu.parentNode.removeChild(roomMenu);
    roomMenu = null;
  }

  function openRoomMenu(btn, id) {
    var r = inDbRoom(id);
    var on = st.active.indexOf(id) >= 0;
    var listening = st.listening.indexOf(id) >= 0;
    var inDb = !!(r && r.in_db);
    var sig = JSON.stringify([id, on, !on && listening, inDb]);
    // 名单重建后重新定位，菜单项没变时保留按钮和焦点。
    if (!roomMenu || roomMenu.dataset.sig !== sig) {
      closeRoomMenu();
      roomMenu = buildRoomMenu(id, on, listening, inDb);
      roomMenu.dataset.room = id;
      roomMenu.dataset.sig = sig;
      document.body.appendChild(roomMenu);
    }
    var m = roomMenu;
    // 贴着三横线按钮右下展开；下方放不下就翻到上方，左右夹在视口内
    var box = btn.getBoundingClientRect();
    var mw = m.offsetWidth, mh = m.offsetHeight;
    var left = Math.min(window.innerWidth - mw - 8, Math.max(8, box.right - mw));
    var top = box.bottom + 6;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, box.top - mh - 6);
    m.style.left = left + "px";
    m.style.top = top + "px";
    roomMenuScrollTop = railBody.scrollTop;
  }

  function buildRoomMenu(id, on, listening, inDb) {
    var items = [];
    items.push({ act: "stage", label: on ? "移出舞台" : "加入舞台" });
    if (!on) items.push({ act: "listen", label: listening ? "停止只听弹幕" : "只听弹幕（不上台）" });
    if (inDb) items.push({ act: "grp", label: "设置分组" });
    else items.push({ act: "add", label: "加入监控台名单" });
    items.push({ act: "color", label: "设置印象色" });
    items.push({ act: "del", label: "移除直播间", danger: true });

    var m = document.createElement("div");
    m.className = "room-menu";
    m.setAttribute("role", "menu");
    items.forEach(function (it) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.className = it.danger ? "danger" : "";
      b.textContent = it.label;
      b.addEventListener("click", function () {
        closeRoomMenu();
        if (it.act === "stage") toggleRoom(id);
        else if (it.act === "listen") toggleListen(id);
        else if (it.act === "grp") openGrp(id);
        else if (it.act === "add") joinMonitor(id);
        else if (it.act === "color") openColor(id);
        else if (it.act === "del") openDel(id);
      });
      m.appendChild(b);
    });
    return m;
  }

  document.addEventListener("mousedown", function (e) {
    if (roomMenu && !roomMenu.contains(e.target)) closeRoomMenu();
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeRoomMenu();
  });
  window.addEventListener("resize", closeRoomMenu);
  // 只跟左栏自己的滚动，不跟弹幕流：弹幕一直在滚，不能把菜单带走
  var railBody = $("#rail-body");
  if (railBody) railBody.addEventListener("scroll", function () {
    // 列表重建可能调整滚动位置，菜单已经按调整后的位置定位。
    if (roomMenu && railBody.scrollTop !== roomMenuScrollTop) closeRoomMenu();
  }, false);

  /* ---------------- 名单操作弹窗 ---------------- */
  function inDbRoom(id) {
    for (var i = 0; i < st.rooms.length; i++) if (st.rooms[i].room_id === id) return st.rooms[i];
    return null;
  }

  function openGrp(id) {
    var r = inDbRoom(id);
    if (!r || !r.in_db) { toast("临时房间没有分组（只有监控名单里的直播间能设分组）", "err"); return; }
    st.grpRoom = id;
    $("#grp-room").textContent = (r.name || ("房间 " + id)) + " · 房间 " + id;
    $("#grp-input").value = r.grp || "";
    var seen = {}, opts = [];
    st.rooms.forEach(function (x) {
      if (x.grp && !seen[x.grp]) { seen[x.grp] = 1; opts.push('<option value="' + esc(x.grp) + '">'); }
    });
    $("#grp-list").innerHTML = opts.join("");
    $("#grp-mask").classList.remove("hide");
    setTimeout(function () { $("#grp-input").focus(); }, 30);
  }
  function closeGrp() { $("#grp-mask").classList.add("hide"); }
  function saveGrp() {
    var g = $("#grp-input").value.trim();
    api("/api/group", { room: st.grpRoom, grp: g }).then(function (j) {
      var r = inDbRoom(st.grpRoom);
      if (r) r.grp = j.grp || "";
      closeGrp(); renderRail();
      toast("已设为「" + (j.grp || "未分组") + "」", "ok");
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function joinMonitor(id) {
    api("/api/room/add", { room: id }).then(function () {
      toast("已加入监控台名单，现在可以设分组了", "ok");
      return refresh();
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function openDel(id) {
    st.delRoom = id;
    var r = inDbRoom(id);
    var db = !!(r && r.in_db);
    $("#del-room").textContent = roomName(id) + " · 房间 " + id + (db ? "" : "（临时房间）");
    $("#del-disable").classList.toggle("hide", !db);
    $("#del-delete").classList.toggle("hide", !db);
    $("#del-temp").classList.toggle("hide", db);
    $("#del-mask").classList.remove("hide");
  }
  function closeDel() { $("#del-mask").classList.add("hide"); }
  function doRemove(mode) {
    api("/api/room/remove", { room: st.delRoom, mode: mode }).then(function () {
      closeDel();
      var r = inDbRoom(st.delRoom) || {};
      toast(mode === "hide" ? "已在本工具隐藏"
        : (mode === "disable" ? "已从监控台停用"
          : (r.in_db ? "已从监控台名单删除" : "已从列表删除")), "ok");
      return refresh();
    }).catch(function (e) { toast(e.message, "err"); });
  }

  /* ---------------- 下行速度 ---------------- */
  function fmtBps(b) {
    b = b || 0;
    if (b >= 1048576) return (b / 1048576).toFixed(b >= 10485760 ? 0 : 1) + " MB/s";
    if (b >= 1024) return Math.round(b / 1024) + " KB/s";
    return Math.round(b) + " B/s";
  }
  function pollNet() {
    api("/api/net").then(function (j) {
      st.net = j;
      $("#m-net").textContent = fmtBps(j.total_bps);
      var rooms = j.rooms || {};
      var ids = Object.keys(rooms).map(function (k) { return parseInt(k, 10); })
        .filter(function (id) { return st.active.indexOf(id) >= 0; });
      ids.sort(function (a, b) { return (rooms[String(b)] || 0) - (rooms[String(a)] || 0); });
      var head = '<div class="np-head">合计 ' + fmtBps(j.total_bps) + " · " + ids.length + " 路</div>";
      var rows = ids.length ? ids.map(function (id) {
        return '<div class="np-row"><span class="np-n">' + esc(shortName(id)) + "</span><b>" + fmtBps(rooms[String(id)]) + "</b></div>";
      }).join("") : '<div class="np-empty">暂无下行流量</div>';
      $("#net-pop").innerHTML = head + rows;
    }).catch(function () {});
  }

  /* ---------------- 内存：顶栏徽标 + 一键回收 ---------------- */
  function memMb() {
    return (window.performance && performance.memory) ? performance.memory.usedJSHeapSize / 1048576 : null;
  }
  function pollMem() {
    var mb = memMb();
    var el = $("#m-mem");
    if (!el) return;
    el.textContent = mb === null ? "–" : (mb >= 1024 ? (mb / 1024).toFixed(2) + " GB" : Math.round(mb) + " MB");
    el.classList.toggle("warn", mb !== null && mb > 1200);
  }
  var recycleBusy = false;
  function recycleAll() {
    if (recycleBusy) return;
    recycleBusy = true;
    toast("正在回收播放器内存…", "ok");
    var ids = Object.keys(st.tiles).map(function (k) { return parseInt(k, 10); });
    ids.forEach(function (id) { stopStream(id); });
    setTimeout(function () {
      ids.forEach(function (id) {
        var d = st.tiles[id];
        if (!d) return;
        d.dataset.retry = ""; d.dataset.failed = "";
        var v = $("video", d);
        if (v) attachStream(id, d, v);
      });
      recycleBusy = false;
      pollMem();
      toast("已重建全部播放器", "ok");
    }, 350);
  }

  function toggleRoom(id, forceAdd) {
    var cur = st.active.slice();
    var i = cur.indexOf(id);
    if (forceAdd) {
      if (i >= 0) { toast("这个直播间已经在舞台上了"); return; }   // 手填已上屏的房间号不该把它踢下去
      cur.push(id);
    } else if (i >= 0) cur.splice(i, 1);
    else cur.push(id);
    if (cur.length > (st.snap ? st.snap.max_rooms : 16)) { toast("最多同时观看 " + (st.snap.max_rooms || 16) + " 个直播间", "err"); return; }
    setActive(cur);
  }

  // 拖动标题栏换位：舞台顺序就是 st.active 的顺序，POST 回服务端后写进 state，重开也保持
  function reorderTiles(from, to) {
    var cur = st.active.slice();
    var i = cur.indexOf(from), j = cur.indexOf(to);
    if (i < 0 || j < 0 || i === j) return;
    cur.splice(j, 0, cur.splice(i, 1)[0]);
    st.active = cur;
    renderGrid();
    api("/api/active", { rooms: cur })
      .then(function () { toast("位置已调整", "ok"); return refresh(); })
      .catch(function (e) { toast("调整位置失败：" + e.message, "err"); });
  }

  var setActiveBusy = false;
  function setActive(rooms) {
    if (setActiveBusy) return;
    setActiveBusy = true;
    var prevActive = st.active.slice();
    st.active = rooms.slice();
    syncZoom(prevActive, st.active);
    st.listening = st.listening.filter(function (r) { return st.active.indexOf(r) < 0; });
    st.msgs = st.msgs.filter(function (m) { return watching(m.room); });
    try {
      renderGrid(); renderRail(); renderHeader(); renderChatFilter(); rebuildFeed();
    } catch (e) {
      console.error(e);
      toast("界面重绘失败：" + e.message, "err");
    } finally {
      setActiveBusy = false;   // 先解锁：请求失败也不能让左栏失去响应
    }
    api("/api/active", { rooms: rooms })
      .then(function () { return refresh(); })
      .catch(function (e) { toast("切换直播间失败：" + e.message, "err"); });
  }

  // 只听弹幕：只连该房间的弹幕 WS，不上舞台、不拉视频流
  var setListenBusy = false;
  function setListening(rooms) {
    if (setListenBusy) return;
    setListenBusy = true;
    st.listening = rooms.filter(function (r) { return st.active.indexOf(r) < 0; });
    st.msgs = st.msgs.filter(function (m) { return watching(m.room); });
    try {
      renderRail(); renderHeader(); renderChatFilter(); rebuildFeed();
    } catch (e) {
      console.error(e);
      toast("界面重绘失败：" + e.message, "err");
    } finally {
      setListenBusy = false;
    }
    api("/api/listen", { rooms: rooms })
      .then(function () { return refresh(); })
      .catch(function (e) { toast("只听弹幕切换失败：" + e.message, "err"); });
  }

  function toggleListen(id, forceAdd) {
    if (st.active.indexOf(id) >= 0) { toast("这个直播间已在舞台，直接收弹幕"); return; }
    var cur = st.listening.slice();
    var i = cur.indexOf(id);
    if (forceAdd) {
      if (i >= 0) { toast("这个直播间已经在只听弹幕了"); return; }
      cur.push(id);
    } else if (i >= 0) cur.splice(i, 1);
    else cur.push(id);
    var max = st.snap ? (st.snap.max_listen || 16) : 16;
    if (cur.length > max) { toast("最多同时只听 " + max + " 个直播间", "err"); return; }
    setListening(cur);
  }

  /* ---------------- 舞台 ---------------- */
  function layoutFor(n) {
    if (st.solo) return { cols: 1, rows: 1, cap: 1 };
    if (st.preset !== "auto") {
      var p = st.preset.split("x");
      var c = Math.max(1, parseInt(p[0], 10) || 1), r = Math.max(1, parseInt(p[1], 10) || 1);
      return { cols: c, rows: r, cap: c * r };
    }
    if (n <= 1) return { cols: 1, rows: 1, cap: 1 };
    if (n === 2) return { cols: 2, rows: 1, cap: 2 };
    if (n <= 4) return { cols: 2, rows: 2, cap: 4 };
    if (n <= 6) return { cols: 3, rows: 2, cap: 6 };
    if (n <= 9) return { cols: 3, rows: 3, cap: 9 };
    if (n <= 12) return { cols: 4, rows: 3, cap: 12 };
    return { cols: 4, rows: 4, cap: 16 };
  }

  /* 重点观察：把某一路放大成 2x2 / 3x3 格（只影响本机观感，只记在当前页面） */
  function zoomOf(id) {
    var n = parseInt(st.zoom[id], 10) || 1;
    return n >= 2 ? n : 1;
  }
  function zoomLimit() {
    var L = layoutFor(st.active.length);
    return Math.min(3, L.cols, L.rows);
  }
  // 房间每次进舞台都从 1×1 开始：只保留仍在舞台上的重点观察，
  // 刷新页面也不读旧尺寸（不再写 localStorage）。
  function syncZoom(prev, next) {
    prev.forEach(function (id) {
      if (next.indexOf(id) < 0) delete st.zoom[id];
    });
    next.forEach(function (id) {
      if (prev.indexOf(id) < 0) delete st.zoom[id];
    });
  }
  function cycleZoom(id) {
    if (st.solo) { toast("单路视图已经占满舞台，不用再放大", "err"); return; }
    var lim = zoomLimit();
    if (lim < 2) { toast("当前排布放不下 2×2：先把排布切到「4」或更大", "err"); return; }
    var n = zoomOf(id);
    var next = n === 1 ? 2 : (n === 2 && lim >= 3 ? 3 : 1);
    if (next === 1) delete st.zoom[id]; else st.zoom[id] = next;
    renderGrid();
    toast(next === 1 ? "已取消重点观察" : ("重点观察：" + roomName(id) + " 放大到 " + next + "×" + next + "（占 " + next * next + " 格）"));
  }

  function renderGrid() {
    // 单路房间移出舞台后，回到剩余房间或空舞台。
    if (st.solo && st.active.indexOf(st.solo) < 0) st.solo = 0;
    $("#solo-exit").classList.toggle("hide", !st.solo);
    var ids = st.solo ? [st.solo] : st.active;
    var grid = $("#grid");
    var empty = $("#stage-empty");
    $$(".presets button").forEach(function (b) { b.classList.toggle("on", b.dataset.preset === st.preset); });
    if (!ids.length) {
      // 舞台清空时必须真的把播放器停掉，否则「未选择直播间」底下还挂着十几个解码器
      Object.keys(st.tiles).forEach(function (k) {
        var id = parseInt(k, 10);
        stopStream(id);
        try { st.tiles[k].remove(); } catch (e) {}
        delete st.tiles[k];
      });
      grid.classList.add("hide");
      empty.classList.remove("hide");
      $("#stage-meta").textContent = "未选择直播间";
      if (stageSync) stageSync.refresh();
      return;
    }
    grid.classList.remove("hide");
    empty.classList.add("hide");
    var L = layoutFor(ids.length);
    grid.style.gridTemplateColumns = "repeat(" + L.cols + ", minmax(0,1fr))";
    grid.style.gridTemplateRows = "repeat(" + L.rows + ", minmax(0,1fr))";
    // 重点观察的格子会占 2x2 / 3x3，用 dense 让后面的小格子补进空位
    grid.style.gridAutoFlow = st.solo ? "row" : "row dense";
    var cells = L.cols * L.rows;
    var shown = [];
    var used = 0;
    for (var zi = 0; zi < ids.length; zi++) {
      var zid = ids[zi];
      var zz = st.solo ? 1 : Math.min(zoomOf(zid), L.cols, L.rows);
      if (!st.solo && used + zz * zz > cells) { break; }   // 装不下的排到后面（仍在收弹幕）
      used += zz * zz;
      shown.push(zid);
    }
    Object.keys(st.tiles).forEach(function (k) {
      if (shown.indexOf(parseInt(k, 10)) < 0) { stopStream(parseInt(k, 10)); st.tiles[k].remove(); delete st.tiles[k]; }
    });
    shown.forEach(function (id, idx) {
      var t = st.tiles[id];
      if (!t) { t = buildTile(id); st.tiles[id] = t; }
      t.dataset.idx = idx;
      // 顺序没变就不要重排：每次快照都 appendChild 会让 16 个 <video> 反复重排，看起来就是定期小卡顿
      if (grid.children[idx] !== t) grid.appendChild(t);
      var zn = st.solo ? 1 : Math.min(zoomOf(id), L.cols, L.rows);
      t.style.gridColumn = zn > 1 ? "span " + zn : "";
      t.style.gridRow = zn > 1 ? "span " + zn : "";
      t.classList.toggle("zoomed", zn > 1);
      var zb = $('[data-act="zoom"]', t);
      if (zb) {
        zb.classList.toggle("on", zn > 1);
        zb.title = zn > 1 ? ("取消重点观察（当前 " + zn + "×" + zn + "）") : "重点观察：把这一路放大到 2×2 / 3×3";
      }
      updateTileHead(t, id);
    });
    // 注意：这里不要批量重连降档——16 路同时重建会集体卡在「正在连接直播流」，
    // 默认画质在 buildTile/attachStream 里按当前路数决定，已建的格子保持用户/当时的档位。
    var hidden = ids.length - shown.length;
    var zN = 0, zS = 0;
    shown.forEach(function (id) {
      var z = st.solo ? 1 : Math.min(zoomOf(id), L.cols, L.rows);
      if (z > 1) { zN++; zS = z; }
    });
    $("#stage-meta").textContent = (st.solo ? "单路 · " : "") + ids.length + " 路 / " + L.cols + "×" + L.rows +
      (zN ? " · 重点观察 " + zN + " 格 " + zS + "×" + zS : "") +
      (hidden > 0 ? " · 还有 " + hidden + " 路未上屏（仍在收弹幕，切 AUTO 或更大排布查看）" : "");
    if (stageSync) stageSync.refresh();
  }

  /* ---- 直播流：优先本地 WebSocket-FLV，仅有 HLS 时从 CDN 播放 ---- */
  function destroyPlayer(stream) {
    clearInterval(stream.readyTimer);
    clearInterval(stream.nudgeTimer);
    clearTimeout(stream.startTimer);
    var p = stream.player;
    stream.player = null;
    if (!p) return;
    if (stream.kind === "flv") {
      try { p.pause(); } catch (e) {}
      try { p.unload(); } catch (e) {}
      try { p.detachMediaElement(); } catch (e) {}
    }
    try { p.destroy(); } catch (e) {}
  }

  function stopStream(id, video) {
    if (stageSync) stageSync.invalidate(id);
    delete st.stall[id];
    var stream = st.players[id];
    if (stream) {
      delete st.players[id];
      clearTimeout(stream.retryTimer);
      destroyPlayer(stream);
    }
    // 只 destroy 播放器还不够：<video> 上挂着的 MediaSource 要一起丢掉，否则 16 路会一直占着解码内存
    var v = video || (st.tiles[id] ? $("video", st.tiles[id]) : null);
    if (v) {
      try { v.pause(); } catch (e) {}
      try { v.removeAttribute("src"); } catch (e) {}
      v.dataset.ldseek = "0";
      v.dataset.ldstuck = "0";
      v.playbackRate = 1;
      try { v.load(); } catch (e) {}
    }
  }

  function markPlaying(d) {
    if (!d) return;
    if (d.dataset.retry) { d.dataset.retry = "0"; }
    d.dataset.failed = ""; d.dataset.offline = "";
    d.classList.remove("loading");
    var f = $(".fill", d); if (f) f.classList.add("hide");
  }

  // FLV 里的时间戳是 CDN 的绝对毫秒，起播时 currentTime=0 处根本没有数据，
  // 不自己跳到 buffered 末尾就会永远停在 readyState=2（缓冲一直涨、画面不动）。
  // 起播时 CDN 会一次灌一段积压（几秒），对齐到 live 边缘只做前几次，之后靠 watchdog 兜。
  function seekLive(video) {
    if (stageSync && stageSync.owns(video)) return;
    if (video.dataset.playbackKind !== "flv") return;
    var tries = parseInt(video.dataset.ldseek || "0", 10);
    if (tries >= 3) return;
    var b = video.buffered;
    if (!b || !b.length) return;
    var end = b.end(b.length - 1);
    if (!(end > 0)) return;
    if (video.currentTime === 0 || video.currentTime < end - 3) {
      try { video.currentTime = Math.max(0, end - latTarget()); } catch (e) {}
      window.__ldStats.seeks++;
    }
    if (video.currentTime > 0) video.dataset.ldseek = String(tries + 1);
  }

  // <video> 会跟着格子活很久：事件监听只绑一次，否则每次重连都叠一层，越用越慢
  // 卡住自愈：解封装/解码偶发停在 readyState<3（缓冲里明明有数据），
  // 主动 seek 回 live 边缘一次就能醒过来；连续两次检查都没恢复才动手，避免抖动时乱跳。
  window.__ldStats = window.__ldStats || { nudges: 0, seeks: 0, stalls: 0, lastStall: 0 };

  // 解码跟不上时（高码率 1080p60 常见）自动降一档画质，比反复 seek 更有效。
  // 同一次页面生命周期里每个房间只降一次，避免在 1080p/720p 之间来回跳。
  function degradeQuality(room) {
    if (st.degraded[room]) return;
    var list = st.qualities[room] || QUALITY_FALLBACK;
    var cur = st.quality[room] || defaultQn();
    var lower = list.filter(function (q) { return q.qn < cur; })
      .sort(function (a, b) { return b.qn - a.qn; })[0];
    if (!lower) return;
    st.degraded[room] = 1;
    st.quality[room] = lower.qn;
    st.qUser[room] = 1;
    saveQn(room, lower.qn);   // 下次打开这个房间直接用降下来的档位
    toast(roomName(room) + " 解码跟不上，已自动降到「" + lower.label + "」", "warn");
    var t = st.tiles[room];
    if (t) {
      var sel = $(".sel-q", t);
      if (sel) fillQuality(room, sel, lower.qn);
      var v = $("video", t);
      if (v) attachStream(room, t, v);
    }
  }

  function noteStall(room) {
    var now = Date.now();
    // 刚起播的 25 秒内不记：重建播放器的瞬间本来就会 readyState 掉一下
    var t = st.tiles[room];
    if (t && t.dataset.attachedAt && now - parseInt(t.dataset.attachedAt, 10) < 25000) return;
    var hist = (st.stallHist[room] || []).filter(function (ts) { return now - ts < 120000; });
    hist.push(now);
    st.stallHist[room] = hist;
    if (hist.length >= 4) degradeQuality(room);
  }
  function nudgeIfStuck(d, video) {
    if (stageSync && stageSync.owns(video)) return;
    if (!video.getAttribute("src") && !video.currentSrc) return;
    if (video.paused || d.dataset.paused === "1") { video.dataset.ldstuck = "0"; return; }
    if (video.readyState >= 3) { video.dataset.ldstuck = "0"; return; }
    if (video.dataset.ldstuck === "0" || !video.dataset.ldstuck) {
      window.__ldStats.stalls++;
      window.__ldStats.lastStall = +video.currentTime.toFixed(2);
    }
    var b = video.buffered;
    var end = b && b.length ? b.end(b.length - 1) : 0;
    if (!(end - video.currentTime > 1.0)) { video.dataset.ldstuck = "0"; return; }
    var n = parseInt(video.dataset.ldstuck || "0", 10) + 1;
    video.dataset.ldstuck = String(n);
    if (n < 2) return;
    video.dataset.ldstuck = "0";
    try { video.currentTime = Math.max(0, end - latTarget()); } catch (e) {}
    window.__ldStats.nudges++;
    if (end - video.currentTime >= (st.mode === "smooth" ? 2.0 : 1.0)) noteStall(d.dataset.room || "");
    if (window.__ldDbg) console.log("[livedeck] nudge", video.dataset.ldseek, end.toFixed(1));
  }

  function bindVideo(d, video) {
    if (video.dataset.ldb) return;
    video.dataset.ldb = "1";
    video.addEventListener("playing", function () { markPlaying(d); });
    video.addEventListener("loadedmetadata", function () { seekLive(video); });
    video.addEventListener("canplay", function () { seekLive(video); });
    video.addEventListener("progress", function () { seekLive(video); });
    video.addEventListener("loadeddata", function () {
      seekLive(video);
      if (!video.paused && video.readyState >= 2) markPlaying(d);
    });
  }

  function attachStream(id, d, video, force) {
    stopStream(id, video);
    var fill = $(".fill", d), small = $(".fill .small", d);
    var inf = (st.info && st.info[String(id)]) || {};
    if (inf.known && inf.live_status === 0) {
      d.classList.remove("loading");
      d.dataset.offline = "1";
      if (fill) fill.classList.remove("hide");
      if (small) small.textContent = "未开播 · 开播后自动接入（也可点 ↗ 用浏览器打开）";
      return;
    }
    d.dataset.offline = "";
    d.classList.add("loading");
    if (fill) fill.classList.remove("hide");
    if (small) small.textContent = "正在连接直播流…";
    if (video.dataset.paused === "1") return;
    var stream = { player: null, failed: false };
    st.players[id] = stream;
    function current() { return st.players[id] === stream; }
    function fail(msg, retry) {
      if (!current() || stream.failed) return;
      stream.failed = true;
      if (stageSync) stageSync.invalidate(id);
      destroyPlayer(stream);
      d.classList.remove("loading");
      if (fill) fill.classList.remove("hide");
      var n = parseInt(d.dataset.retry || "0", 10);
      var live = !inf.known || inf.live_status === 1;
      if (retry !== false && live && n < 5) {
        d.dataset.retry = String(n + 1);
        if (small) small.textContent = msg + "（" + (n + 1) + "/5，稍后自动重试）";
        stream.retryTimer = setTimeout(function () {
          if (current() && st.tiles[id] === d) attachStream(id, d, video, true);
        }, 6000);
      } else {
        d.dataset.failed = "1";
        if (small) small.textContent = msg;
      }
    }
    var qn = window.__ldQn || st.quality[id] || savedQn(id) || defaultQn();
    api("/api/playback?room=" + id + "&qn=" + qn + (force ? "&force=1" : "")).then(function (source) {
      if (!current()) return;
      if (source.list && source.list.length) {
        st.qualities[id] = source.list;
        fillQuality(id, $(".sel-q", d), source.current || qn);
      }
      stream.kind = source.kind;
      video.dataset.playbackKind = source.kind;
      bindVideo(d, video);
      applyVolume(id, video);
      if (source.kind === "hls") {
        if (!(window.Hls && Hls.isSupported())) {
          fail("这个浏览器不支持 HLS 播放，请点 ↗ 用浏览器打开", false);
          return;
        }
        startHlsPlayer(source, stream, video, fail);
      } else {
        if (!(window.mpegts && mpegts.isSupported())) {
          fail("这个浏览器不支持 MSE 播放，请点 ↗ 用浏览器打开", false);
          return;
        }
        startFlvPlayer(id, qn, stream, video, fail);
      }
      if (stream.failed) return;
      if (stream.kind === "flv") {
        stream.nudgeTimer = setInterval(function () { nudgeIfStuck(d, video); }, 1500);
      }
      d.dataset.smoothAt = String(Date.now());
      d.dataset.attachedAt = String(Date.now());
      // 起播时用短轮询兜住 playing 事件，停止或超时后释放计时器。
      stream.readyTimer = setInterval(function () {
        if (video.readyState >= 2 && !video.paused && !video.ended) {
          markPlaying(d);
          clearInterval(stream.readyTimer);
        }
      }, 400);
      stream.startTimer = setTimeout(function () {
        clearInterval(stream.readyTimer);
        if (video.readyState < 2) fail("画面还没出来 · 点 ↻ 重试，或 ↗ 用浏览器打开");
      }, stream.kind === "hls" ? 25000 : 15000);
    }).catch(function (err) {
      fail(err.message + " · 点 ↻ 重试，或 ↗ 用浏览器打开");
    });
  }

  function playVideo(video) {
    try { var pr = video.play(); if (pr && pr.catch) pr.catch(function () {}); } catch (e) {}
  }

  function startHlsPlayer(source, stream, video, fail) {
    function checkCdn(raw) {
      var url = new URL(raw);
      var host = url.hostname.toLowerCase().replace(/\.$/, "");
      var allowed = source.cdn_suffixes.some(function (suffix) {
        return host === suffix.slice(1) || host.slice(-suffix.length) === suffix;
      });
      if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password || !allowed) {
        throw new Error("HLS 请求地址不在直播 CDN 白名单内");
      }
    }
    var p = new Hls({
      backBufferLength: 30,
      liveSyncDurationCount: st.mode === "smooth" ? 3 : 2,
      liveMaxLatencyDurationCount: 6,
      progressive: true,
      xhrSetup: function (xhr, url) { checkCdn(url); },
      fetchSetup: function (context, init) {
        return Promise.resolve().then(function () {
          checkCdn(context.url);
          init.credentials = "omit";
          init.redirect = "error";
          return new Request(context.url, init);
        });
      }
    });
    stream.player = p;
    p.on(Hls.Events.ERROR, function (event, data) {
      if (data.fatal) fail("HLS 画面加载失败 · 点 ↻ 重试，或 ↗ 用浏览器打开");
    });
    p.on(Hls.Events.MANIFEST_PARSED, function () {
      if (stream.player === p) playVideo(video);
    });
    p.loadSource(source.url);
    p.attachMedia(video);
  }

  function startFlvPlayer(id, qn, stream, video, fail) {
    // FLV 走 WebSocket，避开同源 HTTP 连接数上限（16 路视频会挤死 SSE）。
    var url = "/api/flv?room=" + id + "&qn=" + qn + "&_=" + Date.now();
    if (st.streamPort) {
      var wsp = location.protocol === "https:" ? "wss:" : "ws:";
      var wh = location.hostname || "127.0.0.1";
      if (wh === "localhost") wh = "127.0.0.1";
      url = wsp + "//" + wh + ":" + st.streamPort + "/flv?room=" + id + "&qn=" + qn;
    }
    var popts = {
      enableStashBuffer: true, stashInitialSize: st.mode === "smooth" ? 2048 : 1024, lazyLoad: false,
      // smooth：不裁 SourceBuffer（裁剪时会把播放头附近的数据一起丢掉，得等下一个关键帧）。
      // 代价是内存随播放时长增长，所以 smooth 模式每 20 分钟软重连一次回收。
      autoCleanupSourceBuffer: st.mode !== "smooth",
      autoCleanupMaxBackwardDuration: 60, autoCleanupMinBackwardDuration: 30,
      // 不靠 mpegts 的追帧（它拿 buffered 的绝对时间戳跟阈值比，直播流时间戳很大时会频繁 seek 卡顿），
      // 改成：起播自己跳到 live 边缘 + liveSync 微调播放速度，避免跳帧式追帧。
      liveBufferLatencyChasing: false, liveBufferLatencyChasingOnPaused: false
    };
    if (window.__ldOpts) { for (var ok in window.__ldOpts) popts[ok] = window.__ldOpts[ok]; }
    var p = mpegts.createPlayer({ type: "flv", isLive: true, url: url }, popts);
    stream.player = p;
    p.on(mpegts.Events.ERROR, function (type, detail) {
      fail("画面加载失败（" + esc(String(detail || type)) + "）· 点 ↻ 重试，或 ↗ 用浏览器打开");
    });
    p.attachMediaElement(video);
    p.load();
    if (stream.player === p) playVideo(video);
  }

  function applyVolume(id, video) {
    var v = st.volume[id];
    if (v === undefined) { video.muted = true; return; }
    video.volume = Math.max(0, Math.min(1, v / 100));
    video.muted = v === 0;
  }

  function buildTile(id) {
    var d = document.createElement("div");
    d.className = "tile loading";
    d.dataset.room = id;
    d.innerHTML = '<video muted autoplay playsinline preload="none"></video>' +
      '<div class="thead">' +
      '<span class="dot off"></span><span class="nm">' + esc(roomName(id)) + "</span>" +
      '<span class="sync-badge hide">联动</span>' +
      '<span class="lb idle">…</span><span class="gd hide"></span><span class="cnt">—</span>' +
      '<span class="acts"><button title="重点观察：把这一路放大到 2×2 / 3×3" data-act="zoom">▣</button>' +
      '<button title="只看这一路" data-act="solo">⤢</button>' +
      '<button title="声音开/关" data-act="mute">🔇</button>' +
      '<button title="在浏览器新标签打开" data-act="open">↗</button>' +
      '<button title="重载画面" data-act="reload">↻</button>' +
      '<button title="移出舞台" data-act="close">×</button></span></div>' +
      '<div class="tctl"><select class="sel-q" title="这一路的画质"></select>' +
      '<input class="vol" type="range" min="0" max="100" step="5" value="100" title="这一路的音量">' +
      '<b class="vnum">100%</b></div>' +
      '<div class="fill"><div><div class="spin"></div><div class="small">正在连接直播流…</div></div></div>';
    var video = $("video", d);
    video.setAttribute("draggable", "false");
    // 拖动格子标题栏可以把这一路挪到别的位置（顺序即舞台排布，会存到服务端）
    var thead = $(".thead", d);
    thead.setAttribute("draggable", "true");
    thead.title = "按住标题栏拖动，可调整这一路在舞台上的位置";
    thead.addEventListener("dragstart", function (e) {
      if (st.solo) { e.preventDefault(); return; }
      st.dragId = id;
      d.classList.add("dragging");
      try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(id)); } catch (err) {}
    });
    thead.addEventListener("dragend", function () {
      st.dragId = 0;
      d.classList.remove("dragging");
      $$(".tile").forEach(function (x) { x.classList.remove("drop-target"); });
    });
    d.addEventListener("dragover", function (e) {
      if (!st.dragId || st.dragId === id) return;
      e.preventDefault();
      d.classList.add("drop-target");
    });
    d.addEventListener("dragleave", function () { d.classList.remove("drop-target"); });
    d.addEventListener("drop", function (e) {
      e.preventDefault();
      d.classList.remove("drop-target");
      var from = st.dragId || parseInt(e.dataTransfer.getData("text/plain"), 10) || 0;
      st.dragId = 0;
      if (!from || from === id) return;
      reorderTiles(from, id);
    });
    d.addEventListener("dblclick", function () {
      st.solo = st.solo ? 0 : id; renderGrid();
    });
    $$("[data-act]", d).forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var act = btn.dataset.act;
        if (act === "close") toggleRoom(id);
        else if (act === "zoom") cycleZoom(id);
        else if (act === "solo") { st.solo = id; renderGrid(); }
        else if (act === "open") window.open("https://live.bilibili.com/" + id, "_blank", "noopener");
        else if (act === "mute") {
          video.muted = !video.muted;
          btn.textContent = video.muted ? "🔇" : "🔊";
          if (video.muted) {
            st.volume[id] = 0; vol.value = "0"; vnum.textContent = "0%";
          } else {
            var cur = st.volume[id] || 80;
            st.volume[id] = cur; vol.value = String(cur); vnum.textContent = cur + "%";
            video.volume = cur / 100;
          }
        } else if (act === "reload") {
          d.dataset.retry = ""; d.dataset.failed = "";
          attachStream(id, d, video, true);
        }
      });
    });
    // 画质 / 音量：只作用于这一路
    var selQ = $(".sel-q", d), vol = $(".vol", d), vnum = $(".vnum", d);
    fillQuality(id, selQ, st.quality[id] || savedQn(id) || defaultQn());
    if (st.volume[id] !== undefined) { vol.value = String(st.volume[id]); vnum.textContent = st.volume[id] + "%"; }
    selQ.addEventListener("click", function (ev) { ev.stopPropagation(); });
    selQ.addEventListener("change", function () {
      st.qUser[id] = 1;   // 用户手动选过的画质，不再被「多路自动降档」改回去
      st.quality[id] = parseInt(selQ.value, 10) || 10000;
      delete st.degraded[id];       // 手动选过就重新给一次自动降档的机会
      st.stallHist[id] = [];
      saveQn(id, st.quality[id]);
      d.dataset.retry = ""; d.dataset.failed = "";
      attachStream(id, d, video);
      toast(roomName(id) + " 画质：" + selQ.options[selQ.selectedIndex].text, "ok");
    });
    vol.addEventListener("click", function (ev) { ev.stopPropagation(); });
    vol.addEventListener("input", function () {
      var v = parseInt(vol.value, 10);
      st.volume[id] = v;
      video.volume = v / 100;
      video.muted = v === 0;
      vnum.textContent = v + "%";
      var mb = $('[data-act="mute"]', d);
      if (mb) mb.textContent = v === 0 ? "🔇" : "🔊";
    });
    var tctl = $(".tctl", d);
    if (tctl) tctl.addEventListener("dblclick", function (ev) { ev.stopPropagation(); });
    attachStream(id, d, video);
    $("#grid").appendChild(d);
    return d;
  }

  function syncStreams() {
    Object.keys(st.tiles).forEach(function (k) {
      var id = parseInt(k, 10), t = st.tiles[k];
      var inf = (st.info && st.info[String(id)]) || {};
      if (!inf.known) return;
      if (inf.live_status === 1 && (t.dataset.offline === "1" || t.dataset.failed === "1")) {
        t.dataset.offline = ""; t.dataset.failed = "";
        attachStream(id, t, $("video", t));
      }
    });
  }

  // 弹幕通道状态给人看的中文名（内部 state 是 open / auth / closed 这些）
  function bridgeLabel(s) {
    return { open: "已连接", auth: "已认证", connecting: "连接中", closed: "已断开",
      denied: "被拒绝", error: "异常", online: "已连接" }[s] || "未连接";
  }

  function updateTileHead(t, id) {
    var b = (st.snap && st.snap.bridge && st.snap.bridge.rooms || {})[String(id)] || {};
    var dot = $(".thead .dot", t);
    var lb = $(".thead .lb", t);
    var nm = $(".thead .nm", t);
    nm.textContent = roomName(id);
    nm.title = roomName(id);   // 格子窄时用省略号，悬浮看全名
    var finf = (st.info && st.info[String(id)]) || {};
    // 与顶栏「在播 N」、左栏徽标同一口径，见 liveStatusOf()
    var ls = liveStatusOf(id).status;
    lb.className = "lb " + (ls === 1 ? "on" : "idle");
    lb.textContent = ls === 1 ? "LIVE" : ls === 2 ? "轮播" : ls === 0 ? "OFF" : "待连接";
    lb.title = ls === 0 ? "未开播" : ls === -1 ? "还没拿到房间开播状态（房间信息每 20 秒刷一次）" : "";
    var stateOk = b.state === "open" || b.state === "auth";
    dot.className = "dot " + (stateOk ? "on" : b.state === "denied" || b.state === "error" || b.state === "closed" ? "off" : "warn");
    dot.title = "弹幕通道：" + bridgeLabel(b.state) + (b.detail ? " · " + b.detail : "");
    // 这里显示的是直播间当前人气（在线人数），不是弹幕条数；弹幕条数在左栏名单里
    var cnt = $(".thead .cnt", t);
    if (cnt) {
      if (ls === 1) {
        // 这个数字是「房间观众」：B 站房间页右侧那个 房间观众(N)，
        // 来源是弹幕流里的 ONLINE_RANK_COUNT.online_count（在线榜人数），不是人气也不是「看过」。
        var viewers = b.viewers || 0;
        cnt.textContent = viewers > 0 ? fmtOnline(viewers) : "—";
        cnt.title = viewers > 0 ? "当前房间观众（与 B 站「房间观众(N)」同源）"
                                : "等待在线榜数据，弹幕通道连通后会自动出现";
      } else {
        cnt.textContent = "—";
        cnt.title = ls === 0 ? "未开播" : "还没拿到房间开播状态";
      }
    }
    // 大航海（舰长/提督/总督）总人数：来自 HTTP 的 getInfoByRoom.guard_info.count，
    // 弹幕流里只有「谁上舰了」，没有总数，所以这个数字不会实时跳，跟着房间信息每 20 秒刷。
    var gd = $(".thead .gd", t);
    if (gd) {
      var guards = ls === 1 ? (b.guards || finf.guards || 0) : 0;
      if (guards > 0) {
        gd.textContent = "舰 " + guards;
        gd.classList.remove("hide");
        gd.title = "大航海（舰长/提督/总督）共 " + guards + " 人，与 B 站房间页「大航海(N)」同源";
      } else {
        gd.textContent = "";
        gd.classList.add("hide");
      }
    }
    t.classList.toggle("focus", st.solo === id);
    // 整个格子不要挂 title：鼠标扫过画面会弹「auth」这类内部状态，
    // 弹幕通道状态看左上角圆点的 title 就够了。
    if (t.title) t.removeAttribute("title");
  }

  /* ---------------- 弹幕流 ---------------- */
  function comboById(id) {
    for (var i = 0; i < st.combos.length; i++) {
      if (st.combos[i].id === id) return st.combos[i];
    }
    return null;
  }

  function comboLive(c) {
    // 「在场」按舞台 + 只听弹幕算：只听的房间同样在收弹幕，不能被当成缺失
    var watched = watchIds();
    return (c.rooms || []).filter(function (r) { return watched.indexOf(r) >= 0; }).length;
  }

  function mainChatSource() {
    return st.filterCombo ? "c:" + st.filterCombo : String(st.filterRoom);
  }

  function renderChatFilter() {
    var sel = $("#chat-room");
    var cur = mainChatSource();
    var watch = watchIds();
    var html = '<option value="0">合并 · 全部 ' + watch.length + " 路</option>";
    if (st.combos.length) {
      html += '<optgroup label="临时组合">' + st.combos.map(function (c) {
        return '<option value="c:' + c.id + '">' + esc(c.name) + " · " + comboLive(c) + "/" + c.rooms.length + " 路</option>";
      }).join("") + "</optgroup>";
    }
    if (st.active.length) {
      html += '<optgroup label="单个直播间">' + st.active.map(function (r) {
        return '<option value="' + r + '">' + esc(roomName(r)) + "</option>";
      }).join("") + "</optgroup>";
    }
    if (st.listening.length) {
      html += '<optgroup label="只听弹幕">' + st.listening.map(function (r) {
        return '<option value="' + r + '">' + esc(roomName(r)) + "</option>";
      }).join("") + "</optgroup>";
    }
    sel.innerHTML = html;
    sel.value = cur;
    if (sel.value !== cur) { st.filterCombo = 0; st.filterRoom = 0; sel.value = "0"; }
    if (st.chatSplit) {
      var sub = $("#chat-sub-room");
      sub.innerHTML = html;
      sub.value = st.subSource;
      if (!sub.value) {
        var room = watch.filter(function (r) { return String(r) !== sel.value; })[0];
        sub.value = String(room || watch[0] || 0);
      }
      // 没有房间时保留原选择，等下一次收到名单再判断。
      if (watch.length || sub.value !== "0") st.subSource = sub.value;
    }
    var c2 = $("#send-to");
    var keep = String(st.sendTo || st.active[0] || 0);
    c2.innerHTML = st.active.map(function (r) {
      return '<option value="' + r + '">' + esc(roomName(r)) + "</option>";
    }).join("");
    if (!st.active.length) c2.innerHTML = '<option value="0">未选择直播间</option>';
    c2.value = keep;
    st.sendTo = parseInt(c2.value || "0", 10) || st.active[0] || 0;
    c2.value = String(st.sendTo);
    renderChatHead();
  }

  function renderChatHead() {
    var n = st.msgs.filter(function (m) { return afterClear(m, st.mainPane); }).length;
    $("#chat-count").textContent = n + " 条";
    var cb = st.filterCombo ? comboById(st.filterCombo) : null;
    $("#chat-mode-label").textContent = cb ? (cb.name + " · " + comboLive(cb) + " 路")
      : st.filterRoom ? roomName(st.filterRoom) : ("全部 " + watchIds().length + " 路");
  }

  /* ---------------- 临时组合 ----------------
     把舞台或只听弹幕里的若干房间并成一条弹幕流，存在服务端 state 里，重启还在。 */
  function comboPanelOpen() {
    return !$("#combo-panel").classList.contains("hide");
  }
  function closeCombo() {
    $("#combo-panel").classList.add("hide");
  }

  function renderComboPanel() {
    var list = $("#combo-list");
    if (!st.combos.length) {
      list.innerHTML = '<div class="cp-empty">还没有组合。下面勾选舞台或只听弹幕里的房间，保存即可。</div>';
    } else {
      list.innerHTML = st.combos.map(function (c) {
        var names = c.rooms.map(roomName).join("、");
        return '<div class="cp-row" data-id="' + c.id + '">' +
          '<div class="cp-nm">' + esc(c.name) + "<i>" + comboLive(c) + "/" + c.rooms.length + " 路</i>" +
          '<span title="' + esc(names) + '">' + esc(names) + "</span></div>" +
          '<button class="mini" data-act="use">切换</button>' +
          '<button class="mini" data-act="del">删除</button></div>';
      }).join("");
    }
    var box = $("#combo-rooms");
    function pick(r) {
      return '<label><input type="checkbox" value="' + r + '"><span>' + esc(roomName(r)) + "</span></label>";
    }
    var parts = [];
    if (st.active.length) parts.push('<div class="cp-grp">在舞台</div>' + st.active.map(pick).join(""));
    if (st.listening.length) parts.push('<div class="cp-grp listen">只听弹幕</div>' + st.listening.map(pick).join(""));
    box.innerHTML = parts.join("") || '<div class="cp-empty">还没有在舞台或只听弹幕的房间，先从左栏加几路。</div>';
  }

  function openCombo() {
    $("#combo-name").value = "";
    renderComboPanel();
    $("#combo-panel").classList.remove("hide");
    $("#combo-name").focus();
  }

  function saveCombo() {
    var rooms = $$("#combo-rooms input:checked").map(function (i) { return parseInt(i.value, 10); })
      .filter(function (r) { return r > 0; });
    if (rooms.length < 2) return toast("组合至少选 2 路（舞台或只听弹幕都可以）", "err");
    var items = st.combos.map(function (c) { return { id: c.id, name: c.name, rooms: c.rooms }; });
    items.push({ name: $("#combo-name").value.trim(), rooms: rooms });
    api("/api/combos", { combos: items }).then(function (j) {
      st.combos = j.combos || [];
      var last = st.combos[st.combos.length - 1];
      if (last) { st.filterCombo = last.id; st.filterRoom = 0; }
      renderChatFilter(); rebuildFeed(); renderChatHead(); renderComboPanel();
      closeCombo();
      toast("已保存组合「" + (last ? last.name : "") + "」，切到它就只看这几个房间");
    }).catch(function (e) { toast(e.message || "保存组合失败", "err"); });
  }

  function deleteCombo(id) {
    var items = st.combos.filter(function (c) { return c.id !== id; })
      .map(function (c) { return { id: c.id, name: c.name, rooms: c.rooms }; });
    api("/api/combos", { combos: items }).then(function (j) {
      st.combos = j.combos || [];
      if (st.filterCombo === id) { st.filterCombo = 0; st.filterRoom = 0; }
      renderChatFilter(); rebuildFeed(); renderChatHead(); renderComboPanel();
      toast("已删除组合");
    }).catch(function (e) { toast(e.message || "删除组合失败", "err"); });
  }

  function afterClear(m, pane) {
    // 初始历史视为第 0 批。清屏后即使历史请求稍晚完成，也不会把它补回这一栏。
    return (st.msgArrival.get(m) || 0) > pane.clearedThrough;
  }

  function feedFilter(source, pane) {
    var combo = source.indexOf("c:") === 0 ? comboById(parseInt(source.slice(2), 10)) : null;
    var rooms = combo ? combo.rooms : null;
    var room = Number(source) || 0;
    return function (m) {
      if (!afterClear(m, pane) || !watching(m.room)) return false;
      if (rooms ? rooms.indexOf(m.room) < 0 : room && m.room !== room) return false;
      return passKind(m, pane.kind);
    };
  }

  function passKind(m, kind) {
    if (kind === "danmaku") return m.kind === "danmaku" || m.kind === "sys";
    if (kind === "gift") return m.kind === "gift" || m.kind === "guard";
    if (kind === "sc") return m.kind === "sc";
    return true;
  }

  /* ---------------- 房间印象色 ---------------- */
  // 种子只提供色相：亮度/饱和度在这里钳制，保证标签字在深色底上始终可读
  function hexRgb(hex) {
    var s = String(hex == null ? "" : hex).trim().replace("#", "");
    if (s.length === 3) s = s.charAt(0) + s.charAt(0) + s.charAt(1) + s.charAt(1) + s.charAt(2) + s.charAt(2);
    if (!/^[0-9a-f]{6}$/i.test(s)) return null;
    var n = parseInt(s, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function rgbHex(r, g, b) {
    var f = function (v) {
      v = Math.max(0, Math.min(255, Math.round(v || 0)));
      return (v < 16 ? "0" : "") + v.toString(16);
    };
    return "#" + f(r) + f(g) + f(b);
  }
  function rgbHsl(c) {
    var r = c.r / 255, g = c.g / 255, b = c.b / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, h = 0, s = 0, d = mx - mn;
    if (d > 0) {
      s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
      if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h = Math.round(h * 60) % 360;
    }
    return { h: h, s: s, l: l };
  }
  function colorSeed(hex) {
    var c = hexRgb(hex);
    if (!c) return null;
    var hsl = rgbHsl(c);
    return { hex: rgbHex(c.r, c.g, c.b), h: hsl.h, s: hsl.s, l: hsl.l, chroma: hsl.s >= 0.18 };
  }
  function hueGap(a, b) {
    var d = Math.abs(a - b) % 360;
    return d > 180 ? 360 - d : d;
  }
  function tagStyleOf(c, id) {
    var h = hue(id);
    var plain = "background:hsla(" + h + ",68%,52%,.16);color:hsl(" + h + ",82%,74%);border:1px solid hsla(" + h + ",68%,58%,.34)";
    if (!c || !c.on || !c.main) return plain;
    var m = colorSeed(c.main), s = c.second ? colorSeed(c.second) : null;
    if (!m) return plain;
    // 两色都彩色且色相拉得开：双色渐变底；否则视为不可合理兼容，只按主色
    if (m.chroma && s && s.chroma && hueGap(m.h, s.h) >= 24) {
      return "background:linear-gradient(90deg,hsla(" + m.h + ",68%,52%,.30),hsla(" + s.h + ",68%,52%,.30));color:hsl(" + m.h + ",82%,74%);border:1px solid hsla(" + m.h + ",68%,58%,.40)";
    }
    // 黑白/深灰+浅灰这类无彩色组合（熊猫色）：中性双色底
    if (!m.chroma && s && !s.chroma && Math.abs(m.l - s.l) >= 0.35) {
      return "background:linear-gradient(90deg,rgba(0,0,0,.34),rgba(255,255,255,.22));color:#e8eef8;border:1px solid rgba(255,255,255,.26)";
    }
    if (!m.chroma) return "background:hsla(0,0%,100%,.09);color:#e8eef8;border:1px solid hsla(0,0%,100%,.22)";
    return "background:hsla(" + m.h + ",68%,52%,.17);color:hsl(" + m.h + ",82%,74%);border:1px solid hsla(" + m.h + ",68%,58%,.36)";
  }
  function tagStyle(id) { return tagStyleOf(st.colors[String(id)], id); }

  function tagFor(id) {
    return '<span class="tag" data-room="' + id + '" style="' + tagStyle(id) + '" title="' +
      esc(roomName(id)) + '">' + esc(roomName(id)) + "</span>";
  }

  // 改色后把已经上屏的该房间标签一起重刷，不用清屏
  function repaintTags(id) {
    var style = tagStyle(id);
    $$('.m .tag[data-room="' + id + '"]').forEach(function (el) { el.setAttribute("style", style); });
  }

  /* ---------------- 印象色弹窗 ---------------- */
  function colFill(kind, hex) {
    var rgb = hexRgb(hex);
    var val = rgb ? rgbHex(rgb.r, rgb.g, rgb.b) : "";
    $("#col-" + kind + "-hex").value = val;
    $("#col-" + kind + "-pick").value = val || (kind === "main" ? "#fb7299" : "#2ee6d6");
    ["r", "g", "b"].forEach(function (ch) {
      $("#col-" + kind + "-" + ch).value = rgb ? String(rgb[ch]) : "";
    });
  }
  function colHex(kind) {
    var rgb = hexRgb($("#col-" + kind + "-hex").value);
    return rgb ? rgbHex(rgb.r, rgb.g, rgb.b) : "";
  }
  function colFromHex(kind) {
    var rgb = hexRgb($("#col-" + kind + "-hex").value);
    if (!rgb) return;
    $("#col-" + kind + "-pick").value = rgbHex(rgb.r, rgb.g, rgb.b);
    ["r", "g", "b"].forEach(function (ch) { $("#col-" + kind + "-" + ch).value = String(rgb[ch]); });
  }
  function colFromRgb(kind) {
    var v = ["r", "g", "b"].map(function (ch) { return $("#col-" + kind + "-" + ch).value; });
    if (v.some(function (x) { return x === ""; })) return;
    var rgb = { r: parseInt(v[0], 10), g: parseInt(v[1], 10), b: parseInt(v[2], 10) };
    if (isNaN(rgb.r) || isNaN(rgb.g) || isNaN(rgb.b)) return;
    var hex = rgbHex(rgb.r, rgb.g, rgb.b);
    $("#col-" + kind + "-hex").value = hex;
    $("#col-" + kind + "-pick").value = hex;
  }
  function updateColPrev() {
    var c = { main: colHex("main"), second: colHex("sec"), on: $("#col-on").checked };
    var el = $("#col-prev-tag");
    el.setAttribute("style", tagStyleOf(c, st.colRoom));
    el.textContent = roomName(st.colRoom);
  }
  function openColor(id) {
    st.colRoom = id;
    var c = st.colors[String(id)] || {};
    $("#col-room").textContent = roomName(id) + " · 房间 " + id;
    $("#col-on").checked = c.main ? c.on !== false : true;
    colFill("main", c.main || "");
    colFill("sec", c.second || "");
    $("#col-clear").classList.toggle("hide", !c.main);
    updateColPrev();
    $("#col-mask").classList.remove("hide");
  }
  function closeColor() { $("#col-mask").classList.add("hide"); }
  function saveColor() {
    var on = $("#col-on").checked;
    var main = colHex("main"), second = colHex("sec");
    if (on && !main) { toast("开启印象色需要先选主色（色盘 / 吸色 / 填 RGB 都行）", "err"); return; }
    api("/api/room/color", { room: st.colRoom, main: main, second: second, on: on }).then(function (j) {
      if (j.color && j.color.main) st.colors[String(st.colRoom)] = j.color;
      else delete st.colors[String(st.colRoom)];
      repaintTags(st.colRoom);
      closeColor();
      toast(j.color && j.color.on ? "印象色已保存，弹幕标签按主色显示" : "印象色已关闭，标签回到自动配色", "ok");
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function clearColor() {
    api("/api/room/color", { room: st.colRoom, clear: true }).then(function () {
      delete st.colors[String(st.colRoom)];
      repaintTags(st.colRoom);
      closeColor();
      toast("已清除该房间的印象色", "ok");
    }).catch(function (e) { toast(e.message, "err"); });
  }
  // 吸色器取的是屏幕上真实渲染的像素，弹窗遮罩的 backdrop-filter 会把底下的直播间画面
  // 糊成一片（还压暗 72%），取到的只能是模糊色。所以吸色期间临时摘掉遮罩的模糊与压暗，
  // 并藏起弹窗本身，让用户对着清晰的画面取色，取完 / 取消后原样恢复。
  function pickingUI(on) {
    if (on) { document.body.classList.add("picking"); }
    else { document.body.classList.remove("picking"); }
  }
  function pickScreen(kind) {
    if (!window.EyeDropper) { toast("当前浏览器不支持屏幕吸色，请改用色盘或直接填 RGB", "err"); return; }
    var ed;
    try { ed = new window.EyeDropper(); }
    catch (e) { toast("吸色失败：" + e.message, "err"); return; }
    pickingUI(true);
    // 先等两帧，让「去模糊」落到屏幕上再开吸色器，否则第一眼还是旧的模糊画面
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        ed.open().then(function (r) {
          pickingUI(false);
          colFill(kind, r.sRGBHex);
          updateColPrev();
        }).catch(function () { pickingUI(false); });
      });
    });
  }


  function msgNode(m) {
    if (m.kind === "sys") {
      return '<div class="m sys">' + tagFor(m.room) + '<span class="body">' + esc(m.text) + "</span></div>";
    }
    if (m.kind === "sc") {
      return '<div class="m sc">' + tagFor(m.room) +
        '<span class="body"><span class="price">¥' + esc(m.price || "") + "</span> " +
        '<span class="u">' + esc(m.uname) + "</span> " + '<span class="x">' + esc(m.text) + "</span></span></div>";
    }
    var body, rowCls = m.kind || "danmaku";
    if (m.kind === "gift") {
      body = '<span class="u">' + esc(m.uname) + '</span><span class="x">送出 ' + esc(m.text) +
        (m.num > 1 ? " ×" + m.num : "") + "</span>";
    } else if (m.kind === "guard") {
      // 大航海三档：1=总督 2=提督 3=舰长（hub.py GUARD_NAMES），整行按等级染色，见 style.css .m.guard.g1~g3；
      // 行首徽标用 B 站大航海的锚形图标（img/guard1~3.png，1 红金 / 2 紫 / 3 蓝）
      var lv = parseInt(m.guard_level || 3, 10);
      if (lv !== 1 && lv !== 2) lv = 3;
      var gname = m.text || (lv === 1 ? "总督" : lv === 2 ? "提督" : "舰长");
      rowCls += " g" + lv;
      body = '<img class="gl" src="img/guard' + lv + '.png?v=20261109i" alt="' + esc(gname) + '" title="' + esc(gname) + '">' +
        '<span class="u">' + esc(m.uname) + '</span>' +
        '<span class="x">开通了' + esc(gname) + (m.num > 1 ? " ×" + m.num + " 个月" : "") + "</span>";
    } else {
      // 服务端把 Msg.extra 的键摊平进消息本身（self / emoticon / guard_level 都是顶层字段）
      var self = m.self;
      var emoUrl = m.emoticon && m.emoticon.url;
      body = '<span class="u' + (self ? " self" : "") + '">' + esc(m.uname) + "</span>" +
        (emoUrl
          ? '<img class="emo" src="' + esc(emoUrl) + '" alt="' + esc(m.text) + '" title="' + esc(m.text) +
            '" referrerpolicy="no-referrer" loading="lazy">'
          : '<span class="x">' + esc(m.text) + "</span>");
    }
    return '<div class="m ' + rowCls + (m.self ? " self" : "") + '" data-seq="' + m.seq + '">' +
      '<span class="t">' + clock(m.ts) + "</span>" + tagFor(m.room) + '<span class="body">' + body + "</span></div>";
  }

  // 弹幕再密也只在 60ms 内合并一次 DOM 写入：逐条 insert 会让主线程频繁重排，多路视频时特别明显
  function queueMsg(m) {
    st.q.push(m);
    if (!st.qTimer) st.qTimer = setTimeout(flushFeed, 60);
  }

  function flushFeed() {
    if (st.qTimer) clearTimeout(st.qTimer);
    st.qTimer = null;
    var batch = st.q.splice(0, st.q.length);
    if (!batch.length) return;
    appendFeed($("#feed"), batch, mainChatSource(), st.mainPane);
    if (st.chatSplit) appendFeed($("#feed-sub"), batch, st.subSource, st.subPane);
    renderChatHead();
    paintRate();
    $("#m-total").textContent = st.received;
  }

  function appendFeed(feed, batch, source, pane) {
    var shown = batch.filter(feedFilter(source, pane));
    if (!shown.length) return;
    var empty = $(".chat-empty", feed);
    if (empty) empty.remove();
    var near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 160 || !feed.scrollTop;
    feed.insertAdjacentHTML("beforeend", shown.map(msgNode).join(""));
    while (feed.children.length > 320) feed.removeChild(feed.firstChild);
    if (near || shown.some(function (m) { return m.self; })) feed.scrollTop = feed.scrollHeight;
  }

  function rebuildFeedPane(feed, source, pane) {
    var list = st.msgs.filter(feedFilter(source, pane));
    feed.innerHTML = list.length ? list.slice(-260).map(msgNode).join("")
      : '<div class="chat-empty">等待弹幕…<br>所选来源的消息会在这里实时显示</div>';
    if (list.length) feed.scrollTop = feed.scrollHeight;
  }

  function rebuildFeed() {
    // 先消化待绘制的消息，避免同一条消息回填后又从队列追加。
    flushFeed();
    rebuildFeedPane($("#feed"), mainChatSource(), st.mainPane);
    if (st.chatSplit) rebuildFeedPane($("#feed-sub"), st.subSource, st.subPane);
  }

  function bindChatPane(feed, pane, source, kinds, clear) {
    var buttons = $$("button[data-kind]", kinds);
    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        if (pane.kind === b.dataset.kind) return;
        flushFeed();
        pane.kind = b.dataset.kind;
        buttons.forEach(function (x) {
          x.classList.toggle("on", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
        rebuildFeedPane(feed, source(), pane);
      });
    });
    clear.addEventListener("click", function () {
      pane.clearedThrough = st.received;
      rebuildFeedPane(feed, source(), pane);
      if (pane === st.mainPane) renderChatHead();
    });
  }

  function renderChatDisplay() {
    $("#chat-feeds").classList.toggle("hide-time", !st.chatShowTime);
    $("#chat-feeds").classList.toggle("split", st.chatSplit);
    $("#chat-divider").classList.toggle("hide", !st.chatSplit);
    $("#chat-sub").classList.toggle("hide", !st.chatSplit);
    var time = $("#chat-time-toggle");
    time.classList.toggle("on", st.chatShowTime);
    time.setAttribute("aria-pressed", String(st.chatShowTime));
    time.title = st.chatShowTime ? "隐藏弹幕时间" : "显示弹幕时间";
    var split = $("#chat-split-toggle");
    split.classList.toggle("on", st.chatSplit);
    split.setAttribute("aria-pressed", String(st.chatSplit));
    split.title = st.chatSplit ? "关闭弹幕子栏" : "开启弹幕子栏";
  }

  function bindChatResize() {
    var box = $("#chat-feeds");
    var divider = $("#chat-divider");
    var feeds = [$("#feed"), $("#feed-sub")];
    var min = Number(divider.getAttribute("aria-valuemin"));
    var max = Number(divider.getAttribute("aria-valuemax"));
    var share = Number(divider.getAttribute("aria-valuenow"));
    var drag = null;

    function setShare(value) {
      var following = feeds.filter(function (feed) {
        return feed.scrollHeight - feed.scrollTop - feed.clientHeight <= 2;
      });
      share = Math.max(min, Math.min(max, Math.round(value)));
      box.style.setProperty("--chat-sub-share", String(share / 100));
      divider.setAttribute("aria-valuenow", String(share));
      divider.setAttribute("aria-valuetext", "子栏占 " + share + "%");
      following.forEach(function (feed) {
        feed.scrollTo({ top: feed.scrollHeight, behavior: "instant" });
      });
    }

    function saveShare() {
      try { localStorage.setItem("ld.chat-sub-share", String(share)); } catch (e) {}
    }

    var saved = share;
    try { saved = Number(localStorage.getItem("ld.chat-sub-share")) || share; } catch (e) {}
    setShare(saved);

    divider.addEventListener("pointerdown", function (ev) {
      if (!ev.isPrimary || ev.button !== 0 || drag) return;
      ev.preventDefault();
      divider.focus();
      drag = { id: ev.pointerId, offset: ev.clientY - divider.getBoundingClientRect().top };
      divider.setPointerCapture(ev.pointerId);
      divider.classList.add("dragging");
    });
    divider.addEventListener("pointermove", function (ev) {
      if (!drag || ev.pointerId !== drag.id || !st.chatSplit) return;
      var bounds = box.getBoundingClientRect();
      var available = bounds.height - divider.offsetHeight;
      if (available <= 0) return;
      var mainHeight = ev.clientY - drag.offset - bounds.top;
      setShare(100 * (1 - mainHeight / available));
    });
    // 释放或取消指针时都会失去捕获，拖出分隔线也能保存最后的比例。
    divider.addEventListener("lostpointercapture", function (ev) {
      if (!drag || ev.pointerId !== drag.id) return;
      drag = null;
      divider.classList.remove("dragging");
      saveShare();
    });
    divider.addEventListener("keydown", function (ev) {
      var next;
      if (ev.key === "ArrowUp") next = share + 5;
      else if (ev.key === "ArrowDown") next = share - 5;
      else if (ev.key === "Home") next = min;
      else if (ev.key === "End") next = max;
      else return;
      ev.preventDefault();
      setShare(next);
      saveShare();
    });
  }

  function onMessage(m) {
    if (!m || st.seen[m.seq]) return;
    if (!watching(m.room)) return;   // 不属于当前舞台/只听弹幕的消息不计入统计、不上屏
    st.seen[m.seq] = 1;
    // seq 全局自增：只保留最近一段去重记录，连续看几小时也不会让 seen 无限膨胀
    if (m.seq > st.seenBase + 6000) {
      var keep = Object.create(null), base = m.seq - 6000;
      Object.keys(st.seen).forEach(function (k) { if (+k > base) keep[k] = 1; });
      st.seen = keep;
      st.seenBase = base;
    }
    st.received++;
    st.msgArrival.set(m, st.received);
    st.stamps.push(Date.now() / 1000);
    st.msgs.push(m);
    if (st.msgs.length > 600) st.msgs.splice(0, 200);
    queueMsg(m);
  }

  /* ---------------- SSE ---------------- */
  // 右下角那个「实时 · SSE」是这条推送通道的状态灯，不是装饰：
  // 连上 = 实时 · SSE；断开重连 = 重连中…；一直没连上 = 未连接。
  function setSseTip(state) {
    var tip = $("#chat-tip");
    if (!tip) return;
    if (state === "open") {
      tip.textContent = "实时 · SSE";
      tip.className = "on";
    } else if (state === "retry") {
      tip.textContent = "重连中…";
      tip.className = "warn";
    } else {
      tip.textContent = "未连接";
      tip.className = "off";
    }
  }

  function connect() {
    if (st.sse) { try { st.sse.close(); } catch (e) {} }
    setSseTip("waiting");
    var es = new EventSource("/api/stream");
    st.sse = es;
    st.sseRetryAt = 0;
    es.onopen = function () { st.sseTries = 0; st.sseRetryAt = 0; if (st.sse === es) setSseTip("open"); };
    es.onmessage = function (ev) {
      var j;
      try { j = JSON.parse(ev.data); } catch (e) { return; }
      if (j.type === "snapshot") { applySnapshot(j.data); }
      else if (j.type === "msg") { onMessage(j.data); }
      else if (j.type === "status" || j.type === "online" || j.type === "room_live" || j.type === "room_info"
               || j.type === "viewers" || j.type === "watched" || j.type === "engine") {
        onSideEvent(j);
      }
    };
    es.onerror = function () {
      if (st.sse !== es) return;          // 已经被更新的连接取代
      es.close();
      setSseTip("retry");
      st.sseTries++;
      var wait = Math.min(15000, 1000 * Math.pow(1.6, st.sseTries));
      st.sseRetryAt = Date.now() + wait;
      setTimeout(connect, wait);
    };
  }

  function onSideEvent(j) {
    if (j.type === "engine") {
      // 引擎状态变了（启动中 / 就绪 / 等待选台 / 离线）服务端立刻推，不用等 12 秒的 pollSnap
      if (!st.snap) return;
      var bs = st.snap.bridge;
      bs.status = j.status;
      bs.state = j.state || bs.state || "";
      bs.error = j.error || "";
      renderHeader();
      return;
    }
    if (j.type === "status") {
      if (!st.snap) return;
      var br = st.snap.bridge.rooms[String(j.room)];
      if (!br) return;
      br.state = j.state; br.detail = j.detail;
      var t = st.tiles[j.room];
      if (t) updateTileHead(t, j.room);
      renderHeader();
      return;
    }
    if (j.type === "online" || j.type === "room_live") {
      var r = st.rooms.filter(function (x) { return x.room_id === j.room; })[0];
      if (!r) return;
      if (j.type === "room_live") r.live_status = j.live_status;
      if (j.type === "online") r.online = j.online;
      var tt = st.tiles[j.room];
      if (tt) updateTileHead(tt, j.room);
      return;
    }
    if (j.type === "viewers" || j.type === "watched") {
      var br2 = (st.snap && st.snap.bridge && st.snap.bridge.rooms || {})[String(j.room)];
      if (br2) { if (j.type === "viewers") br2.viewers = j.viewers; else br2.watched = j.watched; }
      var t3 = st.tiles[j.room];
      if (t3) updateTileHead(t3, j.room);
      return;
    }
    if (j.type === "room_face") {
      var rf = st.rooms.filter(function (x) { return x.room_id === j.room; })[0];
      if (!rf) return;
      if (j.face) rf.face = j.face;
      if (j.uname) rf.name = j.uname;
      renderRailSoon();
      return;
    }
    if (j.type === "room_info") {
      var rr = st.rooms.filter(function (x) { return x.room_id === j.room; })[0];
      if (rr) {
        rr.live_status = j.info.live_status; rr.title = j.info.title; rr.online = j.info.online;
        renderRailSoon();   // 左栏标题和直播状态跟随房间信息刷新
      }
      var t2 = st.tiles[j.room];
      if (t2) updateTileHead(t2, j.room);
      return;
    }
  }

  /* ---------------- 发送 ---------------- */
  function bindComposer() {
    var input = $("#send-text");
    input.addEventListener("input", function () {
      var n = input.value.length;
      var len = $("#send-len");
      len.textContent = n + "/100";
      len.classList.toggle("over", n > 100);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSend(); }
    });
    $("#send-btn").addEventListener("click", doSend);
    $("#send-emo").addEventListener("click", function () {
      if ($("#emo-panel").classList.contains("hide")) openEmo(); else closeEmo();
    });
    document.addEventListener("click", function (e) {
      var panel = $("#emo-panel");
      if (!panel || panel.classList.contains("hide")) return;
      if (panel.contains(e.target) || $("#send-emo").contains(e.target)) return;
      closeEmo();
    });
  }

  /* ---------------- 表情包 ---------------- */
  function emoRoomNow() {
    return parseInt($("#send-to").value || "0", 10) || st.sendTo || 0;
  }
  function closeEmo() {
    $("#emo-panel").classList.add("hide");
  }
  function openEmo() {
    var panel = $("#emo-panel");
    var room = emoRoomNow();
    if (!room) { toast("先在「发到」里选择直播间", "err"); return; }
    panel.classList.remove("hide");
    if (st.emoRoom !== room) { st.emoRoom = room; st.emoTab = 0; }
    if (st.emo[room]) { renderEmo(room); return; }
    panel.innerHTML = '<div class="emo-loading">正在读取该直播间的表情…</div>';
    api("/api/emoticons?room=" + room).then(function (d) {
      st.emo[room] = d;
      renderEmo(room);
    }).catch(function (e) {
      panel.innerHTML = '<div class="emo-loading">' + esc(e.message) + "</div>";
    });
  }
  function renderEmo(room) {
    var d = st.emo[room];
    var panel = $("#emo-panel");
    if (!d || !d.ok) {
      panel.innerHTML = '<div class="emo-loading">' + esc((d && d.message) || "表情包不可用") +
        (d && d.need_login ? "，请先登录 B 站账号" : "") + "</div>";
      return;
    }
    var packs = d.packages || [];
    if (!packs.length) { panel.innerHTML = '<div class="emo-loading">这个直播间没有可用表情包</div>'; return; }
    var ti = Math.min(Math.max(st.emoTab, 0), packs.length - 1);
    var tabs = packs.map(function (p, i) {
      return '<button class="emo-tab' + (i === ti ? " on" : "") + '" data-i="' + i + '">' + esc(p.name) + "</button>";
    }).join("");
    var pk = packs[ti];
    var hasLocked = false;
    var items = pk.items.map(function (it) {
      if (it.locked) hasLocked = true;
      return '<button class="emo-item' + (it.locked ? " locked" : "") + '" data-unique="' + esc(it.unique) + '"' +
        ' title="' + esc(it.name + (it.locked ? " · 需要" + (it.lock_label || "粉丝牌 / 舰长权限") : "")) + '">' +
        '<img src="' + esc(it.url) + '" alt="' + esc(it.name) + '" loading="lazy" referrerpolicy="no-referrer"></button>';
    }).join("");
    panel.innerHTML =
      '<div class="emo-head"><span>表情包</span><button class="emo-close" id="emo-close" title="关闭">×</button></div>' +
      '<div class="emo-tabs">' + tabs + "</div>" +
      '<div class="emo-grid">' + (items || '<div class="emo-loading">这个表情包是空的</div>') + "</div>" +
      '<div class="emo-tip">点一下直接发到「' + esc(roomName(room)) + "」" +
      (hasLocked ? " · 灰掉的需要粉丝牌 / 舰长权限" : "") + "</div>";
    $("#emo-close").onclick = function (ev) { ev.stopPropagation(); closeEmo(); };
    $$(".emo-tab").forEach(function (b) {
      // 切 tab 会整块重绘面板：旧节点在冒泡到 document 前已脱离 DOM，
      // 外层「点空白处关闭」就会误判成点了外面而收起面板，所以这里拦住冒泡。
      b.onclick = function (ev) {
        ev.stopPropagation();
        st.emoTab = parseInt(b.dataset.i, 10) || 0;
        renderEmo(room);
      };
    });
    $$(".emo-item").forEach(function (b) {
      b.onclick = function () {
        if (b.classList.contains("locked")) { toast("这个表情需要粉丝牌 / 舰长权限，当前账号不可用", "err"); return; }
        var img = b.querySelector("img");
        sendEmo(room, b.dataset.unique, img ? img.getAttribute("alt") : "");
      };
    });
  }
  function sendEmo(room, unique, name) {
    api("/api/send", { room: room, emoticon: unique }).then(function () {
      toast("表情「" + (name || "表情") + "」已发到「" + roomName(room) + "」", "ok");
      closeEmo();
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function doSend() {
    var input = $("#send-text");
    var text = input.value.trim();
    var room = parseInt($("#send-to").value || "0", 10);
    var btn = $("#send-btn");
    if (!room) { toast("先在左侧选择要观看的直播间", "err"); return; }
    if (!text) { toast("先输入要发送的弹幕", "err"); return; }
    if (text.length > 100) { toast("弹幕最多 100 字", "err"); return; }
    if (st.sendAt[room] && Date.now() - st.sendAt[room] < 3000) { toast("同一房间 3 秒内只能发一条", "err"); return; }
    btn.disabled = true;
    api("/api/send", { room: room, text: text }).then(function () {
      st.sendAt[room] = Date.now();
      input.value = "";
      $("#send-len").textContent = "0/100";
      toast("已发送到「" + roomName(room) + "」", "ok");
    }).catch(function (e) {
      toast(e.message, "err");
    }).then(function () { setTimeout(function () { btn.disabled = false; }, 1200); });
  }

  /* ---------------- 登录弹窗 ---------------- */
  function openLogin() {
    $("#login-mask").classList.remove("hide");
    setLoginTab("qr");
    startQr();
    refresh();
  }
  function closeLogin() {
    $("#login-mask").classList.add("hide");
    if (st.qrTimer) { clearInterval(st.qrTimer); st.qrTimer = null; }
  }
  function setLoginTab(tab) {
    $$("#login-tabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === tab); });
    $("#login-qr").classList.toggle("hide", tab !== "qr");
    $("#login-paste").classList.toggle("hide", tab !== "paste");
  }
  function startQr() {
    var box = $("#qr-box");
    box.innerHTML = '<div class="ph">正在申请二维码…</div>';
    $("#qr-status").innerHTML = '<span class="dot warn"></span>正在向 B 站申请二维码…';
    api("/api/login/start", {}).then(function (j) {
      st.qrKey = j.qrcode_key;
      box.innerHTML = j.svg || '<div class="ph">二维码生成失败</div>';
      st.qrDeadline = Date.now() + ((j.expires_in || 180) * 1000);
      $("#qr-status").innerHTML = '<span class="dot on"></span>等待扫码… <span style="color:var(--dim-2)">（约 3 分钟内有效）</span>';
      pollQr();
    }).catch(function (e) {
      box.innerHTML = '<div class="ph">二维码申请失败</div>';
      $("#qr-status").innerHTML = '<span class="dot off"></span>' + esc(e.message);
    });
  }
  function pollQr() {
    if (st.qrTimer) clearInterval(st.qrTimer);
    st.qrTimer = setInterval(function () {
      if (Date.now() > st.qrDeadline + 5000) {
        clearInterval(st.qrTimer); st.qrTimer = null;
        $("#qr-status").innerHTML = '<span class="dot off"></span>二维码已过期，点「换一张」重试';
        return;
      }
      api("/api/login/poll", { key: st.qrKey }).then(function (j) {
        var s = j.status;
        if (s === "waiting") return;
        if (s === "scanned") {
          $("#qr-status").innerHTML = '<span class="dot warn"></span>已扫码，请在手机上确认登录';
          return;
        }
        if (s === "expired") {
          $("#qr-status").innerHTML = '<span class="dot off"></span>二维码已过期，点「换一张」重试';
          clearInterval(st.qrTimer); st.qrTimer = null;
          return;
        }
        if (s === "success") {
          clearInterval(st.qrTimer); st.qrTimer = null;
          var msg = j.message || "登录成功";
          var net = j.login && (j.login.state === "network" || j.login.state === "risk");
          $("#qr-status").innerHTML = '<span class="dot ' + (net ? "warn" : "on") + '"></span>' +
            esc(msg) + ((j.login && j.login.uname) ? "：" + esc(j.login.uname) : "");
          toast(msg + (j.login && j.login.can_send ? "" : "，但缺少 bili_jct，暂不能发弹幕"), net ? "warn" : "ok");
          setTimeout(function () { closeLogin(); refresh(); }, net ? 2000 : 700);
          return;
        }
        if (s === "error") {
          $("#qr-status").innerHTML = '<span class="dot off"></span>' + esc(j.message || "登录失败");
        }
      }).catch(function (e) {
        $("#qr-status").innerHTML = '<span class="dot off"></span>' + esc(e.message);
      });
    }, 1800);
  }

  /* ---------------- 顶栏设置菜单 ---------------- */
  // 监控台（8765 管理台）/ 使用说明（#help-mask）/ 网络代理（#proxy-mask）/ 更新检查（#upd-mask）/ 关于（#about-mask）
  function setMenuOpen() { return !$("#settings-menu").classList.contains("hide"); }
  function openSettings() {
    $("#settings-menu").classList.remove("hide");
    $("#settings-btn").setAttribute("aria-expanded", "true");
  }
  function closeSettings() {
    $("#settings-menu").classList.add("hide");
    $("#settings-btn").setAttribute("aria-expanded", "false");
  }

  /* ---------------- 关于弹窗 ---------------- */
  // 版本号不在前端写死（AGENTS R5）：打开时向 /api/version 取一次，取不到就留「—」。
  var aboutVerLoaded = false;
  function openAbout() {
    $("#about-mask").classList.remove("hide");
    loadAboutVersion();
  }
  function closeAbout() { $("#about-mask").classList.add("hide"); }
  function loadAboutVersion() {
    if (aboutVerLoaded) return;
    aboutVerLoaded = true;
    api("/api/version").then(function (j) {
      var el = $("#about-ver");
      if (el && j && j.version) el.textContent = "v" + j.version;
    }).catch(function () { aboutVerLoaded = false; });
  }

  /* ---------------- 更新检查弹窗 ---------------- */
  // 比对在服务端 /api/update 做（读取 GitHub 最新正式发行版），前端只展示结论，不下载、不升级。
  function openUpdate() {
    $("#upd-mask").classList.remove("hide");
    $("#upd-status").textContent = "正在检查…";
    $("#upd-status").className = "upd-status";
    $("#upd-cur").textContent = "—";
    $("#upd-latest").textContent = "—";
    /* 这里直接用 fetch 而不是 api()：检查失败时服务端仍回 200 + ok:false + current，
       api() 会把 ok:false 当异常抛掉，就拿不到「当前版本」与具体错误了。 */
    fetch("/api/update").then(function (r) {
      return r.text().then(function (t) {
        var j = {};
        try { j = JSON.parse(t); } catch (e) { j = { ok: false, error: "返回不是 JSON" }; }
        return j;
      });
    }).then(function (j) {
      var st = $("#upd-status");
      if (j && j.current) $("#upd-cur").textContent = "v" + j.current;
      if (j && j.url) $("#upd-link").href = j.url;
      if (j && j.ok) {
        $("#upd-latest").textContent = "v" + j.latest;
        if (j.has_update) {
          st.textContent = "发现新版本 v" + j.latest + "，可到仓库看更新说明";
          st.className = "upd-status upd-new";
        } else {
          st.textContent = "已是最新版本";
          st.className = "upd-status upd-ok";
        }
      } else {
        st.textContent = "检查失败：" + ((j && j.error) || "仓库不可访问");
        st.className = "upd-status upd-fail";
      }
    }).catch(function () {
      $("#upd-status").textContent = "检查失败：服务端没有响应";
      $("#upd-status").className = "upd-status upd-fail";
    });
  }
  function closeUpdate() { $("#upd-mask").classList.add("hide"); }

  /* ---------------- 网络代理弹窗 ---------------- */
  // 配置存 config/proxy.json：服务端负责校验、落盘、写本进程环境变量，前端只管读写。
  var proxyLoaded = false;
  function proxyTag(text) { var t = $("#proxy-tag"); if (t) t.textContent = text; }
  function proxyStatus(text, kind) {
    var el = $("#proxy-status");
    el.textContent = text;
    el.className = "proxy-status" + (kind ? " " + kind : "");
  }
  function proxyMode() {
    var list = $$("input[name=proxy-mode]");
    for (var i = 0; i < list.length; i++) if (list[i].checked) return list[i].value;
    return "system";
  }
  function proxyModeSet(mode) {
    $$("input[name=proxy-mode]").forEach(function (r) { r.checked = (r.value === mode); });
  }
  /* 菜单右侧小字：三种模式各一句，别让用户以为只有「开 / 关」 */
  function proxyTagOf(mode) { return mode === "custom" ? "自定义" : (mode === "none" ? "不使用" : "跟随系统"); }
  function fillProxy(j) {
    var mode = j.mode || "system";
    proxyModeSet(mode);
    $("#proxy-url").value = j.url || "";
    $("#proxy-url").disabled = mode !== "custom";
    proxyTag(proxyTagOf(mode));
  }
  // 页面加载就把菜单右侧的「跟随系统 / 自定义 / 不使用」刷对，不用等用户点开
  function loadProxyTag() {
    if (proxyLoaded) return;
    proxyLoaded = true;
    api("/api/proxy").then(function (j) {
      proxyTag(proxyTagOf(j.mode || "system"));
    }).catch(function () { proxyLoaded = false; });
  }
  function openProxy() {
    $("#proxy-mask").classList.remove("hide");
    proxyStatus("读配置中…");
    api("/api/proxy").then(function (j) {
      fillProxy(j);
      proxyStatus(j.mode === "custom" ? ("已保存：" + j.url)
        : (j.mode === "none" ? "当前：不使用代理" : "当前：使用系统代理"));
    }).catch(function (e) {
      proxyStatus("读配置失败：" + (e && e.message ? e.message : "服务端没有响应"), "fail");
    });
  }
  function closeProxy() { $("#proxy-mask").classList.add("hide"); }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    $("#login-chip").addEventListener("click", openLogin);
    $("#login-close").addEventListener("click", closeLogin);
    $("#login-close2").addEventListener("click", closeLogin);
    // 顶栏右上角「设置」：监控台 / 使用说明 / 更新检查 / 关于
    $("#settings-btn").addEventListener("click", function (e) {
      e.stopPropagation();
      if (setMenuOpen()) closeSettings(); else openSettings();
    });
    $$("#settings-menu .set-item").forEach(function (b) {
      b.addEventListener("click", function () {
        var act = b.dataset.set;
        closeSettings();
        if (act === "help") $("#help-mask").classList.remove("hide");
        else if (act === "check") openUpdate();
        else if (act === "about") openAbout();
        else if (act === "proxy") openProxy();
      });
    });
    document.addEventListener("click", function (e) {
      if (!setMenuOpen()) return;
      if (e.target.closest && e.target.closest(".setwrap")) return;
      closeSettings();
    });
    $("#help-close").addEventListener("click", function () { $("#help-mask").classList.add("hide"); });
    $("#help-mask").addEventListener("click", function (e) { if (e.target === this) this.classList.add("hide"); });
    $("#about-close").addEventListener("click", closeAbout);
    $("#about-mask").addEventListener("click", function (e) { if (e.target === this) closeAbout(); });
    $("#upd-close").addEventListener("click", closeUpdate);
    $("#upd-ok").addEventListener("click", closeUpdate);
    $("#upd-mask").addEventListener("click", function (e) { if (e.target === this) closeUpdate(); });
    $("#proxy-close").addEventListener("click", closeProxy);
    $("#proxy-cancel").addEventListener("click", closeProxy);
    $("#proxy-mask").addEventListener("click", function (e) { if (e.target === this) closeProxy(); });
    $$("input[name=proxy-mode]").forEach(function (r) {
      r.addEventListener("change", function () {
        $("#proxy-url").disabled = proxyMode() !== "custom";
        if (proxyMode() === "custom") $("#proxy-url").focus();
      });
    });
    $("#proxy-test").addEventListener("click", function () {
      var mode = proxyMode();
      var url = $("#proxy-url").value.trim();
      if (mode === "none") { proxyStatus("当前是「不使用代理」，不用测试", "fail"); return; }
      if (mode === "custom" && !url) { proxyStatus("先填代理地址，例如 http://127.0.0.1:7890", "fail"); return; }
      proxyStatus(mode === "custom" ? "正在通过代理访问 Gitee…" : "正在按系统代理访问 Gitee…");
      api("/api/proxy/test", { mode: mode, url: url }).then(function (j) {
        if (j && j.passed) proxyStatus("连接成功（HTTP " + j.status + "，" + j.ms + " ms）", "ok");
        else proxyStatus(((j && j.error) || "连接失败：未知原因"), "fail");
      }).catch(function (e) {
        proxyStatus("连接失败：" + (e && e.message ? e.message : "服务端没有响应"), "fail");
      });
    });
    $("#proxy-save").addEventListener("click", function () {
      proxyStatus("保存中…");
      api("/api/proxy", { mode: proxyMode(), url: $("#proxy-url").value.trim() })
        .then(function (j) {
          fillProxy(j);
          var head = j.mode === "custom" ? ("已保存：" + j.url)
            : (j.mode === "none" ? "已保存：不使用代理" : "已保存：使用系统代理");
          proxyStatus(head + "；" + (j.note || "已生效"), "ok");
        }).catch(function (e) {
          proxyStatus("保存失败：" + (e && e.message ? e.message : "服务端没有响应"), "fail");
        });
    });
    $("#login-mask").addEventListener("click", function (e) { if (e.target === this) closeLogin(); });
    $$("#login-tabs button").forEach(function (b) {
      b.addEventListener("click", function () { setLoginTab(b.dataset.tab); });
    });
    $("#qr-refresh").addEventListener("click", startQr);
    $("#paste-save").addEventListener("click", function () {
      var body = {
        sessdata: $("#p-sess").value.trim(),
        bili_jct: $("#p-jct").value.trim(),
        dedeuserid: $("#p-dede").value.trim()
      };
      api("/api/login/paste", body).then(function (j) {
        if (j && j.warn) { toast("已保存登录信息；" + j.warn, "warn"); }
        else { toast("已保存登录信息", "ok"); }
        closeLogin(); refresh();
      }).catch(function (e) { toast(e.message, "err"); });
    });
    $("#logout-btn").addEventListener("click", function () {
      api("/api/login/logout", {}).then(function () { toast("已退出登录（本地凭据已清除）"); closeLogin(); refresh(); })
        .catch(function (e) { toast(e.message, "err"); });
    });

    applyMode();
    try { localStorage.removeItem("ld.zoom"); } catch (e) {}   // 旧版本留下的尺寸，不再读取
    try { st.bgKeep = localStorage.getItem("ld.bg") === "1"; } catch (e) {}
    applyBg();
    $("#mode-btn").addEventListener("click", function () {
      setMode(st.mode === "smooth" ? "low" : "smooth");
    });
    $("#bg-btn").addEventListener("click", function () { setBg(!st.bgKeep); });
    // 舞台头的「?」：播放模式 / 后台播放的说明浮层
    $("#q-btn").addEventListener("click", function (e) {
      e.stopPropagation();
      $("#q-pop").classList.toggle("hide");
    });
    $("#q-close").addEventListener("click", function () { $("#q-pop").classList.add("hide"); });
    document.addEventListener("click", function (e) {
      var pop = $("#q-pop");
      if (!pop || pop.classList.contains("hide")) return;
      if (e.target.closest && (e.target.closest("#q-pop") || e.target.closest("#q-btn"))) return;
      pop.classList.add("hide");
    });
    $("#mem-wrap").addEventListener("click", recycleAll);
    // 页面切到后台 / 最小化时，浏览器会继续解码十几路视频，内存只涨不降；
    // 默认隐藏 5 秒后就暂停全部播放器（弹幕在服务端照收），回到前台每隔 120ms 逐个重连，避免十几路同时重建。
    // 想让监控墙在后台一直跑，可以点舞台头的「后台暂停」切到「后台续播」。
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        if (st.bgKeep) return;
        if (st.hideTimer) clearTimeout(st.hideTimer);
        st.hideTimer = setTimeout(function () {
          if (!document.hidden || st.bgKeep) return;
          Object.keys(st.players).forEach(function (k) {
            var d = st.tiles[k];
            if (d) {
              d.dataset.hidden = "1";
              var fill = $(".fill", d), small = $(".fill .small", d);
              if (fill) fill.classList.remove("hide");
              if (small) small.textContent = "已暂停（窗口在后台，省内存）· 回到页面自动恢复";
            }
            stopStream(parseInt(k, 10));
          });
        }, 5000);
      } else {
        if (st.hideTimer) { clearTimeout(st.hideTimer); st.hideTimer = null; }
        var nth = 0;
        Object.keys(st.tiles).forEach(function (k) {
          var d = st.tiles[k];
          if (!d || d.dataset.hidden !== "1") return;
          d.dataset.hidden = "";
          d.dataset.retry = ""; d.dataset.failed = "";
          var v = $("video", d);
          if (!v) return;
          var wait = (nth++) * 120;
          setTimeout(function () {
            if (!document.hidden && st.tiles[k] === d) attachStream(parseInt(k, 10), d, v);
          }, wait);
        });
        pollMem();
      }
    });
    $("#rail-toggle").addEventListener("click", function () {
      st.railCompact = !st.railCompact;
      closeRoomMenu();
      $("#app").classList.toggle("rail-compact", st.railCompact);
      var label = st.railCompact ? "展开房间列表" : "收起房间列表";
      this.title = label;
      this.setAttribute("aria-label", label);
      this.setAttribute("aria-expanded", String(!st.railCompact));
      renderRail();
    });
    $("#rail-search").addEventListener("input", function () { renderRail(); });
    $("#rail-unhide").addEventListener("click", function () {
      api("/api/room/unhide", {}).then(function () { toast("已恢复隐藏的直播间"); return refresh(); })
        .catch(function (e) { toast(e.message, "err"); });
    });
    $("#grp-cancel").addEventListener("click", closeGrp);
    $("#grp-save").addEventListener("click", saveGrp);
    $("#grp-mask").addEventListener("click", function (e) { if (e.target === this) closeGrp(); });
    $("#grp-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); saveGrp(); }
    });
    $("#col-close").addEventListener("click", closeColor);
    $("#col-save").addEventListener("click", saveColor);
    $("#col-clear").addEventListener("click", clearColor);
    $("#col-mask").addEventListener("click", function (e) { if (e.target === this) closeColor(); });
    $("#col-on").addEventListener("change", updateColPrev);
    $("#col-main-pick").addEventListener("input", function () { colFill("main", this.value); updateColPrev(); });
    $("#col-sec-pick").addEventListener("input", function () { colFill("sec", this.value); updateColPrev(); });
    $("#col-main-hex").addEventListener("input", function () { colFromHex("main"); updateColPrev(); });
    $("#col-sec-hex").addEventListener("input", function () { colFromHex("sec"); updateColPrev(); });
    ["main", "sec"].forEach(function (kind) {
      ["r", "g", "b"].forEach(function (ch) {
        $("#col-" + kind + "-" + ch).addEventListener("input", function () { colFromRgb(kind); updateColPrev(); });
      });
    });
    $("#col-sec-clear").addEventListener("click", function () { colFill("sec", ""); updateColPrev(); });
    $("#col-main-drop").addEventListener("click", function () { pickScreen("main"); });
    $("#col-sec-drop").addEventListener("click", function () { pickScreen("sec"); });
    if (!window.EyeDropper) {
      $("#col-main-drop").classList.add("hide");
      $("#col-sec-drop").classList.add("hide");
    }
    $("#del-cancel").addEventListener("click", closeDel);
    $("#del-mask").addEventListener("click", function (e) { if (e.target === this) closeDel(); });
    $("#del-hide").addEventListener("click", function () { doRemove("hide"); });
    $("#del-disable").addEventListener("click", function () { doRemove("disable"); });
    $("#del-delete").addEventListener("click", function () { doRemove("delete"); });
    $("#del-temp").addEventListener("click", function () { doRemove("delete"); });
    $("#rail-refresh").addEventListener("click", function () {
      api("/api/reload", {}).then(function () { toast("已重新读取监控名单"); return refresh(); })
        .catch(function (e) { toast(e.message, "err"); });
    });
    /* 手填输入：房间号、主播 UID、直播间链接都能认，交给服务端判定 */
    function addHow(d) {
      if (d.via === "db_room" || d.via === "db_uid") return "监控台名单";
      return d.via === "uid" ? "按主播 UID" : "按房间号";
    }
    function resolveAndAdd(listen) {
      var raw = ($("#add-room").value || "").trim();
      if (!raw) { toast("请输入房间号、主播 UID 或直播间链接", "err"); return; }
      api("/api/resolve?q=" + encodeURIComponent(raw)).then(function (d) {
        $("#add-room").value = "";
        var label = d.uname || ("房间 " + d.room);
        toast("已" + (listen ? "只听" : "加入舞台") + "：" + label + " · " + d.room + "（" + addHow(d) + "）");
        if (listen) toggleListen(d.room, true); else toggleRoom(d.room, true);
      }).catch(function (e) {
        toast(e.message || "没找到这个直播间", "err");
      });
    }
    $("#add-form").addEventListener("submit", function (e) {
      e.preventDefault();
      resolveAndAdd(false);
    });
    $("#add-listen").addEventListener("click", function () {
      resolveAndAdd(true);
    });
    $$(".presets button").forEach(function (b) {
      b.addEventListener("click", function () {
        st.preset = b.dataset.preset;
        renderGrid();
        api("/api/grid", { grid: b.dataset.preset === "auto" ? 0 : parseInt(b.dataset.preset.split("x")[0], 10) * 10 + parseInt(b.dataset.preset.split("x")[1], 10) }).catch(function () {});
      });
    });
    $("#solo-exit").addEventListener("click", function () { st.solo = 0; renderGrid(); });
    $("#chat-room").addEventListener("change", function () {
      var v = String(this.value || "0");
      if (v.indexOf("c:") === 0) {
        st.filterCombo = parseInt(v.slice(2), 10) || 0;
        st.filterRoom = 0;
      } else {
        st.filterCombo = 0;
        st.filterRoom = parseInt(v, 10) || 0;
      }
      flushFeed();
      rebuildFeedPane($("#feed"), mainChatSource(), st.mainPane);
      renderChatHead();
      // 只听弹幕的房间不接受发送，发送目标仍限定在舞台上
      if (st.filterRoom && st.active.indexOf(st.filterRoom) >= 0) {
        st.sendTo = st.filterRoom;
        $("#send-to").value = String(st.filterRoom);
      }
    });
    try {
      st.chatShowTime = localStorage.getItem("ld.chat-time") !== "0";
      st.chatSplit = localStorage.getItem("ld.chat-split") === "1";
      st.subSource = localStorage.getItem("ld.chat-sub-source") || "";
    } catch (e) {}
    renderChatDisplay();
    bindChatResize();
    $("#chat-time-toggle").addEventListener("click", function () {
      st.chatShowTime = !st.chatShowTime;
      renderChatDisplay();
      try { localStorage.setItem("ld.chat-time", st.chatShowTime ? "1" : "0"); } catch (e) {}
    });
    $("#chat-split-toggle").addEventListener("click", function () {
      flushFeed();
      st.chatSplit = !st.chatSplit;
      renderChatDisplay();
      if (st.chatSplit) {
        renderChatFilter();
        rebuildFeedPane($("#feed-sub"), st.subSource, st.subPane);
      } else {
        $("#feed-sub").textContent = "";
      }
      try { localStorage.setItem("ld.chat-split", st.chatSplit ? "1" : "0"); } catch (e) {}
    });
    $("#chat-sub-room").addEventListener("change", function () {
      flushFeed();
      st.subSource = this.value;
      rebuildFeedPane($("#feed-sub"), st.subSource, st.subPane);
      try { localStorage.setItem("ld.chat-sub-source", st.subSource); } catch (e) {}
    });
    $("#combo-open").addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (comboPanelOpen()) closeCombo(); else openCombo();
    });
    $("#combo-close").addEventListener("click", function (ev) { ev.stopPropagation(); closeCombo(); });
    $("#combo-cancel").addEventListener("click", closeCombo);
    $("#combo-save").addEventListener("click", saveCombo);
    $("#combo-list").addEventListener("click", function (ev) {
      var row = ev.target.closest(".cp-row");
      if (!row) return;
      var id = parseInt(row.dataset.id, 10) || 0;
      var act = ev.target.dataset.act || "";
      if (act === "use") {
        var c = comboById(id);
        st.filterCombo = id; st.filterRoom = 0;
        $("#chat-room").value = "c:" + id;
        flushFeed();
        rebuildFeedPane($("#feed"), mainChatSource(), st.mainPane);
        renderChatHead(); closeCombo();
        if (c) toast("已切到组合「" + c.name + "」· " + comboLive(c) + " 路");
      } else if (act === "del") {
        deleteCombo(id);
      }
    });
    document.addEventListener("click", function (ev) {
      if (!comboPanelOpen()) return;
      var p = $("#combo-panel");
      if (p.contains(ev.target) || $("#combo-open").contains(ev.target)) return;
      closeCombo();
    });
    bindChatPane($("#feed"), st.mainPane, mainChatSource, $("#chat-main-kinds"), $("#chat-clear"));
    bindChatPane($("#feed-sub"), st.subPane, function () { return st.subSource; },
      $("#chat-sub-kinds"), $("#chat-sub-clear"));
    $("#send-to").addEventListener("change", function () {
      st.sendTo = parseInt(this.value, 10) || 0;
      closeEmo();
    });
    bindComposer();
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (!$("#upd-mask").classList.contains("hide")) closeUpdate();
        else if (!$("#about-mask").classList.contains("hide")) closeAbout();
        else if (!$("#proxy-mask").classList.contains("hide")) closeProxy();
        else if (!$("#login-mask").classList.contains("hide")) closeLogin();
        else if (!$("#grp-mask").classList.contains("hide")) closeGrp();
        else if (!$("#del-mask").classList.contains("hide")) closeDel();
        else if (!$("#col-mask").classList.contains("hide")) closeColor();
        else if (!$("#q-pop").classList.contains("hide")) $("#q-pop").classList.add("hide");
        else if (setMenuOpen()) closeSettings();
        else if (comboPanelOpen()) closeCombo();
        else if (st.solo) { st.solo = 0; renderGrid(); }
      }
    });
  }

  /* ---------------- 断线自愈 ---------------- */
  // 中继端口变了（服务重启过）：所有播放器重建，否则它们会一直连那个没人监听的端口
  function rebuildStreams(msg) {
    if (msg) toast(msg, "warn");
    Object.keys(st.tiles).forEach(function (k) {
      var id = parseInt(k, 10), t = st.tiles[k], v = $("video", t);
      if (!v) return;
      t.dataset.failed = ""; t.dataset.offline = "";
      delete st.stall[id];
      attachStream(id, t, v);
    });
  }

  // 播放器还活着但 readyState 掉到 1 以下（中继被静默关掉、解码停摆）时没有任何 error 事件可等
  // 低延迟 <-> 流畅优先：切换后重建全部播放器（参数在建 player 时生效）
  function modeLabel() { return st.mode === "smooth" ? "流畅优先" : "低延迟"; }
  function applyMode() {
    var b = $("#mode-btn");
    if (b) {
      b.textContent = modeLabel();
      b.classList.toggle("on", st.mode === "smooth");
      b.title = st.mode === "smooth"
        ? "流畅优先：缓冲约 4 秒、不裁剪 SourceBuffer（高码率房间更稳，延迟更大）· 点击切回低延迟"
        : "低延迟：缓冲约 2.5 秒 · 偶发卡顿时可点这里切到「流畅优先」";
    }
  }
  function setMode(mode) {
    st.mode = mode === "smooth" ? "smooth" : "low";
    try { localStorage.setItem("ld.mode", st.mode); } catch (e) {}
    applyMode();
    Object.keys(st.tiles).forEach(function (k) {
      var id = parseInt(k, 10), t = st.tiles[k], v = $("video", t);
      if (v && st.players[id]) { t.dataset.retry = ""; t.dataset.failed = ""; attachStream(id, t, v); }
    });
    toast("播放模式：" + modeLabel() + (st.mode === "smooth" ? "（缓冲更大、更抗卡顿）" : "（延迟更小）"), "ok");
  }

  // 后台暂停 / 后台续播：默认隐藏 5 秒停流省内存；续播则窗口最小化也继续拉流解码
  function applyBg() {
    var b = $("#bg-btn");
    if (!b) return;
    b.textContent = st.bgKeep ? "后台续播" : "后台暂停";
    b.classList.toggle("on", st.bgKeep);
    b.title = st.bgKeep
      ? "后台续播：窗口最小化 / 切到别的标签页后继续拉流解码（更耗内存和 CPU，适合长期盯盘）· 点击改成后台暂停"
      : "后台暂停：窗口最小化 / 切到别的标签页 5 秒后暂停全部画面（省内存，弹幕照收），回来自动恢复 · 点击改成后台续播";
  }
  function setBg(on) {
    st.bgKeep = !!on;
    try { localStorage.setItem("ld.bg", st.bgKeep ? "1" : "0"); } catch (e) {}
    applyBg();
    if (st.bgKeep) {
      if (st.hideTimer) { clearTimeout(st.hideTimer); st.hideTimer = null; }
      toast("后台续播：窗口最小化后画面继续播放", "ok");
    } else {
      toast("后台暂停：最小化或切走 5 秒后暂停画面以省内存", "ok");
      if (document.hidden) {
        Object.keys(st.players).forEach(function (k) { stopStream(parseInt(k, 10)); });
      }
    }
  }

  function watchStreams() {
    if (document.visibilityState !== "visible") return;
    var now = Date.now();
    Object.keys(st.tiles).forEach(function (k) {
      var id = parseInt(k, 10), t = st.tiles[k], v = $("video", t);
      var stream = st.players[id];
      if (!v || !stream || !stream.player || t.dataset.offline === "1" || t.dataset.hidden === "1") {
        delete st.stall[id]; return;
      }
      // 流畅优先模式不裁 SourceBuffer，靠每 20 分钟软重连回收内存
      if (stream.kind === "flv" && st.mode === "smooth" && t.dataset.smoothAt && now - parseInt(t.dataset.smoothAt, 10) > 1200000 && !v.paused) {
        t.dataset.smoothAt = String(now);
        attachStream(id, t, v);
        return;
      }
      if (v.readyState >= 2 && !v.paused) { delete st.stall[id]; return; }
      if (!st.stall[id]) { st.stall[id] = now; return; }
      if (now - st.stall[id] > 12000) {
        delete st.stall[id];
        t.dataset.failed = "";
        attachStream(id, t, v);
      }
    });
  }

  // EventSource 被休眠/网络切换掐掉时不一定有 onerror，兜底重建
  function watchSse() {
    if (st.sse && st.sse.readyState !== 2) return;
    if (st.sseRetryAt && Date.now() < st.sseRetryAt) return;
    connect();
  }

  // 12 秒一次的软刷新：登录态、引擎状态、房间信息（是否开播）、中继端口都可能变
  function pollSnap() {
    api("/api/snapshot").then(function (j) {
      if (!st.snap) return;
      var prevPort = st.streamPort;
      st.snap.login = j.login;
      st.snap.bridge.status = j.bridge.status;
      st.snap.bridge.state = j.bridge.state || "";
      st.snap.bridge.error = j.bridge.error;
      st.info = j.info || st.info || {};
      st.rooms = j.rooms || st.rooms;   // 左栏开播徽标也要跟随（服务端每 10 秒重读监控台状态）
      var listenBefore = st.listening.join(",");
      if (j.listening) st.listening = j.listening.filter(function (r) { return st.active.indexOf(r) < 0; });
      st.streamPort = j.stream_port || 0;
      renderRail();
      if (listenBefore !== st.listening.join(",")) {
        renderChatFilter();
        rebuildFeed();
      }
      loginChip(); renderHeader();
      var br = j.bridge.rooms || {};
      Object.keys(br).forEach(function (k) {
        if (st.snap.bridge.rooms[k]) { st.snap.bridge.rooms[k].state = br[k].state; st.snap.bridge.rooms[k].detail = br[k].detail; }
      });
      Object.keys(st.tiles).forEach(function (k) { updateTileHead(st.tiles[k], parseInt(k, 10)); });
      if (prevPort && st.streamPort && prevPort !== st.streamPort) rebuildStreams("直播流通道已重启，正在重连");
      else syncStreams();
    }).catch(function () {});
  }

  /* ---------------- 启动 ---------------- */
  function stageSyncRoom(id) {
    var tile = st.tiles[id];
    return tile ? { id: id, name: roomName(id), tile: tile, video: $("video", tile), stream: st.players[id] } : null;
  }

  function restoreStagePlayback(video, stream) {
    if ((document.hidden && !st.bgKeep) || video.dataset.paused === "1" || !stream.player || video.ended) return;
    var calibration = window.LiveDeckCalibration;
    var target = stream.kind === "hls" ? stream.player.liveSyncPosition : calibration.livePosition(video, latTarget());
    if (Number.isFinite(target) && Math.abs(target - video.currentTime) > .25 && calibration.contains(video, target)) {
      video.currentTime = target;
    }
    if (video.paused) playVideo(video);
  }

  function boot() {
    if (window.createLiveDeckStageSync && window.LiveDeckCalibration) {
      stageSync = window.createLiveDeckStageSync({
        room: stageSyncRoom,
        rooms: function () { return st.active.map(stageSyncRoom).filter(Boolean); },
        isOnStage: function (id) { return st.active.indexOf(id) >= 0; },
        buffer: latTarget, play: playVideo, restore: restoreStagePlayback, notify: toast
      });
      $("#stage-sync-btn").disabled = false;
    }
    bind();
    loadProxyTag();
    refresh().then(function () {
      if (st.snap) {
        var g = parseInt(st.snap.grid || 0, 10);
        var ok = [22, 32, 33, 44].indexOf(g) >= 0;
        st.preset = ok ? Math.floor(g / 10) + "x" + (g % 10) : "auto";
        renderGrid();
        var rooms = watchIds();
        return api("/api/history?rooms=" + rooms.join(",") + "&limit=160").then(function (j) {
          (j.messages || []).forEach(function (m) {
            if (st.seen[m.seq]) return;
            st.seen[m.seq] = 1; st.msgs.push(m);
          });
          rebuildFeed(); renderChatHead();
        });
      }
    }).then(function () {
      connect();
      pollNet();
      setInterval(pollNet, 2000);
      pollMem();
      setInterval(pollMem, 5000);
      setInterval(pollSnap, 12000);
      setInterval(watchStreams, 4000);
      setInterval(watchSse, 5000);
    }).catch(function (e) { toast("初始化失败：" + e.message, "err"); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
