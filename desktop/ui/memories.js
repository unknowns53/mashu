// Memory: active and retired rules and standing temporary notes, each read and edited in place.
"use strict";

const mem = {
  view: "active",
  rows: [],
  query: "",
  filters: { delivery: "", project: "", topic: "" },
  selected: null,
  detail: null,
  panel: null, // "edit", "delivery", "temporary", "retire", "restore", "reason", "to_memory"
  creating: null, // "memory" or "temporary" while the new form is shown
  contentLimit: null, // the Settings screen's limit on one memory body
};

const memSeg = segmented(
  [
    ["active", t("memory.view.active")],
    ["retired", t("memory.view.retired")],
    ["temporary", t("memory.view.temporary")],
  ],
  mem.view,
  (view) => {
    mem.view = view;
    mem.filters = { delivery: "", project: "", topic: "" };
    mem.selected = null;
    mem.panel = null;
    mem.creating = null;
    reload().catch(fail);
  }
);
const memNew = button("", () => {
  mem.creating = mem.view === "temporary" ? "temporary" : "memory";
  mem.panel = null;
  renderMemoryDetail();
}, { kind: "icon", title: t("memory.new") });
memNew.append(icon("plus"));
const memSort = sortControl("memories", () => {
  renderMemoryList();
});

const memFilterDelivery = h("select", { "aria-label": t("filter.delivery") });
const memFilterProject = h("select", { "aria-label": t("filter.project") });
const memFilterTopic = h("select", { "aria-label": t("filter.topic") });
const memFilterRow = h("div", { class: "filter-row" }, memFilterDelivery, memFilterProject, memFilterTopic);

function onMemoryFilterChange() {
  mem.filters.delivery = memFilterDelivery.value;
  mem.filters.project = memFilterProject.value;
  mem.filters.topic = memFilterTopic.value;
  mem.selected = keepSelection(shownMemories().map(memoryId), mem.selected);
  loadMemoryDetail().catch(fail);
}
memFilterDelivery.addEventListener("change", onMemoryFilterChange);
memFilterProject.addEventListener("change", onMemoryFilterChange);
memFilterTopic.addEventListener("change", onMemoryFilterChange);

function syncMemoryFilters() {
  const fillSelect = (select, options, current) => {
    select.replaceChildren(...options.map(([value, label]) => h("option", { value, text: label })));
    select.value = options.some(([value]) => value === current) ? current : "";
    return select.value;
  };
  if (mem.view === "temporary") {
    memFilterDelivery.hidden = true;
    memFilterTopic.hidden = true;
    mem.filters.delivery = "";
    mem.filters.topic = "";
    const projectNames = [...new Set(mem.rows.flatMap((row) => row.project_names || []))].sort();
    memFilterProject.hidden = !projectNames.length;
    mem.filters.project = fillSelect(
      memFilterProject,
      [
        ["", `${t("filter.project")}: ${t("common.all")}`],
        ["__all", t("common.every_area")],
        ...projectNames.map((name) => [name, name]),
      ],
      mem.filters.project
    );
    memFilterRow.hidden = memFilterProject.hidden;
    return;
  }
  const projectNames = [...new Set(mem.rows.flatMap((row) => row.project_names || []))].sort();
  const topicNames = [...new Set([...mem.rows.map((row) => row.topic_name).filter(Boolean), mem.filters.topic].filter(Boolean))].sort();
  memFilterDelivery.hidden = false;
  mem.filters.delivery = fillSelect(
    memFilterDelivery,
    [
      ["", `${t("filter.delivery")}: ${t("common.all")}`],
      ["always", t("delivery.always")],
      ["project", t("delivery.project")],
      ["topic", t("delivery.topic")],
    ],
    mem.filters.delivery
  );
  memFilterProject.hidden = !projectNames.length;
  mem.filters.project = fillSelect(
    memFilterProject,
    [["", `${t("filter.project")}: ${t("common.all")}`], ...projectNames.map((name) => [name, name])],
    mem.filters.project
  );
  memFilterTopic.hidden = !topicNames.length;
  mem.filters.topic = fillSelect(
    memFilterTopic,
    [["", `${t("filter.topic")}: ${t("common.all")}`], ...topicNames.map((name) => [name, name])],
    mem.filters.topic
  );
  memFilterRow.hidden = false;
}

