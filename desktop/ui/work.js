// Work: tasks by activity and status, with their history, and the projects that hold them.
"use strict";

const work = {
  view: "active",
  rows: [],
  projects: [],
  projectFilter: "",
  includeArchivedProjects: false,
  query: "",
  selected: null,
  panel: null, // "edit", "close", "add_ruling", "add_artifact", "add_folder", "supersede:<id>", "settle:<id>", "withdraw:<id>", "folder:<path>"
  creating: null, // "task" or "project" while the new form is shown
};

/** How many attempts show before the rest fold away. */
const ATTEMPTS_SHOWN = 5;

/** How many decisions in force show before the rest fold away. */
const DECISIONS_SHOWN = 10;

const workSeg = segmented(
  [
    ["active", t("work.view.active")],
    ["dormant", t("work.view.dormant")],
    ["closed", t("work.view.closed")],
    ["projects", t("work.view.projects")],
  ],
  work.view,
  (view) => {
    work.view = view;
    work.selected = null;
    work.panel = null;
    work.creating = null;
    reload().catch(fail);
  }
);
const workNew = button(
  t("common.add"),
  () => {
    guardDirty(() => {
      work.creating = work.view === "projects" ? "project" : "task";
      work.panel = null;
      renderWorkDetail();
    });
  },
  { kind: "small" }
);
workNew.prepend(icon("plus"));
const workSort = sortControl("work", () => renderWork());

const workProjectFilter = h("select", { "aria-label": t("filter.project") });
workProjectFilter.addEventListener("change", () => {
  work.projectFilter = workProjectFilter.value;
  work.selected = keepSelection(shownWork().map(workId), work.selected);
  renderWork();
});
const workArchivedInput = h("input", {
  type: "checkbox",
  on: {
    change: (event) => {
      work.includeArchivedProjects = event.target.checked;
      reload().catch(fail);
    },
  },
});
const workArchivedCheck = h("label", { class: "check", hidden: true }, workArchivedInput, t("work.show_archived"));
const workFilterRow = h("div", { class: "filter-row" }, workProjectFilter, workArchivedCheck);

const workFilterCount = h("span", { class: "sub" });
const workFilterClear = button(
  t("filter.clear"),
  () => {
    work.query = "";
    work.projectFilter = "";
    const search = workList.pane.querySelector("[data-search]");
    if (search) search.value = "";
    work.selected = keepSelection(shownWork().map(workId), work.selected);
    renderWork();
  },
  { kind: "quiet small" }
);
const workFilterMeta = h("div", { class: "filter-meta", hidden: true }, workFilterCount, workFilterClear);

function syncWorkFilters() {
  const isProjects = work.view === "projects";
  workArchivedCheck.hidden = !isProjects;
  if (isProjects) {
    workProjectFilter.hidden = true;
    workFilterRow.hidden = false;
    return;
  }
  const names = [...new Set([...work.projects, ...work.rows.map((r) => r.task.project_name), work.projectFilter].filter(Boolean))].sort();
  workProjectFilter.hidden = !names.length;
  if (names.length) {
    workProjectFilter.replaceChildren(
      ...[["", `${t("filter.project")}: ${t("common.all")}`], ...names.map((name) => [name, name])].map(([value, label]) =>
        h("option", { value, text: label })
      )
    );
    workProjectFilter.value = names.includes(work.projectFilter) ? work.projectFilter : "";
    work.projectFilter = workProjectFilter.value;
  }
  workFilterRow.hidden = workProjectFilter.hidden;
}

function workSortKeys() {
  if (work.view === "projects") return [["created", t("sort.created")], ["name", t("sort.name")]];
  return [["updated", t("sort.updated")], ["created", t("sort.created")], ["kind", t("work.project")]];
}

function workSortValue(row, key) {
  if (work.view === "projects") return key === "name" ? row.name : row.created_at;
  if (key === "created") return row.task.created_at;
  if (key === "kind") return row.task.project_name;
  return row.task.last_activity_at || row.state.updated_at;
}

const workList = listPane(
  workSeg,
  h(
    "div",
    { class: "row search-row" },
    searchBox((query) => {
      work.query = query;
      work.selected = keepSelection(shownWork().map(workId), work.selected);
      renderWork();
    }),
    workNew
  ),
  workFilterRow,
  workSort,
  workFilterMeta
);
const workDetail = detailPane();
const workOrder = heldOrder();

function workId(row) {
  return work.view === "projects" ? row.project_id : row.task.task_id;
}

