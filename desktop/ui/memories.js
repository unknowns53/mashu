// Memory: active rules, standing/expired temporary notes, topics, and retired rules.
"use strict";

const mem = {
  view: "active",
  rows: [],
  topicCapacity: 800,
  query: "",
  filters: { delivery: "", project: "", topic: "" },
  includeExpired: false,
  includeArchivedTopics: false,
  selected: null,
  detail: null,
  panel: null, // "edit", "temporary", "retire", "restore", "to_memory"
  creating: null, // "memory", "temporary", or "topic" while the new form is shown
  prefillTemp: null, // optional { content, projects } when re-adding an expired temporary note
  contentLimit: null, // the Settings screen's limit on one memory body
};

const memSeg = segmented(
  [
    ["active", t("memory.view.active")],
    ["temporary", t("memory.view.temporary")],
    ["topics", t("memory.view.topics")],
    ["retired", t("memory.view.retired")],
  ],
  mem.view,
  (view) => {
    mem.view = view;
    mem.filters = { delivery: "", project: "", topic: "" };
    mem.selected = null;
    mem.panel = null;
    mem.creating = null;
    mem.prefillTemp = null;
    reload().catch(fail);
  }
);
const memNew = button(
  t("common.add"),
  () => {
    guardDirty(() => {
      mem.prefillTemp = null;
      mem.creating = mem.view === "temporary" ? "temporary" : mem.view === "topics" ? "topic" : "memory";
      mem.panel = null;
      renderMemoryDetail();
    });
  },
  { kind: "small", title: t("memory.new") }
);
memNew.prepend(icon("plus"));

const memSort = sortControl("memories", () => {
  renderMemoryList();
});

const memFilterDelivery = h("select", { "aria-label": t("filter.delivery") });
const memFilterProject = h("select", { "aria-label": t("filter.project") });
const memFilterTopic = h("select", { "aria-label": t("filter.topic") });
const memExpiredInput = h("input", {
  type: "checkbox",
  on: {
    change: (event) => {
      mem.includeExpired = event.target.checked;
      reload().catch(fail);
    },
  },
});
const memExpiredCheck = h("label", { class: "check", hidden: true }, memExpiredInput, t("temporary.show_expired"));
const memArchivedTopicsInput = h("input", {
  type: "checkbox",
  on: {
    change: (event) => {
      mem.includeArchivedTopics = event.target.checked;
      reload().catch(fail);
    },
  },
});
const memArchivedTopicsCheck = h("label", { class: "check", hidden: true }, memArchivedTopicsInput, t("memory.show_archived"));

const memFilterRow = h(
  "div",
  { class: "filter-row" },
  memFilterDelivery,
  memFilterProject,
  memFilterTopic,
  memExpiredCheck,
  memArchivedTopicsCheck
);
const memFilterCount = h("span", { class: "sub" });
const memFilterClear = button(
  t("filter.clear"),
  () => {
    mem.query = "";
    mem.filters = { delivery: "", project: "", topic: "" };
    const search = memList.pane.querySelector("[data-search]");
    if (search) search.value = "";
    mem.selected = keepSelection(shownMemories().map(memoryId), mem.selected);
    loadMemoryDetail().catch(fail);
  },
  { kind: "quiet small" }
);
const memFilterMeta = h("div", { class: "filter-meta", hidden: true }, memFilterCount, memFilterClear);

