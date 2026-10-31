// The form. All the real work happens in the local program (the program folder); this only sends it PDFs and shows progress.
const API = "http://127.0.0.1:8765";
const POLL_MS = 2000;
const SHOW_DONE = 10;     // a queue of 200 stays readable: finished and waiting cards are capped
const SHOW_WAITING = 10;

const STATUS = {
  queued: ["Waiting", ""],
  chatgpt: ["ChatGPT working", ""],
  ready: ["Chunks ready", ""],
  filling: ["Filling panel", ""],
  review: ["Ready to review", "warn"],
  uploading: ["Uploading", ""],
  done: ["Uploaded", "ok"],
  failed: ["Failed", "bad"],
  discarded: ["Discarded", ""],
};
const COUNTS = [
  ["Waiting", ["queued"]],
  ["In ChatGPT", ["chatgpt"]],
  ["To upload", ["ready", "filling", "review", "uploading"]],
  ["Uploaded", ["done"]],
  ["Failed", ["failed"]],
];
const FIRST = ["review", "failed", "uploading", "filling", "chatgpt", "ready"];

const $ = id => document.getElementById(id);
let picked = [];

function el(tag, props = {}, ...kids) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...kids);
  return node;
}

// ---- form ----

function addFiles(files) {
  for (const f of files) {
    const isPdf = f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf");
    if (isPdf && !picked.some(p => p.name === f.name && p.size === f.size)) picked.push(f);
  }
  renderPicked();
}

function renderPicked() {
  $("picked").replaceChildren(...picked.map(f => {
    const remove = el("button", { className: "link", textContent: "Remove", type: "button" });
    remove.onclick = () => { picked = picked.filter(p => p !== f); renderPicked(); };
    return el("li", {}, el("span", { textContent: f.name, title: f.name }), remove);
  }));
  $("pickedCount").textContent = picked.length > 1 ? `${picked.length} PDFs selected` : "";
  $("start").disabled = !picked.length || $("server").dataset.up !== "1";
}

async function start() {
  const auto = $("auto").checked;
  $("start").disabled = true;
  const files = picked;
  picked = [];
  renderPicked();
  $("sendError").hidden = true;
  for (const f of files) {
    const url = `${API}/jobs?name=${encodeURIComponent(f.name)}&auto=${auto ? 1 : 0}`;
    try {
      const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/pdf" }, body: f });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    } catch (e) {
      picked.push(f);
      renderPicked();
      $("sendError").hidden = false;
      $("sendError").textContent = `Could not send ${f.name}: ${e.message}`;
    }
  }
  poll();
}

// ---- jobs ----

async function act(id, action) {
  await fetch(`${API}/jobs/${id}/${action}`, { method: "POST" }).catch(() => {});
  poll();
}

function jobCard(job) {
  const [label, tone] = STATUS[job.status] || [job.status, ""];
  const card = el("div", { className: "job" },
    el("div", { className: "job-head" },
      el("span", { className: "job-name", textContent: job.name, title: job.name }),
      el("span", { className: `pill ${tone}`, textContent: label })));

  if (job.step) card.append(el("p", { className: "job-step", textContent: job.step }));
  if (job.error) card.append(el("p", { className: "error", textContent: job.error }));
  if (job.warnings.length) {
    card.append(el("ul", { className: "warnings" }, ...job.warnings.map(w => el("li", { textContent: w }))));
  }
  if (job.chunks.length) {
    const list = job.chunks.map(c => el("li", {},
      `${c.section || "(no section)"} `, el("small", { textContent: `${c.words} words` })));
    card.append(el("details", { open: job.status === "review" },
      el("summary", { textContent: `${job.chunks.length} chunks` }), el("ul", {}, ...list)));
  }
  if (job.status === "review") {
    const go = el("button", { className: "go", textContent: "Upload now" });
    const no = el("button", { textContent: "Discard" });
    go.onclick = () => { go.disabled = no.disabled = true; act(job.id, "upload"); };
    no.onclick = () => { go.disabled = no.disabled = true; act(job.id, "discard"); };
    card.append(el("div", { className: "job-actions" }, go, no));
  }
  if (job.status === "failed") {
    const again = el("button", { textContent: "Try again" });
    again.onclick = () => { again.disabled = true; act(job.id, "retry"); };
    card.append(el("div", { className: "job-actions" }, again));
  }
  return card;
}

function renderSummary(jobs) {
  $("counts").replaceChildren(...COUNTS.map(([label, statuses]) => {
    const count = jobs.filter(j => statuses.includes(j.status)).length;
    return el("div", { className: "count" }, el("strong", { textContent: count }), el("span", { textContent: label }));
  }));
}

function renderAccounts(accounts) {
  $("accountsBox").hidden = !accounts.length;
  $("accountsTitle").textContent = `ChatGPT accounts (${accounts.filter(a => a.state !== "Signed out").length})`;
  $("accounts").replaceChildren(...accounts.map(a => el("li", {},
    el("span", { textContent: a.name, title: a.name }), el("small", { textContent: a.state }))));
}

function visibleJobs(jobs) {
  const rank = j => { const i = FIRST.indexOf(j.status); return i < 0 ? FIRST.length : i; };
  const active = jobs.filter(j => FIRST.includes(j.status)).sort((a, b) => rank(a) - rank(b));
  const waiting = jobs.filter(j => j.status === "queued");
  const finished = jobs.filter(j => j.status === "done" || j.status === "discarded");
  const hidden = Math.max(0, waiting.length - SHOW_WAITING) + Math.max(0, finished.length - SHOW_DONE);
  return { list: [...active, ...waiting.slice(0, SHOW_WAITING), ...finished.slice(0, SHOW_DONE)], hidden };
}

let lastSnapshot = "";

async function poll() {
  try {
    const res = await fetch(`${API}/jobs`);
    const data = await res.json();
    setServer(true);
    const snapshot = JSON.stringify(data);
    if (snapshot === lastSnapshot) return;   // redrawing would close open details and reset buttons
    lastSnapshot = snapshot;
    $("notice").hidden = !data.notice;
    $("notice").textContent = data.notice;
    $("jobsBox").hidden = !data.jobs.length;
    renderSummary(data.jobs);
    renderAccounts(data.accounts);
    const { list, hidden } = visibleJobs(data.jobs);
    $("jobs").replaceChildren(...list.map(jobCard));
    $("hidden").textContent = hidden ? `${hidden} more not shown` : "";
  } catch {
    setServer(false);
  }
}

function setServer(up) {
  const pill = $("server");
  pill.dataset.up = up ? "1" : "0";
  pill.textContent = up ? "Connected" : "Offline";
  pill.className = `pill ${up ? "ok" : "bad"}`;
  $("offline").hidden = up;
  $("start").disabled = !picked.length || !up;
}

// ---- wiring ----

$("files").onchange = e => { addFiles(e.target.files); e.target.value = ""; };
$("start").onclick = start;
$("auto").onchange = e => chrome.storage.local.set({ auto: e.target.checked });
chrome.storage.local.get("auto", v => { $("auto").checked = !!v.auto; });

for (const [name, over] of [["dragover", true], ["dragenter", true], ["dragleave", false], ["drop", false]]) {
  $("drop").addEventListener(name, e => { e.preventDefault(); $("drop").classList.toggle("over", over); });
}
$("drop").addEventListener("drop", e => addFiles(e.dataTransfer.files));

poll();
setInterval(poll, POLL_MS);
