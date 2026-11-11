// What every page shares. Every change goes through `mashu desktop-backend`; nothing is
// decided here. Stored text is only ever set as textContent, never parsed as markup.
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);

const app = { busy: false, current: null, pages: new Map(), status: null, schemaWarned: false };

// ---------------------------------------------------------------- elements

/**
 * An element. `props` sets class, text, title, type, value, checked, disabled, hidden, for,
 * placeholder, and on (event handlers); anything else becomes an attribute. Null and false
 * children are skipped; strings become text.
 */
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "on") for (const [type, fn] of Object.entries(value)) node.addEventListener(type, fn);
    else if (["value", "checked", "disabled", "hidden", "title", "type", "placeholder", "rows"].includes(key)) node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const ICONS = {
  inbox: "M4 13h4l1.5 3h5L16 13h4M4 13l2.5-7.5A2 2 0 0 1 8.4 4h7.2a2 2 0 0 1 1.9 1.5L20 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z",
  rules: "M6 4h11a1 1 0 0 1 1 1v15l-6-3.5L6 20V5a1 1 0 0 1 0-1z",
  work: "M9 6h11M9 12h11M9 18h11M4.5 6l1 1 2-2M4.5 12l1 1 2-2M4.5 18l1 1 2-2",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-3.5-3.5",
  plus: "M12 5v14M5 12h14",
  close: "M6 6l12 12M18 6 6 18",
  check: "M5 12.5l4.5 4.5L19 7.5",
};

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function button(text, onClick, { kind = "", title, disabled, id } = {}) {
  return h("button", { type: "button", class: `btn ${kind}`.trim(), text, title, disabled, id, on: { click: onClick } });
}

// ---------------------------------------------------------------- values

function short(id) {
  return String(id || "").slice(0, 8);
}

/** The start of a long text, for a picker or a reference to another row. */
function excerpt(text, length = 48) {
  const chars = [...String(text || "").replace(/\s+/g, " ")];
  return chars.length > length ? `${chars.slice(0, length).join("")}…` : chars.join("");
}

function day(value) {
  return String(value || "").slice(0, 10);
}

/** Today, yesterday, n days ago, or the date when it is further back than a month. */
function when(value) {
  const at = Date.parse(value);
  if (Number.isNaN(at)) return day(value);
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const days = Math.ceil((start.getTime() - at) / 86400000);
  if (days <= 0) return t("common.today");
  if (days === 1) return t("common.yesterday");
  if (days <= 30) return t("common.days_ago", { n: days });
  return day(value);
}

function clock(date = new Date()) {
  return date.toLocaleTimeString(language === "ja" ? "ja-JP" : "en-US", { hour: "2-digit", minute: "2-digit" });
}

/** Who wrote something, with the user named as such. */
function who(actor) {
  if (actor === "user") return t("common.by_user");
  if (actor === "agent") return t("common.by_agent");
  return actor || "-";
}

/** A path from the home directory written from ~, as the store writes them. */
function tilde(path) {
  return String(path || "").replace(/^(\/Users\/[^/]+|\/home\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)(?=$|[/\\])/, "~");
}

/**
 * The account a ledger row gives. A rule recorded without a reason carries a fixed English
 * line in its place, said here in the window's language; null when nothing more was written.
 */
const FIXED_ACCOUNTS = [
  ["recorded by the user's own hand", "explicit"],
  ["a user instruction carried by an agent", "claimed"],
  ["a successor an agent drafted for its own replace proposal", "proposed"],
];

function fixedAccount(text) {
  const found = FIXED_ACCOUNTS.find(([start]) => String(text || "").startsWith(start));
  return found ? found[1] : null;
}

function errorText(error) {
  return String((error && error.message) || error);
}

/** Whether a search occurs, ignoring case, in any of the values given. */
function matches(query, ...values) {
  if (!query) return true;
  const needle = query.toLowerCase();
  return values.some((value) => JSON.stringify(value ?? "").toLowerCase().includes(needle));
}

// ---------------------------------------------------------------- the store

