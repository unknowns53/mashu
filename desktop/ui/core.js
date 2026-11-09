// What every tab shares. Every change goes through `mashu desktop-backend`; nothing is
// decided here. Stored text is only ever set as textContent, never parsed as markup.
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);

const app = { busy: false, current: null, tabs: new Map() };

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

function errorText(error) {
  return String((error && error.message) || error);
}

function say(text, kind = "") {
  const message = $("message");
  message.textContent = text;
  message.className = kind;
  message.hidden = !text;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function short(id) {
  return String(id).slice(0, 8);
}

function day(value) {
  return String(value || "").slice(0, 10);
}

function ageDays(value) {
  const at = Date.parse(value);
  return Number.isNaN(at) ? 0 : Math.max(0, Math.floor((Date.now() - at) / 86400000));
}

/** The id still on screen if it is, otherwise the first row's. */
function keepSelection(rows, idOf, current) {
  if (rows.some((row) => idOf(row) === current)) return current;
  return rows.length ? idOf(rows[0]) : null;
}

/**
 * Fill a clickable list. `text` and `sub` give each row's two lines; `mark` may give a
 * leading {text, className}; `dim` greys a row out.
 */
function fillList(listId, rows, { idOf, selected, text, sub, mark, dim, pick }) {
  $(listId).replaceChildren(
    ...rows.map((row) => {
      const item = element("li");
      item.setAttribute("aria-selected", String(idOf(row) === selected));
      if (dim && dim(row)) item.classList.add("dim");
      const line = element("span", undefined, "line");
      const leading = mark && mark(row);
      if (leading) line.append(element("span", `${leading.text} `, leading.className));
      line.append(text(row));
      item.append(line, element("span", sub(row), "sub"));
      item.addEventListener("click", () => pick(idOf(row)));
      return item;
    })
  );
}

/** Label and value pairs as a definition list, lists shown one item per line. */
function fillFields(listId, pairs, empty = "(nothing written)") {
  const shown = pairs.filter(
    ([, value]) => value !== null && value !== undefined && value !== "" &&
      (!Array.isArray(value) || value.length)
  );
  const list = $(listId);
  list.replaceChildren(
    ...shown.flatMap(([label, value]) => [
      element("dt", label),
      element("dd", Array.isArray(value) ? value.map((item) => `• ${item}`).join("\n") : String(value)),
    ])
  );
  if (!shown.length) list.replaceChildren(element("dd", empty, "muted"));
}

/** A titled section around the nodes given; null and false children are skipped. */
function block(title, ...children) {
  const node = element("section");
  if (title) node.append(element("h3", title));
  node.append(...children.filter(Boolean));
  return node;
}

/** Label and value pairs as a definition list node, leaving out the empty ones. */
function pairs(list) {
  const node = element("dl");
  for (const [label, value] of list) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value) && !value.length) continue;
    node.append(
      element("dt", label),
      element("dd", Array.isArray(value) ? value.map((item) => `• ${item}`).join("\n") : String(value))
    );
  }
  return node;
}

/** A list whose items are render(item): a string or an array of nodes and strings. */
function bullets(list, render, empty = "none") {
  if (!list || !list.length) return element("p", empty, "muted");
  const node = element("ul");
  for (const entry of list) {
    const item = element("li");
    const shown = render(entry);
    item.append(...(Array.isArray(shown) ? shown : [shown]));
    node.append(item);
  }
  return node;
}

function rowOf(...nodes) {
  const node = element("div", undefined, "row");
  node.append(...nodes);
  return node;
}