function workRow() {
  return work.rows.find((row) => workId(row) === work.selected) || null;
}

function shownWork() {
  workSort.setKeys(workSortKeys());
  syncWorkFilters();
  const isProjects = work.view === "projects";
  const found = isProjects
    ? work.rows.filter((row) => matches(work.query, row.project_id, row.name, row.summary, ...row.folders.map(tilde)))
    : work.rows.filter((row) => {
        if (work.projectFilter && row.task.project_name !== work.projectFilter) return false;
        return matches(
          work.query,
          row.task.task_id,
          row.task.name,
          row.task.project_name,
          row.state,
          row.rulings,
          row.settled_rulings,
          row.superseded_rulings,
          row.withdrawn_rulings,
          row.decisions,
          row.attempts,
          row.artifacts
        );
      });
  const activeFilter = !!(work.query || (!isProjects && work.projectFilter));
  workFilterMeta.hidden = !activeFilter;
  if (activeFilter) workFilterCount.textContent = t("common.items", { n: found.length, max: work.rows.length });
  const key = [work.view, workSort.state.key, workSort.state.dir, work.query, isProjects ? "" : work.projectFilter].join("\n");
  return workOrder.place(sortRows(found, workSort.state, workSortValue), workId, key);
}

function pickWork(id) {
  guardDirty(() => {
    work.selected = id;
    work.panel = null;
    work.creating = null;
    renderWork();
  });
}

/** Open a task from elsewhere, such as a task card in another page. */
async function openTask(taskId, view = "active") {
  work.view = view;
  workSeg.select(view);
  work.projectFilter = "";
  work.query = "";
  workList.pane.querySelector("[data-search]").value = "";
  work.selected = taskId;
  work.panel = null;
  work.creating = null;
  await show("work");
}

/** Open tasks filtered to a specific project. */
async function openProjectTasks(projectName, view = "active") {
  work.view = view;
  workSeg.select(view);
  work.projectFilter = projectName;
  work.query = "";
  workList.pane.querySelector("[data-search]").value = "";
  work.selected = null;
  work.panel = null;
  work.creating = null;
  await show("work");
}

/** Open the projects tab. */
async function openProjects() {
  work.view = "projects";
  workSeg.select("projects");
  work.projectFilter = "";
  work.query = "";
  workList.pane.querySelector("[data-search]").value = "";
  work.selected = null;
  work.panel = null;
  work.creating = null;
  await show("work");
}

function renderWork() {
  const projects = work.view === "projects";
  workNew.title = projects ? t("work.project_new") : t("work.task_new");
  fillList(
    workList.list,
    [
      {
        rows: shownWork().map((row) =>
          projects
            ? {
                id: row.project_id,
                primary: row.name,
                secondary: [row.folders.length ? tilde(row.folders[0]) : t("work.project_no_folder"), projectTasks(row)],
                dim: !!row.archived_at,
              }
            : {
                id: row.task.task_id,
                primary: row.task.name,
                secondary: [
                  row.proposal ? t("inbox.group.closes") : null,
                  row.task.project_name,
                  row.task.status === "closed" ? named("close.outcome", row.task.outcome) : null,
                  when(workSort.state.key === "created" ? row.task.created_at : row.task.last_activity_at || row.state.updated_at),
                ],
              }
        ),
      },
    ],
    { selected: work.selected, onPick: pickWork, empty: t("work.empty") }
  );
  renderWorkDetail();
}

function workTogglePanel(name) {
  work.panel = work.panel === name ? null : name;
  renderWorkDetail();
}

function renderWorkDetail() {
  const target = workDetail.inner;
  workDetail.mark(`${work.view}:${work.creating || work.selected}:${work.panel === "edit"}`);
  if (work.creating === "task") return newTaskForm(target);
  if (work.creating === "project") return projectForm(target, null);
  const row = workRow();
  if (!row) {
    target.replaceChildren(h("p", { class: "placeholder", text: work.rows.length ? t("common.nothing_selected") : "" }));
    return;
  }
  if (work.view === "projects") {
    if (work.panel === "edit") return projectForm(target, row);
    target.replaceChildren(...projectDetail(row));
    return;
  }
  if (work.panel === "edit") return editTaskForm(target, row);
  target.replaceChildren(...taskDetail(row).filter(Boolean));
}

// ---------------------------------------------------------------- one task