/** The result, or an Error whose `reply` carries the refusal's details (conflicts, candidates…). */
async function call(op, args = {}) {
  const reply = await invoke("backend", { op, args });
  if (!reply.ok) {
    const error = new Error(reply.error || "refused");
    error.reply = reply;
    throw error;
  }
  return reply.result;
}

function loadChoices() {
  return call("choices");
}

// ---------------------------------------------------------------- feedback

/** How many notes the corner holds at once; older ones give way. */
const TOAST_LIMIT = 3;

/**
 * A note in the corner: success fades on its own, a warning or error stays until closed.
 * A note already showing is not shown twice, so one failure repeated by several calls is one note.
 */
function toast(text, kind = "ok") {
  if (!text) return;
  const shown = [...$("toasts").children];
  if (shown.some((note) => note.querySelector(".text").textContent === text)) return;
  for (const old of shown.slice(0, Math.max(0, shown.length - TOAST_LIMIT + 1))) old.remove();
  const node = h("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, h("div", { class: "text", text }));
  const close = () => node.remove();
  node.append(h("button", { type: "button", class: "btn quiet icon small", title: t("common.close"), on: { click: close } }, icon("close")));
  $("toasts").append(node);
  if (kind === "ok") setTimeout(close, 3500);
}

function fail(error) {
  const text = errorText(error);
  const missing = /could not start/.test(text)
    ? "checkout"
    : /database ".*" does not exist/.test(text)
      ? "database"
      : /Is the server running|connection refused/i.test(text)
        ? "server"
        : null;
  if (missing) {
    showSetup(missing);
    return;
  }
  toast(text, "error");
}

// ---------------------------------------------------------------- first run

/** The note left for the window that opens after an install, read once. */
const SETUP_NOTE = "mashu.setup.done";

/**
 * In place of every page while Mashu cannot answer: install it or point at it when there is
 * no checkout, create its database, or wait for PostgreSQL. Each way out reloads the window.
 */
async function showSetup(missing) {
  if (app.setup === missing) return;
  app.setup = missing;
  document.body.classList.add("setup");
  const page = h("div", { class: "setup-page" });
  $("page").replaceChildren(page);
  if (missing === "database") {
    append(page, [
      h("h1", { class: "title", text: t("setup.database_title") }),
      h("p", { class: "sub", text: t("setup.database_lead") }),
      h("div", { class: "actions" }, setupButton(t("setup.database_create"), () => invoke("setup_database"))),
    ]);
    return;
  }
  if (missing === "server") {
    append(page, [
      h("h1", { class: "title", text: t("setup.server_title") }),
      h("p", { class: "sub", text: t("setup.server_lead") }),
      h("div", { class: "actions" }, button(t("setup.retry"), () => location.reload(), { kind: "primary" })),
    ]);
    return;
  }
  append(page, [
    h("h1", { class: "title", text: t("setup.title") }),
    h("p", { class: "sub", text: t("setup.lead") }),
    await installSection(),
    usePlaceSection(await invoke("checkout_get").catch(() => ({}))),
  ]);
}

/** A primary button that runs one setup step and reloads into the app when it is done. */
function setupButton(label, work, progress) {
  const node = button(
    label,
    async () => {
      node.disabled = true;
      const stop = await listen("setup-step", (event) => {
        if (progress) progress.textContent = t(`setup.step.${event.payload}`);
      });
      try {
        await work();
        location.reload();
      } catch (error) {
        toast(errorText(error), "error");
        if (progress) progress.textContent = "";
        node.disabled = false;
      } finally {
        stop();
      }
    },
    { kind: "primary" }
  );
  return node;
}

async function installSection() {
  const body = h("div");
  const input = h("input", { type: "text" });
  const render = async () => {
    const check = await invoke("setup_check", { path: input.value });
    if (!input.value) input.value = check.path;
    const needs = [
      ["git", check.git],
      ["uv", check.uv],
      ["postgres", check.postgres],
      ["postgres_running", check.postgres_running],
      ["free", check.free],
    ];
    const ready = needs.every(([, ok]) => ok);
    const progress = h("p", { class: "sub" });
    body.replaceChildren(
      h(
        "ul",
        { class: "checklist" },
        needs.map(([name, ok]) =>
          h(
            "li",
            { class: ok ? "ok" : "missing" },
            icon(ok ? "check" : "close"),
            h("span", { text: t(`setup.need.${name}`) }),
            ok ? null : h("span", { class: "sub", text: t(`setup.need.${name}.how`) })
          )
        )
      ),
      h(
        "div",
        { class: "actions" },
        ready
          ? setupButton(t("setup.install"), () => {
              try {
                localStorage.setItem(SETUP_NOTE, "1");
              } catch {
                // Without storage the note on registering agents is simply not shown.
              }
              return invoke("setup_install", { path: input.value });
            }, progress)
          : null,
        button(t("setup.recheck"), () => render().catch(fail))
      ),
      progress
    );
  };
  input.addEventListener("change", () => render().catch(fail));
  await render();
  return section(t("setup.install_title"), null, h("label", { class: "field" }, h("span", { class: "label", text: t("setup.folder") }), input), body);
}

function usePlaceSection(place) {
  const input = h("input", { type: "text", value: place.path || "", placeholder: "~/MyApp/mashu" });
  const save = async (event) => {
    event.preventDefault();
    try {
      await invoke("checkout_set", { path: input.value });
      location.reload();
    } catch (error) {
      const text = errorText(error);
      toast(/not a git checkout/.test(text) ? t("setup.not_checkout") : /has no/.test(text) ? t("setup.no_venv") : text, "error");
    }
  };
  return section(
    t("setup.use_title"),
    null,
    h(
      "form",
      { class: "row interval-row", on: { submit: save } },
      input,
      h("button", { type: "submit", class: "btn", text: t("setup.save") })
    ),
    h("p", { class: "sub", text: t("setup.hint") })
  );
}

/** After an install, say once that agents are registered by hand. */
function noteAfterSetup() {
  try {
    if (localStorage.getItem(SETUP_NOTE)) {
      localStorage.removeItem(SETUP_NOTE);
      toast(t("setup.agents"), "warn");
    }
  } catch {
    // Storage is a convenience here.
  }
}

/** Ask before something that cannot be taken back. Resolves true on the confirming button. */
function confirmDialog({ title, lines = [], list = [], confirm, danger = false }) {
  const dialog = $("dialog");
  return new Promise((resolve) => {
    const finish = (answer) => {
      dialog.close();
      resolve(answer);
    };
    const yes = button(confirm, () => finish(true), { kind: danger ? "danger solid" : "primary" });
    dialog.replaceChildren();
    append(dialog, [
      h("h1", { text: title }),
      lines.map((line) => h("p", { text: line })),
      list.length ? h("ul", null, list.map((line) => h("li", { text: line }))) : null,
      h("div", { class: "form-actions" }, button(t("common.cancel"), () => finish(false)), yes),
    ]);
    dialog.onclose = () => resolve(false);
    dialog.showModal();
    yes.focus();
  });
}

/**
 * Carry one decision to the store, then read everything again. work() may return a warning
 * string to show instead of `done`. handle(error) may take a refusal over, returning true.
 */
async function act(work, done, handle) {
  if (app.busy) return;
  app.busy = true;
  document.body.dataset.busy = "";
  try {
    const returned = await work();
    const note = typeof returned === "string" ? returned : "";
    await reload();
    toast(note || done, note ? "warn" : "ok");
  } catch (error) {
    if (!(handle && handle(error))) fail(error);
  } finally {
    app.busy = false;
    delete document.body.dataset.busy;
  }
}

/**
 * Send a write that may repeat retired rules. When it does, show why they were retired and
 * offer to send it again acknowledging exactly those.
 */
function acknowledging(send, done) {
  return act(() => send(undefined), done, (error) => {
    const conflicts = error.reply && error.reply.conflicts;
    if (!conflicts) return false;
    confirmDialog({
      title: t("conflicts.prompt"),
      list: conflicts.map(
        (row) => `${named("retire.kind", row.retirement_kind || "legacy")} · ${day(row.retired_at)} — ${row.retire_reason}`
      ),
      confirm: t("conflicts.write_anyway"),
      danger: true,
    }).then((yes) => {
      if (yes) act(() => send(conflicts.map((row) => row.memory_id)), done);
    });
    return true;
  });
}

// ---------------------------------------------------------------- pages

/**
 * Register a page. `spec` gives nav (the nav key and icon), root (built once), load() to read
 * what it shows, badge(status) for the number on its nav item, and keys for shortcuts.
 */
function page(name, spec) {
  app.pages.set(name, spec);
  const item = h(
    "button",
    { type: "button", class: "nav-item", id: `nav-${name}`, on: { click: () => show(name).catch(fail) } },
    icon(spec.icon),
    h("span", { class: "label", text: t(spec.nav) }),
    h("span", { class: "badge", hidden: true })
  );
  $("nav-items").append(item);
}

async function show(name) {
  app.current = name;
  for (const other of app.pages.keys()) {
    const item = $(`nav-${other}`);
    if (other === name) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  $("page").replaceChildren(app.pages.get(name).root);
  await reload();
}

async function reload() {
  const spec = app.pages.get(app.current);
  const [status] = await Promise.all([call("status"), spec.load ? spec.load() : null]);
  showStatus(status);
}

/** Remember what update_check found and offer a newer release under the navigation. */
function showUpdate(info) {
  app.update = info;
  const note = $("update-note");
  note.hidden = !info.available;
  note.textContent = t("update.note", { version: info.latest });
}

function showStatus(status) {
  app.status = status;
  for (const [name, spec] of app.pages) {
    const badge = $(`nav-${name}`).querySelector(".badge");
    const n = spec.badge ? spec.badge(status) : 0;
    badge.hidden = !n;
    badge.textContent = String(n);
  }
  $("checked").textContent = t("nav.checked", { time: clock() });
  if (status.schema_pending.length && !app.schemaWarned) {
    app.schemaWarned = true;
    toast(t("common.schema_behind"), "warn");
  }
}

// ---------------------------------------------------------------- the list pane

/** A list pane: a head (controls) over a scrolling list. */
function listPane(...head) {
  const list = h("div", { class: "list-scroll" });
  const pane = h("div", { class: "list-pane" }, head.length ? h("div", { class: "list-head" }, head) : null, list);
  return { pane, list };
}

function detailPane() {
  const inner = h("div", { class: "detail" });
  const pane = h("div", { class: "detail-pane" }, inner);
  let shown = null;
  /** Say what the pane now shows; it scrolls back to the top when that changed. */
  const mark = (key) => {
    if (key !== shown) pane.scrollTop = 0;
    shown = key;
  };
  return { pane, inner, mark };
}

/**
 * Fill a list. Each group is {title, count, rows}; each row is {id, primary, secondary
 * (strings), dim, flag}. The row with `selected` is marked; picking one calls onPick(id).
 */
function fillList(container, groups, { selected, onPick, empty, note }) {
  const total = groups.reduce((sum, group) => sum + group.rows.length, 0);
  const nodes = [];
  for (const group of groups) {
    if (!group.rows.length && group.title) continue;
    if (group.title) nodes.push(h("div", { class: "list-group" }, group.title, h("span", { class: "count", text: String(group.rows.length) })));
    nodes.push(
      h(
        "ul",
        { class: "list", role: "listbox" },
        group.rows.map((row) =>
          h(
            "li",
            {
              class: `item${row.dim ? " dim" : ""}`,
              role: "option",
              "aria-selected": String(row.id === selected),
              "data-id": row.id,
              on: { click: () => onPick(row.id) },
            },
            h("div", { class: "primary" }, row.flag ? h("span", { class: "flag", text: `${row.flag} ` }) : null, row.primary),
            row.secondary && row.secondary.filter(Boolean).length
              ? h("div", { class: "secondary" }, row.secondary.filter(Boolean).map((text) => h("span", { text })))
              : null
          )
        )
      )
    );
  }
  if (!total) nodes.push(h("p", { class: "list-empty", text: empty }));
  if (note) nodes.push(h("p", { class: "list-note", text: note }));
  container.replaceChildren(...nodes);
  const picked = container.querySelector('[aria-selected="true"]');
  if (picked) picked.scrollIntoView({ block: "nearest" });
}

/** Rows in display order, for moving the selection with the keyboard. */
function listIds(container) {
  return [...container.querySelectorAll(".item")].map((node) => node.dataset.id);
}

/**
 * The id still listed if it is. A row that left the list hands the selection to its nearest
 * neighbour in `before`, the order shown until now, looking below it first; otherwise the
 * first row is chosen.
 */
function keepSelection(ids, current, before = []) {
  if (ids.includes(current)) return current;
  const at = before.indexOf(current);
  if (at >= 0) {
    const near = [...before.slice(at + 1), ...before.slice(0, at).reverse()].find((id) => ids.includes(id));
    if (near) return near;
  }
  return ids.length ? ids[0] : null;
}

function searchBox(onInput) {
  const input = h("input", { type: "search", placeholder: t("common.search"), on: { input: () => onInput(input.value.trim()) } });
  input.dataset.search = "";
  return h("label", { class: "search" }, icon("search"), input);
}

/** A per-viewer setting kept in this window's storage; the fallback when there is none. */
function remembered(key, fallback) {
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    return saved ?? fallback;
  } catch (error) {
    return fallback;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (error) {
    // Kept for this window only.
  }
}

/**
 * A sort picker: which key, and which way. setKeys([[key, label]]) changes the keys a view
 * offers, keeping the chosen one where it still exists. The choice is kept per `name`.
 */
function sortControl(name, onChange) {
  const state = remembered(`mashu.sort.${name}`, { key: "updated", dir: "desc" });
  const select = h("select", { class: "sort-key", "aria-label": t("sort.label") });
  const flip = h("button", { type: "button", class: "btn small sort-dir" });
  const save = () => {
    remember(`mashu.sort.${name}`, state);
    flip.textContent = t(state.dir === "desc" ? "sort.desc" : "sort.asc");
  };
  select.addEventListener("change", () => {
    state.key = select.value;
    save();
    onChange();
  });
  flip.addEventListener("click", () => {
    state.dir = state.dir === "desc" ? "asc" : "desc";
    save();
    onChange();
  });
  const node = h("div", { class: "row sort-row" }, h("span", { class: "sort-label", text: t("sort.label") }), select, flip);
  node.state = state;
  node.setKeys = (keys) => {
    select.replaceChildren(...keys.map(([value, label]) => h("option", { value, text: label })));
    if (!keys.some(([value]) => value === state.key)) state.key = keys[0][0];
    select.value = state.key;
    save();
  };
  return node;
}

/** Keys that hold a time; any other key keeps each of its groups newest first. */
const DATE_KEYS = ["updated", "created", "until"];

/**
 * Rows ordered by valueOf(row, key), the way `state` says; empty values go last either way.
 * Under a key many rows share (delivery, project…), each group stays newest first whichever
 * way the groups run.
 */
function sortRows(rows, state, valueOf) {
  if (!DATE_KEYS.includes(state.key)) rows = sortRows(rows, { key: "updated", dir: "desc" }, valueOf);
  const sign = state.dir === "desc" ? -1 : 1;
  return rows
    .map((row, index) => ({ row, index, value: valueOf(row, state.key) }))
    .sort((a, b) => {
      const blankA = a.value === null || a.value === undefined || a.value === "";
      const blankB = b.value === null || b.value === undefined || b.value === "";
      if (blankA !== blankB) return blankA ? 1 : -1;
      if (!blankA && a.value !== b.value) return (a.value < b.value ? -1 : 1) * sign;
      return a.index - b.index;
    })
    .map((entry) => entry.row);
}

/** A row of view buttons; the chosen one is aria-pressed. Returns the control. */
function segmented(views, current, onChange) {
  const buttons = views.map(([value, label]) =>
    h("button", {
      type: "button",
      text: label,
      "aria-pressed": String(value === current),
      on: {
        click: () => {
          for (const other of buttons) other.setAttribute("aria-pressed", String(other === buttons[views.findIndex(([v]) => v === value)]));
          onChange(value);
        },
      },
    })
  );
  const control = h("div", { class: "seg" }, buttons);
  control.select = (value) => {
    buttons.forEach((node, index) => node.setAttribute("aria-pressed", String(views[index][0] === value)));
  };
  return control;
}

// ---------------------------------------------------------------- detail content

function section(title, count, ...children) {
  return h(
    "section",
    null,
    h("h2", null, title, count !== undefined && count !== null ? h("span", { class: "count", text: String(count) }) : null),
    children
  );
}

/** Label and value pairs. Empty values are left out, unless showEmpty marks them unwritten. */
function fieldList(pairs, { showEmpty = false } = {}) {
  const node = h("dl", { class: "fields" });
  for (const [label, value] of pairs) {
    const blank = value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length);
    if (blank && !showEmpty) continue;
    let shown;
    if (blank) shown = h("dd", { class: "empty", text: t("common.empty_field") });
    else if (Array.isArray(value)) shown = h("dd", null, h("ul", null, value.map((item) => h("li", { text: item }))));
    else shown = h("dd", null, value instanceof Node ? value : String(value));
    node.append(h("dt", { text: label }), shown);
  }
  return node;
}

/** A list of entries, each rendered as [when, text, ...notes]; notes are [label, text]. */
function entries(rows, render, { empty = t("common.none"), old } = {}) {
  if (!rows || !rows.length) return h("p", { class: "empty-note", text: empty });
  return h(
    "ul",
    { class: "entries" },
    rows.map((row) => {
      const [whenText, text, ...notes] = render(row);
      return h(
        "li",
        { class: `entry${old && old(row) ? " old" : ""}` },
        whenText ? h("div", { class: "when", text: whenText }) : null,
        h("div", { class: "text", text }),
        notes
          .filter((note) => note && note[1])
          .map(([label, value]) => h("div", { class: "note" }, label ? h("b", { text: label }) : null, value))
      );
    })
  );
}

/** Ledger kinds whose row records who wrote the rule; `prevention` holds its body then. */
const RECORDING_KINDS = ["explicit", "claimed", "proposed"];

/**
 * Evidence rows. A row that records who wrote the rule shows the reason written with it, or
 * one line saying who wrote it when no reason was; its prevention is only the body as first
 * written, which the revisions already show.
 */
function evidenceEntries(rows) {
  return entries(rows, (row) => {
    const id = row.ledger_id && short(row.ledger_id);
    if (RECORDING_KINDS.includes(row.kind)) {
      const fixed = fixedAccount(row.what);
      if (fixed) return [[id, day(row.created_at)].filter(Boolean).join(" · "), t(`evidence.text.${fixed}`)];
      return [[named("evidence.kind", row.kind), id, day(row.created_at)].filter(Boolean).join(" · "), row.what];
    }
    return [
      [named("evidence.kind", row.kind), id, day(row.created_at), row.created_by && who(row.created_by)].filter(Boolean).join(" · "),
      row.what,
      [t("evidence.prevention"), row.prevention],
    ];
  });
}

function conflictEntries(rows) {
  return entries(rows, (row) => [
    `${named("retire.kind", row.retirement_kind || "legacy")} · ${day(row.retired_at)}`,
    row.content || "",
    [t("retire.reason"), row.retire_reason],
  ]);
}

function meterRow(name, n, max) {
  const fill = h("span");
  const ratio = max ? n / max : 0;
  fill.style.width = `${Math.min(100, Math.round(ratio * 100))}%`;
  return h(
    "div",
    { class: "meter-row" },
    h("span", { class: "name", text: name }),
    h("div", { class: `meter${ratio > 1 ? " over" : ratio > 0.85 ? " high" : ""}` }, fill),
    h("span", { class: "value", text: `${n} / ${max}` })
  );
}

/** A metadata line; parts are text or nodes, joined by the language's separator. */
function meta(...parts) {
  const node = h("p", { class: "meta" });
  parts.filter(Boolean).forEach((part, index) => {
    if (index) node.append(language === "ja" ? "・" : " · ");
    node.append(part);
  });
  return node;
}

/** The short id agents and the CLI use; a click copies the whole id. */
function idTag(id) {
  return h("button", {
    type: "button",
    class: "id",
    text: short(id),
    title: t("common.copy_id"),
    on: {
      click: () =>
        navigator.clipboard
          .writeText(String(id))
          .then(() => toast(t("common.copied", { id: short(id) })))
          .catch(fail),
    },
  });
}

/** A reference to another row: its short id and the start of its text. */
function reference(id, text, length = 40) {
  return [short(id), excerpt(text, length)].filter(Boolean).join("  ");
}

// ---------------------------------------------------------------- forms

/**
 * A form in place of what a pane showed. Each field is {name, label, kind, value, options,
 * hint, max, maxItems, rows, show, big}: kind is text, area, lines (one item per line),
 * select, radio, or number; show(values) hides the field while it returns false; options
 * are [value, label, description]. Returns the form node.
 */
function form({ title, fields, submit, onSubmit, onCancel, kind = "primary" }) {
  const node = h("form", { class: "form" });
  if (title) node.append(h("h1", { class: "title", text: title }));
  const inputs = [];
  for (const field of fields) {
    const counter = h("span", { class: "counter" });
    let input;
    if (field.kind === "area" || field.kind === "lines") {
      input = h("textarea", { rows: field.rows || (field.kind === "lines" ? 4 : 3), class: field.big ? "big" : "" });
      input.value = field.kind === "lines" ? (field.value || []).join("\n") : field.value || "";
    } else if (field.kind === "select") {
      input = h("select", null, field.options.map(([value, label]) => h("option", { value, text: label })));
      if (field.value !== undefined && field.value !== null) input.value = field.value;
    } else if (field.kind === "radio") {
      input = h(
        "div",
        { class: "radio-list" },
        field.options.map(([value, label, description]) =>
          h(
            "label",
            null,
            h("input", { type: "radio", name: `f-${field.name}`, value, checked: value === field.value }),
            h("span", null, h("div", { class: "what", text: label }), description ? h("div", { class: "why", text: description }) : null)
          )
        )
      );
    } else {
      input = h("input", { type: field.kind === "number" ? "number" : "text", placeholder: field.placeholder });
      if (field.step) input.step = field.step;
      input.value = field.value ?? "";
    }
    const label = h(
      "label",
      { class: "field" },
      h("span", { class: "label" }, h("span", { text: field.label }), field.max || field.maxItems ? counter : null),
      input,
      field.hint ? h("div", { class: "hint", text: field.hint }) : null
    );
    inputs.push({ field, input, label, counter });
    node.append(label);
  }
  const valueOf = ({ field, input }) => {
    if (field.kind === "radio") {
      const chosen = input.querySelector("input:checked");
      return chosen ? chosen.value : "";
    }
    if (field.kind === "lines") return input.value.split("\n").map((line) => line.trim()).filter(Boolean);
    if (field.kind === "number") return Number(input.value);
    return input.value;
  };
  const values = () => Object.fromEntries(inputs.map((entry) => [entry.field.name, valueOf(entry)]));
  const refresh = () => {
    const current = values();
    for (const entry of inputs) {
      const { field, label, counter } = entry;
      if (field.show) label.hidden = !field.show(current);
      if (field.maxItems) {
        const n = current[field.name].length;
        counter.textContent = t("common.items", { n, max: field.maxItems });
        counter.classList.toggle("over", n > field.maxItems);
      } else if (field.max) {
        const n = [...String(current[field.name])].length;
        counter.textContent = t("common.chars", { n, max: field.max });
        counter.classList.toggle("over", n > field.max);
      }
    }
  };
  const grow = (area) => {
    area.style.height = "auto";
    area.style.height = `${area.scrollHeight + 2}px`;
  };
  node.addEventListener("input", (event) => {
    if (event.target.tagName === "TEXTAREA") grow(event.target);
    refresh();
  });
  node.addEventListener("change", refresh);
  refresh();

  const save = h("button", { type: "submit", class: `btn ${kind}`, text: submit || t("common.save") });
  node.append(
    h(
      "div",
      { class: "form-actions" },
      save,
      button(t("common.cancel"), () => onCancel && onCancel()),
      h("span", { class: "hint", text: t("common.save_hint") })
    )
  );
  node.addEventListener("submit", (event) => {
    event.preventDefault();
    onSubmit(values());
  });
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      node.requestSubmit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      if (onCancel) onCancel();
    }
  });
  requestAnimationFrame(() => {
    for (const area of node.querySelectorAll("textarea")) grow(area);
    const first = node.querySelector("textarea, input:not([type=radio]), select");
    if (first) {
      first.focus();
      if (first.setSelectionRange && first.value) first.setSelectionRange(first.value.length, first.value.length);
    }
  });
  return node;
}

