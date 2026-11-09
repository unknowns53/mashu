// Work: tasks by lease and status, with their history, and the projects that hold them.
"use strict";

const work = { view: "active", rows: [], selected: null };

function workId(row) {
  return work.view === "projects" ? row.project_id : row.task.task_id;
}

function workRow() {
  return work.rows.find((row) => workId(row) === work.selected) || null;
}

function shownWork() {
  const query = $("work-search").value.trim();
  if (work.view === "projects") {
    return work.rows.filter((row) => matches(query, row.project_id, row.name, row.scope_name));
  }
  return work.rows.filter((row) =>
    matches(
      query,
      row.task.task_id,
      row.task.name,
      row.task.project_name,
      row.activity,
      row.state,
      row.proposal && [row.proposal.outcome, row.proposal.reason],
      row.attempts,
      row.decisions,
      row.checkpoints,
      row.artifacts
    )
  );
}

/** Switch to a view from elsewhere, such as the home screen. */
async function openWork(view) {
  for (const button of $("work-views").querySelectorAll("button[data-view]")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  work.view = view;
  work.selected = null;
  await show("work");
}

function renderWork() {
  const rows = shownWork();
  const projects = work.view === "projects";
  fillList("work-list", rows, {
    idOf: workId,
    selected: work.selected,
    text: (row) => (projects ? row.name : row.task.name),
    sub: (row) =>
      projects
        ? `${short(row.project_id)} · ${row.n_active} active · ${row.n_dormant} dormant · ${row.n_closed} closed`
        : `${short(row.task.task_id)} · ${row.task.project_name} · ${row.activity} · ${row.age_days}d` +
          (row.task.status === "closed" ? ` · ${row.task.outcome}` : ""),
    mark: (row) =>
      !projects && row.proposal
        ? row.proposal.stale
          ? { text: "◌", className: "mark stale" }
          : { text: "●", className: "mark" }
        : null,
    pick: (id) => {
      work.selected = id;
      closeForms();
      renderWork();
    },
  });
  $("no-work").hidden = rows.length > 0;

  const row = workRow();
  if (!row) {
    $("work-body").replaceChildren();
    $("work-actions").replaceChildren();
  } else if (projects) {
    renderProject(row);
  } else {
    renderTask(row);
  }
  updateButtons();
}

function renderProject(row) {
  $("work-body").replaceChildren(
    element("h2", row.name, "title"),
    element("p", short(row.project_id), "muted"),
    pairs([
      ["scope", row.scope_name || "- (its cards go to every scope)"],
      ["tasks", `${row.n_active} active · ${row.n_dormant} dormant · ${row.n_closed} closed`],
      ["created", day(row.created_at)],
    ])
  );
  $("work-actions").replaceChildren(
    button("Edit…", () => editProject(row).catch((error) => say(errorText(error), "error")))
  );
}

function renderTask(row) {
  const { task, state, proposal } = row;
  const nodes = [
    element("h2", task.name, "title"),
    element("p", `${short(task.task_id)} · ${task.project_name} · ${row.heading}`, "muted"),
    pairs([
      ["status", `${task.status} / ${row.activity}`],
      ["size", `card ${row.card_tokens}/${row.card_capacity} · detail ${row.detail_tokens}/${row.detail_capacity}`],
      ["outcome", task.outcome],
      ["close reason", task.close_reason],
    ]),
  ];
  if (proposal) {
    const box = element("section", undefined, "proposal");
    box.append(
      element("p", `Proposed ${proposal.outcome} by ${proposal.proposed_by} on ${proposal.on_date}`),
      element("p", `reason: ${proposal.reason}`)
    );
    if (proposal.stale) {
      box.append(element("p", `The task state was written after this proposal, as of ${row.as_of}.`, "warn"));
    }
    const confirm = element("label");
    const staleOk = element("input");
    staleOk.type = "checkbox";
    staleOk.id = "stale-ok";
    confirm.append(staleOk, " close on it anyway");
    confirm.hidden = !proposal.stale;
    const accept = button("Accept proposal", () => acceptProposal(row), "primary");
    accept.id = "accept";
    const keep = button("Keep open (drop proposal)", () =>
      act(() => call("task.keep", { task_id: task.task_id }), `${task.name} is still live; the proposal is gone and the lease renewed.`)
    );
    keep.id = "keep";
    staleOk.addEventListener("change", updateButtons);
    box.append(rowOf(confirm, accept, keep));
    nodes.push(box);
  }
  nodes.push(
    block(
      "State",
      pairs([
        ["goal", state.goal],
        ["approach", state.approach],
        ["status", state.status_text],
        ["open questions", state.open_questions],
        ["blockers", state.blockers],
        ["next actions", state.next_actions],
      ])
    )
  );
  const artifacts = new Map((row.artifacts || []).map((item) => [item.reference_id, item]));
  nodes.push(
    block(
      "Artifacts",
      bullets(row.artifacts, (item) =>
        `${short(item.reference_id)} · ${day(item.created_at)} · ${item.kind || "other"} ${item.locator}` +
          (item.label ? ` (${item.label})` : "")
      )
    ),
    block(
      "Checkpoints",
      bullets(row.checkpoints, (item) => {
        const evidence = (item.evidence || []).map((id) => {
          const found = artifacts.get(id);
          return found ? `${found.kind} ${found.locator}` : short(id);
        });
        return `${day(item.created_at)} · ${item.created_by}: ${item.what_changed}` +
          (evidence.length ? ` [${evidence.join(", ")}]` : "");
      })
    ),
    block(
      "Attempts",
      bullets(row.attempts, (item) =>
        `${day(item.created_at)} · ${item.created_by}: ${item.attempt} → ${item.result}` +
          (item.reason ? `; ${item.reason}` : "") +
          (item.next ? `; next: ${item.next}` : "")
      )
    ),
    block(
      "Decisions",
      bullets(row.decisions, (item) =>
        `${day(item.created_at)} · ${item.created_by}: ${item.decision}` +
          (item.reason ? ` (${item.reason})` : "") +
          ((item.supersedes_ids || []).length ? ` replaces ${item.supersedes_ids.map(short).join(", ")}` : "")
      )
    )
  );
  $("work-body").replaceChildren(...nodes);

  if (task.status === "closed") {
    $("work-actions").replaceChildren(
      button("Reopen", () => act(() => call("task.reopen", { task_id: task.task_id }), `Reopened ${task.name}.`))
    );
    return;
  }
  $("work-actions").replaceChildren(
    block(
      "Decide",
      rowOf(
        button("Edit…", () => editTask(row).catch((error) => say(errorText(error), "error"))),
        button("Renew lease", () =>
          act(() => call("task.renew", { task_id: task.task_id }), `Renewed ${task.name}; nothing said about whether it is finished.`)
        ),
        button("Close…", () => closeTask(row).catch((error) => say(errorText(error), "error")), "danger")
      )
    )
  );
}

function acceptProposal(row) {
  act(
    () =>
      call("task.accept", {
        task_id: row.task.task_id,
        proposal: row.proposal,
        state_at: row.state.updated_at,
      }),
    `Closed ${row.task.name} as ${row.proposal.outcome}, on its proposal.`
  );
}

async function closeTask(row) {
  const choices = await loadChoices();
  const proposal = row.proposal;
  showForm("form-work", {
    title: `Close ${row.task.name}`,
    fields: [
      {
        name: "outcome",
        label: "outcome",
        kind: "select",
        options: choices.outcomes.map((outcome) => [outcome, outcome]),
        value: proposal ? proposal.outcome : "completed",
      },
      {
        name: "reason",
        label: "reason",
        kind: "area",
        rows: 3,
        hint: proposal
          ? "Left empty with the proposal's outcome, the proposal's grounds are kept."
          : "may be empty",
      },
    ],
    submit: "Close",
    onSubmit: (values) => {
      const reason =
        values.reason.trim() || (proposal && proposal.outcome === values.outcome ? proposal.reason : "");
      act(
        () => call("task.close", { task_id: row.task.task_id, outcome: values.outcome, reason }),
        `Closed ${row.task.name} as ${values.outcome}.`
      );
    },
  });
}

async function editTask(row) {
  const choices = await loadChoices();
  const { task, state } = row;
  showForm("form-work", {
    title: `Edit ${task.name}`,
    fields: [
      { name: "name", label: "task name", value: task.name },
      {
        name: "project",
        label: "project",
        kind: "select",
        options: choices.projects.map((name) => [name, name]),
        value: task.project_name,
      },
      { name: "goal", label: "goal", value: state.goal, hint: "at most 80 characters; empty clears it" },
      { name: "approach", label: "approach", kind: "area", rows: 3, value: state.approach },
      { name: "status_text", label: "status", value: state.status_text, hint: "at most 120 characters" },
      { name: "open_questions", label: "open questions", kind: "lines", value: state.open_questions, hint: "one per line, at most 5" },
      { name: "blockers", label: "blockers", kind: "lines", value: state.blockers, hint: "one per line, at most 5" },
      { name: "next_actions", label: "next actions", kind: "lines", value: state.next_actions, hint: "one per line, at most 5" },
    ],
    onSubmit: (values) =>
      act(
        () => call("task.update", { task_id: task.task_id, expect_updated_at: state.updated_at, ...values }),
        `Edited ${values.name}.`
      ),
  });
}

async function newTask() {
  const choices = await loadChoices();
  if (!choices.projects.length) {
    say("There are no open projects yet; create one first.", "warn");
    return;
  }
  showForm("form-work", {
    title: "New task",
    fields: [
      { name: "name", label: "task name" },
      {
        name: "project",
        label: "project",
        kind: "select",
        options: choices.projects.map((name) => [name, name]),
      },
      { name: "goal", label: "goal", hint: "optional, at most 80 characters" },
    ],
    submit: "Create",
    onSubmit: (values) => {
      const create = (force) => call("task.create", { ...values, force });
      const done = `Created ${values.name}.`;
      act(() => create(false), done, (error) => {
        const candidates = error.reply && error.reply.candidates;
        if (!candidates) return false;
        say(errorText(error), "warn");
        confirmBox(
          "Similar open tasks already exist:",
          candidates.map((item) => `${short(item.task.task_id)} · ${item.task.name} [${item.activity}]`),
          "Create it anyway",
          () => act(() => create(true), done)
        );
        return true;
      });
    },
  });
}

async function projectForm(row) {
  const choices = await loadChoices();
  showForm("form-work", {
    title: row ? `Edit ${row.name}` : "New project",
    fields: [
      { name: "name", label: "project name", value: row ? row.name : "" },
      {
        name: "scope",
        label: "scope",
        kind: "select",
        options: [["", "none — its cards go to every scope"], ...choices.scopes.map((name) => [name, name])],
        value: row ? row.scope_name || "" : "",
      },
    ],
    submit: row ? "Save" : "Create",
    onSubmit: (values) =>
      act(
        () => call("project.save", { project_id: row ? row.project_id : null, name: values.name, scope: values.scope || null }),
        row ? `Edited ${values.name}.` : `Created ${values.name}.`
      ),
  });
}

function editProject(row) {
  return projectForm(row);
}

tab("work", {
  async load() {
    work.rows =
      work.view === "projects"
        ? (await call("work.projects")).projects
        : (await call("work.tasks", { activity: work.view })).tasks;
    work.selected = keepSelection(shownWork(), workId, work.selected);
    renderWork();
  },
  buttons() {
    const row = work.view === "projects" ? null : workRow();
    const accept = $("accept");
    if (accept && row && row.proposal) {
      accept.disabled = app.busy || (row.proposal.stale && !$("stale-ok").checked);
    }
  },
  status(status) {
    const proposed = status.close_proposals.length;
    $("work-count").textContent = proposed ? `(${proposed} proposed)` : "";
  },
});

viewButtons("work-views", (view) => {
  work.view = view;
  work.selected = null;
  closeForms();
  reload().catch((error) => say(errorText(error), "error"));
});
$("work-search").addEventListener("input", () => {
  work.selected = keepSelection(shownWork(), workId, work.selected);
  renderWork();
});
$("task-new").addEventListener("click", () => newTask().catch((error) => say(errorText(error), "error")));
$("project-new").addEventListener("click", () => projectForm(null).catch((error) => say(errorText(error), "error")));