function taskDetail(row) {
  const { task, state, proposal } = row;
  const closed = task.status === "closed";
  const dormant = row.activity === "dormant";
  const nodes = [
    meta(
      idTag(task.task_id),
      task.project_name,
      named("work.activity", row.activity),
      t("work.as_of", { date: row.as_of }),
      !closed && task.active_until
        ? t("work.lease", { date: day(task.active_until), left: remainingDaysLabel(task.active_until) })
        : null
    ),
    h("h1", { class: "title", text: task.name }),
  ];
  if (dormant) nodes.push(h("div", { class: "callout warn" }, h("p", { text: t("work.dormant_note") })));
  if (closed) {
    nodes.push(
      h(
        "div",
        { class: "callout" },
        h("p", { text: `${named("close.outcome", task.outcome)}${task.close_reason ? ` — ${task.close_reason}` : ""}` })
      )
    );
  }
  if (proposal) {
    nodes.push(
      h(
        "div",
        { class: `callout${proposal.stale ? " warn" : ""}` },
        h("p", { text: t("close.meta", { outcome: named("close.outcome", proposal.outcome), when: when(proposal.proposed_at) }) }),
        h("p", { text: proposal.reason }),
        h(
          "div",
          { class: "actions" },
          button(t("nav.open_inbox"), () => {
            inbox.selected = `t:${task.task_id}`;
            show("inbox").catch(fail);
          }, { kind: "small" })
        )
      )
    );
  }

  nodes.push(
    h(
      "div",
      { class: "actions" },
      closed
        ? button(t("work.reopen"), () => act(() => call("task.reopen", { task_id: task.task_id }), t("work.reopened")))
        : [
            button(t("common.edit"), () => workTogglePanel("edit")),
            dormant
              ? button(t("work.restore_active"), () => act(() => call("task.renew", { task_id: task.task_id }), t("work.restored_active")))
              : [
                  button(t("work.renew"), () => act(() => call("task.renew", { task_id: task.task_id }), t("work.renewed")), { title: t("work.renew_title") }),
                  button(t("work.pause"), () => act(() => call("task.pause", { task_id: task.task_id }), t("work.paused")), { kind: "quiet" }),
                ],
            h("span", { class: "spacer" }),
            button(t("work.close"), () => workTogglePanel("close"), { kind: "danger quiet" }),
          ]
    )
  );
  if (work.panel === "close") nodes.push(closePanel(row));

  nodes.push(
    h(
      "div",
      { class: "size-line" },
      meterRow(t("work.card"), row.card_tokens, row.card_capacity),
      meterRow(t("work.detail"), row.detail_tokens, row.detail_capacity)
    ),
    section(
      t("work.state"),
      null,
      fieldList(
        [
          [t("work.goal"), state.goal],
          [t("work.approach"), state.approach],
          [t("work.status"), state.status_text],
          [t("work.open_questions"), state.open_questions],
          [t("work.blockers"), state.blockers],
          [t("work.next_actions"), state.next_actions],
        ],
        { showEmpty: true }
      )
    ),
    rulingsSection(row),
    decisionsSection(row.decisions || []),
    attemptsSection(row.attempts || []),
    artifactsSection(row),
    (row.checkpoints || []).length
      ? section(
          t("work.checkpoints"),
          row.checkpoints.length,
          entries(row.checkpoints, (item) => [`${day(item.created_at)} · ${who(item.created_by)}`, item.what_changed])
        )
      : null
  );
  return nodes;
}

function addRulingPanel(row, choices) {
  return h(
    "div",
    { class: "inline-panel" },
    form({
      fields: [
        { name: "ruling", label: t("work.ruling_text"), kind: "area", rows: 2, max: choices.ruling_chars || 200 },
        { name: "user_instruction", label: t("work.ruling_quote"), kind: "area", rows: 2, max: choices.ruling_words_chars || 240, hint: t("work.ruling_quote_hint") },
      ],
      submit: t("common.add"),
      onCancel: () => workTogglePanel("add_ruling"),
      onSubmit: (values) =>
        act(async () => {
          await call("task.ruling_add", {
            task_id: row.task.task_id,
            expect_updated_at: row.state.updated_at,
            ruling: values.ruling,
            user_instruction: values.user_instruction,
          });
          work.panel = null;
        }, t("work.ruling_added")),
    })
  );
}