/** A small panel asking for a reason before a decision; the reason is required. */
function reasonPanel({ label, confirm, danger, onConfirm, onCancel }) {
  const panel = h("div", { class: "inline-panel" });
  const formNode = form({
    fields: [{ name: "reason", label, kind: "area", rows: 2 }],
    submit: confirm,
    kind: danger ? "danger solid" : "primary",
    onCancel,
    onSubmit: (values) => {
      const reason = values.reason.trim();
      if (!reason) {
        toast(t("common.reason_required"), "warn");
        return;
      }
      onConfirm(reason);
    },
  });
  panel.append(formNode);
  return panel;
}

// ---------------------------------------------------------------- delivery fields

function deliveryOptions() {
  return ["always", "scope", "topic"].map((value) => [value, t(`delivery.${value}`), t(`delivery.${value}_long`).replace(/^[^（—]*[（—]\s*/, "").replace(/）$/, "")]);
}

/** Options for a topic picker: none, each open topic, and a new one described in place. */
function topicOptions(topics) {
  return [
    ["", t("delivery.topic_choose")],
    ...topics.map((topic) => [topic.topic_id, `${topic.name} — ${topic.trigger}`]),
    ["__new", t("delivery.topic_new")],
  ];
}

/** The topic part of a request from a form with topic and new_topic_* fields. */
function topicArgs(values) {
  if (values.topic === "__new") {
    return {
      new_topic: {
        name: values.new_topic_name,
        trigger: values.new_topic_trigger,
        scope: values.new_topic_scope || null,
      },
    };
  }
  return values.topic ? { topic_id: values.topic } : {};
}