/** The keys each view sorts by: dates it has, and the kind that groups its rows. */
function memorySortKeys() {
  if (mem.view === "temporary") {
    return [["created", t("sort.created")], ["until", t("sort.until")], ["kind", t("delivery.area")]];
  }
  return [
    ["updated", t("sort.updated")],
    ["created", t("sort.created")],
    ["kind", mem.view === "retired" ? t("retire.kind") : t("delivery.title")],
  ];
}

function memorySortValue(row, key) {
  if (key === "created") return row.created_at;
  if (key === "until") return row.expires_at;
  if (key === "kind") {
    if (mem.view === "temporary") return row.project_name || "";
    if (mem.view === "retired") return named("retire.kind", row.retirement_kind || "legacy");
    return `${["always", "project", "topic"].indexOf(row.delivery)} ${row.topic_name || row.project_name || ""}`;
  }
  return row.updated_at || row.created_at;
}

const memList = listPane(
  memSeg,
  h(
    "div",
    { class: "row search-row" },
    searchBox((query) => {
      mem.query = query;
      mem.selected = keepSelection(shownMemories().map(memoryId), mem.selected);
      loadMemoryDetail().catch(fail);
    }),
    memNew
  ),
  memFilterRow,
  memSort
);
const memDetail = detailPane();
const memOrder = heldOrder();

function memoryId(row) {
  return mem.view === "temporary" ? row.context_id : row.memory_id;
}

function memoryRow() {
  return mem.rows.find((row) => memoryId(row) === mem.selected) || null;
}

function shownMemories() {
  memSort.setKeys(memorySortKeys());
  syncMemoryFilters();
  const { delivery, project, topic } = mem.filters;
  const found = mem.rows.filter((row) => {
    if (mem.view === "temporary") {
      if (project === "__all" && (row.project_names || []).length) return false;
      if (project && project !== "__all" && !(row.project_names || []).includes(project)) return false;
    } else {
      if (delivery && row.delivery !== delivery) return false;
      if (project && !(row.project_names || []).includes(project)) return false;
      if (topic && row.topic_name !== topic) return false;
    }
    return matches(mem.query, memoryId(row), row.content, row.project_name, row.delivery, row.topic_name, row.retire_reason);
  });
  const key = [mem.view, memSort.state.key, memSort.state.dir, mem.query, delivery, project, topic].join("\n");
  return memOrder.place(sortRows(found, memSort.state, memorySortValue), memoryId, key);
}

/** Open a rule from elsewhere, such as a mistake listed in Settings or a change proposal. */
async function openMemory(memoryId, status) {
  let view = status === "retired" ? "retired" : "active";
  if (!status) {
    const got = await call("memory.get", { memory_id: memoryId }).catch(() => null);
    if (got && got.status === "retired") view = "retired";
  }
  mem.view = view;
  memSeg.select(view);
  mem.query = "";
  mem.filters = { delivery: "", project: "", topic: "" };
  memList.pane.querySelector("[data-search]").value = "";
  mem.selected = memoryId;
  mem.panel = null;
  mem.creating = null;
  await show("memories");
}

/** Open a temporary note from elsewhere, such as a relocated retirement record. */
async function openTemporary(contextId) {
  mem.view = "temporary";
  memSeg.select("temporary");
  mem.query = "";
  mem.filters = { delivery: "", project: "", topic: "" };
  memList.pane.querySelector("[data-search]").value = "";
  mem.selected = contextId;
  mem.panel = null;
  mem.creating = null;
  await show("memories");
}

/** Open active memories filtered to one topic, such as from Settings › Topics. */
async function openTopicMemories(topicName) {
  mem.view = "active";
  memSeg.select("active");
  mem.query = "";
  mem.filters = { delivery: "topic", project: "", topic: topicName };
  memList.pane.querySelector("[data-search]").value = "";
  mem.selected = null;
  mem.panel = null;
  mem.creating = null;
  await show("memories");
}

function pickMemory(id) {
  mem.selected = id;
  mem.panel = null;
  mem.creating = null;
  loadMemoryDetail().catch(fail);
}

