// Thriftly frontend: plain DOM, no build step. Listing text comes from eBay,
// so it is only ever inserted with textContent / attributes, never innerHTML.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const POLL_MS = 2500;

const state = { user: null, searches: [], selectedId: null, pollTimer: null, authMode: "login" };

// ---------- helpers ----------------------------------------------------------

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== "/api/me") { showAuth(); throw new Error("Signed out"); }
  if (!res.ok) {
    let msg = `Something went wrong (${res.status}).`;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") msg = data.detail;
      else if (Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg).join(" ");
    } catch (_) { /* not JSON */ }
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return res.status === 204 ? null : res.json();
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}

const money = (cents) => `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const isBusy = (s) => s.state === "queued" || s.state === "running";
const STATE_LABEL = { queued: "Queued", running: "Searching", ok: "Done", no_results: "No matches", error: "Failed" };

function timeAgo(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function showError(id, msg) { const n = $(id); n.textContent = msg || ""; n.hidden = !msg; }

// ---------- auth ---------------------------------------------------------------

function setAuthMode(mode) {
  state.authMode = mode;
  $("#tab-login").setAttribute("aria-selected", mode === "login");
  $("#tab-signup").setAttribute("aria-selected", mode === "signup");
  $("#auth-submit").textContent = mode === "login" ? "Sign in" : "Create account";
  $("#auth-form input[name=password]").autocomplete = mode === "login" ? "current-password" : "new-password";
  showError("#auth-error", "");
}

function showAuth() {
  state.user = null;
  clearTimeout(state.pollTimer);
  $("#app-view").hidden = true; $("#account").hidden = true; $("#auth-view").hidden = false;
  $("#auth-form input[name=email]").focus();
}

async function showApp(user) {
  state.user = user;
  $("#account-email").textContent = user.email;
  $("#auth-view").hidden = true; $("#account").hidden = false; $("#app-view").hidden = false;
  await loadSearches();
  const fromHash = Number(location.hash.replace("#search-", ""));
  const pick = state.searches.find((s) => s.id === fromHash) || state.searches[0];
  if (pick) selectSearch(pick.id); else $("#search-input").focus();
}

$("#tab-login").addEventListener("click", () => setAuthMode("login"));
$("#tab-signup").addEventListener("click", () => setAuthMode("signup"));

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const btn = $("#auth-submit"); btn.disabled = true;
  try {
    const user = await api(`/api/auth/${state.authMode}`, {
      method: "POST", body: { email: form.get("email"), password: form.get("password") },
    });
    e.target.reset();
    await showApp(user);
  } catch (err) {
    showError("#auth-error", err.message);
  } finally { btn.disabled = false; }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  history.replaceState(null, "", "/");
  showAuth();
});

// ---------- searches list ---------------------------------------------------------

async function loadSearches() {
  state.searches = await api("/api/searches");
  renderSearchList();
}

function renderSearchList() {
  const list = $("#search-list");
  list.replaceChildren(...state.searches.map((s) => el("li", {},
    el("button", { "aria-current": s.id === state.selectedId ? "true" : "false", onclick: () => selectSearch(s.id) },
      el("span", { class: "q", text: s.raw_query, title: s.raw_query }),
      el("span", { class: "meta" },
        el("span", { class: `badge ${s.state}`, text: STATE_LABEL[s.state] || s.state }),
        el("span", { text: `${s.match_count} match${s.match_count === 1 ? "" : "es"}` }),
        s.watching ? el("span", { text: "· watching" }) : null)))));
  $("#search-list-empty").hidden = state.searches.length > 0;
}

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#search-input");
  const query = input.value.trim();
  if (query.length < 3) { showError("#search-error", "Describe what you're looking for."); return; }
  showError("#search-error", "");
  try {
    const s = await api("/api/searches", { method: "POST", body: { query, watch: $("#search-watch").checked } });
    input.value = "";
    await loadSearches();
    selectSearch(s.id);
  } catch (err) { showError("#search-error", err.message); }
});

// ---------- search detail ---------------------------------------------------------

async function selectSearch(id) {
  state.selectedId = id;
  history.replaceState(null, "", `#search-${id}`);
  renderSearchList();
  await refreshDetail();
}