function onMemoryFilterChange() {
  mem.filters.delivery = memFilterDelivery.value;
  if (mem.filters.delivery === "always") {
    mem.filters.project = "";
    mem.filters.topic = "";
  } else if (mem.filters.delivery === "project") {
    mem.filters.topic = "";
    mem.filters.project = memFilterProject.value;
  } else if (mem.filters.delivery === "topic") {
    mem.filters.project = "";
    mem.filters.topic = memFilterTopic.value;
  } else {
    mem.filters.project = memFilterProject.value;
    mem.filters.topic = memFilterTopic.value;
  }
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
  memExpiredCheck.hidden = mem.view !== "temporary";
  memArchivedTopicsCheck.hidden = mem.view !== "topics";

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
    memFilterRow.hidden = false;
    return;
  }

  if (mem.view === "topics") {
    memFilterDelivery.hidden = true;
    memFilterTopic.hidden = true;
    mem.filters.delivery = "";
    mem.filters.topic = "";
    const projectNames = [...new Set(mem.rows.map((row) => row.project_name).filter(Boolean))].sort();
    memFilterProject.hidden = !projectNames.length;
    mem.filters.project = fillSelect(
      memFilterProject,
      [
        ["", `${t("filter.project")}: ${t("common.all")}`],
        ["__all", t("topic.area_all")],
        ...projectNames.map((name) => [name, name]),
      ],
      mem.filters.project
    );
    memFilterRow.hidden = false;
    return;
  }

  const projectNames = [...new Set([...mem.rows.flatMap((row) => row.project_names || []), mem.filters.project].filter(Boolean))].sort();
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
  const hideProject = mem.filters.delivery === "always" || mem.filters.delivery === "topic" || !projectNames.length;
  const hideTopic = mem.filters.delivery === "always" || mem.filters.delivery === "project" || !topicNames.length;
  memFilterProject.hidden = hideProject;
  mem.filters.project = hideProject
    ? ""
    : fillSelect(
        memFilterProject,
        [["", `${t("filter.project")}: ${t("common.all")}`], ...projectNames.map((name) => [name, name])],
        mem.filters.project
      );
  memFilterTopic.hidden = hideTopic;
  mem.filters.topic = hideTopic
    ? ""
    : fillSelect(
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
  if (mem.view === "topics") {
    return [["name", t("sort.name")], ["rules", t("topic.rules")], ["tokens", t("topic.tokens")]];
  }
  if (mem.view === "retired") {
    return [
      ["retired", t("retire.when")],
      ["updated", t("sort.updated")],
      ["created", t("sort.created")],
      ["kind", t("retire.kind")],
    ];
  }
  return [
    ["updated", t("sort.updated")],
    ["created", t("sort.created")],
    ["kind", t("delivery.title")],
  ];
}

function memorySortValue(row, key) {
  if (mem.view === "topics") {
    if (key === "rules") return row.rules || 0;
    if (key === "tokens") return row.body_tokens || 0;
    return row.name || "";
  }
  if (key === "retired") return row.retired_at || row.updated_at || row.created_at;
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
  memSort,
  memFilterMeta
);
const memDetail = detailPane();
const memOrder = heldOrder();

function memoryId(row) {
  if (mem.view === "temporary") return row.context_id;
  if (mem.view === "topics") return row.topic_id;
  return row.memory_id;
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
      return matches(mem.query, row.context_id, row.content, row.project_name);
    }
    if (mem.view === "topics") {
      if (project === "__all" && row.project_name) return false;
      if (project && project !== "__all" && row.project_name !== project) return false;
      return matches(mem.query, row.topic_id, row.name, row.trigger, row.project_name, row.action, row.source);
    }
    if (delivery && row.delivery !== delivery) return false;
    if (project && !(row.project_names || []).includes(project)) return false;
    if (topic && row.topic_name !== topic) return false;
    return matches(mem.query, memoryId(row), row.content, row.project_name, row.delivery, row.topic_name, row.retire_reason);
  });
  const activeFilter = !!(mem.query || delivery || project || topic);
  memFilterMeta.hidden = !activeFilter;
  if (activeFilter) memFilterCount.textContent = t("common.items", { n: found.length, max: mem.rows.length });
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

/** Open a temporary note from elsewhere, such as a relocated retirement record or preview. */
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

/** Open active memories filtered to one topic, such as from Topics view. */
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

/** Open active memories filtered to one project, such as from Project detail. */
async function openProjectMemories(projectName) {
  mem.view = "active";
  memSeg.select("active");
  mem.query = "";
  mem.filters = { delivery: "", project: projectName, topic: "" };
  memList.pane.querySelector("[data-search]").value = "";
  mem.selected = null;
  mem.panel = null;
  mem.creating = null;
  await show("memories");
}

async function openTopics() {
  mem.view = "topics";
  memSeg.select("topics");
  mem.query = "";
  mem.filters = { delivery: "", project: "", topic: "" };
  memList.pane.querySelector("[data-search]").value = "";
  mem.selected = null;
  mem.panel = null;
  mem.creating = null;
  await show("memories");
}

function pickMemory(id) {
  guardDirty(() => {
    mem.selected = id;
    mem.panel = null;
    mem.creating = null;
    mem.prefillTemp = null;
    loadMemoryDetail().catch(fail);
  });
}

async function loadMemoryDetail() {
  const row = memoryRow();
  mem.detail = row && (mem.view === "active" || mem.view === "retired") ? await call("memory.get", { memory_id: row.memory_id }) : null;
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
    const { rows } = await call("memory.list", { view: "temporary", include_expired: true });
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
  if (mem.view === "retired") return day(memorySortValue(row, key === "retired" || key === "created" ? key : "updated"));
  return day(memorySortValue(row, key === "created" ? "created" : "updated"));
}

