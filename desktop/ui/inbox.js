// Inbox: everything waiting for the user's decision, in one list. Candidates are admitted,
// made temporary, declined, deferred, or reworded; Memory change proposals edited, applied,
// declined, or withdrawn; close proposals accepted, adjusted, or dropped.
"use strict";

const inbox = {
  candidates: [],
  changes: [],
  closes: [],
  hiddenDeferred: 0,
  contentLimit: null, // the Settings screen's limit on one memory body
  includeDeferred: false,
  selected: null,
  panel: null, // "admit", "to_temporary", "edit", "decline", "defer", "change_edit", "withdraw", "edit_close"
};

const inboxList = listPane(
  h(
    "label",
    { class: "check" },
    h("input", {
      type: "checkbox",
      on: {
        change: (event) => {
          inbox.includeDeferred = event.target.checked;
          reload().catch(fail);
        },
      },
    }),
    t("inbox.show_deferred")
  )
);
const inboxDetail = detailPane();

function inboxItems() {
  return [
    ...inbox.candidates.map((row) => ({ id: `c:${row.nomination_id}`, type: "candidate", row })),
    ...inbox.changes.map((row) => ({ id: `m:${row.change_id}`, type: "change", row })),
    ...inbox.closes.map((row) => ({ id: `t:${row.task.task_id}`, type: "close", row })),
  ];
}

function inboxItem() {
  return inboxItems().find((item) => item.id === inbox.selected) || null;
}

function pickInbox(id) {
  guardDirty(() => {
    inbox.selected = id;
    inbox.panel = null;
    renderInbox();
  });
}

function changeBody(row) {
  return (
    (row.successor_snapshot && row.successor_snapshot.content) ||
    (row.target && row.target.content) ||
    ""
  );
}

function renderInbox() {
  fillList(
    inboxList.list,
    [
      {
        title: t("inbox.group.candidates"),
        rows: inbox.candidates.map((row) => ({
          id: `c:${row.nomination_id}`,
          primary: row.content,
          secondary: [named("candidate.kind", row.kind), row.project_name, when(row.created_at)],
          dim: !!row.deferred_at,
        })),
      },
      {
        title: t("inbox.group.changes"),
        rows: inbox.changes.map((row) => ({
          id: `m:${row.change_id}`,
          primary: changeBody(row),
          secondary: [named("change.op", row.operation), row.retirement_kind && named("retire.kind", row.retirement_kind), when(row.proposed_at)],
        })),
      },
      {
        title: t("inbox.group.closes"),
        rows: inbox.closes.map((row) => ({
          id: `t:${row.task.task_id}`,
          primary: row.task.name,
          secondary: [named("close.outcome", row.proposal.outcome), row.task.project_name, when(row.proposal.proposed_at)],
          flag: row.proposal.stale ? "!" : null,
        })),
      },
    ],
    {
      selected: inbox.selected,
      onPick: pickInbox,
      empty: t("inbox.empty"),
      note: inbox.hiddenDeferred ? t("inbox.deferred_hidden", { n: inbox.hiddenDeferred }) : null,
    }
  );

  const item = inboxItem();
  inboxDetail.mark(inbox.selected);
  if (!item) {
    inboxDetail.inner.replaceChildren(h("p", { class: "placeholder", text: inboxItems().length ? t("common.nothing_selected") : "" }));
    return;
  }
  const render = { candidate: candidateDetail, change: changeDetail, close: closeDetail }[item.type];
  inboxDetail.inner.replaceChildren(...render(item.row).filter(Boolean));
}

/** Open or close an inline panel, then draw again. */
function togglePanel(name) {
  inbox.panel = inbox.panel === name ? null : name;
  renderInbox();
}

/** A checkbox that must be ticked before `target` can be pressed. */
function readGate(conflicts, blocking, target) {
  if (!blocking.length) return null;
  target.disabled = true;
  return h(
    "label",
    { class: "check" },
    h("input", { type: "checkbox", on: { change: (event) => (target.disabled = !event.target.checked || app.busy) } }),
    t("conflicts.read")
  );
}

// ---------------------------------------------------------------- candidates