async function loadMemoryDetail() {
  const row = memoryRow();
  mem.detail = row && mem.view !== "temporary" ? await call("memory.get", { memory_id: row.memory_id }) : null;
  if (mem.detail && mem.detail.status === "retired") await nameRetirement(mem.detail);
  renderMemoryList();
  renderMemoryDetail();
}

/** Where a retired rule went, by the words of what took its place. */
async function nameRetirement(detail) {
  if (detail.superseded_by) {
    const successor = await call("memory.get", { memory_id: detail.superseded_by }).catch(() => null);
    detail.successor_content = reference(detail.superseded_by, successor ? successor.content : "", 400);
    detail.successor_status = successor ? successor.status : null;
  }
  if (detail.relocated_to_id) {
    const { rows } = await call("memory.list", { view: "temporary" });
    const note = rows.find((item) => item.context_id === detail.relocated_to_id);
    detail.destination_content = reference(detail.relocated_to_id, note ? note.content : t("temporary.title"), 400);
    detail.destination_standing = !!note;
  }
}

function deliveryShort(row) {
  if (row.delivery === "topic") return `${t("delivery.topic")}: ${row.topic_name}`;
  if (row.delivery === "project") return row.project_name || t("delivery.project");
  return t("delivery.always");
}

/** The date the list is ordered by: the key's own date, or the update date inside a group. */
function sortedDay(row) {
  const key = memSort.state.key;
  return day(memorySortValue(row, key === "created" ? "created" : "updated"));
}

function renderMemoryList() {
  const rows = shownMemories();
  memNew.title = mem.view === "temporary" ? t("memory.temporary_new") : t("memory.new");
  memNew.hidden = mem.view === "retired";
  fillList(
    memList.list,
    [
      {
        rows: rows.map((row) => ({
          id: memoryId(row),
          primary: row.content,
          secondary:
            mem.view === "temporary"
              ? [temporaryDeliveryText(row), t("temporary.until", { when: day(row.expires_at) })]
              : mem.view === "retired"
                ? [named("retire.kind", row.retirement_kind || "legacy"), day(row.retired_at)]
                : [deliveryShort(row), row.delivery !== "project" ? row.project_name : null, sortedDay(row)],
        })),
      },
    ],
    { selected: mem.selected, onPick: pickMemory, empty: t("memory.empty") }
  );
}

function memTogglePanel(name) {
  mem.panel = mem.panel === name ? null : name;
  renderMemoryDetail();
}

function renderMemoryDetail() {
  const target = memDetail.inner;
  memDetail.mark(`${mem.view}:${mem.creating || mem.selected}`);
  if (mem.creating === "memory") return newMemoryForm(target);
  if (mem.creating === "temporary") return newTemporaryForm(target);
  const row = memoryRow();
  if (!row) {
    target.replaceChildren(h("p", { class: "placeholder", text: mem.rows.length ? t("common.nothing_selected") : "" }));
    return;
  }
  const nodes = mem.view === "temporary" ? temporaryDetail(row) : memoryDetail(mem.detail || row);
  target.replaceChildren(...nodes.filter(Boolean));
}

// ---------------------------------------------------------------- one rule

function memoryEditForm(detail) {
  const box = h("div");
  loadChoices()
    .then((choices) => {
      const isTopic = (values) => values.delivery === "topic";
      const isProject = (values) => values.delivery === "project";
      const reached = detail.project_names || [];
      box.replaceChildren(
        form({
          fields: [
            { name: "content", label: t("memory.rule"), kind: "area", rows: 3, value: detail.content, max: mem.contentLimit, big: true },
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: detail.delivery },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: reached, show: isProject },
            ...topicFields(choices, detail.topic_id, isTopic),
          ],
          onCancel: () => memTogglePanel("edit"),
          onSubmit: (values) =>
            acknowledging(
              (acknowledged) =>
                call("memory.revise", {
                  memory_id: detail.memory_id,
                  content: values.content,
                  delivery: values.delivery,
                  ...(isProject(values) ? { projects: values.projects } : {}),
                  ...topicArgs(isTopic(values) ? values : {}),
                  acknowledged,
                }).then((result) => {
                  mem.panel = null;
                  return result;
                }),
              t("memory.revised")
            ),
        })
      );
    })
    .catch(fail);
  return box;
}