function renderMemoryList() {
  const rows = shownMemories();
  memNew.title =
    mem.view === "temporary" ? t("memory.temporary_new") : mem.view === "topics" ? t("topic.new") : t("memory.new");
  memNew.hidden = mem.view === "retired";
  fillList(
    memList.list,
    [
      {
        rows: rows.map((row) => {
          if (mem.view === "temporary") {
            return {
              id: row.context_id,
              primary: row.content,
              secondary: [
                temporaryDeliveryText(row),
                row.expired ? t("temporary.expired") : remainingDaysLabel(row.expires_at),
              ],
              dim: !!row.expired,
            };
          }
          if (mem.view === "topics") {
            return {
              id: row.topic_id,
              primary: row.name,
              secondary: [
                row.project_name || t("topic.area_all"),
                t("work.project_rules", { n: row.rules }),
                `${row.body_tokens} / ${mem.topicCapacity}`,
              ],
              dim: !!row.archived_at,
            };
          }
          if (mem.view === "retired") {
            return {
              id: row.memory_id,
              primary: row.content,
              secondary: [named("retire.kind", row.retirement_kind || "legacy"), day(row.retired_at)],
            };
          }
          return {
            id: row.memory_id,
            primary: row.content,
            secondary: [deliveryShort(row), row.delivery !== "project" ? row.project_name : null, sortedDay(row)],
          };
        }),
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
  memDetail.mark(`${mem.view}:${mem.creating || mem.selected}:${mem.panel || ""}`);
  if (mem.creating === "memory") return newMemoryForm(target);
  if (mem.creating === "temporary") return newTemporaryForm(target);
  if (mem.creating === "topic") return topicForm(target, null);
  const row = memoryRow();
  if (!row) {
    target.replaceChildren(h("p", { class: "placeholder", text: mem.rows.length ? t("common.nothing_selected") : "" }));
    return;
  }
  if (mem.view === "topics") {
    if (mem.panel === "edit") return topicForm(target, row);
    target.replaceChildren(...topicDetail(row));
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
      const currentWhy = latestReason(detail);
      box.replaceChildren(
        form({
          fields: [
            { name: "content", label: t("memory.rule"), kind: "area", rows: 3, value: detail.content, max: mem.contentLimit, big: true },
            { name: "why", label: t("memory.why"), kind: "area", rows: 2, max: 500, value: currentWhy, hint: t("memory.edit_why_hint") },
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: detail.delivery },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: reached, show: isProject },
            ...topicFields(choices, detail.topic_id, isTopic),
          ],
          onCancel: () => memTogglePanel("edit"),
          onSubmit: (values) => {
            if (!checkMemoryDelivery(values)) return;
            acknowledging(
              (acknowledged) =>
                call("memory.revise", {
                  memory_id: detail.memory_id,
                  content: values.content,
                  delivery: values.delivery,
                  ...(isProject(values) ? { projects: values.projects } : {}),
                  ...topicArgs(isTopic(values) ? values : {}),
                  ...(values.why.trim() !== currentWhy.trim() && values.why.trim() ? { why: values.why } : {}),
                  acknowledged,
                }).then((result) => {
                  mem.panel = null;
                  return result;
                }),
              t("memory.revised")
            );
          },
        })
      );
    })
    .catch(fail);
  return box;
}