function candidateDetail(row) {
  const nodes = [
    meta(
      idTag(row.nomination_id),
      row.project_name
        ? t("candidate.meta_area", { kind: named("candidate.kind", row.kind), area: row.project_name, when: when(row.created_at) })
        : t("candidate.meta", { kind: named("candidate.kind", row.kind), when: when(row.created_at) })
    ),
  ];

  if (inbox.panel === "edit") {
    nodes.push(
      form({
        fields: [{ name: "content", label: t("memory.rule"), kind: "area", rows: 3, value: row.content, max: inbox.contentLimit, big: true }],
        onCancel: () => togglePanel("edit"),
        onSubmit: (values) =>
          act(async () => {
            await call("review.revise", { nomination_id: row.nomination_id, content: values.content });
            inbox.panel = null;
          }, t("candidate.saved_wording")),
      })
    );
  } else {
    nodes.push(h("p", { class: "rule-text", text: row.content }));
  }
  if (row.deferred_at) {
    nodes.push(h("div", { class: "callout warn" }, h("p", { text: t("candidate.deferred", { reason: row.defer_reason || "" }) })));
  }

  if (inbox.panel !== "edit") {
    const admit = button(t("candidate.admit"), () => togglePanel("admit"), { kind: "primary", title: t("candidate.admit_title") });
    const gate = readGate(row.conflict_rows || [], row.blocking_conflict_ids, admit);
    nodes.push(
      h(
        "div",
        { class: "actions" },
        admit,
        button(t("candidate.to_temporary"), () => togglePanel("to_temporary")),
        button(t("candidate.edit"), () => togglePanel("edit")),
        row.deferred_at
          ? button(t("candidate.undefer"), () =>
              act(() => call("review.undefer", { nomination_id: row.nomination_id }), t("candidate.undeferred"))
            )
          : null,
        h("span", { class: "spacer" }),
        row.deferred_at ? null : button(t("candidate.defer"), () => togglePanel("defer"), { kind: "quiet" }),
        button(t("candidate.decline"), () => togglePanel("decline"), { kind: "danger quiet" })
      )
    );
    if (gate) nodes.push(gate);
  }

  if (inbox.panel === "admit") nodes.push(admitPanel(row));
  if (inbox.panel === "to_temporary") nodes.push(candidateToTemporaryPanel(row));
  for (const [name, op, label, done, danger] of [
    ["decline", "review.decline", "candidate.decline_label", "candidate.declined", true],
    ["defer", "review.defer", "candidate.defer_label", "candidate.deferred_done", false],
  ]) {
    if (inbox.panel !== name) continue;
    nodes.push(
      reasonPanel({
        label: t(label),
        confirm: t(name === "decline" ? "candidate.decline" : "candidate.defer"),
        danger,
        onCancel: () => togglePanel(name),
        onConfirm: (reason) =>
          act(async () => {
            await call(op, { nomination_id: row.nomination_id, reason });
            inbox.panel = null;
          }, t(done)),
      })
    );
  }

  if ((row.conflict_rows || []).length) {
    nodes.push(section(t("conflicts.title"), row.conflict_rows.length, conflictEntries(row.conflict_rows)));
  }
  nodes.push(section(t("evidence.title"), (row.evidence_rows || []).length, evidenceEntries(row.evidence_rows)));
  return nodes;
}

