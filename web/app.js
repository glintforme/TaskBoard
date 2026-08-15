/* 悬浮任务板 · 后台设置页逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);
const SCOPE_LABEL = { daily: "日常任务", today: "今日任务", tomorrow: "明日任务", week: "周常任务", month: "月常任务" };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function iso(d) {
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
const TODAY = iso(new Date());
const TOMORROW = iso(new Date(Date.now() + 86400000));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || ("请求失败 HTTP " + res.status));
  }
  return data;
}

let toastTimer = null;
function toast(msg, isErr) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

function setOnline(ok) {
  const pill = $("statusPill");
  pill.className = "status-pill " + (ok ? "online" : "offline");
  $("statusText").textContent = ok ? "服务已连接" : "服务未连接";
}

// ---------------- 标签页 ----------------
const TAB_LOAD = {
  today: () => loadScopeTab("today"),
  tomorrow: () => loadScopeTab("tomorrow"),
  week: () => loadScopeTab("week"),
  month: () => loadScopeTab("month"),
  all: () => loadAll(),
  history: () => loadHistory(),
  ai: () => loadAiPage(),
  settings: () => loadSettings()
};

function goTab(name) {
  location.hash = name;
}

function applyTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  const panel = $("panel-" + name);
  if (panel) panel.classList.add("active");
  if (TAB_LOAD[name]) TAB_LOAD[name]();
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", () => {
  const name = (location.hash || "#today").replace("#", "");
  applyTab(TAB_LOAD[name] ? name : "today");
});

// ---------------- 统计与状态 ----------------
async function loadStats() {
  try {
    const d = await api("/api/stats");
    $("stToday").textContent = d.stats.today_completed;
    $("stOverdue").textContent = d.stats.overdue_count;
    $("stTotal").textContent = d.stats.total_completed;
    $("stTasks").textContent = d.stats.total_tasks;
    $("todayLabel").textContent = "今天是 " + fmtCN(new Date()) + " · 数据实时同步桌面悬浮窗";
    setOnline(true);
  } catch (e) {
    setOnline(false);
  }
}

function fmtCN(d) {
  const wk = ["日", "一", "二", "三", "四", "五", "六"][d.getDay()];
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 星期${wk}`;
}

async function todayDoneSet() {
  try {
    const d = await api("/api/completions?date=" + TODAY + "&limit=10000");
    return new Set(d.completions.map((c) => c.task_id));
  } catch (e) { return new Set(); }
}

// ---------------- 任务渲染 ----------------
function taskCard(t, doneToday) {
  const scope = t.scope || "today";
  const badge = `<span class="badge ${scope === "daily" ? "daily" : ""}">${SCOPE_LABEL[scope] || scope}</span>`;
  const meta = [];
  if (t.due_date) meta.push(`📅 ${esc(t.due_date)}`);
  if (t.start_at) meta.push(`▶ ${esc(t.start_at.replace("T", " "))}`);
  if (t.remind_at) meta.push(`🔔 ${esc(t.remind_at.replace("T", " "))}`);
  const effDeadline = t.effective_deadline || t.deadline;
  if (effDeadline) {
    const overdue = !doneToday && effDeadline < nowLocal();
    meta.push(`<span class="${overdue ? "overdue" : ""}">⏰ 截止 ${esc(effDeadline.replace("T", " "))}${overdue ? " 已逾期" : ""}</span>`);
  }
  if (t.note) meta.push(`💬 ${esc(t.note)}`);
  if (doneToday) meta.push(`<span class="badge done">✓ 今日已完成</span>`);
  return `<div class="task ${doneToday ? "completed" : ""}" id="task-${t.id}">
    <div class="main">
      <div class="t">${esc(t.title)}</div>
      <div class="meta">${badge}${meta.map((m) => `<span>${m}</span>`).join("")}</div>
    </div>
    <div class="ops">
      <button class="btn mini ${doneToday ? "done" : "primary"}" onclick="toggleComplete(${t.id})">${doneToday ? "✓ 已完成" : "完 成"}</button>
      <button class="btn mini ghost" onclick="editTask(${t.id})">编辑</button>
      <button class="btn mini danger" onclick="delTask(${t.id})">删除</button>
    </div>
  </div>`;
}

function nowLocal() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function emptyTip(txt) {
  return `<div class="empty-tip">${txt}</div>`;
}

async function loadScopeTab(key) {
  const done = await todayDoneSet();
  // 今日/明日按日期过滤；周常/月常显示该类型全部任务（悬浮窗自行过滤当周/当月）
  let query;
  if (key === "today") query = "?scope=today&due_date=" + TODAY;
  else if (key === "tomorrow") query = "?scope=tomorrow&due_date=" + TOMORROW;
  else query = "?scope=" + key;
  try {
    const d = await api("/api/tasks" + query);
    const list = $("list-" + key);
    if (!d.tasks.length) {
      list.innerHTML = emptyTip(key === "today"
        ? "今日还没有任务，在上方输入后回车添加；也可以去「AI 生成」让 AI 帮你规划 ✨"
        : "这个时间段的计划还是空的，添加上面的输入框写一条吧～");
    } else {
      list.innerHTML = d.tasks.map((t) => taskCard(t, done.has(t.id))).join("");
    }
  } catch (e) {
    toast(e.message, true);
  }
}

async function addTask(ev, key) {
  ev.preventDefault();
  const titleIn = $("in-" + key);
  const noteIn = $("note-" + key);
  const title = titleIn.value.trim();
  if (!title) return false;
  let scope = key;
  let due = key === "today" ? TODAY : (key === "tomorrow" ? TOMORROW : TODAY);
  if (key === "today") {
    const sel = $("scope-today");
    scope = sel.value; // today | daily
  }
  const remind = $("remind-" + key) ? $("remind-" + key).value || null : null;
  const deadline = $("deadline-" + key) ? $("deadline-" + key).value || null : null;
  const start = $("start-" + key) ? $("start-" + key).value || null : null;
  try {
    await api("/api/tasks", {
      method: "POST",
      body: { title, scope, due_date: due, note: noteIn.value.trim(), remind_at: remind, deadline, start_at: start }
    });
    titleIn.value = "";
    noteIn.value = "";
    if ($("remind-" + key)) $("remind-" + key).value = "";
    if ($("deadline-" + key)) $("deadline-" + key).value = "";
    if ($("start-" + key)) $("start-" + key).value = "";
    toast(remind ? "已添加任务 ✓（到点将响铃提醒）" : "已添加任务 ✓");
    loadScopeTab(key);
    loadStats();
  } catch (e) {
    toast(e.message, true);
  }
  return false;
}

// ---------------- 编辑任务 ----------------
function openEdit() {
  $("editMask").classList.add("show");
}

function closeEdit() {
  $("editMask").classList.remove("show");
}

async function editTask(id) {
  try {
    const d = await api("/api/tasks/" + id);
    const t = d.task;
    $("edit-title").value = t.title || "";
    $("edit-note").value = t.note || "";
    $("edit-scope").value = t.scope || "today";
    $("edit-start").value = t.start_at ? t.start_at.slice(0, 16) : "";
    $("edit-deadline").value = (t.deadline || "").slice(0, 16);
    $("edit-remind").value = (t.remind_at || "").slice(0, 16);
    $("editModal").dataset.id = id;
    openEdit();
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveEdit() {
  const id = $("editModal").dataset.id;
  const body = {
    title: $("edit-title").value.trim(),
    note: $("edit-note").value.trim(),
    scope: $("edit-scope").value,
    start_at: $("edit-start").value || null,
    deadline: $("edit-deadline").value || null,
    remind_at: $("edit-remind").value || null
  };
  if (!body.title) { toast("任务标题不能为空", true); return; }
  try {
    await api("/api/tasks/" + id, { method: "PUT", body });
    toast("已保存修改 ✓");
    closeEdit();
    const name = (location.hash || "#today").replace("#", "");
    if (TAB_LOAD[name]) TAB_LOAD[name]();
    loadStats();
  } catch (e) {
    toast(e.message, true);
  }
}

async function toggleComplete(id) {
  const done = await todayDoneSet();
  try {
    if (done.has(id)) {
      await api(`/api/tasks/${id}/uncomplete`, { method: "POST", body: {} });
      toast("已撤销完成");
    } else {
      await api(`/api/tasks/${id}/complete`, { method: "POST", body: {} });
      toast("任务完成 ✓ 已保存记录");
    }
    const name = (location.hash || "#today").replace("#", "");
    if (TAB_LOAD[name]) TAB_LOAD[name]();
    loadStats();
  } catch (e) {
    toast(e.message, true);
  }
}

async function delTask(id) {
  if (!confirm("确定删除该任务吗？相关完成记录也会一并删除。")) return;
  try {
    await api("/api/tasks/" + id, { method: "DELETE" });
    toast("已删除任务");
    const name = (location.hash || "#today").replace("#", "");
    if (TAB_LOAD[name]) TAB_LOAD[name]();
    loadStats();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------- 全部任务 ----------------
async function loadAll() {
  const q = encodeURIComponent($("in-all-q").value.trim());
  const scope = $("all-scope").value;
  const done = await todayDoneSet();
  try {
    const d = await api("/api/tasks?scope=" + (scope || "all") + (q ? "&q=" + q : ""));
    const list = $("list-all");
    if (!d.tasks.length) {
      list.innerHTML = emptyTip("没有匹配的任务。");
    } else {
      list.innerHTML = d.tasks.map((t) => taskCard(t, done.has(t.id))).join("");
    }
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------- 历史完成 ----------------
async function loadHistory() {
  try {
    const d = await api("/api/completions?limit=500");
    const list = $("list-history");
    $("historyHint").textContent = "共 " + d.completions.length + " 条记录";
    if (!d.completions.length) {
      list.innerHTML = emptyTip("还没有完成任务记录，加油！完成的任务会显示在这里。");
      return;
    }
    const groups = {};
    d.completions.forEach((c) => {
      (groups[c.date] = groups[c.date] || []).push(c);
    });
    list.innerHTML = Object.keys(groups).sort().reverse().map((date) => `
      <div class="hist-group">
        <h3>📅 ${esc(date)}（${groups[date].length} 条）</h3>
        ${groups[date].map((c) => `
          <div class="hist-row">
            <span class="time">${esc((c.completed_at || "").slice(11, 19) || c.completed_at)}</span>
            <span class="txt">${esc(c.title)}</span>
            <span class="badge">${SCOPE_LABEL[c.scope] || "任务"}</span>
            <div class="ops">
              <button class="btn mini ghost" onclick="undoCompletion(${c.task_id},'${c.date}')">撤销完成</button>
              <button class="btn mini danger" onclick="delCompletion(${c.id})">删记录</button>
            </div>
          </div>`).join("")}
      </div>`).join("");
  } catch (e) {
    toast(e.message, true);
  }
}

async function undoCompletion(taskId, date) {
  try {
    await api("/api/completions", { method: "POST", body: { action: "uncomplete", task_id: taskId, date } });
    toast("已撤销该完成记录");
    loadHistory(); loadStats();
  } catch (e) { toast(e.message, true); }
}

async function delCompletion(id) {
  try {
    await api("/api/completions/" + id, { method: "DELETE" });
    toast("已删除记录");
    loadHistory(); loadStats();
  } catch (e) { toast(e.message, true); }
}

// ---------------- AI 生成 ----------------
let aiResults = {};

async function loadAiPage() {
  try {
    const s = await api("/api/ai/status");
    const parts = [];
    s.all.forEach((p) => {
      parts.push(p.has_key
        ? `<span class="ok">✓ ${p.label}（${p.model}）Key 已检测到：${esc(p.key_masked)}</span>`
        : `<span class="bad">✗ ${p.label}（${p.model}）：未检测到 Key</span>`);
    });
    if (s.openai && s.openai.has_key) {
      parts.push(`<span class="err">✗ OpenAI：${esc(s.openai.unavailable)}</span>`);
    }
    $("aiStatus").innerHTML =
      "API Key 检测（进程环境变量 → 系统/用户环境变量自动读取）：<br>" + parts.join("<br>") +
      "<br><small>支持的环境变量名：DEEPSEEK_API_KEY、GOOGLE_API_KEY、MOONSHOT_API_KEY、GEMINI_API_KEY、KIMIK3_API_KEY 等。设置/修改系统变量后无需重启应用。</small> " +
      `<button class="btn mini ghost" onclick="loadAiPage()">重新检测</button>`;
  } catch (e) {
    $("aiStatus").innerHTML = `<span class="err">状态查询失败：${esc(e.message)}</span>`;
  }
}

async function aiGenerate() {
  const scopes = [];
  if ($("ai-s-daily").checked) scopes.push("daily");
  if ($("ai-s-today").checked) scopes.push("today");
  if ($("ai-s-tomorrow").checked) scopes.push("tomorrow");
  if ($("ai-s-week").checked) scopes.push("week");
  if ($("ai-s-month").checked) scopes.push("month");
  if (!scopes.length) { toast("请至少选择一个生成范围", true); return; }
  const btn = event.target;
  btn.disabled = true; btn.textContent = "生成中…";
  try {
    const d = await api("/api/ai/generate", {
      method: "POST",
      body: {
        scopes,
        context: $("ai-context").value.trim(),
        count: parseInt($("ai-count").value, 10) || 5,
        provider: $("ai-provider").value
      }
    });
    aiResults = {};
    const scopeOrder = ["daily", "today", "tomorrow", "week", "month"];
    const parts = [];
    let warnMsg = "";
    scopeOrder.forEach((s) => {
      const r = d.results[s];
      if (!r) return;
      aiResults[s] = (r.tasks || []).map((t) => ({ ...t, checked: true }));
      if (r.source === "fallback") warnMsg = r.message;
      if (r.source === "error") warnMsg = r.message;
      parts.push(`<div class="hist-group"><h3>${SCOPE_LABEL[s]}（${aiResults[s].length} 条）</h3>` +
        aiResults[s].map((t, i) => `
          <div class="ai-item">
            <input type="checkbox" id="ai-chk-${s}-${i}" checked onchange="aiResults['${s}'][${i}].checked=this.checked">
            <div class="main">
              <div class="t">${esc(t.title)}</div>
              ${t.note ? `<div class="meta"><span>💬 ${esc(t.note)}</span></div>` : ""}
            </div>
          </div>`).join("") + "</div>");
    });
    $("aiResult").innerHTML = parts.join("");
    $("aiActions").hidden = false;
    const status = $("aiStatus");
    const msg = d.message || warnMsg;
    if (warnMsg && d.source !== "api") {
      status.innerHTML = `<span class="err">⚠ ${esc(warnMsg)}</span>`;
      toast("AI 不可用，已用内置模板生成", true);
    } else {
      status.innerHTML = `<span class="ok">✓ ${esc(msg)}</span><br>` + status.innerHTML.split("<br>").slice(1).join("<br>");
    }
  } catch (e) {
    toast("AI 生成失败：" + e.message, true);
  }
  btn.disabled = false; btn.textContent = "✨ 生成任务";
}

async function aiSaveSelected() {
  const dueMap = { today: TODAY, tomorrow: TOMORROW, week: TODAY, month: TODAY, daily: null };
  const picked = [];
  Object.keys(aiResults).forEach((s) => {
    (aiResults[s] || []).forEach((t) => {
      if (t.checked) picked.push({ title: t.title, note: t.note, scope: s, due_date: dueMap[s] });
    });
  });
  if (!picked.length) { toast("请至少勾选一条任务", true); return; }
  let ok = 0;
  for (const t of picked) {
    try {
      await api("/api/tasks", { method: "POST", body: t });
      ok++;
    } catch (e) { /* skip duplicate-ish */ }
  }
  toast(`已保存 ${ok}/${picked.length} 条任务 ✓`);
  aiClear();
  loadStats();
}