function addArtifactPanel(row) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const kinds = (choices.artifact_kinds || ["file", "document", "url", "other"]).map((kind) => [
        kind,
        named("artifact.kind", kind) || kind,
      ]);
      panel.append(
        form({
          fields: [
            { name: "kind", label: t("work.artifact_kind"), kind: "select", options: kinds, value: "file" },
            { name: "locator", label: t("work.artifact_locator"), max: 500 },
            { name: "label", label: t("work.artifact_label"), max: 300 },
          ],
          submit: t("common.add"),
          onCancel: () => workTogglePanel("add_artifact"),
          onSubmit: (values) =>
            act(async () => {
              await call("task.artifact_add", {
                task_id: row.task.task_id,
                expect_updated_at: row.state.updated_at,
                kind: values.kind,
                locator: values.locator,
                label: values.label,
              });
              work.panel = null;
            }, t("work.artifact_added")),
        })
      );
    })
    .catch(fail);
  return panel;
}

function rulingsSection(row) {
  const rulings = row.rulings || [];
  const settled = row.settled_rulings || [];
  const superseded = row.superseded_rulings || [];
  const withdrawn = row.withdrawn_rulings || [];
  const open = row.task.status !== "closed";
  const addBtn = open ? button(t("work.ruling_add"), () => workTogglePanel("add_ruling"), { kind: "quiet small" }) : null;
  const supersedeKey = (id) => `supersede:${id}`;
  const settleKey = (id) => `settle:${id}`;
  const withdrawKey = (id) => `withdraw:${id}`;

  const rulingTextById = new Map();
  for (const item of rulings) rulingTextById.set(item.ruling_id, item.ruling);
  for (const list of [settled, superseded, withdrawn]) {
    for (const item of list) rulingTextById.set(item.ruling_id, item.ruling);
  }

  let addPanelNode = null;
  if (work.panel === "add_ruling") {
    addPanelNode = h("div");
    loadChoices()
      .then((choices) => addPanelNode.replaceChildren(addRulingPanel(row, choices)))
      .catch(fail);
  }

  const inlinePanelFor = (item) => {
    if (!open) return null;
    if (work.panel === supersedeKey(item.ruling_id)) {
      const slot = h("div", { class: "inline-panel" });
      loadChoices()
        .then((choices) => {
          slot.append(
            form({
              fields: [
                { name: "ruling", label: t("work.ruling_new_text"), kind: "area", rows: 2, value: item.ruling, max: choices.ruling_chars || 200 },
                { name: "user_instruction", label: t("work.ruling_quote"), kind: "area", rows: 2, max: choices.ruling_words_chars || 240, hint: t("work.ruling_quote_hint") },
              ],
              submit: t("work.ruling_supersede"),
              onCancel: () => workTogglePanel(supersedeKey(item.ruling_id)),
              onSubmit: (values) =>
                act(async () => {
                  await call("task.ruling_add", {
                    task_id: row.task.task_id,
                    expect_updated_at: row.state.updated_at,
                    ruling: values.ruling,
                    user_instruction: values.user_instruction,
                    supersedes: [item.ruling_id],
                  });
                  work.panel = null;
                }, t("work.ruling_superseded")),
            })
          );
        })
        .catch(fail);
      return slot;
    }
    if (work.panel === settleKey(item.ruling_id)) {
      return h(
        "div",
        { class: "inline-panel" },
        form({
          fields: [
            { name: "settled_in", label: t("work.ruling_settled_in"), max: 500 },
            { name: "note", label: t("work.ruling_settled_note"), max: 300 },
          ],
          submit: t("work.ruling_settle"),
          onCancel: () => workTogglePanel(settleKey(item.ruling_id)),
          onSubmit: (values) =>
            act(async () => {
              await call("task.ruling_settle", {
                task_id: row.task.task_id,
                expect_updated_at: row.state.updated_at,
                ruling_id: item.ruling_id,
                settled_in: values.settled_in,
                note: values.note,
              });
              work.panel = null;
            }, t("work.ruling_settled")),
        })
      );
    }
    if (work.panel === withdrawKey(item.ruling_id)) {
      return reasonPanel({
        label: t("work.ruling_withdraw_reason"),
        confirm: t("work.ruling_withdraw"),
        danger: true,
        onCancel: () => workTogglePanel(withdrawKey(item.ruling_id)),
        onConfirm: (reason) =>
          act(async () => {
            await call("task.ruling_withdraw", {
              task_id: row.task.task_id,
              expect_updated_at: row.state.updated_at,
              ruling_id: item.ruling_id,
              user_instruction: reason,
            });
            work.panel = null;
          }, t("work.ruling_withdrawn")),
      });
    }
    return null;
  };

  return sectionWithAction(
    t("work.rulings"),
    rulings.length,
    addBtn,
    addPanelNode,
    entries(rulings, (item) => [
      h(
        "div",
        { class: "entry-head" },
        h("span", { class: "when meta" }, idTag(item.ruling_id), " ", day(item.on)),
        open ? button(t("work.ruling_supersede"), () => workTogglePanel(supersedeKey(item.ruling_id)), { kind: "quiet small" }) : null,
        open ? button(t("work.ruling_settle"), () => workTogglePanel(settleKey(item.ruling_id)), { kind: "quiet small" }) : null,
        open ? button(t("work.ruling_withdraw"), () => workTogglePanel(withdrawKey(item.ruling_id)), { kind: "quiet small danger" }) : null
      ),
      item.ruling,
      [t("work.user_said"), item.user_said],
      inlinePanelFor(item),
    ]),
    settled.length
      ? h(
          "details",
          { class: "more" },
          h("summary", { text: t("work.rulings_settled", { n: settled.length }) }),
          entries(
            settled,
            (item) => [
              h("span", { class: "when meta" }, idTag(item.ruling_id), " ", day(item.created_at)),
              item.ruling,
              [t("work.user_said"), item.user_instruction],
              [t("work.settled_in"), item.settled_note ? `${item.settled_in} (${item.settled_note})` : item.settled_in],
            ],
            { old: () => true }
          )
        )
      : null,
    superseded.length
      ? h(
          "details",
          { class: "more" },
          h("summary", { text: t("work.rulings_superseded", { n: superseded.length }) }),
          entries(
            superseded,
            (item) => [
              h("span", { class: "when meta" }, idTag(item.ruling_id), " ", day(item.created_at)),
              item.ruling,
              [t("work.user_said"), item.user_instruction],
              [t("work.superseded_by"), reference(item.superseded_by, rulingTextById.get(item.superseded_by) || "", 60)],
            ],
            { old: () => true }
          )
        )
      : null,
    withdrawn.length
      ? h(
          "details",
          { class: "more" },
          h("summary", { text: t("work.rulings_withdrawn", { n: withdrawn.length }) }),
          entries(
            withdrawn,
            (item) => [
              h("span", { class: "when meta" }, idTag(item.ruling_id), " ", day(item.created_at)),
              item.ruling,
              [t("work.user_said"), item.user_instruction],
              [t("work.withdrawn_because"), item.withdrawn_because],
            ],
            { old: () => true }
          )
        )
      : null
  );
}