function admitPanel(row) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const isTopic = (values) => values.delivery === "topic";
      const isProject = (values) => values.delivery === "project";
      const initialProjects = row.project_name ? [row.project_name] : [];
      panel.append(
        form({
          fields: [
            { name: "content", label: t("memory.rule"), kind: "area", rows: 3, value: row.content, max: inbox.contentLimit, big: true },
            { name: "why", label: t("memory.why"), kind: "area", rows: 2, max: 500, hint: t("memory.why_hint") },
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: row.project_id ? "project" : "always" },
            { name: "projects", label: t("delivery.areas"), kind: "checks", options: projectChecks(choices), value: initialProjects, show: isProject },
            ...topicFields(choices, "", isTopic),
          ],
          submit: t("candidate.admit"),
          onCancel: () => togglePanel("admit"),
          onSubmit: (values) => {
            if (!checkMemoryDelivery(values)) return;
            act(async () => {
              const result = await call("review.admit", {
                nomination_id: row.nomination_id,
                expected_version: row.version,
                content: values.content,
                why: values.why,
                delivery: values.delivery,
                ...(isProject(values) ? { projects: values.projects } : {}),
                ...topicArgs(isTopic(values) ? values : {}),
                conflict_ids: row.blocking_conflict_ids,
              });
              inbox.panel = null;
              if (result.changed === "candidate") return t("candidate.changed");
              if (result.changed === "conflicts") return t("candidate.conflicts_changed");
              return "";
            }, t("candidate.admitted"));
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

function candidateToTemporaryPanel(row) {
  const panel = h("div", { class: "inline-panel" });
  loadChoices()
    .then((choices) => {
      const initial = row.project_name ? [row.project_name] : [];
      panel.append(
        form({
          fields: [
            { name: "content", label: t("temporary.text"), kind: "area", rows: 3, value: row.content, big: true },
            { name: "days", label: t("memory.days"), kind: "number", step: "0.5", value: 7, hint: t("memory.days_hint") },
            ...temporaryDeliveryFields(choices, initial),
          ],
          submit: t("candidate.to_temporary"),
          onCancel: () => togglePanel("to_temporary"),
          onSubmit: (values) => {
            if (!checkTemporaryDelivery(values)) return;
            act(async () => {
              const result = await call("review.to_temporary", {
                nomination_id: row.nomination_id,
                expected_version: row.version,
                content: values.content,
                days: values.days,
                projects: temporaryProjectsFrom(values),
              });
              inbox.panel = null;
              if (result.changed === "candidate") return t("candidate.changed");
              return "";
            }, t("candidate.made_temporary"));
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

// ---------------------------------------------------------------- Memory changes

function changeEditPanel(row) {
  const panel = h("div", { class: "inline-panel" });
  const target = row.target || {};
  const op = row.operation;
  loadChoices()
    .then((choices) => {
      const fields = [];
      if (op === "replace") {
        const currentBody = (row.successor && row.successor.content) || (row.successor_snapshot && row.successor_snapshot.content) || "";
        fields.push({
          name: "successor_content",
          label: t("change.replacement_body"),
          kind: "area",
          rows: 3,
          value: currentBody,
          max: inbox.contentLimit,
          big: true,
        });
      }
      if (op !== "redeliver") {
        const field = op === "restore" ? "restore_reason" : "retire_reason";
        fields.push({
          name: "reason",
          label: t("retire.reason"),
          kind: "area",
          rows: 3,
          value: row[field] || "",
        });
      }
      const editsDelivery = op === "redeliver" || op === "replace";
      const isTopic = (values) => values.delivery === "topic";
      const isProject = (values) => values.delivery === "project";
      if (editsDelivery) {
        const initialDelivery = row.successor_delivery || target.delivery || "always";
        const initialProject = row.successor_project_name || (target.project_names && target.project_names[0]) || (choices.projects[0] || "");
        const initialTopic = row.successor_topic_id || (row.successor_topic_name && row.successor_topic_trigger ? "__new" : "");
        fields.push(
          { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: initialDelivery },
          { name: "project", label: t("delivery.area"), kind: "select", options: choices.projects.map((name) => [name, name]), value: initialProject, show: isProject },
          ...topicFields(choices, initialTopic, isTopic)
        );
        if (initialTopic === "__new") {
          for (const field of fields) {
            if (field.name === "new_topic_name") field.value = row.successor_topic_name || "";
            if (field.name === "new_topic_trigger") field.value = row.successor_topic_trigger || "";
            if (field.name === "new_topic_project") field.value = row.successor_project_name || "";
          }
        }
      }
      panel.append(
        form({
          fields,
          onCancel: () => togglePanel("change_edit"),
          onSubmit: (values) => {
            if (editsDelivery && !checkMemoryDelivery(values, { allowMultiProject: false })) return;
            act(async () => {
              const args = {
                change_id: row.change_id,
                version: row.version,
                target_revision_id: target.current_revision_id,
              };
              if (op === "replace") args.successor_content = values.successor_content;
              if (op !== "redeliver") args.reason = values.reason;
              if (editsDelivery) {
                args.delivery = values.delivery;
                if (isProject(values)) args.project = values.project;
                Object.assign(args, topicArgs(isTopic(values) ? values : {}));
              }
              await call("change.edit", args);
              inbox.panel = null;
            }, t("change.saved"));
          },
        })
      );
    })
    .catch(fail);
  return panel;
}

function changeDetail(row) {
  const target = row.target || {};
  const isReplace = row.operation === "replace";
  const snapshot = row.successor_snapshot || {};
  const openTargetBtn = button(
    t("change.open_target"),
    () => openMemory(row.target_memory_id, target.status).catch(fail),
    { kind: "quiet small" }
  );
  const nodes = [
    meta(idTag(row.change_id), t("change.meta", { op: named("change.op", row.operation), when: when(row.proposed_at) }), who(row.proposed_by)),
  ];
  if (isReplace) {
    nodes.push(h("p", { class: "rule-text", text: snapshot.content || (row.successor && row.successor.content) || "" }));
    if (row.successor_changed && row.successor) {
      nodes.push(h("div", { class: "callout warn" }, h("p", { text: t("change.replacement_changed", { content: row.successor.content }) })));
    }
  } else {
    nodes.push(
      h("p", { class: "rule-text", text: target.content }),
      h("p", { class: "sub" }, deliveryText(target), " ", openTargetBtn)
    );
  }
  if (row.operation !== "restore" && target.status === "retired" && target.retirement) {
    const retirement = target.retirement;
    nodes.push(
      h(
        "div",
        { class: "callout warn" },
        h("p", { text: t("change.target_retired") }),
        h("p", { text: `${named("retire.kind", retirement.kind || "legacy")} · ${day(retirement.retired_at)} — ${retirement.reason || ""}` })
      )
    );
  }

  const apply = button(t("change.apply"), () =>
    act(async () => {
      const result = await call("change.apply", {
        change_id: row.change_id,
        version: row.version,
        conflict_ids: row.blocking_conflict_ids,
      });
      return result.changed ? t("change.changed") : "";
    }, t("change.applied")),
    { kind: "primary" }
  );
  const gate = readGate(row.conflicts || [], row.blocking_conflict_ids, apply);
  const editLabel =
    row.operation === "redeliver"
      ? t("change.edit_delivery")
      : isReplace
        ? t("change.edit")
        : t("change.edit_reason");
  const rejectOp = row.proposed_by === "user" ? "withdraw" : "decline";
  nodes.push(
    h(
      "div",
      { class: "actions" },
      apply,
      button(editLabel, () => togglePanel("change_edit")),
      h("span", { class: "spacer" }),
      button(t(`change.${rejectOp}`), () => togglePanel(rejectOp), { kind: "danger quiet" })
    )
  );
  if (gate) nodes.push(gate);

  if (inbox.panel === "change_edit") nodes.push(changeEditPanel(row));
  for (const [name, op, done, danger] of [
    ["decline", "change.decline", "change.declined", true],
    ["withdraw", "change.withdraw", "change.withdrawn", true],
  ]) {
    if (inbox.panel !== name) continue;
    nodes.push(
      reasonPanel({
        label: t(`change.${name}_label`),
        confirm: t(`change.${name}`),
        danger,
        onCancel: () => togglePanel(name),
        onConfirm: (reason) =>
          act(async () => {
            await call(op, { change_id: row.change_id, reason });
            inbox.panel = null;
          }, t(done)),
      })
    );
  }

  if (isReplace) {
    nodes.push(
      section(
        t("change.target_to_retire"),
        null,
        h("p", { class: "rule-text quiet", text: target.content }),
        h("p", { class: "sub" }, deliveryText(target), " ", openTargetBtn)
      )
    );
  }
  if (row.operation === "retire" || isReplace) {
    const destination = row.destination || {};
    nodes.push(
      section(
        t("change.retirement"),
        null,
        fieldList([
          [t("retire.kind"), named("retire.kind", row.retirement_kind)],
          [t("retire.reason"), row.retire_reason],
          [t("change.moved_to"), row.relocated_to_id ? reference(row.relocated_to_id, destination.content || t("temporary.title"), 80) : null],
          [t("temporary.area"), row.relocated_to_id ? destination.project_name || t("common.every_area") : null],
          [t("change.until"), destination.expires_at ? day(destination.expires_at) : null],
        ])
      )
    );
  }
  if (row.operation === "restore") nodes.push(section(t("change.restore"), null, fieldList([[t("retire.reason"), row.restore_reason]])));
  const moveLines = deliveryMoveLines(row);
  if (moveLines.length) {
    nodes.push(section(t("change.delivery"), null, entries(moveLines, (line) => [null, line])));
  }
  if ((row.conflicts || []).length) nodes.push(section(t("conflicts.title"), row.conflicts.length, conflictEntries(row.conflicts)));
  nodes.push(
    section(
      t("evidence.title"),
      row.evidence.length,
      entries(row.evidence, (item) => [
        [named("evidence.kind", item.kind || "artifact"), item.id && short(item.id), item.ref].filter(Boolean).join(" · "),
        item.observation,
      ])
    )
  );
  return nodes;
}

// ---------------------------------------------------------------- close proposals

function closeEditPanel(row) {
  const { task, proposal } = row;
  return h(
    "div",
    { class: "inline-panel" },
    form({
      fields: [
        {
          name: "outcome",
          label: t("close.outcome"),
          kind: "radio",
          value: proposal.outcome,
          options: ["completed", "abandoned", "superseded"].map((outcome) => [outcome, named("close.outcome", outcome)]),
        },
        {
          name: "reason",
          label: t("common.reason"),
          kind: "area",
          rows: 2,
          value: proposal.reason || "",
          hint: t("work.close_reason_hint"),
        },
      ],
      submit: t("work.close"),
      kind: "danger solid",
      onCancel: () => togglePanel("edit_close"),
      onSubmit: (values) =>
        act(async () => {
          await call("task.close", { task_id: task.task_id, outcome: values.outcome, reason: values.reason });
          inbox.panel = null;
        }, t("work.closed")),
    })
  );
}

function closeDetail(row) {
  const { task, state, proposal } = row;
  const accept = button(t("close.accept"), () =>
    act(
      () => call("task.accept", { task_id: task.task_id, proposal, state_at: state.updated_at }),
      t("close.accepted")
    ),
    { kind: "primary" }
  );
  const nodes = [
    meta(idTag(task.task_id), t("close.meta", { outcome: named("close.outcome", proposal.outcome), when: when(proposal.proposed_at) }), who(proposal.proposed_by)),
    h("h1", { class: "title", text: task.name }),
    h("p", { class: "sub", text: `${task.project_name}  ·  ${t("work.as_of", { date: row.as_of })}` }),
    proposal.reason && proposal.reason.trim() ? h("div", { class: "callout" }, h("p", { text: proposal.reason })) : null,
  ];
  let gate = null;
  if (proposal.stale) {
    accept.disabled = true;
    nodes.push(h("div", { class: "callout warn" }, h("p", { text: t("close.stale") })));
    gate = h(
      "label",
      { class: "check" },
      h("input", { type: "checkbox", on: { change: (event) => (accept.disabled = !event.target.checked) } }),
      t("close.stale_ok")
    );
  }
  nodes.push(
    h(
      "div",
      { class: "actions" },
      accept,
      button(t("close.edit_close"), () => togglePanel("edit_close")),
      button(
        t("close.keep"),
        () => act(() => call("task.keep", { task_id: task.task_id }), t("close.kept")),
        { title: t("close.keep_title") }
      ),
      h("span", { class: "spacer" }),
      button(t("close.open_task"), () => openTask(task.task_id, row.activity === "dormant" ? "dormant" : "active").catch(fail), { kind: "quiet" })
    ),
    gate,
    inbox.panel === "edit_close" ? closeEditPanel(row) : null,
    section(
      t("work.state"),
      null,
      fieldList([
        [t("work.goal"), state.goal],
        [t("work.approach"), state.approach],
        [t("work.status"), state.status_text],
        [t("work.open_questions"), state.open_questions],
        [t("work.blockers"), state.blockers],
        [t("work.next_actions"), state.next_actions],
      ])
    ),
    (row.rulings || []).length
      ? section(
          t("work.rulings"),
          row.rulings.length,
          entries(row.rulings, (item) => [day(item.on), item.ruling, [t("work.user_said"), item.user_said]])
        )
      : null
  );
  return nodes;
}

page("inbox", {
  nav: "nav.inbox",
  icon: "inbox",
  root: h("div", { class: "page" }, inboxList.pane, inboxDetail.pane),
  list: inboxList.list,
  selected: () => inbox.selected,
  pick: pickInbox,
  async load() {
    const before = listIds(inboxList.list);
    const [queue, changeList, active, dormant] = await Promise.all([
      call("review.list", { include_deferred: inbox.includeDeferred }),
      call("change.list"),
      call("work.tasks", { activity: "active" }),
      call("work.tasks", { activity: "dormant" }),
    ]);
    inbox.candidates = queue.candidates;
    inbox.hiddenDeferred = queue.hidden_deferred || 0;
    inbox.contentLimit = queue.content_limit || changeList.content_limit;
    inbox.changes = changeList.changes;
    inbox.closes = [...active.tasks, ...dormant.tasks].filter((row) => row.proposal);
    const ids = inboxItems().map((item) => item.id);
    if (!ids.includes(inbox.selected)) inbox.panel = null;
    inbox.selected = keepSelection(ids, inbox.selected, before);
    renderInbox();
  },
  badge: (status) => status.pending_ready + status.memory_changes + status.close_proposals.length,
  keys: {
    a: () => inboxItem() && inboxItem().type === "candidate" && togglePanel("admit"),
    e: () => {
      const item = inboxItem();
      if (!item) return;
      if (item.type === "candidate") togglePanel("edit");
      else if (item.type === "change") togglePanel("change_edit");
    },
  },
});
