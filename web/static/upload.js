const STEM = /[-_.](f_cu|b_cu|edge_cuts|npth|pth|job|f_mask|b_mask|f_silkscreen|b_silkscreen|f_paste|b_paste|in\d+_cu)\b.*$/i;
const AUTO = "auto";
const pick = document.getElementById("pick"), drop = document.getElementById("drop");
const found = document.getElementById("found"), go = document.getElementById("go");
// the job page reuses this for "replace files", where there is no name field
const nameRow = document.getElementById("nameRow") || {}, jobName = document.getElementById("jobName") || document.createElement("input");
const err = document.getElementById("err");
const boardName = n => {
  const base = n.split(/[\\/]/).pop();
  let stem = base.replace(STEM, "");
  if (stem === base) stem = base.replace(/\.[^.]+$/, "");
  return stem.trim();
};
const fkey = f => `${f.name}:${f.size}:${f.lastModified}`;

let choices = [];      // [{value, label}] roles the server offers
let autos = [];        // server's auto-detected role per file
let rows = [];         // [{file, li, sel}] in pick.files order
let seq = 0;           // guards against out-of-order /detect replies
const forced = new Map();   // fkey -> role the user picked explicitly
let nameTouched = false;

const labelOf = r => (choices.find(c => c.value === r) || {label: r}).label;
const roleOf = i => forced.get(fkey(rows[i].file)) || autos[i] || AUTO;

function buildRow(file) {
  const li = document.createElement("li");
  const name = document.createElement("span");
  name.className = "fname";
  name.textContent = file.name;
  name.title = file.name;
  const sel = document.createElement("select");
  sel.name = "role";
  sel.className = "role";
  sel.setAttribute("aria-label", `file type for ${file.name}`);
  li.append(name, sel);
  return {file, li, sel};
}

function paint() {
  const roles = rows.map((_, i) => roleOf(i));
  rows.forEach((row, i) => {
    row.li.className = roles[i] === "ignore" ? "dim" : "ok";
    row.sel.value = forced.has(fkey(row.file)) ? forced.get(fkey(row.file)) : AUTO;
  });
  const has = r => roles.includes(r);
  const top = has("copper_top"), bot = has("copper_bottom");
  const copper = top || bot;
  err.textContent = top && bot ? "Both top and bottom copper are selected \u2014 set the one you don't want to mill to ignore." : "";
  nameRow.hidden = !copper;
  if (copper && !nameTouched) {
    const i = roles.findIndex(r => r === "copper_top" || r === "copper_bottom");
    jobName.value = boardName(rows[i].file.name);
  }
  go.disabled = !(copper && !(top && bot) && has("edge") && (has("drill") || has("drill_pth") || has("drill_npth")));
}

async function refresh() {
  const mine = ++seq;
  err.textContent = "";
  found.replaceChildren();
  const files = [...pick.files];
  rows = files.map(buildRow);
  found.append(...rows.map(r => r.li));
  if (!files.length) {
    autos = [];
    go.disabled = true;
    nameRow.hidden = true;
    return;
  }
  const fd = new FormData();
  files.forEach(f => fd.append("files", f, f.name));
  let data;
  try {
    const r = await fetch("/detect", {method: "POST", body: fd});
    data = await r.json();
    if (mine !== seq) return;
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  } catch (ex) {
    if (mine !== seq) return;
    autos = [];
    rows.forEach(({sel}) => { sel.disabled = true; });
    err.textContent = "could not read the files: " + ex.message;
    go.disabled = true;
    return;
  }
  choices = data.choices;
  autos = data.files.map(f => f.role);
  rows.forEach((row, i) => {
    row.sel.append(new Option(`auto \u2014 ${labelOf(autos[i] || AUTO)}`, AUTO));
    for (const c of choices) row.sel.append(new Option(c.label, c.value));
    row.sel.addEventListener("change", () => {
      if (row.sel.value === AUTO) forced.delete(fkey(row.file));
      else forced.set(fkey(row.file), row.sel.value);
      paint();
    });
  });
  paint();
}

jobName.addEventListener("input", () => { nameTouched = true; });
pick.addEventListener("change", refresh);
["dragover", "dragenter"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", e => {
  e.preventDefault();
  const seen = new Set([...pick.files].map(fkey));
  const dt = new DataTransfer();
  [...pick.files, ...e.dataTransfer.files].forEach(f => {
    if (!seen.has(fkey(f))) { seen.add(fkey(f)); dt.items.add(f); }
  });
  pick.files = dt.files;
  refresh();
});
go.form.addEventListener("submit", async e => {
  e.preventDefault();
  go.disabled = true;
  const r = await fetch(e.target.action, {method: "POST", body: new FormData(e.target)});
  if (r.redirected) { location = r.url; return; }
  let msg = await r.text();
  try { msg = JSON.parse(msg).detail; } catch (_) {}
  document.getElementById("err").textContent = msg;
  go.disabled = false;
});