async function refreshDetail() {
  clearTimeout(state.pollTimer);
  const id = state.selectedId;
  if (!id) return;
  let search;
  try { search = await api(`/api/searches/${id}`); }
  catch (err) {
    if (err.status === 404) { state.selectedId = null; await loadSearches(); renderEmptyDetail(); }
    return;
  }
  if (id !== state.selectedId) return; // user moved on
  renderDetail(search);
  // keep the list badge in sync
  const i = state.searches.findIndex((s) => s.id === id);
  if (i >= 0) { state.searches[i] = { ...state.searches[i], ...search }; renderSearchList(); }
  if (isBusy(search)) state.pollTimer = setTimeout(refreshDetail, POLL_MS);
}

function renderEmptyDetail() {
  $("#detail").replaceChildren(el("div", { class: "placeholder muted" }, el("p", { text: "Pick a search on the left, or start a new one." })));
}

function chipsFor(p) {
  if (!p) return [];
  const chips = [el("span", { class: "chip", text: `“${p.keywords}”` })];
  if (p.min_price_cents != null && p.max_price_cents != null) chips.push(el("span", { class: "chip", text: `${money(p.min_price_cents)}–${money(p.max_price_cents)}` }));
  else if (p.max_price_cents != null) chips.push(el("span", { class: "chip", text: `≤ ${money(p.max_price_cents)}` }));
  else if (p.min_price_cents != null) chips.push(el("span", { class: "chip", text: `≥ ${money(p.min_price_cents)}` }));
  for (const c of p.conditions || []) chips.push(el("span", { class: "chip", text: c }));
  return chips;
}

function statusLine(s) {
  if (isBusy(s)) return [el("span", { class: "spinner", "aria-hidden": "true" }),
    el("span", { text: "Searching eBay and checking each listing… usually 10–40 seconds." })];
  if (s.state === "error") return [el("span", { text: "The last run failed. Try “Refresh now” in a minute." })];
  const parts = [`${s.matches.length} match${s.matches.length === 1 ? "" : "es"}`,
    `updated ${timeAgo(s.last_refreshed_at)}`];
  if (s.broaden_count) parts.push(`broadened ${s.broaden_count}× to find enough`);
  if (s.watching) parts.push(`rechecked every\u00a0${s.requery_interval_minutes >= 60 ? `${s.requery_interval_minutes / 60}\u00a0h` : `${s.requery_interval_minutes}\u00a0min`}`);
  return [el("span", { text: parts.join(" · ") })];
}

function renderDetail(s) {
  const frag = $("#detail-template").content.cloneNode(true);
  $(".detail-title", frag).textContent = s.raw_query;
  $(".chips", frag).replaceChildren(...chipsFor(s.parsed_params));

  const watch = $(".watch-toggle", frag);
  watch.checked = s.watching;
  watch.addEventListener("change", async () => {
    try { await api(`/api/searches/${s.id}`, { method: "PATCH", body: { watch: watch.checked } }); await loadSearches(); }
    catch (err) { watch.checked = !watch.checked; alert(err.message); }
  });

  const refresh = $(".refresh", frag);
  refresh.disabled = isBusy(s);
  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    try { await api(`/api/searches/${s.id}/refresh`, { method: "POST" }); } catch (err) { alert(err.message); }
    refreshDetail();
  });

  $(".delete", frag).addEventListener("click", async () => {
    if (!confirm(`Delete “${s.raw_query}” and its matches?`)) return;
    await api(`/api/searches/${s.id}`, { method: "DELETE" });
    state.selectedId = null;
    history.replaceState(null, "", "/");
    await loadSearches();
    renderEmptyDetail();
  });

  $(".status", frag).replaceChildren(...statusLine(s));
  const grid = $(".grid", frag);
  if (!isBusy(s) && s.matches.length === 0 && s.state !== "error") {
    grid.append(el("p", { class: "muted", text: "No listings matched yet. If you’re watching this search, you’ll get an email when one shows up." }));
  }
  grid.append(...s.matches.map(card));
  $("#detail").replaceChildren(frag);
}

