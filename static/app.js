/* 天文观测窗口规划台 — 前端逻辑（原生 JS，无框架） */
"use strict";

/* ================================================================ 全局状态 */
const state = {
  targets: [],          // {id, name, ra, dec, exposure, priority}
  calibrations: [],     // {id, label, start, end}（ISO UTC 字符串）
  pins: {},             // targetId -> {start_min}（手动拖放/缩放产生的固定时段）
  result: null,         // /api/compute 返回结果
  busy: false,
};

const $ = (sel) => document.querySelector(sel);
const SVGNS = "http://www.w3.org/2000/svg";

/* ================================================================ 工具函数 */
function uid(prefix) {
  return prefix + Math.random().toString(36).slice(2, 9);
}

function hsl(i) {
  return `hsl(${(i * 47) % 360}, 72%, 52%)`;
}
function hslLight(i) {
  return `hsl(${(i * 47) % 360}, 60%, 30%)`;
}

function fmtHM(minute, gridStart) {
  const d = new Date(gridStart.getTime() + minute * 60000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}
function isoToLocalInput(iso) {
  // "2026-09-24T12:00Z" -> "2026-09-24T12:00"（按 UTC 显示）
  return iso.replace("Z", "");
}
function localInputToIso(v) {
  // datetime-local 值按 UTC 解释
  return v ? v + "Z" : "";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

function setStatus(kind, text) {
  const el = $("#status");
  el.className = "status " + kind;
  el.textContent = text;
}

/* ================================================================ 输入面板：目标 */
function addTargetRow(t) {
  t = t || {
    id: uid("t_"), name: "", ra: "", dec: "",
    exposure: 30, priority: state.targets.length + 1,
  };
  if (!state.targets.some((x) => x.id === t.id)) state.targets.push(t);

  const idx = state.targets.length;
  const row = document.createElement("div");
  row.className = "target-row";
  row.dataset.id = t.id;
  row.innerHTML = `
    <div class="t-head">
      <span class="dot" style="background:${hsl(idx - 1)}"></span>
      <input class="t-name" placeholder="目标名称" />
      <span class="pin-badge" hidden>已固定</span>
      <button class="icon-btn" title="删除">✕</button>
    </div>
    <div class="t-grid">
      <label>赤经<input class="f-ra" placeholder="00h42m44s" /></label>
      <label>赤纬<input class="f-dec" placeholder="41d16m" /></label>
      <label>曝光(分)<input class="f-exp" type="number" min="1" step="1" /></label>
      <label>优先级<input class="f-pri" type="number" step="1" /></label>
    </div>`;
  const bind = (cls, key) => {
    const input = row.querySelector(cls);
    input.value = t[key] ?? "";
    input.addEventListener("input", () => {
      t[key] = input.type === "number" ? Number(input.value) : input.value;
      input.classList.toggle("invalid", false);
      scheduleRecompute();
    });
  };
  row.querySelector(".t-name").value = t.name;
  row.querySelector(".t-name").addEventListener("input", (e) => {
    t.name = e.target.value;
  });
  bind(".f-ra", "ra");
  bind(".f-dec", "dec");
  bind(".f-exp", "exposure");
  bind(".f-pri", "priority");
  row.querySelector(".icon-btn").addEventListener("click", () => {
    state.targets = state.targets.filter((x) => x.id !== t.id);
    delete state.pins[t.id];
    row.remove();
    refreshPinBadges();
    scheduleRecompute();
  });
  $("#target-rows").appendChild(row);
  refreshPinBadges();
}

function refreshPinBadges() {
  document.querySelectorAll(".target-row").forEach((row) => {
    const badge = row.querySelector(".pin-badge");
    if (badge) badge.hidden = !state.pins[row.dataset.id];
  });
}

/* ============================================================== 输入面板：校准 */
function addCalRow(c) {
  c = c || { id: uid("c_"), label: "", start: "", end: "" };
  if (!state.calibrations.some((x) => x.id === c.id)) state.calibrations.push(c);

  const row = document.createElement("div");
  row.className = "cal-row";
  row.dataset.id = c.id;
  row.innerHTML = `
    <div class="c-head">
      <span>🛠️</span>
      <input class="c-label" placeholder="校准名称，如：平场校准" style="flex:1" />
      <button class="icon-btn" title="删除">✕</button>
    </div>
    <div class="c-grid">
      <label>开始 (UTC)<input class="c-start" type="datetime-local" step="60" /></label>
      <label>结束 (UTC)<input class="c-end" type="datetime-local" step="60" /></label>
    </div>`;
  row.querySelector(".c-label").value = c.label;
  row.querySelector(".c-label").addEventListener("input", (e) => {
    c.label = e.target.value;
    scheduleRecompute();
  });
  for (const [cls, key] of [[".c-start", "start"], [".c-end", "end"]]) {
    const input = row.querySelector(cls);
    if (c[key]) input.value = isoToLocalInput(c[key]);
    input.addEventListener("input", () => {
      c[key] = localInputToIso(input.value);
      scheduleRecompute();
    });
  }
  row.querySelector(".icon-btn").addEventListener("click", () => {
    state.calibrations = state.calibrations.filter((x) => x.id !== c.id);
    row.remove();
    scheduleRecompute();
  });
  $("#cal-rows").appendChild(row);
}

/* ================================================================ 收集输入 */
function collectPayload() {
  return {
    site: {
      lat: $("#site-lat").value.trim(),
      lon: $("#site-lon").value.trim(),
    },
    start_date: $("#start-date").value,
    end_date: $("#end-date").value,
    twilight_mode: $("#twilight-mode").value,
    min_altitude: Number($("#min-alt").value),
    max_airmass: Number($("#max-x").value),
    targets: state.targets.map((t) => ({
      id: t.id, name: t.name || t.id, ra: t.ra, dec: t.dec,
      exposure_minutes: t.exposure, priority: t.priority,
    })),
    calibrations: state.calibrations.map((c) => ({
      id: c.id, label: c.label || "校准", start: c.start, end: c.end,
    })),
    pins: state.pins,
  };
}

/* ================================================================ 计算调度 */
let recomputeTimer = null;
function scheduleRecompute() {
  clearTimeout(recomputeTimer);
  recomputeTimer = setTimeout(() => recompute(), 350);
}

let queuedRecompute = false;
async function recompute(showBusy = true) {
  // 上一次计算尚未完成时排队，结束后用最新输入再算一次，避免丢失拖放结果
  if (state.busy) {
    queuedRecompute = true;
    return;
  }
  state.busy = true;
  if (showBusy) setStatus("busy", "计算中…");
  try {
    state.result = await api("/api/compute", {
      method: "POST",
      body: JSON.stringify(collectPayload()),
    });
    setStatus("ok", "已重算 " + new Date().toLocaleTimeString());
    renderAll();
  } catch (err) {
    setStatus("err", err.message);
  } finally {
    state.busy = false;
    if (queuedRecompute) {
      queuedRecompute = false;
      recompute(showBusy);
    }
  }
}

/* ================================================================ 时间线布局常量 */
const TL = {
  labelW: 132,
  headH: 26,
  calH: 22,
  laneH: 30,
  padR: 12,
};

function tlGeometry() {
  const n = state.result.grid.minutes;
  const days = Math.round(n / 1440);
  const pxPerMin = 1.05;                 // 约 1500px/天
  const plotW = Math.max(600, n * pxPerMin);
  const width = TL.labelW + plotW + TL.padR;
  const lanes = state.result.targets.length;
  const height = TL.headH + TL.calH + lanes * TL.laneH + 10;
  return { n, days, pxPerMin, plotW, width, height, lanes };
}
const xOf = (g, minute) => TL.labelW + minute * g.pxPerMin;
const calY = TL.headH;
const laneY = (i) => TL.headH + TL.calH + i * TL.laneH + 2;
const gridStartDate = () => new Date(state.result.grid.start);

/* ================================================================ 时间线渲染 */
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function renderTimeline() {
  const r = state.result;
  const g = tlGeometry();
  const svg = $("#timeline");
  svg.innerHTML = "";
  svg.setAttribute("width", g.width);
  svg.setAttribute("height", g.height);
  svg.setAttribute("viewBox", `0 0 ${g.width} ${g.height}`);

  /* ---- 背景 ---- */
  svg.appendChild(svgEl("rect", {
    x: 0, y: 0, width: g.width, height: g.height, fill: "#0a0f22",
  }));

  /* ---- 太阳分层带（画在每个目标泳道内 + 校准行）---- */
  const bandColors = {
    night: "#0a0f22", astronomical: "#101a3d",
    nautical: "#1c2c5c", civil: "#2d4078",
  };
  const drawBands = (y, h) => {
    for (const key of ["civil", "nautical", "astronomical", "night"]) {
      for (const b of r.sun_bands[key] || []) {
        svg.appendChild(svgEl("rect", {
          class: "sun-band",
          x: xOf(g, b.start_min), y,
          width: (b.end_min - b.start_min) * g.pxPerMin, height: h,
          fill: bandColors[key],
        }));
      }
    }
  };

  /* ---- 刻度 + 日期分隔 ---- */
  for (let day = 0; day <= g.days; day++) {
    const x = xOf(g, day * 1440);
    svg.appendChild(svgEl("line", {
      x1: x, y1: 0, x2: x, y2: g.height, class: "day-sep",
    }));
    if (day < g.days) {
      const d = new Date(gridStartDate().getTime() + day * 86400000);
      const t = svgEl("text", {
        x: x + 6, y: 14, class: "tick-label",
      });
      t.textContent = `UTC ${d.toISOString().slice(0, 10)}`;
      svg.appendChild(t);
    }
  }
  for (let m = 0; m <= g.n; m += 60) {
    const x = xOf(g, m);
    const major = m % 360 === 0;
    svg.appendChild(svgEl("line", {
      x1: x, x2: x,
      y1: TL.headH - (major ? 6 : 3), y2: g.height,
      class: major ? "tick-major" : "tick-minor",
    }));
    if (major && m < g.n) {
      const hh = String(Math.floor((m % 1440) / 60)).padStart(2, "0");
      const t = svgEl("text", { x: x + 2, y: TL.headH - 8, class: "tick-label" });
      t.textContent = hh;
      svg.appendChild(t);
    }
  }

  /* ---- 左侧标签栏背景 ---- */
  svg.appendChild(svgEl("rect", {
    x: 0, y: 0, width: TL.labelW, height: g.height, fill: "#0d1428",
  }));
  const hdr = svgEl("text", { x: 8, y: 17, class: "row-label", "font-weight": 700 });
  hdr.textContent = "目标 / 校准";
  svg.appendChild(hdr);

  /* ---- 校准行 ---- */
  drawBands(calY, TL.calH - 2);
  svg.appendChild(svgEl("line", {
    x1: 0, y1: calY + TL.calH, x2: g.width, y2: calY + TL.calH, class: "row-sep",
  }));
  const calLbl = svgEl("text", { x: 8, y: calY + 15, class: "row-label" });
  calLbl.textContent = "校准（锁定）";
  svg.appendChild(calLbl);
  for (const c of r.calibrations) {
    const x = xOf(g, c.start_min), w = (c.end_min - c.start_min) * g.pxPerMin;
    const rect = svgEl("rect", {
      x, y: calY + 2, width: Math.max(2, w), height: TL.calH - 6,
      rx: 3, fill: "#8a6d1f", stroke: "#ffce6b", "stroke-width": 1,
    });
    svg.appendChild(rect);
    if (w > 14) {
      const t = svgEl("text", { x: x + 4, y: calY + 15, class: "block-label" });
      t.textContent = c.label;
      svg.appendChild(t);
    }
    attachTooltip(rect, () => [
      `<div class="tt-title">${escapeHtml(c.label)}（固定校准）</div>`,
      `<div class="tt-row"><b>${c.start.slice(11, 16)}–${c.end.slice(11, 16)}</b> UTC</div>`,
      `<div class="tt-row">时长 <b>${c.end_min - c.start_min}</b> 分钟 · 不可被占用</div>`,
    ].join(""));
  }

  /* ---- 每个目标一条泳道 ---- */
  r.targets.forEach((t, i) => {
    const y = laneY(i);
    drawBands(y, TL.laneH - 4);

    svg.appendChild(svgEl("line", {
      x1: 0, y1: y + TL.laneH, x2: g.width, y2: y + TL.laneH, class: "row-sep",
    }));
    const lbl = svgEl("text", { x: 8, y: y + 18, class: "row-label" });
    lbl.textContent = t.name.length > 11 ? t.name.slice(0, 10) + "…" : t.name;
    svg.appendChild(lbl);

    const clipId = `clip-${i}`;
    const defs = svgEl("defs");
    const cp = svgEl("clipPath", { id: clipId });
    cp.appendChild(svgEl("rect", {
      x: TL.labelW, y, width: g.plotW, height: TL.laneH - 4,
    }));
    defs.appendChild(cp);
    svg.appendChild(defs);

    // 可行窗口带
    for (const win of t.windows) {
      svg.appendChild(svgEl("rect", {
        class: "window-band",
        x: xOf(g, win.start_min), y: y + 2,
        width: (win.end_min - win.start_min) * g.pxPerMin,
        height: TL.laneH - 8, rx: 2,
      }));
    }

    // 高度曲线（缩放到泳道高度，仅画高度 >=0 部分）
    if (t.samples.length > 1) {
      const maxAlt = 90;
      const pts = t.samples.map((s) => {
        const norm = Math.max(0, Math.min(maxAlt, s.alt)) / maxAlt;
        const py = y + (TL.laneH - 4) - norm * (TL.laneH - 8);
        return `${xOf(g, s.minute).toFixed(1)},${py.toFixed(1)}`;
      }).join(" ");
      svg.appendChild(svgEl("polyline", {
        points: pts, class: "alt-curve", "clip-path": `url(#${clipId})`,
      }));
    }
  });

  /* ---- 已安排色块（在所有泳道画完后叠放，保证在最上层）---- */
  r.scheduled.forEach((s) => {
    const ti = r.targets.findIndex((t) => t.id === s.target_id);
    if (ti < 0) return;
    drawScheduledBlock(svg, g, s, r.targets[ti], ti, r);
  });
}

function drawScheduledBlock(svg, g, s, targetInfo, laneIdx, r, liveOverride) {
  const y = laneY(laneIdx);
  const start = liveOverride ? liveOverride.start_min : s.start_min;
  const end = liveOverride ? liveOverride.end_min : s.end_min;
  const x = xOf(g, start);
  const w = Math.max(3, (end - start) * g.pxPerMin);
  const invalid = liveOverride && liveOverride.invalid;
  const pinned = liveOverride ? liveOverride.pinned : s.pinned;
  const color = hsl(laneIdx);

  const gEl = svgEl("g", {
    class: "block" + (pinned ? " pinned" : "") + (invalid ? " invalid" : "") +
           (liveOverride ? " block-drag" : ""),
    "data-target-id": s.target_id,
  });
  const rect = svgEl("rect", {
    x, y: y + 3, width: w, height: TL.laneH - 9, rx: 4,
    fill: color, stroke: pinned ? "#ffd866" : "rgba(255,255,255,.35)",
    "stroke-width": pinned ? 1.6 : 1,
    opacity: 0.92,
  });
  gEl.appendChild(rect);

  // 左缘固定标记
  if (pinned) {
    gEl.appendChild(svgEl("rect", {
      x, y: y + 3, width: 3, height: TL.laneH - 9, fill: "#ffd866",
    }));
  }

  const clipId = `clip-b-${s.target_id}`;
  const defs = svgEl("defs");
  const cp = svgEl("clipPath", { id: clipId });
  cp.appendChild(svgEl("rect", { x: x + 2, y, width: Math.max(0, w - 12), height: TL.laneH }));
  defs.appendChild(cp);
  svg.appendChild(defs);
  gEl.setAttribute("text-clip", clipId);

  const label = svgEl("text", { x: x + 6, y: y + 15, class: "block-label" });
  label.textContent = targetInfo.name;
  label.setAttribute("clip-path", `url(#${clipId})`);
  gEl.appendChild(label);
  const tm = svgEl("text", { x: x + 6, y: y + 25, class: "block-time" });
  tm.textContent = `${fmtHM(start, gridStartDate())} · ${end - start}m`;
  tm.setAttribute("clip-path", `url(#${clipId})`);
  gEl.appendChild(tm);

  // 右缘拖缩手柄
  const handle = svgEl("rect", {
    class: "resize-handle",
    x: x + w - 4, y: y + 3, width: 6, height: TL.laneH - 9, rx: 2,
  });
  gEl.appendChild(handle);

  // 拖放事件
  gEl.addEventListener("pointerdown", (e) =>
    startDrag(e, { type: "target", sched: s, laneIdx, targetInfo,
                   mode: e.target === handle ? "resize" : "move" }));

  attachTooltip(gEl, () => {
    const curStart = start, curEnd = end;
    return [
      `<div class="tt-title">${escapeHtml(targetInfo.name)}</div>`,
      `<div class="tt-row"><b>${fmtHM(curStart, gridStartDate())} – ${fmtHM(curEnd, gridStartDate())}</b> UTC</div>`,
      `<div class="tt-row">曝光 <b>${curEnd - curStart}</b> 分钟 · 优先级 <b>${targetInfo.priority}</b></div>`,
      `<div class="tt-row">全天最高高度 <b>${targetInfo.best_altitude}°</b> · 最佳空气质量 <b>${targetInfo.best_airmass ?? "—"}</b></div>`,
      `<div class="tt-row">${pinned ? "🟡 手动固定（重算时保持）" : "🟢 自动排程（可拖动）"}</div>`,
      `<div class="tt-row" style="margin-top:3px">拖动=平移 · 拖右缘=改时长</div>`,
    ].join("");
  });

  svg.appendChild(gEl);
  return gEl;
}

/* ================================================================ 拖动逻辑 */
let drag = null;

function startDrag(e, spec) {
  e.preventDefault();
  e.stopPropagation();
  const svg = $("#timeline");
  svg.setPointerCapture(e.pointerId);
  drag = {
    ...spec,
    pointerId: e.pointerId,
    startPointerMin: minuteFromEvent(e.clientX),
    origStart: spec.sched.start_min,
    origEnd: spec.sched.end_min,
    live: null,
  };
  svg.addEventListener("pointermove", onDragMove);
  svg.addEventListener("pointerup", onDragUp, { once: true });
  $("#drag-hint").textContent = "拖动中…";
}

function minuteFromEvent(clientX) {
  const g = tlGeometry();
  const rect = $("#timeline").getBoundingClientRect();
  const scale = g.width / rect.width;
  const xSvg = (clientX - rect.left) * scale;
  return Math.round((xSvg - TL.labelW) / g.pxPerMin);
}

function findClampWindow(targetId, start, end) {
  const t = state.result.targets.find((x) => x.id === targetId);
  if (!t) return null;
  // 要求整个 [start,end) 落在某一可行窗口内
  const fit = t.windows.find((w) => w.start_min <= start && end <= w.end_min);
  if (fit) return fit;
  // 否则返回夹住它的窗口（用于显示非法）
  return t.windows.find((w) => w.start_min < end && start < w.end_min) || null;
}

function hasCollision(targetId, start, end) {
  const busy = state.result.scheduled
    .filter((s) => s.target_id !== targetId)
    .concat(state.result.calibrations.map((c) => ({
      start_min: c.start_min, end_min: c.end_min,
    })));
  return busy.some((b) => start < b.end_min && b.start_min < end);
}

function onDragMove(e) {
  if (!drag) return;
  const delta = minuteFromEvent(e.clientX) - drag.startPointerMin;
  let start = drag.origStart, end = drag.origEnd;
  if (drag.mode === "move") {
    start = drag.origStart + delta;
    end = drag.origEnd + delta;
  } else {
    end = drag.origEnd + delta;
  }
  // 最短 1 分钟
  if (end - start < 1) end = start + 1;

  const win = findClampWindow(drag.sched.target_id, start, end);
  let valid = !!win && win.start_min <= start && end <= win.end_min &&
              !hasCollision(drag.sched.target_id, start, end);

  // 若越出可行窗口，自动夹回窗口内（保持时长）
  let hint = "";
  if (win && drag.mode === "move") {
    const dur = end - start;
    if (start < win.start_min) { start = win.start_min; end = start + dur; }
    if (end > win.end_min) { end = win.end_min; start = end - dur; }
  } else if (win && drag.mode === "resize") {
    if (end > win.end_min) end = win.end_min;
  }
  const coll = hasCollision(drag.sched.target_id, start, end);
  if (coll) {
    valid = false;
    hint = "⚠ 与其他观测/校准时段重叠，松开将还原";
  }
  if (!win) {
    valid = false;
    hint = "⚠ 已离开该目标的可行观测窗口（高度/暮光/空气质量不满足），松开将还原";
  }
  $("#drag-hint").textContent = hint ||
    `预览：${fmtHM(start, gridStartDate())} – ${fmtHM(end, gridStartDate())} UTC（${end - start} 分钟）`;

  drag.live = { start_min: start, end_min: end, invalid: !valid,
                pinned: true };
  renderTimelineLive();
}

/* 拖动过程中：仅平移/缩放被拖色块，其他元素保持不动（保留 pointer capture） */
function renderTimelineLive() {
  if (!drag || !drag.live) return;
  const g = tlGeometry();
  const el = document.querySelector(
    `#timeline g.block[data-target-id="${CSS.escape(drag.sched.target_id)}"]`);
  if (!el) return;
  const live = drag.live;
  const dx = (live.start_min - drag.sched.start_min) * g.pxPerMin;
  const newW = Math.max(3, (live.end_min - live.start_min) * g.pxPerMin);
  el.setAttribute("transform", `translate(${dx.toFixed(1)},0)`);
  el.classList.add("block-drag");
  el.classList.toggle("invalid", !!live.invalid);

  const mainRect = el.querySelector("rect:not(.resize-handle)");
  if (mainRect) mainRect.setAttribute("width", newW);
  const handle = el.querySelector(".resize-handle");
  if (handle) handle.setAttribute("x", newW - 4);
  // 时间文字随尺寸更新（文字随 group 一起平移）
  const texts = el.querySelectorAll("text");
  if (texts[1]) {
    texts[1].textContent =
      `${fmtHM(live.start_min, gridStartDate())} · ${live.end_min - live.start_min}m`;
  }
  // clipPath 在被平移的 group 内，x 不变，只更新宽度
  const clipRect = el.querySelector("clipPath rect");
  if (clipRect) clipRect.setAttribute("width", Math.max(0, newW - 12));
}

async function onDragUp() {
  const svg = $("#timeline");
  svg.removeEventListener("pointermove", onDragMove);
  const d = drag;
  drag = null;
  $("#drag-hint").textContent = "";
  if (!d) return;

  if (d.live && !d.live.invalid) {
    // 提交：更新 pins（曝光时长也同步到输入）
    state.pins[d.sched.target_id] = { start_min: d.live.start_min };
    const t = state.targets.find((x) => x.id === d.sched.target_id);
    if (t && d.mode === "resize") {
      t.exposure = d.live.end_min - d.live.start_min;
      syncTargetInputs();
    }
    refreshPinBadges();
    await recompute(false);
    setStatus("ok", "已按拖动重算 " + new Date().toLocaleTimeString());
  } else {
    renderTimeline();  // 非法落点 -> 还原
    setStatus("err", "落点无效，已还原");
    setTimeout(() => {
      if (state.result) setStatus("ok", "就绪");
    }, 2000);
  }
}

function syncTargetInputs() {
  document.querySelectorAll(".target-row").forEach((row) => {
    const t = state.targets.find((x) => x.id === row.dataset.id);
    if (!t) return;
    row.querySelector(".f-exp").value = t.exposure;
  });
}

/* ================================================================ Tooltip */
let tipEl = null;
function attachTooltip(el, htmlFn) {
  el.addEventListener("mouseenter", () => {
    tipEl = $("#tooltip");
    tipEl.innerHTML = htmlFn();
    tipEl.hidden = false;
  });
  el.addEventListener("mousemove", (e) => {
    if (!tipEl) return;
    const pad = 14;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + 280 > window.innerWidth) x = e.clientX - 290;
    if (y + 120 > window.innerHeight) y = e.clientY - 130;
    tipEl.style.left = x + "px";
    tipEl.style.top = y + "px";
  });
  el.addEventListener("mouseleave", () => {
    if (tipEl) { tipEl.hidden = true; tipEl = null; }
  });
}

/* ================================================================ 表格/清单 */
function renderTables() {
  const r = state.result;
  const tbody = $("#schedule-table tbody");
  tbody.innerHTML = "";
  const priOf = Object.fromEntries(
    r.targets.map((t) => [t.id, t.priority]));
  r.scheduled.forEach((s, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>${escapeHtml(s.name)}</td>
      <td><code>${s.start.replace("T", " ").replace("Z", "")}</code></td>
      <td><code>${s.end.replace("T", " ").replace("Z", "")}</code></td>
      <td>${s.end_min - s.start_min}</td>
      <td>${priOf[s.target_id] ?? "—"}</td>
      <td>${s.pinned
        ? '<span class="badge pin">🟡 手动固定</span>'
        : '<span class="badge auto">🟢 自动</span>'}</td>`;
    tbody.appendChild(tr);
  });
  if (!r.scheduled.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-hint">尚无已安排的观测</td></tr>`;
  }

  const cbody = $("#cal-table tbody");
  cbody.innerHTML = "";
  r.calibrations.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(c.label)}</td>
      <td><code>${c.start.replace("T", " ").replace("Z", "")}</code></td>
      <td><code>${c.end.replace("T", " ").replace("Z", "")}</code></td>
      <td>${c.end_min - c.start_min}</td>`;
    cbody.appendChild(tr);
  });
  if (!r.calibrations.length) {
    cbody.innerHTML = `<tr><td colspan="4" class="empty-hint">未设置固定校准时段</td></tr>`;
  }

  const box = $("#unscheduled-list");
  box.innerHTML = "";
  if (!r.unscheduled.length) {
    box.innerHTML = `<div class="empty-hint">✅ 全部目标均已安排。</div>`;
  } else {
    for (const u of r.unscheduled) {
      const div = document.createElement("div");
      div.className = "unsched-item";
      div.innerHTML = `<b>${escapeHtml(u.name)}</b><div class="why">${escapeHtml(u.reason)}</div>`;
      box.appendChild(div);
    }
  }

  const wbox = $("#warnings");
  if (r.warnings && r.warnings.length) {
    wbox.hidden = false;
    wbox.innerHTML = `<b>⚠ 提示：</b><ul>${
      r.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
  } else {
    wbox.hidden = true;
    wbox.innerHTML = "";
  }

  const eff = r.constraints;
  $("#effective-hint").innerHTML =
    `空气质量 ≤ <code>${eff.max_airmass}</code> 等效要求高度 ≥ <code>${eff.effective_min_altitude}°</code>，` +
    `实际生效最低高度 <code>${Math.max(eff.min_altitude, eff.effective_min_altitude)}°</code>。`;
}

function renderAll() {
  if (!state.result) return;
  renderTimeline();
  renderTables();
}

/* ================================================================ 版本管理 */
async function loadVersions() {
  const box = $("#version-list");
  try {
    const versions = await api("/api/versions");
    box.innerHTML = "";
    if (!versions.length) {
      box.innerHTML = `<div class="empty-hint">暂无保存版本</div>`;
      return;
    }
    for (const v of versions) {
      const div = document.createElement("div");
      div.className = "version-item";
      div.innerHTML = `
        <div class="v-meta">
          <div class="v-label">${escapeHtml(v.label)}</div>
          <div class="v-date">${v.created_at.replace("T", " ").replace("Z", " UTC")}</div>
        </div>
        <button class="btn small v-load">载入</button>
        <button class="btn small danger v-del">删除</button>`;
      div.querySelector(".v-load").addEventListener("click", () => loadVersion(v.id));
      div.querySelector(".v-del").addEventListener("click", () => deleteVersion(v.id));
      box.appendChild(div);
    }
    refreshVerifyVersionOptions(versions);
  } catch (err) {
    box.innerHTML = `<div class="empty-hint">版本加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function applyRequest(req) {
  $("#site-lat").value = req.site?.lat ?? "";
  $("#site-lon").value = req.site?.lon ?? "";
  $("#start-date").value = req.start_date ?? "";
  $("#end-date").value = req.end_date ?? "";
  $("#twilight-mode").value = req.twilight_mode ?? "night";
  $("#min-alt").value = req.min_altitude ?? 20;
  $("#max-x").value = req.max_airmass ?? 2.5;
  state.targets = [];
  $("#target-rows").innerHTML = "";
  (req.targets || []).forEach((t) => addTargetRow({
    id: t.id, name: t.name, ra: t.ra, dec: t.dec,
    exposure: t.exposure_minutes, priority: t.priority,
  }));
  state.calibrations = [];
  $("#cal-rows").innerHTML = "";
  (req.calibrations || []).forEach((c) =>
    addCalRow({ id: c.id, label: c.label, start: c.start, end: c.end }));
  state.pins = req.pins && typeof req.pins === "object" ? { ...req.pins } : {};
  refreshPinBadges();
}

async function loadVersion(id) {
  try {
    const v = await api(`/api/versions/${id}`);
    applyRequest(v.request);
    state.result = v.result;
    renderAll();
    // 核验面板同步选中该版本（清空旧版本的实测记录与结果）
    vstate.versionId = id;
    vstate.version = v;
    $("#verify-version").value = id;
    $("#verify-rows").innerHTML = "";
    $("#verify-results").innerHTML = "";
    setStatus("ok", `已载入版本：${v.label}`);
  } catch (err) {
    setStatus("err", err.message);
  }
}

async function deleteVersion(id) {
  if (!confirm("确定删除该版本？")) return;
  try {
    await api(`/api/versions/${id}`, { method: "DELETE" });
    await loadVersions();
  } catch (err) {
    setStatus("err", err.message);
  }
}

/* ================================================================ 实测核验 */
const vstate = {
  versionId: "",      // 当前选中的核验版本 id
  version: null,      // 该版本完整详情（含 result，只读使用）
};

function refreshVerifyVersionOptions(versions) {
  const sel = $("#verify-version");
  sel.innerHTML = `<option value="">— 请选择已保存版本 —</option>` +
    versions.map((v) =>
      `<option value="${v.id}">${escapeHtml(v.label)}（${v.created_at.slice(0, 16).replace("T", " ")}）</option>`
    ).join("");
  if (vstate.versionId && versions.some((v) => v.id === vstate.versionId)) {
    sel.value = vstate.versionId;
  } else {
    sel.value = "";
    vstate.versionId = "";
    vstate.version = null;
    $("#verify-rows").innerHTML = "";
    $("#verify-results").innerHTML = "";
  }
}

function addVerifyRow(rec) {
  if (!vstate.version) {
    setStatus("err", "请先在核验卡片中选择已保存版本");
    return;
  }
  rec = rec || { target_id: "", start: "", end: "" };
  const targets = vstate.version.result.targets || [];
  const row = document.createElement("div");
  row.className = "verify-row";
  row.innerHTML = `
    <select class="v-target">${targets.map((t) =>
      `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`).join("")}</select>
    <input class="v-start" type="datetime-local" step="60" title="开始（UTC 整分钟）" />
    <input class="v-end" type="datetime-local" step="60" title="结束（UTC 整分钟）" />`;
  const del = document.createElement("button");
  del.className = "icon-btn";
  del.title = "删除";
  del.textContent = "✕";
  del.addEventListener("click", () => row.remove());
  row.appendChild(del);
  if (rec.target_id) row.querySelector(".v-target").value = rec.target_id;
  if (rec.start) row.querySelector(".v-start").value = isoToLocalInput(rec.start);
  if (rec.end) row.querySelector(".v-end").value = isoToLocalInput(rec.end);
  $("#verify-rows").appendChild(row);
}

async function selectVerifyVersion(id) {
  vstate.versionId = id;
  vstate.version = null;
  $("#verify-rows").innerHTML = "";
  $("#verify-results").innerHTML = "";
  if (!id) return;
  try {
    vstate.version = await api(`/api/versions/${id}`);
    setStatus("ok", `核验版本：${vstate.version.label}`);
  } catch (err) {
    setStatus("err", err.message);
  }
}

const fmtIsoShort = (iso) => iso.slice(5, 16).replace("T", " ");

function renderVerifyResults(data) {
  const s = data.summary;
  let html = `<div class="vres-summary">核验版本 <b>${escapeHtml(data.label)}</b>：` +
    `<b>${s.target_count}</b> 个目标、<b>${s.record_count}</b> 条实测记录，` +
    `<b>${s.complete_count}</b> 个达标、<b>${s.incomplete_count}</b> 个未达标` +
    `（${data.generated_at} 核验，只读不改动版本）。</div>`;
  for (const r of data.results) {
    const ok = r.complete;
    html += `<div class="vres-item${ok ? "" : " fail"}">` +
      `<div class="vres-head">${ok ? "✅" : "❌"} <b>${escapeHtml(r.name)}</b>` +
      `<span class="badge ${ok ? "done" : "miss"}">${ok ? "完成" : "未完成"}</span></div>` +
      `<div class="vres-nums">原定曝光 <b>${r.planned_minutes}</b> 分 · ` +
      `实测 <b>${r.measured_minutes}</b> 分（${r.record_count} 条记录）· ` +
      `有效 <b>${r.effective_minutes}</b> 分 · ` +
      (r.scheduled
        ? `原定排程 <code>${fmtIsoShort(r.scheduled.start)}–${fmtIsoShort(r.scheduled.end)}</code>`
        : `原版本未排上该目标`) +
      `</div>`;
    if (r.excluded.length) {
      html += `<ul class="vres-excl">` + r.excluded.map((x) =>
        `<li>剔除 <code>${fmtIsoShort(x.start)}–${fmtIsoShort(x.end)}</code>` +
        `（${x.minutes} 分）：${escapeHtml(x.reason)}</li>`).join("") + `</ul>`;
    }
    html += `</div>`;
  }
  $("#verify-results").innerHTML = html;
}

async function runVerify() {
  if (!vstate.versionId) {
    setStatus("err", "请先选择要核验的已保存版本");
    return;
  }
  const records = [];
  for (const row of document.querySelectorAll("#verify-rows .verify-row")) {
    records.push({
      target_id: row.querySelector(".v-target").value,
      start: localInputToIso(row.querySelector(".v-start").value),
      end: localInputToIso(row.querySelector(".v-end").value),
    });
  }
  if (!records.length) {
    setStatus("err", "请至少添加一条实测记录");
    return;
  }
  setStatus("busy", "核验中…");
  try {
    const data = await api(`/api/versions/${vstate.versionId}/verify`, {
      method: "POST",
      body: JSON.stringify({ records }),
    });
    renderVerifyResults(data);
    setStatus("ok",
      `核验完成：${data.summary.complete_count}/${data.summary.target_count} 个目标达标`);
  } catch (err) {
    $("#verify-results").innerHTML =
      `<div class="verify-error">❌ 核验被拒绝（整批未执行）：${escapeHtml(err.message)}</div>`;
    setStatus("err", err.message);
  }
}

/* ================================================================ 模态框 */
function promptModal(title, placeholder) {
  return new Promise((resolve) => {
    $("#modal-title").textContent = title;
    const input = $("#modal-input");
    input.value = "";
    input.placeholder = placeholder || "";
    $("#modal-backdrop").hidden = false;
    input.focus();
    const done = (val) => {
      $("#modal-backdrop").hidden = true;
      input.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Enter") done(input.value);
      if (e.key === "Escape") done(null);
    };
    $("#modal-ok").onclick = () => done(input.value);
    $("#modal-cancel").onclick = () => done(null);
    input.addEventListener("keydown", onKey);
  });
}

function downloadJson(filename, obj) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/* ================================================================ 初始化 */
function init() {
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  $("#start-date").value = iso(today);
  const tomorrow = new Date(today.getTime() + 86400000);
  $("#end-date").value = iso(tomorrow);

  // 默认目标（秋夜星空中可视性良好的几个天体）
  addTargetRow({ id: uid("t_"), name: "M31 仙女座星系", ra: "00h42m44s", dec: "41d16m09s", exposure: 45, priority: 1 });
  addTargetRow({ id: uid("t_"), name: "织女星 Vega", ra: "18h36m56s", dec: "38d47m01s", exposure: 30, priority: 2 });
  addTargetRow({ id: uid("t_"), name: "天狼星 Sirius", ra: "06h45m09s", dec: "-16d42m58s", exposure: 60, priority: 1 });

  // 默认校准：黄昏平场（UTC 时间，用户可自行修改）
  const day = iso(today);
  addCalRow({ id: uid("c_"), label: "黄昏平场", start: `${day}T11:20Z`, end: `${day}T11:50Z` });
  addCalRow({ id: uid("c_"), label: "夜间定标", start: `${day}T15:00Z`, end: `${day}T15:20Z` });

  $("#btn-add-target").addEventListener("click", () => addTargetRow());
  $("#btn-add-cal").addEventListener("click", () => addCalRow());
  $("#btn-recompute").addEventListener("click", () => recompute());
  $("#btn-clear-pins").addEventListener("click", async () => {
    state.pins = {};
    refreshPinBadges();
    await recompute(false);
    setStatus("ok", "已清除全部手动固定并重新自动排程");
  });
  $("#btn-refresh-versions").addEventListener("click", loadVersions);

  // 实测曝光核验
  $("#verify-version").addEventListener("change", (e) => selectVerifyVersion(e.target.value));
  $("#btn-verify-add").addEventListener("click", () => addVerifyRow());
  $("#btn-verify-fill").addEventListener("click", () => {
    if (!vstate.version) {
      setStatus("err", "请先选择要核验的已保存版本");
      return;
    }
    const sched = vstate.version.result.scheduled || [];
    $("#verify-rows").innerHTML = "";
    for (const s of sched) {
      addVerifyRow({ target_id: s.target_id, start: s.start, end: s.end });
    }
    setStatus(sched.length ? "ok" : "err",
      sched.length ? `已填入 ${sched.length} 条原定排程，可直接核验或修改`
                   : "该版本没有已安排的观测时段");
  });
  $("#btn-verify-run").addEventListener("click", runVerify);

  $("#btn-save").addEventListener("click", async () => {
    const label = await promptModal("保存版本", "版本名称（留空则使用时间戳）");
    if (label === null) return;
    setStatus("busy", "保存中…");
    try {
      const v = await api("/api/versions", {
        method: "POST",
        body: JSON.stringify({ label, request: collectPayload() }),
      });
      state.result = v.result;
      renderAll();
      setStatus("ok", `版本已保存：${v.label}`);
      await loadVersions();
    } catch (err) {
      setStatus("err", err.message);
    }
  });

  $("#btn-export").addEventListener("click", async () => {
    try {
      const doc = await api("/api/export", {
        method: "POST",
        body: JSON.stringify(collectPayload()),
      });
      const name = `observation-schedule-${state.result.grid.start.slice(0, 10)}.json`;
      downloadJson(name, doc);
      setStatus("ok", "时间表 JSON 已导出");
    } catch (err) {
      setStatus("err", err.message);
    }
  });

  // 表单顶部参数变化 -> 防抖重算
  for (const id of ["site-lat", "site-lon", "start-date", "end-date",
                    "twilight-mode", "min-alt", "max-x"]) {
    $("#" + id).addEventListener("change", () => recompute());
  }

  loadVersions();
  recompute();
}

document.addEventListener("DOMContentLoaded", init);