/** The fields that choose a topic, shown while isTopic(values). */
function topicFields(choices, current, isTopic) {
  const isNew = (values) => isTopic(values) && values.topic === "__new";
  return [
    { name: "topic", label: t("delivery.topic_pick"), kind: "select", options: topicOptions(choices.topics), value: current || "", show: isTopic },
    { name: "new_topic_name", label: t("delivery.topic_new_name"), show: isNew, max: 40 },
    { name: "new_topic_trigger", label: t("delivery.topic_new_trigger"), hint: t("delivery.topic_new_trigger_hint"), show: isNew, max: 160 },
    {
      name: "new_topic_scope",
      label: t("delivery.topic_new_area"),
      kind: "select",
      options: [["", t("topic.area_all")], ...choices.scopes.map((name) => [name, name])],
      show: isNew,
    },
  ];
}

function areaOptions(choices, none) {
  return [["", none], ...choices.scopes.map((name) => [name, name])];
}

function deliveryText(row) {
  if (row.delivery === "topic") return `${t("delivery.topic")}: ${row.topic_name}`;
  if (row.delivery === "scope") return `${t("delivery.scope")}: ${row.scope_name || "-"}`;
  return t("delivery.always") + (row.scope_name ? ` (${row.scope_name})` : "");
}

// ---------------------------------------------------------------- keyboard

/** Up and down (or j and k) move through the list; / searches; a page may add its own keys. */
document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented || $("dialog").open || event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  const typing = target.closest && target.closest("input, textarea, select");
  if (typing) {
    if (event.key === "Escape" && target.dataset.search !== undefined) target.blur();
    return;
  }
  const spec = app.pages.get(app.current);
  if (!spec) return;
  if (event.key === "/") {
    const search = spec.root.querySelector("[data-search]");
    if (search) {
      event.preventDefault();
      search.focus();
    }
    return;
  }
  const step = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1 }[event.key];
  if (step && spec.list && spec.pick) {
    const ids = listIds(spec.list);
    if (!ids.length) return;
    event.preventDefault();
    const at = ids.indexOf(spec.selected());
    spec.pick(ids[Math.max(0, Math.min(ids.length - 1, at < 0 ? 0 : at + step))]);
    return;
  }
  const handler = spec.keys && spec.keys[event.key];
  if (handler) {
    event.preventDefault();
    handler();
  }
});
