/* Grand Central — Quick Todo popup.
 *
 * Talks to the board's serve.py: GET /grand_central.json for the
 * account list, POST /task {action:"add", account, ...} to create the todo.
 * Auth rides the browser's Cloudflare Access cookie (credentials:include +
 * host_permissions), so remote use just requires being logged in to the
 * board in this Chrome profile. On the LAN it falls back to localhost:8080.
 *
 * E264 (dev #63): the board refuses writes from a chrome-extension:// origin
 * (E161) unless it is this extension's pinned ID AND the request carries the
 * device pairing code, entered once under ⚙︎. A todo can also be filed to a
 * project, the same two calls as the board's "New todo in this project".
 */

const DEFAULT_BASE = "https://gc.example.com";
const FALLBACK_BASE = "http://localhost:8080";

const $ = (id) => document.getElementById(id);
let base = null;          // resolved working base URL for this popup session
let dueVal = "";          // YYYY-MM-DD or ""
let pairCode = "";        // device pairing code, chrome.storage.local only

async function gcFetch(path, opts = {}) {
  return fetch(base + path, { credentials: "include", ...opts,
    headers: { ...(opts.headers || {}), ...(pairCode ? { "X-GC-Dev-Code": pairCode } : {}) } });
}

// ---------- base URL resolution ----------

async function probe(url) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 3500);
  try {
    const r = await fetch(url + "/version", { credentials: "include", signal: ctl.signal });
    const ct = r.headers.get("content-type") || "";
    if (!r.ok || !ct.includes("json")) return false;   // Access login page = HTML
    return true;
  } catch { return false; }
  finally { clearTimeout(t); }
}

async function resolveBase() {
  const saved = (await chrome.storage.sync.get("baseUrl")).baseUrl || DEFAULT_BASE;
  for (const cand of [...new Set([saved, DEFAULT_BASE, FALLBACK_BASE])]) {
    if (await probe(cand)) return cand;
  }
  return null;
}

// ---------- account dropdown ----------
// A todo belongs to exactly ONE mail account (account-first, 2026-09-24), so the
// popup asks for it — no default: capturing into the wrong account is worse than
// one extra click. The last one you used is pre-selected.

function renderAccounts(accounts, last) {
  const sel = $("account");
  sel.innerHTML = "";
  sel.appendChild(Object.assign(document.createElement("option"), { value: "", textContent: "Account…" }));
  for (const a of accounts) {
    const o = document.createElement("option");
    o.value = a.slug;
    o.textContent = a.slug.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
    sel.appendChild(o);
  }
  if (last && accounts.some(a => a.slug === last)) sel.value = last;
}

async function loadAccounts() {
  const { lastAccount } = await chrome.storage.sync.get("lastAccount");
  // cached copy first so the popup is usable instantly, then refresh
  const { accountCache } = await chrome.storage.local.get("accountCache");
  if (accountCache) renderAccounts(accountCache, lastAccount);
  if (!base) return;
  try {
    const r = await gcFetch("/connections");
    if (!r.ok) return;
    const accounts = ((await r.json()).accounts || []).filter(a => a.slug && a.set !== false);
    const keep = $("account").value || lastAccount;
    renderAccounts(accounts, keep);
    chrome.storage.local.set({ accountCache: accounts });
  } catch { /* cached list (or none) is fine */ }
}

// ---------- project dropdown ----------
// Optional. Projects belong to no account; filing only points at the todo,
// which stays in its account (BUILD_SPEC §6, 2026-09-29). Open projects only.

function renderProjects(list, keep) {
  const sel = $("project");
  sel.innerHTML = "";
  sel.appendChild(Object.assign(document.createElement("option"), { value: "", textContent: "No project" }));
  for (const p of list) {
    sel.appendChild(Object.assign(document.createElement("option"), { value: String(p.id), textContent: "📁 " + p.name }));
  }
  if (keep && list.some(p => String(p.id) === keep)) sel.value = keep;
}

async function loadProjects() {
  const { projectCache } = await chrome.storage.local.get("projectCache");
  if (projectCache) renderProjects(projectCache, $("project").value);
  if (!base) return;
  try {
    const r = await gcFetch("/projects");
    if (!r.ok) return;
    const list = ((await r.json()).projects || []).filter(p => p.status === "open")
      .map(p => ({ id: p.id, name: p.name }));
    renderProjects(list, $("project").value);
    chrome.storage.local.set({ projectCache: list });
  } catch { /* cached list (or none) is fine */ }
}