function retirementHistoryEntries(history) {
  return entries(history, (item) => {
    const d = item.detail || {};
    const label = named("memory.history", item.event_type) || item.event_type;
    const body = d.reason || (d.retirement_kind ? named("retire.kind", d.retirement_kind) : label);
    return [
      `${day(item.created_at)} · ${who(item.actor)} · ${label}`,
      body,
      [t("retire.kind"), d.retirement_kind ? named("retire.kind", d.retirement_kind) : null],
    ];
  });
}

function memoryDetail(detail) {
  const retired = detail.status === "retired";
  const nodes = [meta(idTag(detail.memory_id), deliveryText(detail), t("memory.created", { when: day(detail.created_at), who: who(detail.created_by) }))];
  if (mem.panel === "edit") {
    nodes.push(memoryEditForm(detail));
  } else {
    nodes.push(h("p", { class: "rule-text", text: detail.content }));
    if (detail.delivery === "topic" && detail.topic_trigger) {
      nodes.push(h("p", { class: "sub", text: `${t("memory.topic_trigger")}: ${detail.topic_trigger}` }));
    }
  }

  if (retired) {
    const successorCell = detail.superseded_by
      ? h(
          "span",
          null,
          detail.successor_content || short(detail.superseded_by),
          " ",
          button(t("memory.open_successor"), () => openMemory(detail.superseded_by, detail.successor_status).catch(fail), { kind: "quiet small" })
        )
      : null;
    const destinationCell = detail.relocated_to_id
      ? h(
          "span",
          null,
          detail.destination_content || short(detail.relocated_to_id),
          detail.destination_standing ? " " : null,
          detail.destination_standing
            ? button(t("memory.open_destination"), () => openTemporary(detail.relocated_to_id).catch(fail), { kind: "quiet small" })
            : null
        )
      : null;
    nodes.push(
      h(
        "div",
        { class: "callout" },
        fieldList([
          [t("retire.when"), day(detail.retired_at)],
          [t("retire.kind"), named("retire.kind", detail.retirement_kind || "legacy")],
          [t("retire.reason"), detail.retire_reason],
          [t("retire.successor_label"), successorCell],
          [t("change.moved_to"), destinationCell],
        ])
      ),
      h(
        "div",
        { class: "actions" },
        button(t("memory.restore"), () => memTogglePanel("restore"), { kind: "primary" }),
        button(latestReason(detail) ? t("memory.edit_reason") : t("memory.add_reason"), () => memTogglePanel("reason"))
      )
    );
    if (mem.panel === "restore") nodes.push(restorePanel(detail));
    if (mem.panel === "reason") nodes.push(reasonAddPanel(detail));
  } else if (mem.panel !== "edit") {
    nodes.push(
      h(
        "div",
        { class: "actions" },
        button(t("memory.edit"), () => memTogglePanel("edit")),
        button(t("memory.delivery"), () => memTogglePanel("delivery")),
        button(t("memory.to_temporary"), () => memTogglePanel("temporary")),
        button(latestReason(detail) ? t("memory.edit_reason") : t("memory.add_reason"), () => memTogglePanel("reason")),
        h("span", { class: "spacer" }),
        button(t("memory.retire"), () => memTogglePanel("retire"), { kind: "danger quiet" })
      )
    );
    if (mem.panel === "delivery") nodes.push(deliveryPanel(detail));
    if (mem.panel === "temporary") nodes.push(toTemporaryPanel(detail));
    if (mem.panel === "retire") nodes.push(retirePanel(detail));
    if (mem.panel === "reason") nodes.push(reasonAddPanel(detail));
  }

  if (mem.panel !== "edit") {
    // Code points, as the store's char_length and the form's counter count them.
    nodes.push(h("div", { class: "size-line" }, meterRow(t("memory.rule"), [...detail.content].length, mem.contentLimit)));
  }

  nodes.push(
    section(t("evidence.title"), (detail.basis || []).length, evidenceEntries(detail.basis)),
    (detail.revisions || []).length < 2
      ? null
      : section(
          t("memory.revisions"),
          (detail.revisions || []).length,
          entries(detail.revisions, (item) => [
            `${day(item.created_at)} · ${who(item.actor)}`,
            item.content,
            [null, item.note && item.note !== "recorded by the user's own hand" ? item.note : null],
          ])
        ),
    (detail.retirement_history || []).length
      ? section(
          t("memory.retirement_history"),
          detail.retirement_history.length,
          retirementHistoryEntries(detail.retirement_history)
        )
      : null
  );
  return nodes;
}