function button(text, onClick, className) {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

/** Fill a select with [value, label] pairs, keeping the chosen value where it still exists. */
function fillSelect(selectId, options, chosen) {
  const select = $(selectId);
  select.replaceChildren(
    ...options.map(([value, label]) => {
      const option = element("option", label);
      option.value = value;
      return option;
    })
  );
  if (chosen !== undefined && options.some(([value]) => value === chosen)) select.value = chosen;
}

/**
 * Register a tab. A tab may define load() to read what it shows, buttons() to set which
 * of its controls can be used, and status(s) to show its count on the tab.
 */
function tab(name, spec) {
  app.tabs.set(name, spec);
  $(`tab-${name}`).addEventListener("click", () => show(name).catch((e) => say(errorText(e), "error")));
}

async function show(name) {
  app.current = name;
  $("prompt").hidden = true;
  closeForms();
  for (const other of app.tabs.keys()) {
    $(`tab-${other}`).setAttribute("aria-selected", String(other === name));
    $(`pane-${other}`).hidden = other !== name;
  }
  await reload();
}

function updateButtons() {
  for (const spec of app.tabs.values()) if (spec.buttons) spec.buttons();
}

function showStatus(status) {
  for (const spec of app.tabs.values()) if (spec.status) spec.status(status);
  if (status.schema_pending.length) {
    say("The store is behind the code; run 'mashu admin migrate'.", "warn");
  }
}

async function reload(keepMessage = false) {
  const spec = app.tabs.get(app.current);
  const [status] = await Promise.all([call("status"), spec.load ? spec.load() : null]);
  if (!keepMessage) say("");
  showStatus(status);
  updateButtons();
}

/**
 * Carry one decision to the store, then read everything again. work() may return a
 * warning string to show instead of `done`; anything else it returns is ignored.
 * handle(error) may take a refusal over, returning true.
 */
async function act(work, done, handle) {
  if (app.busy) return;
  app.busy = true;
  $("prompt").hidden = true;
  updateButtons();
  try {
    const returned = await work();
    const note = typeof returned === "string" ? returned : "";
    closeForms();
    await reload(true);
    say(note || done, note ? "warn" : "");
  } catch (error) {
    if (!(handle && handle(error))) say(errorText(error), "error");
  } finally {
    app.busy = false;
    updateButtons();
  }
}

/** Ask once before something that cannot be taken back; nothing happens on Cancel. */
function confirmBox(text, lines, yesLabel, onYes) {
  const box = $("prompt");
  const yes = element("button", yesLabel, "primary");
  const no = element("button", "Cancel");
  yes.type = no.type = "button";
  yes.addEventListener("click", () => {
    box.hidden = true;
    onYes();
  });
  no.addEventListener("click", () => {
    box.hidden = true;
  });
  const row = element("div", undefined, "row");
  row.append(yes, no);
  box.replaceChildren(element("p", text), ...lines.map((line) => element("p", line, "muted")), row);
  box.hidden = false;
  yes.focus();
}

/**
 * Send a write that may repeat retired rules. When it does, show why they were retired
 * and offer to send it again acknowledging exactly those.
 */
function acknowledging(send, done) {
  act(() => send(undefined), done, (error) => {
    const conflicts = error.reply && error.reply.conflicts;
    if (!conflicts) return false;
    say(errorText(error), "warn");
    confirmBox(
      "This repeats rules that were retired. Read why before writing it back.",
      conflicts.map(
        (row) =>
          `${short(row.memory_id)} · ${row.retirement_kind || "legacy"} · ${day(row.retired_at)}: ` +
          `${row.retire_reason}`
      ),
      "Write it anyway",
      () => act(() => send(conflicts.map((row) => row.memory_id)), done)
    );
    return true;
  });
}

/**
 * Show a form in the host element. Each field is {name, label, kind, value, options,
 * hint, placeholder, show}: kind is text, area, lines (one item per line), select, or
 * number; show(values) hides the field while it returns false.
 */
function showForm(hostId, { title, fields, submit = "Save", onSubmit }) {
  const host = $(hostId);
  const form = element("form", undefined, "editor");
  form.append(element("h3", title));
  const inputs = [];
  for (const field of fields) {
    const label = element("label", undefined, "field");
    label.append(element("span", field.label));
    let input;
    if (field.kind === "area" || field.kind === "lines") {
      input = element("textarea");
      input.rows = field.rows || (field.kind === "lines" ? 4 : 5);
      input.value = field.kind === "lines" ? (field.value || []).join("\n") : field.value || "";
    } else if (field.kind === "select") {
      input = element("select");
      for (const [value, text] of field.options) {
        const option = element("option", text);
        option.value = value;
        input.append(option);
      }
      if (field.value !== undefined && field.value !== null) input.value = field.value;
    } else {
      input = element("input");
      input.type = field.kind === "number" ? "number" : "text";
      if (field.step) input.step = field.step;
      input.value = field.value ?? "";
    }
    if (field.placeholder) input.placeholder = field.placeholder;
    label.append(input);
    if (field.hint) label.append(element("small", field.hint, "muted"));
    inputs.push({ field, input, label });
    form.append(label);
  }
  const values = () =>
    Object.fromEntries(
      inputs.map(({ field, input }) => [
        field.name,
        field.kind === "lines"
          ? input.value.split("\n").map((line) => line.trim()).filter(Boolean)
          : field.kind === "number"
            ? Number(input.value)
            : input.value,
      ])
    );
  const refresh = () => {
    const current = values();
    for (const { field, label } of inputs) if (field.show) label.hidden = !field.show(current);
  };
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  refresh();

  const save = element("button", submit, "primary");
  save.type = "submit";
  const cancel = element("button", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", closeForms);
  const row = element("div", undefined, "row");
  row.append(save, cancel);
  form.append(row);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    onSubmit(values());
  });
  host.replaceChildren(form);
  host.hidden = false;
  const first = form.querySelector("input, textarea, select");
  if (first) first.focus();
}