// ---------- due-date chips ----------

function isoPlusDays(n) {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function setDue(val, chip) {
  dueVal = val;
  $("due").value = val;
  document.querySelectorAll(".chip").forEach(c => c.classList.toggle("on", c === chip));
}

document.querySelectorAll(".chip").forEach(chip => {
  chip.addEventListener("click", () => {
    if (chip.classList.contains("on")) { setDue("", null); return; }  // toggle off
    const days = { today: 0, tomorrow: 1, nextweek: 7 }[chip.dataset.due];
    setDue(isoPlusDays(days), chip);
  });
});
$("due").addEventListener("change", () => {
  dueVal = $("due").value;
  document.querySelectorAll(".chip").forEach(c => c.classList.remove("on"));
});

// ---------- current tab into notes ----------

$("tabBtn").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || tab.url.startsWith("chrome")) return;
  const n = $("notes");
  n.value = (n.value ? n.value + "\n" : "") + `${tab.title}\n${tab.url}`;
  if (!$("text").value.trim() && tab.title) $("text").value = tab.title;
});

// ---------- status ----------

function status(msg, cls = "") {
  const el = $("status");
  el.className = cls;
  el.innerHTML = msg;
}

function loginPrompt() {
  status(`can't reach the board — <a href="${DEFAULT_BASE}" target="_blank">open it to log in</a>`, "err");
}

// ---------- submit ----------

async function addTodo() {
  const text = $("text").value.trim();
  if (!text) { $("text").focus(); return; }
  if (!base) { loginPrompt(); return; }
  $("add").disabled = true;
  status("adding…");
  try {
    const body = { action: "add", text, source: "manual" };
    if (dueVal) body.due = dueVal;
    if (!$("account").value) { status("pick an account", "err"); $("add").disabled = false; return; }
    body.account = $("account").value;
    chrome.storage.sync.set({ lastAccount: body.account });
    if ($("notes").value.trim()) body.notes = $("notes").value.trim();
    if (!pairCode) { status("enter the pairing code under ⚙︎", "err"); return; }
    const r = await gcFetch("/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("json")) { loginPrompt(); return; }
    const out = await r.json();
    if (r.status === 403) { status("the board refused it — check the pairing code under ⚙︎", "err"); return; }
    if (!out.ok) { status(out.error || "server said no", "err"); return; }
    const project = $("project").value;
    if (project) {
      const r2 = await gcFetch("/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", id: Number(project), kind: "todo", ref: String(out.id) }),
      });
      const out2 = await r2.json().catch(() => ({}));
      if (!out2.ok) { status("todo added, but not filed: " + (out2.error || "server said no"), "err"); return; }
    }
    status(project ? "✓ added and filed" : "✓ added", "ok");
    $("text").value = ""; $("notes").value = "";
    setDue("", null);
    setTimeout(() => window.close(), 650);
  } catch (e) {
    status("network error: " + e.message, "err");
  } finally {
    $("add").disabled = false;
  }
}

$("add").addEventListener("click", addTodo);
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); addTodo(); }
});

// ---------- settings ----------

$("gear").addEventListener("click", async () => {
  const box = $("settings");
  box.classList.toggle("hidden");
  if (!box.classList.contains("hidden")) {
    $("baseUrl").value = (await chrome.storage.sync.get("baseUrl")).baseUrl || DEFAULT_BASE;
    $("pairCode").value = pairCode;
  }
});
$("saveBase").addEventListener("click", async () => {
  const v = $("baseUrl").value.trim().replace(/\/+$/, "");
  await chrome.storage.sync.set({ baseUrl: v || DEFAULT_BASE });
  // local, not sync: the code should not travel to every Chrome signed in as Bart
  pairCode = $("pairCode").value.trim();
  await chrome.storage.local.set({ pairCode });
  $("settings").classList.add("hidden");
  init();
});

// ---------- init ----------

async function init() {
  status("connecting…");
  pairCode = (await chrome.storage.local.get("pairCode")).pairCode || "";
  loadAccounts();              // cached lists render immediately
  loadProjects();
  base = await resolveBase();
  if (!base) { loginPrompt(); return; }
  status(!pairCode ? "enter the pairing code under ⚙︎" : base.includes("localhost") ? "local board" : "");
  loadAccounts();              // now refresh from the live board
  loadProjects();
}

init();
$("text").focus();