function artifactsSection(row) {
  const artifacts = row.artifacts || [];
  const open = row.task.status !== "closed";
  const addBtn = open ? button(t("work.artifact_add"), () => workTogglePanel("add_artifact"), { kind: "quiet small" }) : null;
  return sectionWithAction(
    t("work.artifacts"),
    artifacts.length,
    addBtn,
    work.panel === "add_artifact" ? addArtifactPanel(row) : null,
    entries(artifacts, (item) => [
      h(
        "div",
        { class: "entry-head" },
        h("span", { class: "when", text: `${named("artifact.kind", item.kind) || item.kind || "other"} · ${day(item.created_at)}` }),
        button(
          t("work.artifact_copy"),
          () =>
            navigator.clipboard
              .writeText(tilde(item.locator))
              .then(() => toast(t("common.copied", { id: excerpt(tilde(item.locator), 32) })))
              .catch(fail),
          { kind: "quiet small" }
        )
      ),
      tilde(item.locator),
      [null, item.label && tilde(item.label)],
    ])
  );
}

function decisionsSection(decisions) {
  const replaced = new Set(decisions.flatMap((item) => item.supersedes_ids || []));
  const inForce = decisions.filter((item) => !replaced.has(item.decision_id));
  const old = decisions.filter((item) => replaced.has(item.decision_id));
  const words = new Map(decisions.map((item) => [item.decision_id, item.decision]));
  const render = (item) => [
    `${day(item.created_at)} · ${who(item.created_by)}`,
    item.decision,
    [t("work.attempt_reason"), item.reason],
    [t("work.replaces"), (item.supersedes_ids || []).map((id) => reference(id, words.get(id))).join("\n")],
  ];
  const rest = inForce.slice(DECISIONS_SHOWN);
  return section(
    t("work.decisions"),
    inForce.length,
    entries(inForce.slice(0, DECISIONS_SHOWN), render),
    rest.length ? h("details", { class: "more" }, h("summary", { text: `+ ${rest.length}` }), entries(rest, render)) : null,
    old.length
      ? h(
          "details",
          { class: "more" },
          h("summary", { text: t("work.decisions_superseded", { n: old.length }) }),
          entries(old, render, { old: () => true })
        )
      : null
  );
}

