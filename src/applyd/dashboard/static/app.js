"use strict";
const $ = (id) => document.getElementById(id);
const state = { applications: [], view: "applied", query: "", period: "all", sort: "newest", page: 1, loaded: false, selected: null };
const pageSize = 12;
const names = {applied: "Applied", review: "Needs review", failed: "Failed", infra_error: "Interrupted", in_progress: "In progress", tailored: "Prepared", tested: "Test run", skipped: "Skipped", eligible: "Ready"};
const dateFormat = new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric", year: "numeric"});
const timeFormat = new Intl.DateTimeFormat(undefined, {hour: "numeric", minute: "2-digit"});
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function icon(name) { const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); const use = document.createElementNS(svg.namespaceURI, "use"); use.setAttribute("href", `#i-${name}`); svg.setAttribute("aria-hidden", "true"); svg.append(use); return svg; }
function date(value, time = false) { if (!value) return "Not recorded"; const d = new Date(value); if (Number.isNaN(d.getTime())) return "Not recorded"; return (time ? timeFormat : dateFormat).format(d); }
function avatar(company) { const hash = [...company].reduce((sum, char) => sum + char.charCodeAt(0), 0); const initials = company.split(/\s+/).slice(0, 2).map(s => s[0] || "").join("").toUpperCase(); return element("span", `company-avatar tone-${hash % 4}`, initials); }
function badge(status) { return element("span", `badge ${Object.hasOwn(names, status) ? status : "skipped"}`, names[status] || status); }
function link(href, label, className, iconName) { const a = element("a", className); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; if (iconName) a.append(icon(iconName)); a.append(document.createTextNode(label)); return a; }
function view(name) { state.view = name; state.page = 1; document.querySelectorAll("[data-view]").forEach(button => { const active = button.dataset.view === name; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); }); render(); }
function filtered() { const cutoff = Date.now() - Number(state.period) * 86400000; const q = state.query.toLowerCase(); return state.applications.filter(a => (state.view === "all" || a.status === state.view) && (!q || `${a.company} ${a.title} ${a.locations.join(" ")}`.toLowerCase().includes(q)) && (state.period === "all" || new Date(a.applied_at || a.last_activity_at).getTime() >= cutoff)).sort((a,b) => { if (state.sort === "company") return a.company.localeCompare(b.company) || a.title.localeCompare(b.title); const difference = (a.applied_at || a.last_activity_at || "").localeCompare(b.applied_at || b.last_activity_at || ""); return state.sort === "oldest" ? difference : -difference; }); }
function render() {
  const records = filtered(), pages = Math.max(1, Math.ceil(records.length / pageSize)); state.page = Math.min(state.page, pages);
  $("ledger-title").textContent = {applied: "Applied jobs", all: "All activity", review: "Needs review"}[state.view];
  $("result-count").textContent = records.length.toLocaleString();
  const body = $("rows"); body.replaceChildren();
  const start = (state.page - 1) * pageSize;
  for (const a of records.slice(start, start + pageSize)) {
    const tr = element("tr");
    const first = element("td"), job = element("div", "job-cell"), title = element("div");
    title.append(element("span", "company-name", a.company));
    const button = element("button", "role-button", a.title); button.addEventListener("click", () => showDetail(a)); title.append(button); job.append(avatar(a.company), title); first.append(job); tr.append(first);
    tr.append(element("td", "location-cell", a.locations.join(" / ") || "Location not listed"));
    const status = element("td"); status.append(badge(a.status)); tr.append(status);
    const when = element("td", "date-cell"); when.append(document.createTextNode(a.applied_at ? date(a.applied_at) : "—")); when.append(element("small", "", a.applied_at ? date(a.applied_at, true) : a.status === "applied" ? "Date not recorded" : "Not submitted")); tr.append(when);
    const resume = element("td"); if(a.resume_available) { const aLink = link(a.resume_url, "View PDF", "resume-link", "file"); aLink.setAttribute("aria-label", `View resume for ${a.title} at ${a.company}`); aLink.title = a.resume_provenance === "snapshot" ? "Saved application copy" : "Historical file; original version not verified"; resume.append(aLink); } else resume.append(element("span", "missing-resume", "Unavailable")); tr.append(resume);
    const last = element("td"), open = element("button", "row-open"); open.append(icon("next")); open.setAttribute("aria-label", `Details for ${a.title} at ${a.company}`); open.addEventListener("click", () => showDetail(a)); last.append(open); tr.append(last);
    tr.addEventListener("click", event => { if (!event.target.closest("button,a")) showDetail(a); }); body.append(tr);
  }
  $("empty").hidden = records.length > 0 || !state.loaded;
  const isFiltered = Boolean(state.query) || state.period !== "all";
  $("empty-title").textContent = isFiltered ? "No matching applications" : state.view === "review" ? "Your review queue is clear" : "Your application history starts here";
  $("empty-message").textContent = isFiltered ? "Try another company or role, or widen the date range." : state.view === "review" ? "Applications that need your input will appear here." : "Run applications with the applyd CLI. Confirmed submissions, dates, and saved resumes will appear here.";
  $("clear-filters").hidden = !isFiltered;
  $("page-info").textContent = records.length ? `Showing ${start + 1}–${Math.min(start + pageSize, records.length)} of ${records.length.toLocaleString()} applications` : state.loaded ? "0 applications" : "Loading applications…";
  $("page-number").textContent = `${state.page} / ${pages}`;
  $("previous").disabled = state.page <= 1; $("next").disabled = state.page >= pages;
}
function section(title) { const s = element("section", "detail-section"); s.append(element("h3", "", title)); return s; }
function showDetail(a) {
  state.selected = a.id;
  const content = $("detail-content"); content.replaceChildren();
  const company = element("div", "detail-company"); company.append(avatar(a.company), document.createTextNode(a.company)); content.append(company);
  const title = element("h2", "detail-title", a.title); title.id = "detail-title"; content.append(title, badge(a.status), element("p", "detail-location", a.locations.join(" / ") || "Location not listed"));
  if (a.url) content.append(link(a.url, "Open job posting", "resume-link", "arrow"));
  const facts = element("dl", "detail-facts");
  for (const [label, value] of [["Applied on", a.applied_at ? `${date(a.applied_at)} at ${date(a.applied_at, true)}` : a.status === "applied" ? "Date not recorded" : "Not confirmed"], ["Attempts", String(a.attempt_count)], ["Last activity", date(a.last_activity_at)], ["Source", a.source]]) { const pair = element("div"); pair.append(element("dt", "", label), element("dd", "", value)); facts.append(pair); } content.append(facts);
  const resume = section("Application resume");
  if (a.resume_available) {
    const card = element("div", "resume-card"), label = element("div"); label.append(element("strong", "", "Tailored resume.pdf"), element("small", "", a.resume_provenance === "snapshot" ? "Archived with this application attempt" : "Historical resume file")); card.append(icon("file"), label); resume.append(card);
    const actions = element("div", "resume-actions"); actions.append(link(a.resume_url, "Open resume", "button primary", "file"), link(a.resume_url + "?download=1", "Download", "button", "download")); resume.append(actions);
    resume.append(element("p", "detail-note", a.resume_provenance === "snapshot" ? "This saved copy preserves the PDF supplied to the application runner." : "This application predates saved resume snapshots. The linked file may have changed since the attempt; its original contents cannot be verified."));
  } else resume.append(element("p", "detail-note", "No PDF is available on this device for this application. It may have been moved, removed, or never generated."));
  content.append(resume);
  if(a.reason) { const outcome = section("Recorded outcome"); outcome.append(element("p", "outcome-note", a.reason)); content.append(outcome); }
  const attempts = section("Attempt history"), timeline = element("ol", "timeline");
  for (const attempt of a.attempts) { const li = element("li"); li.append(badge(attempt.status)); const stamp = element("time", "", `${date(attempt.started_at)} at ${date(attempt.started_at,true)}`); stamp.dateTime = attempt.started_at; li.append(stamp); if(attempt.reason) li.append(element("p", "", attempt.reason)); timeline.append(li); }
  if(a.attempts.length) attempts.append(timeline); else attempts.append(element("p", "detail-note", "No browser attempts have been recorded for this job.")); content.append(attempts);
  if (!$("detail").open) $("detail").showModal();
}
async function refresh() {
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/applications", {cache: "no-store"}); if (!response.ok) throw new Error("Could not load local history. Check that the dashboard server is running, then refresh.");
    const data = await response.json(); state.applications = data.applications; state.loaded = true; $("error").hidden = true;
    const confirmed = state.applications.filter(a => a.status === "applied").length, review = state.applications.filter(a => a.status === "review").length;
    $("confirmed-count").textContent = confirmed.toLocaleString(); $("review-count").textContent = review.toLocaleString(); $("nav-all").textContent = state.applications.length; $("nav-applied").textContent = confirmed; $("nav-review").textContent = review;
    $("updated").textContent = `Updated ${date(data.refreshed_at, true)}`;
    const bars = $("activity-bars"); bars.replaceChildren(); const maximum = Math.max(1, ...data.activity.map(d => d.count));
    const descriptions = [];
    for (const day of data.activity) { const slot = element("div", "bar-slot"), bar = element("div", "bar"); bar.style.height = `${Math.max(5, day.count / maximum * 100)}%`; slot.title = `${day.date}: ${day.count} confirmed`; descriptions.push(slot.title); slot.append(bar); bars.append(slot); }
    bars.setAttribute("aria-label", descriptions.join("; ")); $("activity-start").textContent = new Intl.DateTimeFormat(undefined,{month:"short",day:"numeric",timeZone:"UTC"}).format(new Date(data.activity[0].date + "T12:00:00Z"));
    render(); if ($("detail").open && state.selected) { const selected = state.applications.find(a => a.id === state.selected); if(selected) showDetail(selected); }
    if(!data.database_exists) { $("empty-title").textContent = "Set up your local workspace"; $("empty-message").textContent = "Run applyd init in your project folder, then refresh. Your application history will live here."; }
  } catch (error) { $("error").textContent = error.message; $("error").hidden = false; $("updated").textContent = state.loaded ? "Showing last loaded history" : "History unavailable"; $("page-info").textContent = state.loaded ? $("page-info").textContent : "Unable to load applications"; }
  finally { $("refresh").disabled = false; }
}
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => view(button.dataset.view)));
$("review-shortcut").addEventListener("click", () => view("review"));
$("search").addEventListener("input", event => {state.query = event.target.value;state.page = 1;render();});
for (const key of ["period", "sort"]) $(key).addEventListener("change", event => {state[key] = event.target.value;state.page = 1;render();});
$("clear-filters").addEventListener("click", () => {state.query="";state.period="all";state.page=1;$("search").value="";$("period").value="all";render();});
$("previous").addEventListener("click", () => {state.page--;render();}); $("next").addEventListener("click", () => {state.page++;render();});
$("refresh").addEventListener("click", refresh); $("close-detail").addEventListener("click", () => $("detail").close());
$("detail").addEventListener("click", event => { if (event.target === $("detail") && event.clientX < $("detail").getBoundingClientRect().left) $("detail").close(); });
document.addEventListener("keydown", event => { if(event.key === "/" && !$("detail").open && !["INPUT","TEXTAREA","SELECT"].includes(document.activeElement.tagName)) {event.preventDefault();$("search").focus();} });
$("timezone").textContent = Intl.DateTimeFormat().resolvedOptions().timeZone.replaceAll("_", " ");
refresh();
