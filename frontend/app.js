const API = localStorage.api || "http://localhost:8000";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STAT = {needs_review:["Needs review","#c98a00"], verified:["Verified","#2e9e5b"], problem:["Problem found","#d64545"]};
const S = {kind:null, id:null, data:null, page:1, pdf:null, sel:null, tab:"items", reviews:{}};

async function api(p, o){
  const r = await fetch(API + p, o);
  if(!r.ok) throw new Error((await r.text()).slice(0,300));
  return r.json();
}
const rk = it => `${S.kind}:${S.id}:${it.id}`;
const statusOf = it => (S.reviews[rk(it)] || {}).status || "needs_review";

/* ---------- start-up ---------- */
async function init(){
  $("#reviewer").value = localStorage.reviewer || "";
  $("#reviewer").onchange = e => localStorage.reviewer = e.target.value;
  $("#v-review").onclick = () => view("review"); $("#v-db").onclick = () => { view("db"); loadDb(); };
  $("#prev").onclick = () => go(S.page - 1); $("#next").onclick = () => go(S.page + 1);
  $("#pg").onchange = e => go(+e.target.value);
  $("#file").onchange = async e => { const f = e.target.files[0]; if(f) openPdf(await f.arrayBuffer()); };
  window.onresize = drawPdf;
  try{
    const it = await api("/api/items");
    $("#pick").innerHTML =
      `<optgroup label="Law corpus">` + it.law.map(x => `<option value="law:${esc(x.id)}">${esc(x.law_name)} – ${esc(x.jurisdiction)} (${x.pdf_page_count} pages, ${esc(x.review_status)})</option>`).join("") +
      `</optgroup><optgroup label="Uploaded contracts">` + it.contracts.map(x => `<option value="contract:${esc(x.id)}">${esc(x.original_filename)} v${x.version_number} (${x.page_count} pages, ${esc(x.status)})</option>`).join("") + `</optgroup>`;
    $("#pick").onchange = openItem; openItem();
  }catch(e){ $("#tabbody").innerHTML = `<p class="err">Cannot load data: ${esc(e.message)}<br>Is the backend running on ${API}?</p>`; }
}
function view(v){
  $("#review").hidden = v !== "review"; $("#db").hidden = v !== "db";
  $("#v-review").classList.toggle("on", v === "review"); $("#v-db").classList.toggle("on", v === "db");
}
async function openItem(){
  const [kind, ...r] = $("#pick").value.split(":"); const id = r.join(":");
  if(!id) return;
  Object.assign(S, {kind, id, sel:null, page:1, pdf:null, tab:"items"});
  $("#tabbody").innerHTML = "Loading…"; $("#detail").innerHTML = "";
  try{
    S.data = await api(`/api/${kind}/${encodeURIComponent(id)}`);
    S.reviews = await api("/api/reviews");
  }catch(e){ $("#tabbody").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  renderMeta(); loadRemotePdf();
  if(S.data.items[0]) select(S.data.items[0]); else renderTab();
}
function renderMeta(){
  const m = S.data.meta;
  $("#meta").innerHTML = S.kind === "law"
    ? `<b>${esc(m.law_name)}</b> · ${esc(m.jurisdiction)} · DB review status: <b>${esc(m.review_status)}</b> · ${m.pdf_page_count} pages<br>PDF hash <code>${esc(m.pdf_sha256)}</code>`
    : `<b>${esc(m.original_filename)}</b> · version ${m.version_number} · status <b>${esc(m.status)}</b> · engine ${esc(m.extraction_engine || "–")} ${esc(m.extraction_engine_version || "")}<br>PDF hash <code>${esc(m.pdf_sha256)}</code>`;
}

/* ---------- PDF ---------- */
const setMsg = t => $("#pdfmsg").textContent = t;
async function loadRemotePdf(){
  setMsg("Loading PDF…"); $("#hash").textContent = "";
  try{
    const r = await fetch(`${API}/api/pdf/${S.kind}/${encodeURIComponent(S.id)}`);
    if(!r.ok) throw new Error((await r.text()).slice(0,150));
    await openPdf(await r.arrayBuffer());
  }catch(e){ S.pdf = null; setMsg("PDF could not be loaded automatically (" + e.message + "). Click “Load PDF…” and pick the file."); }
}
async function openPdf(buf){
  const h = [...new Uint8Array(await crypto.subtle.digest("SHA-256", buf))].map(b => b.toString(16).padStart(2,"0")).join("");
  $("#hash").innerHTML = h === S.data.meta.pdf_sha256 ? `<b class="ok">PDF hash matches database</b>` : `<b class="bad">PDF hash differs from database</b>`;
  S.pdf = await pdfjsLib.getDocument({data:buf}).promise; setMsg("");
  const n = S.pdf.numPages, e = S.data.expectedPages;
  $("#pgtotal").innerHTML = `/ ${n}` + (n !== e ? ` <b class="bad">(database expects ${e})</b>` : "");
  drawPdf();
}
let task;
async function drawPdf(){
  if(!S.pdf) return;
  const pg = await S.pdf.getPage(Math.min(S.page, S.pdf.numPages));
  const w = $("#canvaswrap").clientWidth - 24, v0 = pg.getViewport({scale:1}), vp = pg.getViewport({scale:w / v0.width});
  const cv = $("#cv"); if(task) try{ task.cancel(); }catch{}
  cv.width = vp.width; cv.height = vp.height;
  task = pg.render({canvasContext:cv.getContext("2d"), viewport:vp});
  try{ await task.promise; }catch{}
}
function go(n){
  if(!S.data) return;
  S.page = Math.max(1, Math.min(n || 1, S.data.expectedPages));
  $("#pg").value = S.page; drawPdf(); renderTab();
}
function select(it){ S.sel = it; go(it.p1); renderDetail(); }

/* ---------- text -> headings / tables ---------- */
function fmt(t){
  const L = String(t || "").split("\n"), o = []; let i = 0;
  const row = l => l.trim() && (l.includes("|") || /\S(\t| {2,})\S/.test(l.trim()));
  const cells = l => (l.includes("|") ? l.trim().replace(/^\||\|$/g,"").split("|") : l.trim().split(/\t| {2,}/)).map(x => x.trim());
  while(i < L.length){
    if(row(L[i])){
      let j = i; while(j < L.length && row(L[j])) j++;
      if(j - i >= 2){
        o.push("<table class='ext'>" + L.slice(i,j).map(cells).filter(c => !c.every(x => /^[-:\s]*$/.test(x)))
          .map((c,k) => "<tr>" + c.map(x => `<${k?"td":"th"}>${esc(x)}</${k?"td":"th"}>`).join("") + "</tr>").join("") + "</table>");
        i = j; continue;
      }
    }
    const l = L[i++].trim(); if(!l) continue;
    const head = l.length < 90 && (/^(article|section|clause|chapter|part|schedule)\b/i.test(l) || /^\d+(\.\d+)*[.)]?\s+[A-Z]/.test(l) && l.length < 70 || (l === l.toUpperCase() && /[A-Z]{3}/.test(l)));
    o.push(head ? `<h5>${esc(l)}</h5>` : `<p>${esc(l)}</p>`);
  }
  return o.join("");
}