function aiClear() {
  aiResults = {};
  $("aiResult").innerHTML = "";
  $("aiActions").hidden = true;
}

// ---------------- 设置 ----------------
async function loadSettings() {
  try {
    const c = await api("/api/config");
    $("cfg-dbpath").value = c.config.db_path;
    $("cfg-port").value = c.config.port;
    $("cfg-host").value = c.config.host;
    $("cfg-autostart").checked = !!c.config.autostart;
    const urls = [];
    urls.push(`本机访问：<span class="url"><a href="http://127.0.0.1:${c.config.port}/">http://127.0.0.1:${c.config.port}/</a></span>`);
    (c.config.lan_ips || []).forEach((ip) => {
      urls.push(`局域网访问：<span class="url"><a href="http://${ip}:${c.config.port}/">http://${ip}:${c.config.port}/</a></span>`);
    });
    $("lanBox").innerHTML = urls.join("<br>") +
      "<br><small>手机 / 平板在同一 WiFi 下用局域网地址即可访问本设置页。</small>";
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveSettings() {
  try {
    const d = await api("/api/config", {
      method: "POST",
      body: {
        db_path: $("cfg-dbpath").value.trim(),
        port: parseInt($("cfg-port").value, 10),
        host: $("cfg-host").value.trim(),
        autostart: $("cfg-autostart").checked
      }
    });
    toast(d.message || "设置已保存");
    $("cfgTip").textContent = d.restart_required
      ? "⚠ 端口/监听地址修改需重启应用后生效：右键悬浮窗 → 退出，然后重新运行 start.bat"
      : (d.db_migrated ? "数据库已迁移到新路径 ✓" : "");
    loadSettings();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------- 提醒（浏览器端也响铃） ----------------
function beep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 880;
    o.type = "sine";
    g.gain.setValueAtTime(0.25, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.9);
    o.start(); o.stop(ctx.currentTime + 0.9);
    setTimeout(() => { try { ctx.close(); } catch (e) {} }, 1200);
  } catch (e) { /* ignore */ }
}

async function pollReminders() {
  try {
    const d = await api("/api/reminders/due");
    if (d.reminders && d.reminders.length) {
      beep();
      const names = d.reminders.slice(0, 3).map((r) => r.title).join("、");
      toast("⏰ 任务提醒：" + names + (d.reminders.length > 3 ? " 等" + d.reminders.length + " 条" : ""));
    }
  } catch (e) { /* 服务未连接时静默 */ }
}

function fmtLocal(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function setTimeDefaults() {
  // 任务时间默认系统当前时间（开始=现在，截止=开始+24小时，用户可自定义）
  const now = new Date();
  const startVal = fmtLocal(now);
  const deadlineVal = fmtLocal(new Date(now.getTime() + 24 * 3600 * 1000));
  ["today", "tomorrow", "week", "month"].forEach((k) => {
    const s = $("start-" + k);
    if (s) s.value = startVal;
    const d = $("deadline-" + k);
    if (d) d.value = deadlineVal;
  });
}

// ---------------- 背景图片（壁纸） ----------------
async function applyBg() {
  try {
    const bg = $("bgimg");
    if (!bg) return;
    const c = await api("/api/config");
    const cfg = c.config || {};
    if (cfg.bg_image) {
      bg.style.backgroundImage = "url('/api/bg')";
      const op = Math.min(1, Math.max(0.1, parseFloat(cfg.bg_opacity) || 0.7));
      // 图片越透明 → 遮罩越深，保证正文可读
      bg.style.setProperty("--bgdim", String(Math.max(0.12, 1 - op * 0.78)));
      bg.classList.add("on");
    } else {
      bg.classList.remove("on");
      bg.style.backgroundImage = "none";
    }
  } catch (e) { /* 服务未连接时静默 */ }
}

// ---------------- 启动 ----------------
function init() {
  setTimeDefaults();
  applyBg();
  $("todayLabel").textContent = "今天是 " + fmtCN(new Date());
  $("todayHint").textContent = "（" + TODAY + "）";
  $("tomorrowHint").textContent = "（" + TOMORROW + "）";
  const now = new Date();
  const wkStart = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() - now.getDay() + 1));
  const wkEnd = iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() - now.getDay() + 7));
  $("weekHint").textContent = `（${wkStart} ~ ${wkEnd}）`;
  $("monthHint").textContent = `（${now.getFullYear()}年${now.getMonth() + 1}月）`;
  const name = (location.hash || "#today").replace("#", "");
  applyTab(TAB_LOAD[name] ? name : "today");
  loadStats();
  setInterval(loadStats, 15000);
  setInterval(pollReminders, 30000);
}

document.addEventListener("DOMContentLoaded", init);