function attemptsSection(attempts) {
  const render = (item) => [
    `${day(item.created_at)} · ${who(item.created_by)}`,
    item.attempt,
    [t("work.attempt_result"), item.result],
    [t("work.attempt_reason"), item.reason],
    [t("work.attempt_next"), item.next],
  ];
  const first = attempts.slice(0, ATTEMPTS_SHOWN);
  const rest = attempts.slice(ATTEMPTS_SHOWN);
  return section(
    t("work.attempts"),
    attempts.length,
    entries(first, render),
    rest.length
      ? h("details", { class: "more" }, h("summary", { text: `+ ${rest.length}` }), entries(rest, render))
      : null
  );
}

function closePanel(row) {
  const proposal = row.proposal;
  return h(
    "div",
    { class: "inline-panel" },
    form({
      fields: [
        {
          name: "outcome",
          label: t("close.outcome"),
          kind: "radio",
          value: proposal ? proposal.outcome : "completed",
          options: ["completed", "abandoned", "superseded"].map((outcome) => [outcome, named("close.outcome", outcome)]),
        },
        {
          name: "reason",
          label: t("common.reason"),
          kind: "area",
          rows: 2,
          hint: proposal ? t("work.close_reason_hint_proposal") : t("work.close_reason_hint"),
        },
      ],
      submit: t("work.close"),
      kind: "danger solid",
      onCancel: () => workTogglePanel("close"),
      onSubmit: (values) => {
        const reason = values.reason.trim() || (proposal && proposal.outcome === values.outcome ? proposal.reason : "");
        act(async () => {
          await call("task.close", { task_id: row.task.task_id, outcome: values.outcome, reason });
          work.panel = null;
        }, t("work.closed"));
      },
    })
  );
}

function editTaskForm(target, row) {
  const { task, state } = row;
  target.replaceChildren();
  loadChoices()
    .then((choices) => {
      const meters = h("div", { class: "size-line" });
      const updateMeters = (values) => {
        const card = taskCardTokens(values.name.trim() || task.name, values, (row.rulings || []).length);
        const detail = taskDetailTokens(values.name.trim() || task.name, values);
        meters.replaceChildren(
          meterRow(t("work.card"), card, row.card_capacity),
          meterRow(t("work.detail"), detail, row.detail_capacity)
        );
      };
      updateMeters({ name: task.name, ...state });
      target.replaceChildren(
        meta(idTag(task.task_id), task.project_name, t("work.as_of", { date: row.as_of })),
        meters,
        form({
          fields: [
            { name: "name", label: t("work.name"), value: task.name, big: true },
            { name: "project", label: t("work.project"), kind: "select", options: choices.projects.map((name) => [name, name]), value: task.project_name },
            { name: "goal", label: t("work.goal"), kind: "area", rows: 1, value: state.goal, max: 80 },
            { name: "approach", label: t("work.approach"), kind: "area", rows: 4, value: state.approach, max: 500 },
            { name: "status_text", label: t("work.status"), kind: "area", rows: 2, value: state.status_text, max: 120 },
            { name: "open_questions", label: t("work.open_questions"), kind: "lines", value: state.open_questions, maxItems: 5, hint: t("work.list_hint") },
            { name: "blockers", label: t("work.blockers"), kind: "lines", value: state.blockers, maxItems: 5, hint: t("work.list_hint") },
            { name: "next_actions", label: t("work.next_actions"), kind: "lines", value: state.next_actions, maxItems: 5, hint: t("work.list_hint") },
          ],
          onUpdate: updateMeters,
          onCancel: () => workTogglePanel("edit"),
          onSubmit: (values) =>
            act(async () => {
              await call("task.update", { task_id: task.task_id, expect_updated_at: state.updated_at, ...values });
              work.panel = null;
            }, t("work.saved")),
        })
      );
    })
    .catch(fail);
}

