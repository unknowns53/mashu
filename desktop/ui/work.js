// Work: tasks by activity and status, with their history, and the projects that hold them.
"use strict";

const work = {
  view: "active",
  rows: [],
  query: "",
  selected: null,
  panel: null, // "edit" or "close"
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
const workNew = button("", () => {
  work.creating = work.view === "projects" ? "project" : "task";
  work.panel = null;
  renderWorkDetail();
}, { kind: "icon" });
workNew.append(icon("plus"));
const workSort = sortControl("work", () => renderWork());

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
  workSort
);
const workDetail = detailPane();

function workId(row) {
  return work.view === "projects" ? row.project_id : row.task.task_id;
}

function workRow() {
  return work.rows.find((row) => workId(row) === work.selected) || null;
}

function shownWork() {
  workSort.setKeys(workSortKeys());
  const found =
    work.view === "projects"
      ? work.rows.filter((row) => matches(work.query, row.project_id, row.name, row.scope_name))
      : work.rows.filter((row) =>
          matches(work.query, row.task.task_id, row.task.name, row.task.project_name, row.state, row.decisions, row.attempts, row.artifacts)
        );
  return sortRows(found, workSort.state, workSortValue);
}

function pickWork(id) {
  work.selected = id;
  work.panel = null;
  work.creating = null;
  renderWork();
}

/** Open a task from elsewhere, such as a task card in another page. */
async function openTask(taskId, view = "active") {
  work.view = view;
  workSeg.select(view);
  work.selected = taskId;
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
                secondary: [row.scope_name, t("work.project_tasks", { a: row.n_active, d: row.n_dormant, c: row.n_closed })],
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
  const nodes = [
    meta(idTag(task.task_id), task.project_name, named("work.activity", row.activity), t("work.as_of", { date: row.as_of })),
    h("h1", { class: "title", text: task.name }),
  ];
  if (row.activity === "dormant") nodes.push(h("div", { class: "callout warn" }, h("p", { text: t("work.dormant_note") })));
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
            button(t("work.renew"), () => act(() => call("task.renew", { task_id: task.task_id }), t("work.renewed")), { title: t("work.renew_title") }),
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
    decisionsSection(row.decisions || []),
    attemptsSection(row.attempts || []),
    section(
      t("work.artifacts"),
      (row.artifacts || []).length,
      entries(row.artifacts, (item) => [
        `${item.kind || "other"} · ${day(item.created_at)}`,
        tilde(item.locator),
        [null, item.label && tilde(item.label)],
      ])
    ),
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
      target.replaceChildren(
        meta(idTag(task.task_id), task.project_name, t("work.as_of", { date: row.as_of })),
        form({
          fields: [
            { name: "name", label: t("work.name"), value: task.name, big: true },
            { name: "project", label: t("work.project"), kind: "select", options: choices.projects.map((name) => [name, name]), value: task.project_name },
            { name: "goal", label: t("work.goal"), kind: "area", rows: 1, value: state.goal, max: 80 },
            { name: "approach", label: t("work.approach"), kind: "area", rows: 4, value: state.approach, max: 500 },
            { name: "status_text", label: t("work.status"), kind: "area", rows: 2, value: state.status_text, max: 120 },
            { name: "open_questions", label: t("work.open_questions"), kind: "lines", value: state.open_questions, maxItems: 5, hint: t("work.one_per_line") },
            { name: "blockers", label: t("work.blockers"), kind: "lines", value: state.blockers, maxItems: 5, hint: t("work.one_per_line") },
            { name: "next_actions", label: t("work.next_actions"), kind: "lines", value: state.next_actions, maxItems: 5, hint: t("work.one_per_line") },
          ],
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
            { name: "name", label: t("work.name") },
            { name: "project", label: t("work.project"), kind: "select", options: choices.projects.map((name) => [name, name]) },
            { name: "goal", label: t("work.goal"), max: 80 },
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

function projectDetail(row) {
  return [
    meta(idTag(row.project_id), row.scope_name || t("work.project_scope_none"), t("work.project_created", { when: day(row.created_at) })),
    h("h1", { class: "title", text: row.name }),
    h("p", { class: "sub", text: t("work.project_tasks", { a: row.n_active, d: row.n_dormant, c: row.n_closed }) }),
    h("div", { class: "actions" }, button(t("common.edit"), () => workTogglePanel("edit"))),
  ];
}

function projectForm(target, row) {
  target.replaceChildren();
  loadChoices()
    .then((choices) => {
      target.replaceChildren(
        form({
          title: row ? null : t("work.project_new"),
          fields: [
            { name: "name", label: t("work.project_name"), value: row ? row.name : "" },
            {
              name: "scope",
              label: t("work.project_scope"),
              kind: "select",
              options: areaOptions(choices, t("work.project_scope_none")),
              value: row ? row.scope_name || "" : "",
            },
          ],
          submit: row ? t("common.save") : t("common.create"),
          onCancel: () => {
            work.creating = null;
            work.panel = null;
            renderWorkDetail();
          },
          onSubmit: (values) =>
            act(async () => {
              await call("project.save", { project_id: row ? row.project_id : null, name: values.name, scope: values.scope || null });
              work.creating = null;
              work.panel = null;
            }, row ? t("work.saved") : t("work.created")),
        })
      );
    })
    .catch(fail);
}

page("work", {
  nav: "nav.work",
  icon: "work",
  root: h("div", { class: "page" }, workList.pane, workDetail.pane),
  list: workList.list,
  selected: () => work.selected,
  pick: pickWork,
  async load() {
    work.rows =
      work.view === "projects"
        ? (await call("work.projects")).projects
        : (await call("work.tasks", { activity: work.view })).tasks;
    work.selected = keepSelection(shownWork().map(workId), work.selected);
    renderWork();
  },
  keys: {
    e: () => workRow() && work.view !== "closed" && workTogglePanel("edit"),
    n: () => workNew.click(),
  },
});