function retirementHistoryEntries(history) {
  const ordered = [...(history || [])].reverse();
  return entries(ordered, (item) => {
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

function memoryHistorySections(detail) {
  return [
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
      : null,
  ].filter(Boolean);
}

function memoryDetail(detail) {
  const retired = detail.status === "retired";
  const nodes = [meta(idTag(detail.memory_id), deliveryText(detail), t("memory.created", { when: day(detail.created_at), who: who(detail.created_by) }))];
  if (mem.panel === "edit" && !retired) {
    const history = memoryHistorySections(detail);
    nodes.push(
      memoryEditForm(detail),
      history.length ? h("details", { class: "form-more" }, h("summary", { text: t("memory.edit_history") }), ...history) : null
    );
    return nodes;
  }
  nodes.push(h("p", { class: "rule-text", text: detail.content }));
  if (detail.delivery === "topic" && detail.topic_trigger) {
    nodes.push(h("p", { class: "sub", text: `${t("memory.topic_trigger")}: ${detail.topic_trigger}` }));
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
        button(t("memory.restore"), () => memTogglePanel("restore"), { kind: "primary" })
      )
    );
    if (mem.panel === "restore") nodes.push(restorePanel(detail));
  } else {
    nodes.push(
      h(
        "div",
        { class: "actions" },
        button(t("memory.edit"), () => memTogglePanel("edit")),
        button(t("memory.to_temporary"), () => memTogglePanel("temporary")),
        h("span", { class: "spacer" }),
        button(t("memory.retire"), () => memTogglePanel("retire"), { kind: "danger quiet" })
      )
    );
    if (mem.panel === "temporary") nodes.push(toTemporaryPanel(detail));
    if (mem.panel === "retire") nodes.push(retirePanel(detail));
  }

  nodes.push(
    h("div", { class: "size-line" }, meterRow(t("memory.rule"), [...detail.content].length, mem.contentLimit)),
    ...memoryHistorySections(detail)
  );
  return nodes;
}

/** Bring a retired rule back, with optional wording edit, reason, and delivery change. */
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
            { name: "content", label: t("memory.rule"), kind: "area", rows: 3, value: detail.content, max: mem.contentLimit, big: true },
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
            if (!checkMemoryDelivery(values)) return;
            acknowledging(
              (acknowledged) =>
                call("memory.restore", {
                  memory_id: detail.memory_id,
                  content: values.content,
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
 * The newest account of why the rule exists, or "" when none was written.
 */
function latestReason(detail) {
  const row = (detail.basis || []).find((item) => item.what && !fixedAccount(item.what));
  return row ? row.what : "";
}

function toTemporaryPanel(detail) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const initial = detail.project_names || [];
      panel.append(
        form({
          fields: [
            { name: "content", label: t("temporary.text"), kind: "area", rows: 3, value: detail.content, big: true },
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
                content: values.content,
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
            { name: "content", label: t("memory.rule"), kind: "area", rows: 3, value: row.content, max: mem.contentLimit, big: true },
            { name: "why", label: t("memory.why"), kind: "area", rows: 2, max: 500, hint: t("memory.why_hint") },
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: reached.length ? "project" : "always" },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: reached, show: isProject },
            ...topicFields(choices, "", isTopic),
          ],
          submit: t("temporary.to_rule"),
          onCancel: () => memTogglePanel("to_memory"),
          onSubmit: (values) => {
            if (!checkMemoryDelivery(values)) return;
            acknowledging(
              (acknowledged) =>
                call("temporary.to_memory", {
                  context_id: row.context_id,
                  content: values.content,
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
            );
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

function temporaryDetail(row) {
  const expired = !!row.expired;
  const nodes = [
    meta(
      idTag(row.context_id),
      temporaryDeliveryText(row),
      t("temporary.until", { when: day(row.expires_at) }),
      expired ? t("temporary.expired") : remainingDaysLabel(row.expires_at)
    ),
  ];
  if (mem.panel === "edit" && !expired) {
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

  nodes.push(h("p", { class: "rule-text", text: row.content }));
  if (expired) {
    nodes.push(
      h("div", { class: "callout" }, h("p", { text: t("temporary.expired_note") })),
      h(
        "div",
        { class: "actions" },
        button(t("temporary.readd"), () => {
          mem.prefillTemp = { content: row.content, projects: row.project_names || [] };
          mem.creating = "temporary";
          mem.panel = null;
          renderMemoryDetail();
        })
      )
    );
  } else {
    nodes.push(
      h(
        "div",
        { class: "actions" },
        button(t("memory.edit"), () => memTogglePanel("edit")),
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
    if (mem.panel === "to_memory") nodes.push(temporaryToMemoryPanel(row));
  }
  nodes.push(
    h(
      "p",
      { class: "sub" },
      t("temporary.size", { chars: [...row.content].length, tokens: pushedCost([row.content]) })
    )
  );
  return nodes;
}

// ---------------------------------------------------------------- one topic

function topicDetail(row) {
  const archived = !!row.archived_at;
  const remove = async () => {
    const yes = await confirmDialog({
      title: t("topic.remove_confirm"),
      lines: [row.name, t("topic.remove_note")],
      confirm: t("common.remove"),
      danger: true,
    });
    if (!yes) return;
    act(async () => {
      const removed = await call("topic.remove", { topic_id: row.topic_id });
      mem.selected = null;
      return removed.removed === "archived" ? t("topic.archived") : "";
    }, t("topic.removed"));
  };
  return [
    meta(idTag(row.topic_id), row.project_name || t("topic.area_all"), archived ? t("common.archived") : null),
    h("h1", { class: "title", text: row.name }),
    h(
      "div",
      { class: "actions" },
      button(t("topic.open_memories"), () => openTopicMemories(row.name).catch(fail), { kind: "primary" }),
      archived ? null : button(t("common.edit"), () => memTogglePanel("edit")),
      h("span", { class: "spacer" }),
      archived ? null : button(t("common.remove"), remove, { kind: "danger quiet" })
    ),
    h("div", { class: "size-line" }, meterRow(t("topic.tokens"), row.body_tokens || 0, mem.topicCapacity)),
    section(
      t("topic.details"),
      null,
      fieldList([
        [t("topic.trigger"), row.trigger],
        [t("topic.area"), row.project_name || t("topic.area_all")],
        [t("topic.action"), row.action],
        [t("topic.source"), row.source_text || row.source],
        [t("topic.rules"), String(row.rules)],
      ])
    ),
  ];
}

function topicForm(target, row) {
  target.replaceChildren();
  loadChoices()
    .then((choices) => {
      target.replaceChildren(
        row ? meta(idTag(row.topic_id), row.project_name || t("topic.area_all")) : null,
        form({
          title: row ? null : t("topic.new"),
          fields: [
            { name: "name", label: t("topic.name"), value: row ? row.name : "", max: 40 },
            { name: "trigger", label: t("topic.trigger"), value: row ? row.trigger : "", hint: t("topic.trigger_hint"), max: 160 },
            { name: "project", label: t("topic.area"), kind: "select", options: projectOptions(choices, t("topic.area_all")), value: row ? row.project_name || "" : "" },
            { name: "action", label: t("topic.action"), value: row ? row.action || "" : "", hint: t("topic.action_hint") },
            { name: "source", label: t("topic.source"), value: row ? row.source || "" : "", hint: t("topic.source_hint") },
          ],
          submit: row ? t("common.save") : t("common.create"),
          onCancel: () => {
            mem.creating = null;
            mem.panel = null;
            renderMemoryDetail();
          },
          onSubmit: (values) =>
            act(async () => {
              const saved = await call("topic.save", {
                topic_id: row ? row.topic_id : null,
                ...values,
                project: values.project || null,
                action: values.action || null,
                source: values.source || null,
              });
              mem.creating = null;
              mem.panel = null;
              if (saved && saved.topic_id) mem.selected = saved.topic_id;
            }, t("topic.saved")),
        })
      );
    })
    .catch(fail);
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
            { name: "why", label: t("memory.why"), kind: "area", rows: 3, max: 500, hint: t("memory.why_hint") },
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: "always" },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: [], show: (values) => values.delivery === "project" },
            ...topicFields(choices, "", isTopic),
          ],
          submit: t("common.add"),
          onCancel: () => {
            mem.creating = null;
            renderMemoryDetail();
          },
          onSubmit: (values) => {
            if (!checkMemoryDelivery(values)) return;
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
            );
          },
        })
      );
    })
    .catch(fail);
}

function newTemporaryForm(target) {
  target.replaceChildren();
  const prefill = mem.prefillTemp || {};
  loadChoices()
    .then((choices) => {
      target.replaceChildren(
        form({
          title: t("memory.temporary_new"),
          fields: [
            { name: "content", label: t("temporary.text"), kind: "area", rows: 3, value: prefill.content || "", big: true },
            ...temporaryDeliveryFields(choices, prefill.projects || []),
            { name: "days", label: t("memory.days"), kind: "number", step: "0.5", value: 3, hint: t("memory.days_hint") },
          ],
          submit: t("common.add"),
          onCancel: () => {
            mem.creating = null;
            mem.prefillTemp = null;
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
              mem.prefillTemp = null;
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
    if (mem.view === "topics") {
      const listed = await call("topic.list", { include_archived: mem.includeArchivedTopics });
      mem.rows = listed.topics;
      mem.topicCapacity = listed.capacity;
    } else {
      const listed = await call("memory.list", {
        view: mem.view,
        ...(mem.view === "temporary" ? { include_expired: mem.includeExpired } : {}),
      });
      mem.rows = listed.rows;
      mem.contentLimit = listed.content_limit;
    }
    mem.selected = keepSelection(shownMemories().map(memoryId), mem.selected, before);
    await loadMemoryDetail();
  },
  keys: {
    e: () => {
      const row = memoryRow();
      if (!row || mem.view === "retired") return;
      if (mem.view === "topics" && row.archived_at) return;
      if (mem.view === "temporary" && row.expired) return;
      memTogglePanel("edit");
    },
    n: () => mem.view !== "retired" && memNew.click(),
  },
});