function newTaskForm(target) {
  target.replaceChildren();
  loadChoices()
    .then((choices) => {
      if (!choices.projects.length) {
        target.replaceChildren(h("p", { class: "placeholder", text: t("work.no_projects") }));
        return;
      }
      target.replaceChildren(
        form({
          title: t("work.task_new"),
          fields: [
            { name: "name", label: t("work.name"), big: true },
            { name: "project", label: t("work.project"), kind: "select", options: choices.projects.map((name) => [name, name]), value: work.projectFilter || choices.projects[0] },
            { name: "goal", label: t("work.goal"), kind: "area", rows: 1, max: 80 },
            {
              kind: "group",
              label: t("work.more_fields"),
              fields: [
                { name: "approach", label: t("work.approach"), kind: "area", rows: 3, max: 500 },
                { name: "status_text", label: t("work.status"), kind: "area", rows: 2, max: 120 },
                { name: "open_questions", label: t("work.open_questions"), kind: "lines", value: [], maxItems: 5, hint: t("work.list_hint") },
                { name: "blockers", label: t("work.blockers"), kind: "lines", value: [], maxItems: 5, hint: t("work.list_hint") },
                { name: "next_actions", label: t("work.next_actions"), kind: "lines", value: [], maxItems: 5, hint: t("work.list_hint") },
              ],
            },
          ],
          submit: t("common.create"),
          onCancel: () => {
            work.creating = null;
            renderWorkDetail();
          },
          onSubmit: (values) => {
            const create = (force) =>
              call("task.create", { ...values, force }).then((result) => {
                work.creating = null;
                return result;
              });
            act(() => create(false), t("work.created"), (error) => {
              const candidates = error.reply && error.reply.candidates;
              if (!candidates) return false;
              confirmDialog({
                title: t("work.similar"),
                list: candidates.map((item) => `${item.task.name}（${named("work.activity", item.activity)}）`),
                confirm: t("work.create_anyway"),
              }).then((yes) => yes && act(() => create(true), t("work.created")));
              return true;
            });
          },
        })
      );
    })
    .catch(fail);
}

// ---------------------------------------------------------------- one project

function projectTasks(row) {
  return t("work.project_tasks", { a: row.n_tasks_active, d: row.n_tasks_dormant, c: row.n_tasks_closed });
}

function addFolderPanel(row) {
  return h(
    "div",
    { class: "inline-panel" },
    form({
      fields: [{ name: "path", label: t("route.path"), kind: "folder", value: "" }],
      submit: t("common.add"),
      onCancel: () => workTogglePanel("add_folder"),
      onSubmit: (values) =>
        act(async () => {
          await call("route.save", { path: values.path, project: row.name });
          work.panel = null;
        }, t("work.folder_added")),
    })
  );
}

function editFolderPanel(row, path) {
  const panel = h("div", { class: "inline-panel" });
  Promise.all([call("route.list"), loadChoices()])
    .then(([{ routes }, choices]) => {
      const route = routes.find((item) => item.path_prefix === path);
      panel.append(
        form({
          fields: [
            { name: "path", label: t("route.path"), kind: "folder", value: path },
            {
              name: "project",
              label: t("work.folder_project"),
              kind: "select",
              options: [["", t("work.folder_ignored")], ...choices.projects.map((name) => [name, name])],
              value: row.name,
            },
          ],
          onCancel: () => workTogglePanel(`folder:${path}`),
          onSubmit: (values) =>
            act(async () => {
              await call("route.save", {
                route_id: route ? route.route_id : null,
                path: values.path,
                project: values.project || null,
              });
              work.panel = null;
            }, t("route.saved")),
        })
      );
    })
    .catch(fail);
  return panel;
}

