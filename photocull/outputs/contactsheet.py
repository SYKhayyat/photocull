"""The HTML contact sheet: the output a human actually reads.

This is what makes the tool trustworthy rather than merely correct. Every
verdict is shown next to the frame it describes, with the subject box drawn on
and the focus point marked, so a wrong subject is visible in a glance instead of
quietly poisoning the numbers. Checking a row costs a second; doing the same
work by hand costs an hour. That gap is the entire value proposition, and it
only exists because the reasoning is visible.

The page is one self-contained file -- thumbnails inlined as data URIs, no
scripts fetched, no server. It opens from a USB stick in five years.

It also writes back: drawing a box on a frame and exporting the result produces
the JSON sidecar the manual detector reads, so overruling the automatic subject
is a drag and a re-run rather than a feature request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from ..config import Config
from ..models import PhotoReport

_PAYLOAD_TOKEN = "__PHOTOCULL_PAYLOAD__"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>photocull contact sheet</title>
<style>
  :root {
    --bg: #14161a; --panel: #1c1f26; --line: #2c313b; --text: #e6e8ec;
    --muted: #9aa3b2; --accent: #6ea8fe; --good: #57c785; --warn: #e0b341; --bad: #e06c6c;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }
  header { position: sticky; top: 0; z-index: 10; background: var(--panel);
           border-bottom: 1px solid var(--line); padding: 12px 16px; }
  h1 { font-size: 15px; margin: 0 0 8px; font-weight: 600; letter-spacing: .01em; }
  .summary { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
  .summary b { color: var(--text); font-weight: 600; }
  .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  select, input, button { background: #22262f; color: var(--text); border: 1px solid var(--line);
                          border-radius: 6px; padding: 5px 8px; font: inherit; font-size: 12px; }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #0d1117; border-color: var(--accent);
                   font-weight: 600; }
  label { color: var(--muted); font-size: 12px; display: flex; align-items: center; gap: 5px; }
  main { padding: 16px; display: grid; gap: 14px; align-items: start;
         grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
          overflow: hidden; display: flex; flex-direction: column; }
  .card.best { border-color: var(--good); }
  .card.failed { opacity: .55; }
  .frame { position: relative; background: #0b0d10; cursor: zoom-in; }
  .shot { position: relative; display: inline-block; line-height: 0; }
  /* Fixed frame height so a portrait beside a landscape does not leave a
     column of dead space. contain rather than cover: cropping the thumbnail
     would move the subject box off the part of the picture it describes. */
  .frame { height: 200px; display: flex; align-items: center; justify-content: center; }
  .frame img { display: block; max-width: 100%; max-height: 100%;
               width: auto; height: auto; }
  .box { position: absolute; border: 2px solid var(--accent); border-radius: 2px;
         box-shadow: 0 0 0 1px rgba(0,0,0,.55); pointer-events: none; }
  .focus { position: absolute; width: 14px; height: 14px; margin: -7px 0 0 -7px;
           border: 2px solid var(--warn); border-radius: 50%; pointer-events: none; }
  .badge { position: absolute; top: 6px; left: 6px; background: rgba(0,0,0,.72);
           border-radius: 4px; padding: 2px 6px; font-size: 11px; }
  .badge.right { left: auto; right: 6px; }
  .meta { padding: 9px 10px; display: grid; gap: 5px; }
  .name { font-size: 12px; word-break: break-all; color: var(--muted); }
  .stars { letter-spacing: 2px; }
  .r5, .r4 { color: var(--good); } .r3 { color: var(--warn); } .r2, .r1, .r0 { color: var(--bad); }
  .why { font-size: 11px; color: var(--muted); }
  table.nums { width: 100%; border-collapse: collapse; font-size: 11px; }
  table.nums td { padding: 1px 0; color: var(--muted); }
  table.nums td:last-child { text-align: right; color: var(--text); font-variant-numeric: tabular-nums; }
  dialog { background: var(--panel); color: var(--text); border: 1px solid var(--line);
           border-radius: 10px; padding: 0; max-width: 94vw; }
  dialog::backdrop { background: rgba(0,0,0,.75); }
  .dlg-head { padding: 10px 14px; border-bottom: 1px solid var(--line);
              display: flex; gap: 10px; align-items: center; justify-content: space-between; }
  .dlg-body { padding: 14px; }
  #canvasWrap { position: relative; display: inline-block; cursor: crosshair; }
  #canvasWrap img { display: block; max-width: 86vw; max-height: 68vh; }
  #drawn { position: absolute; border: 2px dashed var(--good); display: none; }
  .hint { color: var(--muted); font-size: 12px; margin-top: 8px; }
  .empty { color: var(--muted); padding: 40px; text-align: center; grid-column: 1 / -1; }
</style>
</head>
<body>
<header>
  <h1>photocull &mdash; contact sheet</h1>
  <div class="summary" id="summary"></div>
  <div class="controls">
    <label>sort
      <select id="sort">
        <option value="rating">rating</option>
        <option value="subject_background_ratio">subject / background</option>
        <option value="max_local_acutance">peak sharpness</option>
        <option value="subject_or_max_acutance">subject sharpness (box-size dependent)</option>
        <option value="subject_relative_acutance">subject vs frame peak</option>
        <option value="sharp_fraction">depth of field</option>
        <option value="highlight_clipped">clipped highlights</option>
        <option value="group">group</option>
        <option value="filename">filename</option>
      </select>
    </label>
    <label><input type="checkbox" id="desc" checked> descending</label>
    <label>min stars <input type="number" id="minStars" value="0" min="0" max="5" style="width:52px"></label>
    <label>subject
      <select id="detector"><option value="">any</option></select>
    </label>
    <label><input type="checkbox" id="bestOnly"> group best only</label>
    <label><input type="checkbox" id="problemsOnly"> problems only</label>
    <input type="search" id="search" placeholder="filename contains...">
    <button id="exportSubjects">export subject boxes</button>
    <span class="summary" id="count"></span>
  </div>
</header>
<main id="grid"></main>

<dialog id="viewer">
  <div class="dlg-head">
    <strong id="dlgName"></strong>
    <span>
      <button id="clearBox">clear box</button>
      <button class="primary" id="saveBox">save box</button>
      <button id="closeDlg">close</button>
    </span>
  </div>
  <div class="dlg-body">
    <div id="canvasWrap"><img id="dlgImg" alt=""><div id="drawn"></div></div>
    <div class="hint">Drag on the image to mark the real subject. Saved boxes go into
      <code>photocull-subjects.json</code> via &ldquo;export subject boxes&rdquo;, and the next run
      will use them instead of guessing.</div>
    <div class="hint" id="dlgWhy"></div>
  </div>
</dialog>

<script>
const DATA = __PHOTOCULL_PAYLOAD__;
const manual = {};

const $ = (id) => document.getElementById(id);
const num = (v, digits = 2) => (v === null || v === undefined || Number.isNaN(v))
  ? "\\u2014" : (typeof v === "number" ? v.toFixed(digits) : String(v));

function metric(photo, key) {
  if (key === "group") return photo.group.id ?? -1;
  if (key === "rating") return photo.rating ?? -1;
  if (key === "filename") return photo.filename;
  if (key === "subject_or_max_acutance")
    return photo.sharpness.subject_acutance ?? photo.sharpness.max_local_acutance;
  if (key in photo.sharpness) return photo.sharpness[key];
  if (key in photo.exposure) return photo.exposure[key];
  return null;
}

function stars(rating) {
  if (rating === null || rating === undefined) return "";
  return "\\u2605".repeat(rating) + "\\u2606".repeat(5 - rating);
}

function buildDetectorFilter() {
  const sources = [...new Set(DATA.photos.map(p => p.detection.source))].sort();
  for (const source of sources) {
    const option = document.createElement("option");
    option.value = source; option.textContent = source;
    $("detector").appendChild(option);
  }
}

function visible() {
  const minStars = Number($("minStars").value) || 0;
  const detector = $("detector").value;
  const bestOnly = $("bestOnly").checked;
  const problemsOnly = $("problemsOnly").checked;
  const needle = $("search").value.trim().toLowerCase();

  let rows = DATA.photos.filter(p => {
    if ((p.rating ?? 0) < minStars) return false;
    if (detector && p.detection.source !== detector) return false;
    if (bestOnly && !(p.group.is_best)) return false;
    if (problemsOnly && !(p.error || (p.rating ?? 5) <= 2)) return false;
    if (needle && !p.filename.toLowerCase().includes(needle)) return false;
    return true;
  });

  const key = $("sort").value;
  const sign = $("desc").checked ? -1 : 1;
  rows.sort((a, b) => {
    const left = metric(a, key), right = metric(b, key);
    if (typeof left === "string" || typeof right === "string")
      return sign * String(left).localeCompare(String(right));
    const primary = sign * ((left ?? -Infinity) - (right ?? -Infinity));
    if (primary !== 0) return primary;
    // Equal on the chosen key (very common when sorting by rating): fall back to
    // subject isolation so the best frame of a tie still surfaces first.
    return sign * ((a.sharpness.subject_background_ratio ?? -Infinity)
                 - (b.sharpness.subject_background_ratio ?? -Infinity));
  });
  return rows;
}

function card(photo) {
  const element = document.createElement("div");
  element.className = "card" + (photo.group.is_best ? " best" : "") + (photo.error ? " failed" : "");

  const box = photo.detection.box;
  const overlay = box
    ? `<div class="box" style="left:${box.x * 100}%;top:${box.y * 100}%;width:${box.w * 100}%;height:${box.h * 100}%"></div>`
    : "";
  const focus = photo.error ? "" :
    `<div class="focus" style="left:${photo.sharpness.focus_x * 100}%;top:${photo.sharpness.focus_y * 100}%"></div>`;
  const groupBadge = photo.group.size > 1
    ? `<div class="badge right">group ${photo.group.id} &middot; ${photo.group.rank + 1}/${photo.group.size}</div>` : "";

  element.innerHTML = `
    <div class="frame" data-file="${photo.filename}">
      ${photo.thumbnail ? `<span class="shot"><img loading="lazy" src="${photo.thumbnail}" alt="">${overlay}${focus}</span>` : ""}
      <div class="badge">${photo.detection.source}</div>
      ${groupBadge}
    </div>
    <div class="meta">
      <div class="stars r${photo.rating ?? 0}">${stars(photo.rating)}</div>
      <div class="name">${photo.filename}</div>
      ${photo.error ? `<div class="why" style="color:var(--bad)">${photo.error}</div>` : `
      <table class="nums">
        <tr><td>subject</td><td>${num(photo.sharpness.subject_acutance)}</td></tr>
        <tr><td>background</td><td>${num(photo.sharpness.background_acutance)}</td></tr>
        <tr><td>ratio</td><td>${num(photo.sharpness.subject_background_ratio)}</td></tr>
        <tr><td>vs frame peak</td><td>${num(photo.sharpness.subject_relative_acutance)}</td></tr>
        <tr><td>peak local</td><td>${num(photo.sharpness.max_local_acutance)}</td></tr>
        <tr><td>in focus</td><td>${num(photo.sharpness.sharp_fraction * 100, 0)}%</td></tr>
        <tr><td>blur looks like</td><td>${photo.blur.likely_cause}</td></tr>
        <tr><td>clipped</td><td>${num(photo.exposure.highlight_clipped * 100, 1)}%</td></tr>
      </table>
      <div class="why">${(photo.reasons || []).join(" &middot; ")}</div>`}
    </div>`;

  const frame = element.querySelector(".frame");
  if (photo.thumbnail) frame.addEventListener("click", () => openViewer(photo));
  return element;
}

function render() {
  const rows = visible();
  const grid = $("grid");
  grid.innerHTML = "";
  if (!rows.length) {
    grid.innerHTML = '<div class="empty">Nothing matches these filters.</div>';
  } else {
    const fragment = document.createDocumentFragment();
    rows.forEach(photo => fragment.appendChild(card(photo)));
    grid.appendChild(fragment);
  }
  $("count").textContent = `showing ${rows.length} of ${DATA.photos.length}`;
}

let current = null, dragStart = null;

function openViewer(photo) {
  current = photo;
  $("dlgName").textContent = photo.filename;
  $("dlgImg").src = photo.thumbnail;
  $("dlgWhy").textContent =
    `${photo.detection.source}: ${photo.detection.note || "no note"} \\u2014 ${(photo.reasons || []).join("; ")}`;
  const drawn = $("drawn");
  const existing = manual[photo.filename] || photo.detection.box;
  if (existing) {
    Object.assign(drawn.style, {
      display: "block", left: existing.x * 100 + "%", top: existing.y * 100 + "%",
      width: existing.w * 100 + "%", height: existing.h * 100 + "%"
    });
  } else { drawn.style.display = "none"; }
  $("viewer").showModal();
}

function relative(event) {
  const rect = $("dlgImg").getBoundingClientRect();
  return {
    x: Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1),
    y: Math.min(Math.max((event.clientY - rect.top) / rect.height, 0), 1)
  };
}

$("canvasWrap").addEventListener("pointerdown", (event) => {
  dragStart = relative(event);
  $("canvasWrap").setPointerCapture(event.pointerId);
});
$("canvasWrap").addEventListener("pointermove", (event) => {
  if (!dragStart) return;
  const now = relative(event);
  const drawn = $("drawn");
  Object.assign(drawn.style, {
    display: "block",
    left: Math.min(dragStart.x, now.x) * 100 + "%",
    top: Math.min(dragStart.y, now.y) * 100 + "%",
    width: Math.abs(now.x - dragStart.x) * 100 + "%",
    height: Math.abs(now.y - dragStart.y) * 100 + "%"
  });
});
$("canvasWrap").addEventListener("pointerup", (event) => {
  if (!dragStart) return;
  const now = relative(event);
  const box = {
    x: Math.min(dragStart.x, now.x), y: Math.min(dragStart.y, now.y),
    w: Math.abs(now.x - dragStart.x), h: Math.abs(now.y - dragStart.y)
  };
  dragStart = null;
  if (box.w > 0.01 && box.h > 0.01) manual[current.filename] = box;
});

$("saveBox").addEventListener("click", () => {
  if (current && manual[current.filename]) $("viewer").close();
});
$("clearBox").addEventListener("click", () => {
  if (current) { delete manual[current.filename]; $("drawn").style.display = "none"; }
});
$("closeDlg").addEventListener("click", () => $("viewer").close());

$("exportSubjects").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(manual, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "photocull-subjects.json";
  link.click();
  URL.revokeObjectURL(link.href);
});

for (const id of ["sort", "desc", "minStars", "detector", "bestOnly", "problemsOnly", "search"]) {
  $(id).addEventListener("input", render);
}

const s = DATA.summary;
$("summary").innerHTML =
  `<b>${s.analysed}</b> analysed, <b>${s.failed}</b> failed &middot; ` +
  `<b>${s.groups}</b> groups, <b>${s.in_multi_frame_groups}</b> frames have near-duplicates &middot; ` +
  `subject located in <b>${s.subject_found}</b> &middot; ` +
  `blue box = subject, amber ring = sharpest point`;

buildDetectorFilter();
render();
</script>
</body>
</html>
"""


class ContactSheetWriter:
    """Writes the self-contained HTML review page."""

    name = "html"
    extension = ".html"

    def write(self, reports: Sequence[PhotoReport], directory: Path, config: Config) -> Path:
        from ..pipeline import summarise

        payload = {
            "summary": summarise(reports),
            "photos": [report.as_dict(include_thumb=True) for report in reports],
        }
        target = directory / f"contact-sheet{self.extension}"
        # json.dumps output is embedded in a <script> block, so the one sequence
        # that could break out of it has to be neutralised.
        encoded = json.dumps(payload, default=str).replace("</", "<\\/")
        target.write_text(_PAGE.replace(_PAYLOAD_TOKEN, encoded), encoding="utf-8")
        return target
