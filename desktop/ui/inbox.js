// Inbox: everything waiting for the user's decision, in one list. Candidates are admitted,
// declined, deferred, or reworded; Memory change proposals applied, declined, or withdrawn;
// close proposals accepted or dropped.
"use strict";

const inbox = {
  candidates: [],
  changes: [],
  closes: [],
  hiddenDeferred: 0,
  contentLimit: null, // the Settings screen's limit on one memory body
  includeDeferred: false,
  selected: null,
  panel: null, // the open inline panel: "admit", "edit", "decline", "defer", "reason", "withdraw"
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
  inbox.selected = id;
  inbox.panel = null;
  renderInbox();
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
          secondary: [named("candidate.kind", row.kind), row.scope_name, when(row.created_at)],
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
      row.scope_name
        ? t("candidate.meta_area", { kind: named("candidate.kind", row.kind), area: row.scope_name, when: when(row.created_at) })
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
        button(t("candidate.edit"), () => togglePanel("edit")),
        h("span", { class: "spacer" }),
        button(t("candidate.defer"), () => togglePanel("defer"), { kind: "quiet" }),
        button(t("candidate.decline"), () => togglePanel("decline"), { kind: "danger quiet" })
      )
    );
    if (gate) nodes.push(gate);
  }

  if (inbox.panel === "admit") nodes.push(admitPanel(row));
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
      panel.append(
        form({
          fields: [
            { name: "delivery", label: t("delivery.title"), kind: "radio", options: deliveryOptions(), value: row.scope_id ? "scope" : "always" },
            ...topicFields(choices, "", isTopic),
          ],
          submit: t("candidate.admit"),
          onCancel: () => togglePanel("admit"),
          onSubmit: (values) =>
            act(async () => {
              const result = await call("review.admit", {
                nomination_id: row.nomination_id,
                expected_version: row.version,
                delivery: values.delivery,
                ...topicArgs(isTopic(values) ? values : {}),
                conflict_ids: row.blocking_conflict_ids,
              });
              inbox.panel = null;
              if (result.changed === "candidate") return t("candidate.changed");
              if (result.changed === "conflicts") return t("candidate.conflicts_changed");
              return "";
            }, t("candidate.admitted")),
        })
      );
    })
    .catch(fail);
  return panel;
}

// ---------------------------------------------------------------- Memory changes

function changeDetail(row) {
  const target = row.target || {};
  const nodes = [
    meta(idTag(row.change_id), t("change.meta", { op: named("change.op", row.operation), when: when(row.proposed_at) }), who(row.proposed_by)),
    h("p", { class: "rule-text", text: target.content }),
    h("p", { class: "sub", text: deliveryText(target) }),
  ];
  if (target.status === "retired" && target.retirement) {
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
  nodes.push(
    h(
      "div",
      { class: "actions" },
      apply,
      row.operation === "redeliver" ? null : button(t("change.edit_reason"), () => togglePanel("reason")),
      h("span", { class: "spacer" }),
      button(t("change.withdraw"), () => togglePanel("withdraw"), { kind: "quiet" }),
      button(t("change.decline"), () => togglePanel("decline"), { kind: "danger quiet" })
    )
  );
  if (gate) nodes.push(gate);

  if (inbox.panel === "reason") {
    const field = row.operation === "restore" ? "restore_reason" : "retire_reason";
    nodes.push(
      h(
        "div",
        { class: "inline-panel" },
        form({
          fields: [{ name: "reason", label: t("retire.reason"), kind: "area", rows: 3, value: row[field] || "" }],
          onCancel: () => togglePanel("reason"),
          onSubmit: (values) =>
            act(async () => {
              await call("change.reason", {
                change_id: row.change_id,
                version: row.version,
                target_revision_id: target.current_revision_id,
                reason: values.reason,
              });
              inbox.panel = null;
            }, t("change.reason_saved")),
        })
      )
    );
  }
  for (const [name, op, done, danger] of [
    ["decline", "change.decline", "change.declined", true],
    ["withdraw", "change.withdraw", "change.withdrawn", false],
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

  if (row.operation === "retire" || row.operation === "replace") {
    const destination = row.destination || {};
    nodes.push(
      section(
        t("change.retirement"),
        null,
        fieldList([
          [t("retire.kind"), named("retire.kind", row.retirement_kind)],
          [t("retire.reason"), row.retire_reason],
          [t("change.moved_to"), row.relocated_to_id ? reference(row.relocated_to_id, destination.content || t("temporary.title"), 80) : null],
          [t("temporary.area"), row.relocated_to_id ? destination.scope_name || t("common.every_area") : null],
          [t("change.until"), destination.expires_at ? day(destination.expires_at) : null],
        ])
      )
    );
  }
  if (row.operation === "restore") nodes.push(section(t("change.restore"), null, fieldList([[t("retire.reason"), row.restore_reason]])));
  if (row.delivery_move && row.delivery_move.length) {
    nodes.push(section(t("change.delivery"), null, entries(row.delivery_move, (line) => [null, line])));
  }
  if (row.successor) {
    const snapshot = row.successor_snapshot || {};
    nodes.push(
      section(
        t("change.replacement"),
        null,
        h("p", { class: "rule-text quiet", text: snapshot.content }),
        row.successor_changed
          ? h("div", { class: "callout warn" }, h("p", { text: t("change.replacement_changed", { content: row.successor.content }) }))
          : null
      )
    );
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
    h("div", { class: "callout" }, h("p", { text: proposal.reason })),
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
      button(
        t("close.keep"),
        () => act(() => call("task.keep", { task_id: task.task_id }), t("close.kept")),
        { title: t("close.keep_title") }
      )
    ),
    gate,
    section(
      t("work.state"),
      null,
      fieldList([
        [t("work.goal"), state.goal],
        [t("work.status"), state.status_text],
        [t("work.open_questions"), state.open_questions],
        [t("work.blockers"), state.blockers],
        [t("work.next_actions"), state.next_actions],
      ])
    )
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
    inbox.contentLimit = queue.content_limit;
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
    e: () => inboxItem() && inboxItem().type === "candidate" && togglePanel("edit"),
  },
});
