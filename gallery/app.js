const state = { apps: [], query: "", tags: new Set() };
const $ = id => document.getElementById(id);

function esc(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function miniMarkdown(src) {
  const lines = esc(src).split("\n");
  const out = [];
  let inList = false;
  const inline = s => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (const line of lines) {
    const t = line.trim();
    if (/^### /.test(t)) { closeList(); out.push(`<h3>${inline(t.slice(4))}</h3>`); }
    else if (/^## /.test(t)) { closeList(); out.push(`<h3>${inline(t.slice(3))}</h3>`); }
    else if (/^# /.test(t)) { closeList(); out.push(`<h2>${inline(t.slice(2))}</h2>`); }
    else if (/^[-*] /.test(t)) { if (!inList) { out.push("<ul>"); inList = true; } out.push(`<li>${inline(t.slice(2))}</li>`); }
    else if (t === "") { closeList(); }
    else { closeList(); out.push(`<p>${inline(t)}</p>`); }
  }
  closeList();
  return out.join("");
}

async function init() {
  const res = await fetch("../apps.json");
  const data = await res.json();
  state.apps = data.apps;
  await Promise.all(state.apps.map(async a => {
    const r = await fetch("../" + a.prompt);
    a.promptText = r.ok ? (await r.text()).trim() : "";
    a.haystack = [a.title, a.summary, a.tags.join(" "), a.promptText].join(" ").toLowerCase();
  }));
  renderTags();
  render();
}

function allTags() {
  const counts = {};
  for (const a of state.apps) for (const t of a.tags) counts[t] = (counts[t] || 0) + 1;
  return Object.entries(counts).sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]));
}

function renderTags() {
  const el = $("tags");
  el.innerHTML = "";
  for (const [tag, count] of allTags()) {
    const b = document.createElement("button");
    b.className = "chip" + (state.tags.has(tag) ? " active" : "");
    b.textContent = `${tag} · ${count}`;
    b.addEventListener("click", () => {
      state.tags.has(tag) ? state.tags.delete(tag) : state.tags.add(tag);
      renderTags();
      render();
    });
    el.appendChild(b);
  }
}

function matches(a) {
  if (![...state.tags].every(t => a.tags.includes(t))) return false;
  if (!state.query) return true;
  return state.query.split(/\s+/).every(w => a.haystack.includes(w));
}

function promptMatchLine(a) {
  if (!state.query) return null;
  for (const line of a.promptText.split("\n")) {
    const t = line.replace(/^#+\s*/, "").trim();
    if (!t) continue;
    if (state.query.split(/\s+/).some(w => t.toLowerCase().includes(w)))
      return t.length > 140 ? t.slice(0, 140) + "…" : t;
  }
  return null;
}

function render() {
  const visible = state.apps.filter(matches);
  const grid = $("grid");
  grid.innerHTML = "";
  $("empty").hidden = visible.length > 0;
  $("status").textContent = `${visible.length} of ${state.apps.length} apps` +
    (state.tags.size ? ` · labels: ${[...state.tags].join(" + ")}` : "");
  $("clear").hidden = !state.query && !state.tags.size;

  for (const a of visible) {
    const card = document.createElement("article");
    card.className = "card";
    const hit = promptMatchLine(a);
    card.innerHTML = `
      <div class="card-head">
        <span class="icon">${esc(a.icon || "✨")}</span>
        <h2>${esc(a.title)}</h2>
      </div>
      <p class="summary">${esc(a.summary)}</p>
      <div class="card-tags">${a.tags.map(t =>
        `<button class="chip small" data-tag="${esc(t)}">${esc(t)}</button>`).join("")}</div>
      ${hit ? `<p class="prompt-hit">“${esc(hit)}”</p>` : ""}
      <details>
        <summary>The prompt</summary>
        <div class="prompt">${miniMarkdown(a.promptText)}</div>
      </details>
      <div class="actions">
        <a class="btn primary" href="../${esc(a.app)}" target="_blank" rel="noopener">Open app ↗</a>
        <a class="btn" href="../${esc(a.prompt)}" target="_blank" rel="noopener">Raw prompt</a>
      </div>`;
    card.querySelectorAll(".chip.small").forEach(chip =>
      chip.addEventListener("click", () => {
        const t = chip.dataset.tag;
        state.tags.has(t) ? state.tags.delete(t) : state.tags.add(t);
        renderTags();
        render();
      }));
    grid.appendChild(card);
  }
}

$("search").addEventListener("input", e => {
  state.query = e.target.value.trim().toLowerCase();
  render();
});
$("clear").addEventListener("click", () => {
  state.query = ""; state.tags.clear();
  $("search").value = "";
  renderTags();
  render();
});

init();