function deliveryPanel(detail) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const isTopic = (values) => values.delivery === "topic";
      const isProject = (values) => values.delivery === "project";
      const reached = detail.project_names || [];
      panel.append(
        form({
          fields: [
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: detail.delivery },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: reached, show: isProject },
            ...topicFields(choices, detail.topic_id, isTopic),
          ],
          onCancel: () => memTogglePanel("delivery"),
          onSubmit: (values) =>
            act(async () => {
              await call("memory.delivery", {
                memory_id: detail.memory_id,
                delivery: values.delivery,
                ...(isProject(values) ? { projects: values.projects } : {}),
                ...topicArgs(isTopic(values) ? values : {}),
              });
              mem.panel = null;
            }, t("memory.moved")),
        })
      );
    })
    .catch(fail);
  return panel;
}

/** Bring a retired rule back, with its reason and optional delivery change. */
function restorePanel(detail) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const isTopic = (values) => values.delivery === "topic";
      const isProject = (values) => values.delivery === "project";
      const reached = detail.project_names || [];
      const openTopic = choices.topics.some((item) => item.topic_id === detail.topic_id) ? detail.topic_id : "";
      panel.append(
        form({
          fields: [
            { name: "reason", label: t("memory.restore_reason"), kind: "area", rows: 2, hint: t("memory.restore_hint") },
            { name: "delivery", label: t("memory.restore_delivery"), kind: "radio", options: deliveryOptions(), value: detail.delivery || "always" },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: reached, show: isProject },
            ...topicFields(choices, openTopic, isTopic),
          ],
          submit: t("memory.restore"),
          onCancel: () => memTogglePanel("restore"),
          onSubmit: (values) => {
            if (!values.reason.trim()) {
              toast(t("common.reason_required"), "warn");
              return;
            }
            acknowledging(
              (acknowledged) =>
                call("memory.restore", {
                  memory_id: detail.memory_id,
                  reason: values.reason,
                  delivery: values.delivery,
                  ...(isProject(values) ? { projects: values.projects } : {}),
                  ...topicArgs(isTopic(values) ? values : {}),
                  acknowledged,
                }).then((result) => {
                  mem.panel = null;
                  mem.view = "active";
                  mem.selected = detail.memory_id;
                  memSeg.select("active");
                  return result;
                }),
              t("memory.restored")
            );
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

/**
 * The newest account of why the rule exists, or "" when none was written. An incident or
 * friction row tells what happened, and a reason written at remember or later tells why;
 * either is where an edit starts. Evidence comes newest first.
 */
function latestReason(detail) {
  const row = (detail.basis || []).find((item) => item.what && !fixedAccount(item.what));
  return row ? row.what : "";
}

/**
 * Edit the reason starting from the one written last. The ledger only grows, so saving adds
 * the edited text as the newest evidence row and the earlier reason stays below it.
 */
function reasonAddPanel(detail) {
  const current = latestReason(detail);
  return h(
    "div",
    { class: "inline-panel" },
    form({
      fields: [{ name: "why", label: t("memory.why"), kind: "area", rows: 3, max: 500, value: current, hint: t("memory.reason_hint") }],
      onCancel: () => memTogglePanel("reason"),
      onSubmit: (values) => {
        if (values.why.trim() === current.trim()) {
          memTogglePanel("reason");
          return;
        }
        act(async () => {
          await call("memory.add_reason", { memory_id: detail.memory_id, why: values.why });
          mem.panel = null;
        }, t("memory.reason_added"));
      },
    })
  );
}

function toTemporaryPanel(detail) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const initial = detail.delivery === "project" ? detail.project_names || [] : [];
      panel.append(
        form({
          fields: [
            { name: "days", label: t("memory.days"), kind: "number", step: "0.5", value: 7, hint: t("memory.days_hint") },
            ...temporaryDeliveryFields(choices, initial),
          ],
          submit: t("memory.to_temporary"),
          onCancel: () => memTogglePanel("temporary"),
          onSubmit: (values) => {
            if (!checkTemporaryDelivery(values)) return;
            act(async () => {
              await call("memory.to_temporary", {
                memory_id: detail.memory_id,
                days: values.days,
                projects: temporaryProjectsFrom(values),
              });
              mem.panel = null;
            }, t("memory.made_temporary"));
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

/** Retire with a reason only; a move to a temporary note is its own button. */
function retirePanel(detail) {
  return h(
    "div",
    { class: "inline-panel" },
    form({
      fields: [{ name: "reason", label: t("retire.reason"), kind: "area", rows: 2, hint: t("retire.reason_hint") }],
      submit: t("memory.retire"),
      kind: "danger solid",
      onCancel: () => memTogglePanel("retire"),
      onSubmit: (values) =>
        act(async () => {
          await call("memory.retire", { memory_id: detail.memory_id, reason: values.reason });
          mem.panel = null;
        }, t("memory.retired")),
    })
  );
}

// ---------------------------------------------------------------- one temporary note

function temporaryDeliveryPanel(row) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      panel.append(
        form({
          fields: [
            ...temporaryDeliveryFields(choices, row.project_names || []),
            {
              name: "days",
              label: t("temporary.days_edit"),
              kind: "number",
              step: "0.5",
              value: "",
              hint: t("temporary.days_edit_hint", { until: day(row.expires_at) }),
            },
          ],
          onCancel: () => memTogglePanel("delivery"),
          onSubmit: (values) => {
            if (!checkTemporaryDelivery(values)) return;
            act(async () => {
              await call("temporary.revise", {
                context_id: row.context_id,
                content: row.content,
                projects: temporaryProjectsFrom(values),
                ...(values.days !== null ? { days: values.days } : {}),
              });
              mem.panel = null;
            }, t("temporary.revised"));
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

function temporaryToMemoryPanel(row) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const reached = row.project_names || [];
      const isTopic = (values) => values.delivery === "topic";
      const isProject = (values) => values.delivery === "project";
      panel.append(
        form({
          fields: [
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: reached.length ? "project" : "always" },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: reached, show: isProject },
            ...topicFields(choices, "", isTopic),
            { name: "why", label: t("memory.why"), kind: "area", rows: 2, max: 500, hint: t("memory.why_hint") },
          ],
          submit: t("temporary.to_rule"),
          onCancel: () => memTogglePanel("to_memory"),
          onSubmit: (values) =>
            acknowledging(
              (acknowledged) =>
                call("temporary.to_memory", {
                  context_id: row.context_id,
                  delivery: values.delivery,
                  ...(isProject(values) ? { projects: values.projects } : {}),
                  ...topicArgs(isTopic(values) ? values : {}),
                  why: values.why,
                  acknowledged,
                }).then((result) => {
                  mem.panel = null;
                  return result;
                }),
              t("temporary.converted")
            ),
        })
      );
    })
    .catch(fail);
  return panel;
}

function temporaryDetail(row) {
  const nodes = [
    meta(idTag(row.context_id), temporaryDeliveryText(row), t("temporary.until", { when: day(row.expires_at) })),
  ];
  if (mem.panel === "edit") {
    const slot = h("div");
    nodes.push(slot);
    loadChoices()
      .then((choices) => {
        slot.replaceChildren(
          form({
            fields: [
              { name: "content", label: t("temporary.text"), kind: "area", rows: 3, value: row.content, big: true },
              ...temporaryDeliveryFields(choices, row.project_names || []),
              {
                name: "days",
                label: t("temporary.days_edit"),
                kind: "number",
                step: "0.5",
                value: "",
                hint: t("temporary.days_edit_hint", { until: day(row.expires_at) }),
              },
            ],
            onCancel: () => memTogglePanel("edit"),
            onSubmit: (values) => {
              if (!checkTemporaryDelivery(values)) return;
              act(async () => {
                await call("temporary.revise", {
                  context_id: row.context_id,
                  content: values.content,
                  projects: temporaryProjectsFrom(values),
                  ...(values.days !== null ? { days: values.days } : {}),
                });
                mem.panel = null;
              }, t("temporary.revised"));
            },
          })
        );
      })
      .catch(fail);
    return nodes;
  }
  nodes.push(
    h("p", { class: "rule-text", text: row.content }),
    h(
      "div",
      { class: "actions" },
      button(t("memory.edit"), () => memTogglePanel("edit")),
      button(t("temporary.delivery"), () => memTogglePanel("delivery")),
      button(t("temporary.to_rule"), () => memTogglePanel("to_memory")),
      h("span", { class: "spacer" }),
      button(
        t("temporary.end"),
        async () => {
          const yes = await confirmDialog({ title: t("temporary.end_confirm"), lines: [row.content], confirm: t("temporary.end"), danger: true });
          if (yes) act(() => call("temporary.end", { context_id: row.context_id }), t("temporary.ended"));
        },
        { kind: "danger quiet" }
      )
    )
  );
  if (mem.panel === "delivery") nodes.push(temporaryDeliveryPanel(row));
  if (mem.panel === "to_memory") nodes.push(temporaryToMemoryPanel(row));
  return nodes;
}

// ---------------------------------------------------------------- new entries

function newMemoryForm(target) {
  target.replaceChildren();
  loadChoices()
    .then((choices) => {
      const isTopic = (values) => values.delivery === "topic";
      target.replaceChildren(
        form({
          title: t("memory.new"),
          fields: [
            { name: "content", label: t("memory.rule"), kind: "area", rows: 3, max: mem.contentLimit, hint: t("memory.rule_hint"), big: true },
            { name: "why", label: t("memory.why"), kind: "area", rows: 3, hint: t("memory.why_hint") },
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: "always" },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: [], show: (values) => values.delivery === "project" },
            ...topicFields(choices, "", isTopic),
          ],
          submit: t("common.add"),
          onCancel: () => {
            mem.creating = null;
            renderMemoryDetail();
          },
          onSubmit: (values) =>
            acknowledging(
              (acknowledged) =>
                call("memory.remember", {
                  content: values.content,
                  why: values.why,
                  delivery: values.delivery,
                  projects: values.delivery === "project" ? values.projects : [],
                  ...topicArgs(isTopic(values) ? values : {}),
                  acknowledged,
                }).then((result) => {
                  mem.creating = null;
                  return result;
                }),
              t("memory.remembered")
            ),
        })
      );
    })
    .catch(fail);
}

function newTemporaryForm(target) {
  target.replaceChildren();
  loadChoices()
    .then((choices) => {
      target.replaceChildren(
        form({
          title: t("memory.temporary_new"),
          fields: [
            { name: "content", label: t("temporary.text"), kind: "area", rows: 3, big: true },
            ...temporaryDeliveryFields(choices, []),
            { name: "days", label: t("memory.days"), kind: "number", step: "0.5", value: 3, hint: t("memory.days_hint") },
          ],
          submit: t("common.add"),
          onCancel: () => {
            mem.creating = null;
            renderMemoryDetail();
          },
          onSubmit: (values) => {
            if (!checkTemporaryDelivery(values)) return;
            act(async () => {
              await call("temporary.add", {
                content: values.content,
                days: values.days,
                projects: temporaryProjectsFrom(values),
              });
              mem.creating = null;
            }, t("temporary.added"));
          },
        })
      );
    })
    .catch(fail);
}

page("memories", {
  nav: "nav.memories",
  icon: "rules",
  root: h("div", { class: "page" }, memList.pane, memDetail.pane),
  list: memList.list,
  selected: () => mem.selected,
  pick: pickMemory,
  async load() {
    if (!app.busy) memOrder.release();
    const before = listIds(memList.list);
    const listed = await call("memory.list", { view: mem.view });
    mem.rows = listed.rows;
    mem.contentLimit = listed.content_limit;
    mem.selected = keepSelection(shownMemories().map(memoryId), mem.selected, before);
    await loadMemoryDetail();
  },
  keys: {
    e: () => memoryRow() && mem.view !== "retired" && memTogglePanel("edit"),
    n: () => mem.view !== "retired" && memNew.click(),
  },
});