/* ---------- right-hand tabs ---------- */
function renderTab(){
  if(!S.data) return;
  const D = S.data, W = D.warnings, cnt = {};
  D.items.forEach(i => cnt[statusOf(i)] = (cnt[statusOf(i)] || 0) + 1);
  const T = [["items", `Items (${D.items.length})`], ["page", `This page (${S.page})`], ["cov", "Coverage"], ["warn", `Warnings (${W.length})`]];
  $("#tabs").innerHTML = T.map(([k,l]) => `<button class="${S.tab===k?"on":""}" onclick="S.tab='${k}';renderTab()">${l}</button>`).join("") +
    `<span class="grow"></span><small class="muted" style="align-self:center">✔ ${cnt.verified||0} · ✖ ${cnt.problem||0} · ● ${cnt.needs_review||0}</small>`;
  const b = $("#tabbody");
  if(S.tab === "items"){
    b.innerHTML = D.items.map((it,k) => `<div class="row ${S.sel===it?"sel":""} ${it.p1<=S.page&&S.page<=it.p2?"onpage":""}" onclick="select(S.data.items[${k}])">
      <span class="dot" style="--c:${STAT[statusOf(it)][1]}" title="${STAT[statusOf(it)][0]}"></span><span>${esc(it.title)} <small>${esc(it.sub)}</small></span></div>`).join("") || "<p class='muted'>Nothing extracted.</p>";
  } else if(S.tab === "page"){
    let h = "";
    if(S.kind === "contract"){
      const p = (D.pagesText || []).find(x => x.page_number === S.page);
      h += p ? `<p class="muted">Extracted text of page ${S.page} · hash <code>${esc(p.text_sha256.slice(0,12))}</code></p><div class="txt">${p.text.trim() ? fmt(p.text) : "<i>Empty text</i>"}</div>` : `<p class="err">No extracted text row for this page.</p>`;
      h += "<h4>Clauses on this page</h4>" + D.items.filter(i => i.p1 <= S.page && S.page <= i.p2).map(i => `<div class="card"><a class="link" href="#" onclick="select(S.data.items[${D.items.indexOf(i)}]);return false">${esc(i.title)}</a></div>`).join("");
    } else {
      const ps = D.items.flatMap(i => i.passages.filter(p => p.page_number === S.page).map(p => ({...p, t:i.title})));
      h += ps.map(p => `<div class="card"><code>${esc(p.evidence_id)}</code> <small class="muted">${esc(p.t)}</small><div class="txt">${fmt(p.text)}</div></div>`).join("") || "<p class='err'>No passage extracted for this page.</p>";
    }
    b.innerHTML = h;
  } else if(S.tab === "cov"){
    const c = D.coverage;
    b.innerHTML = `<p><b>${c.pages_with_text} of ${c.expected_pages}</b> pages have extracted text. <b>${c.found_items}</b> ${S.kind==="law"?"sections":"clauses"} found (highest number ${c.expected_items}).</p>
      <p class="muted">Green: text and mapped. Amber: text, but no ${S.kind==="law"?"section":"clause"} covers the page. Red: no text.</p>
      <div class="grid">${c.page_map.map(p => `<span class="pgchip ${p.chars===0?"r":p.items===0?"a":"g"} ${p.page===S.page?"cur":""}" onclick="go(${p.page})" title="${p.chars} chars, ${p.items} items">${p.page}</span>`).join("")}</div>
      <p>Skipped (no text): ${c.empty_pages.length ? c.empty_pages.join(", ") : "none"}<br>Not covered by any item: ${c.unmapped_pages.length ? c.unmapped_pages.join(", ") : "none"}</p>
      <p class="muted">Counts and hashes catch missing or changed data. They cannot prove the text matches the PDF: compare visually.</p>`;
  } else {
    b.innerHTML = W.map(x => `<div class="row" onclick="${x.ref ? `select(S.data.items.find(i=>i.id==='${esc(x.ref)}'))` : `go(${x.page||1})`}">
      <span class="badge ${x.level}">${x.level}</span><span><b>${esc(x.where)}</b> ${esc(x.msg)}</span></div>`).join("") || "<p class='ok'>No warnings.</p>";
  }
}