function closeForms() {
  for (const host of document.querySelectorAll(".form-host")) {
    host.hidden = true;
    host.replaceChildren();
  }
}

/** Whether a search occurs, ignoring case, in any of the values given. */
function matches(query, ...values) {
  if (!query) return true;
  const needle = query.toLowerCase();
  return values.some((value) => JSON.stringify(value ?? "").toLowerCase().includes(needle));
}

/** Wire a row of view buttons: each has data-view, and the chosen one is aria-pressed. */
function viewButtons(groupId, onChange) {
  const buttons = [...$(groupId).querySelectorAll("button[data-view]")];
  for (const button of buttons) {
    button.addEventListener("click", () => {
      for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
      onChange(button.dataset.view);
    });
  }
}

/** The three deliveries, each in one plain line. */
const DELIVERIES = [
  ["always", "always — every session, at start"],
  ["scope", "scope — sessions in one Scope, at start"],
  ["topic", "topic — its trigger is listed; read when that work begins"],
];

/** Options for a topic picker: none, each open topic, and a new one described in place. */
function topicOptions(topics, { none = "choose a topic…" } = {}) {
  return [
    ["", none],
    ...topics.map((topic) => [topic.topic_id, `${topic.name} — ${topic.trigger}`]),
    ["__new", "a new topic…"],
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

/** The fields that choose a topic, shown while the delivery field says topic. */
function topicFields(choices, current, isTopic) {
  const isNew = (values) => isTopic(values) && values.topic === "__new";
  return [
    { name: "topic", label: "topic", kind: "select", options: topicOptions(choices.topics), value: current || "", show: isTopic },
    { name: "new_topic_name", label: "new topic name", show: isNew },
    { name: "new_topic_trigger", label: "when to read it", hint: "one sentence", show: isNew },
    {
      name: "new_topic_scope",
      label: "listed in scope",
      kind: "select",
      options: [["", "every session"], ...choices.scopes.map((name) => [name, name])],
      show: isNew,
    },
  ];
}

/** Scope, project, and topic names for the forms, read fresh each time a form opens. */
function loadChoices() {
  return call("choices");
}

/** A typed reason, or a warning and focus on its box when it is empty. */
function required(inputId, what) {
  const value = $(inputId).value.trim();
  if (!value) {
    say(`Write ${what} first.`, "warn");
    $(inputId).focus();
  }
  return value;
}
