/* Grand Central — Quick Todo popup.
 *
 * Talks to the board's serve.py: GET /grand_central.json for the
 * account list, POST /task {action:"add", account, ...} to create the todo.
 * Auth rides the browser's Cloudflare Access cookie (credentials:include +
 * host_permissions), so remote use just requires being logged in to the
 * board in this Chrome profile. On the LAN it falls back to localhost:8080.
 */

const DEFAULT_BASE = "https://gc.example.com";
const FALLBACK_BASE = "http://localhost:8080";

const $ = (id) => document.getElementById(id);
let base = null;          // resolved working base URL for this popup session
let dueVal = "";          // YYYY-MM-DD or ""

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
    const r = await fetch(base + "/connections", { credentials: "include" });
    if (!r.ok) return;
    const accounts = ((await r.json()).accounts || []).filter(a => a.slug && a.set !== false);
    const keep = $("account").value || lastAccount;
    renderAccounts(accounts, keep);
    chrome.storage.local.set({ accountCache: accounts });
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
    const r = await fetch(base + "/task", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("json")) { loginPrompt(); return; }
    const out = await r.json();
    if (!out.ok) { status(out.error || "server said no", "err"); return; }
    status("✓ added", "ok");
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
  }
});
$("saveBase").addEventListener("click", async () => {
  const v = $("baseUrl").value.trim().replace(/\/+$/, "");
  await chrome.storage.sync.set({ baseUrl: v || DEFAULT_BASE });
  $("settings").classList.add("hidden");
  init();
});

// ---------- init ----------

async function init() {
  status("connecting…");
  loadAccounts();              // cached list renders immediately
  base = await resolveBase();
  if (!base) { loginPrompt(); return; }
  status(base.includes("localhost") ? "local board" : "");
  loadAccounts();              // now refresh from the live board
}

init();
$("text").focus();