/** A project with the folders its sessions open in, which are added, edited, and removed here. */
function projectDetail(row) {
  const archived = !!row.archived_at;
  const removeFolder = async (path) => {
    const yes = await confirmDialog({ title: t("work.folder_remove_confirm"), lines: [tilde(path), t("work.folder_remove_note")], confirm: t("common.remove"), danger: true });
    if (yes) act(() => call("route.remove", { path }), t("work.folder_removed"));
  };
  const remove = async () => {
    const yes = await confirmDialog({ title: t("work.project_remove_confirm"), lines: [row.name, t("project.remove_note")], confirm: t("common.remove"), danger: true });
    if (!yes) return;
    act(async () => {
      const removed = await call("project.remove", { project_id: row.project_id });
      work.selected = null;
      return removed.removed === "archived" ? t("project.archived") : "";
    }, t("project.removed"));
  };
  return [
    meta(idTag(row.project_id), t("work.project_created", { when: day(row.created_at) }), archived ? t("common.archived") : null),
    h("h1", { class: "title", text: row.name }),
    row.summary ? h("p", { class: "sub", text: row.summary }) : null,
    h("p", { class: "sub", text: [projectTasks(row), t("work.project_rules", { n: row.n_rules }), t("work.project_tokens", { n: row.push_tokens })].join("・") }),
    h(
      "div",
      { class: "actions" },
      button(t("work.open_project_tasks"), () => openProjectTasks(row.name, "active").catch(fail), { kind: "small" }),
      button(t("work.open_project_memories"), () => openProjectMemories(row.name).catch(fail), { kind: "small" }),
      archived ? null : button(t("common.edit"), () => workTogglePanel("edit")),
      h("span", { class: "spacer" }),
      archived ? null : button(t("common.remove"), remove, { kind: "danger quiet" })
    ),
    section(
      t("work.project_folders"),
      row.folders.length,
      row.folders.length
        ? h(
            "ul",
            { class: "folder-list" },
            row.folders.map((path) =>
              h(
                "li",
                null,
                h(
                  "div",
                  { class: "folder-row" },
                  h("span", { class: "mono", text: tilde(path) }),
                  archived
                    ? null
                    : h(
                        "span",
                        { class: "folder-ops" },
                        button(t("common.edit"), () => workTogglePanel(`folder:${path}`), { kind: "quiet small" }),
                        button(t("common.remove"), () => removeFolder(path), { kind: "quiet small danger" })
                      )
                ),
                !archived && work.panel === `folder:${path}` ? editFolderPanel(row, path) : null
              )
            )
          )
        : h("p", { class: "sub", text: t("work.project_no_folder_note") }),
      !archived && work.panel === "add_folder" ? addFolderPanel(row) : null,
      archived ? null : h("div", { class: "actions" }, button(t("work.folder_add"), () => workTogglePanel("add_folder")))
    ),
  ].filter(Boolean);
}

function projectForm(target, row) {
  target.replaceChildren(
    form({
      title: row ? null : t("work.project_new"),
      fields: row
        ? [
            { name: "name", label: t("work.project_name"), value: row.name },
            { name: "summary", label: t("project.summary"), kind: "area", rows: 2, value: row.summary || "" },
          ]
        : [
            {
              name: "path",
              label: t("work.project_folder"),
              kind: "folder",
              value: "",
              hint: t("work.project_folder_hint"),
              onPick: (path, inputOf) => {
                const name = inputOf("name");
                if (name && !name.value.trim()) name.value = folderName(path);
              },
            },
            { name: "name", label: t("work.project_name"), value: "" },
            { name: "summary", label: t("project.summary"), kind: "area", rows: 2, value: "" },
          ],
      submit: row ? t("common.save") : t("common.create"),
      onCancel: () => {
        work.creating = null;
        work.panel = null;
        renderWorkDetail();
      },
      onSubmit: (values) =>
        act(async () => {
          const saved = row
            ? await call("project.save", { project_id: row.project_id, name: values.name, summary: values.summary })
            : await call("project.save", { name: values.name, summary: values.summary, path: values.path.trim() || null });
          work.creating = null;
          work.panel = null;
          if (saved.unchecked) return t("project.unchecked");
          if (saved.malformed && saved.malformed.length) return t("project.malformed");
          return "";
        }, row ? t("work.saved") : t("work.created")),
    })
  );
}

page("work", {
  nav: "nav.work",
  icon: "work",
  root: h("div", { class: "page" }, workList.pane, workDetail.pane),
  list: workList.list,
  selected: () => work.selected,
  pick: pickWork,
  async load() {
    if (!app.busy) workOrder.release();
    const before = listIds(workList.list);
    if (work.view === "projects") {
      work.rows = (await call("work.projects", { include_archived: work.includeArchivedProjects })).projects;
      work.projects = work.rows.filter((row) => !row.archived_at).map((row) => row.name);
    } else {
      const [tasksRes, choices] = await Promise.all([call("work.tasks", { activity: work.view }), loadChoices()]);
      work.rows = tasksRes.tasks;
      work.projects = choices.projects || [];
    }
    work.selected = keepSelection(shownWork().map(workId), work.selected, before);
    renderWork();
  },
  keys: {
    e: () => {
      const row = workRow();
      if (!row || work.view === "closed") return;
      if (work.view === "projects" && row.archived_at) return;
      workTogglePanel("edit");
    },
    n: () => workNew.click(),
  },
});