function card(m) {
  const dropped = m.first_price_cents && m.first_price_cents > m.price_cents;
  const score = m.relevance_score >= 0.9 ? ["Strong match", "score strong"] : ["Good match", "score"];
  const history = el("div", { class: "history" });
  const node = el("article", { class: `card${m.is_active ? "" : " inactive"}` },
    el("a", { class: "img", href: m.web_link, target: "_blank", rel: "noopener noreferrer", tabindex: "-1", "aria-hidden": "true" },
      m.image_url ? el("img", { src: m.image_url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null),
    el("div", { class: "body" },
      el("div", { class: "price" }, el("span", { text: money(m.price_cents) }),
        dropped ? el("span", { class: "was", text: money(m.first_price_cents) }) : null,
        dropped ? el("span", { class: "drop", text: `−${Math.round(100 * (1 - m.price_cents / m.first_price_cents))}%` }) : null),
      el("a", { class: "title", href: m.web_link, target: "_blank", rel: "noopener noreferrer", text: m.name }),
      el("div", { class: "foot" },
        el("span", { text: m.is_active ? (m.condition || "") : "Probably sold" }),
        el("span", { class: score[1], text: score[0] })),
      el("button", { class: "link small", text: "Price history", onclick: (e) => togglePriceHistory(m.id, history, e.target) })),
    history);
  return node;
}

async function togglePriceHistory(listingId, container, button) {
  if (container.childElementCount) { container.replaceChildren(); button.textContent = "Price history"; return; }
  button.textContent = "Loading…";
  try {
    const points = await api(`/api/listings/${listingId}/price-history`);
    container.replaceChildren(sparkline(points));
    button.textContent = "Hide price history";
  } catch (err) { button.textContent = "Price history"; alert(err.message); }
}

function sparkline(points) {
  const wrap = el("div", {});
  if (points.length < 2) {
    wrap.append(el("p", { class: "muted small", text: points.length ? `Same price since ${new Date(points[0].observed_at).toLocaleDateString()}.` : "No history yet." }));
    return wrap;
  }
  const W = 200, H = 40, pad = 4;
  const xs = points.map((p) => new Date(p.observed_at).getTime());
  const ys = points.map((p) => p.price_cents);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const sx = (x) => pad + (x1 === x0 ? 0 : (x - x0) / (x1 - x0)) * (W - 2 * pad);
  const sy = (y) => H - pad - (y1 === y0 ? 0.5 : (y - y0) / (y1 - y0)) * (H - 2 * pad);
  // step line: a price holds until the next observation
  let d = `M${sx(xs[0])},${sy(ys[0])}`;
  for (let i = 1; i < xs.length; i++) d += ` H${sx(xs[i])} V${sy(ys[i])}`;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Price went from ${money(ys[0])} to ${money(ys[ys.length - 1])}`);
  const path = document.createElementNS(ns, "path"); path.setAttribute("d", d); svg.append(path);
  const dot = document.createElementNS(ns, "circle");
  dot.setAttribute("cx", sx(xs[xs.length - 1])); dot.setAttribute("cy", sy(ys[ys.length - 1])); dot.setAttribute("r", 3);
  svg.append(dot);
  wrap.append(svg, el("p", { class: "muted small", text: `${money(ys[0])} → ${money(ys[ys.length - 1])} (${points.length} recorded prices)` }));
  return wrap;
}

// ---------- boot ----------------------------------------------------------------

(async function boot() {
  setAuthMode("login");
  try { await showApp(await api("/api/me")); }
  catch (_) { showAuth(); }
})();