/* ---------- detail + review ---------- */
function renderDetail(){
  const it = S.sel; if(!it){ $("#detail").innerHTML = ""; return; }
  const r = S.reviews[rk(it)] || {}, cur = r.status || "needs_review"; let x = "";
  if(it.passages) x = "<h4>Passages and evidence IDs</h4>" + it.passages.map(p => `<div class="card"><button class="link" onclick="go(${p.page_number})">page ${p.page_number}</button> <code>${esc(p.evidence_id)}</code> ${p.has_embedding ? "" : "<small class='muted'>no embedding</small>"}<div class="txt">${fmt(p.text)}</div></div>`).join("");
  if(it.spans) x = "<h4>Clause → PDF page mapping</h4><table class='raw'><tr><th>PDF page</th><th>Characters</th><th>Mapped text</th></tr>" +
    it.spans.map(s => `<tr><td><button class="link" onclick="go(${s.page_number})">page ${s.page_number}</button></td><td>${s.s}–${s.e}</td><td>${esc(it.text.slice(s.s, s.e).slice(0,160))}</td></tr>`).join("") + "</table>";
  $("#detail").innerHTML = `<h3 style="margin:0">${esc(it.title)}</h3><small class="muted">${esc(it.sub)} · id <code>${esc(it.id)}</code> · hash <code>${esc(it.sha.slice(0,16))}</code></small>
    <div class="rev"><b>Review</b><br>` + Object.entries(STAT).map(([k,[l,c]]) => `<button class="st ${cur===k?"on":""}" style="--c:${c}" onclick="setRev('${k}')">${l}</button>`).join("") +
    `<textarea id="note" placeholder="Reviewer notes">${esc(r.note || "")}</textarea><button onclick="setRev()">Save note</button> ${r.at ? `<small class="muted">${esc(r.reviewer || "unknown")} · ${esc(r.at)}</small>` : ""}</div>
    <h4>Full extracted text</h4><div class="txt">${fmt(it.text)}</div>` + x;
}
async function setRev(status){
  const it = S.sel;
  S.reviews[rk(it)] = await api("/api/reviews", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({key:rk(it), status: status || statusOf(it), note:$("#note").value, reviewer:$("#reviewer").value})});
  renderDetail(); renderTab();
}

/* ---------- database tables + relationships ---------- */
async function loadDb(){
  const o = await api("/api/db/overview");
  $("#dbside").innerHTML = "<h4 style='margin-top:0'>Tables</h4>" +
    o.tables.map(t => `<div class="row" onclick="showTable('${t.name}',0)"><span>${esc(t.name)} <small>${t.rows} rows</small></span></div>`).join("") +
    "<h4>Relationships</h4>" + o.relations.map(f => `<div class="fk"><b>${esc(f.from_table)}</b> → <b>${esc(f.to_table)}</b><br><small class="muted">${esc(f.definition)}</small></div>`).join("");
}
async function showTable(name, off){
  const t = await api(`/api/db/table/${name}?limit=25&offset=${off}`);
  $("#dbmain").innerHTML = `<h3 style="margin-top:0">${esc(name)}</h3>
    <button ${off ? "" : "disabled"} onclick="showTable('${name}',${Math.max(0, off-25)})">Previous</button>
    <button ${t.rows.length < 25 ? "disabled" : ""} onclick="showTable('${name}',${off+25})">Next</button> <small class="muted">rows ${off+1}–${off+t.rows.length}. Long text is cut at 600 characters.</small>
    <div style="overflow:auto"><table class="raw"><tr>${t.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr>` +
    t.rows.map(r => "<tr>" + t.columns.map(c => `<td>${esc(r[c] === null ? "∅" : r[c])}</td>`).join("") + "</tr>").join("") + "</table></div>";
}
init();
