// Changes: proposals to retire, replace, restore, or redeliver an existing Memory.
"use strict";

const changes = { rows: [], selected: null };

function changeRow() {
  return changes.rows.find((row) => row.change_id === changes.selected) || null;
}

function changeSummary(row) {
  const body =
    (row.successor_snapshot && row.successor_snapshot.content) ||
    (row.successor && row.successor.content) ||
    (row.target && row.target.content) ||
    "";
  return body;
}

function retirementText(item) {
  return [
    `${item.retirement_kind || "legacy"}`,
    item.retired_at ? `retired ${day(item.retired_at)}` : null,
    item.superseded_by ? `successor ${short(item.superseded_by)}` : null,
    item.relocated_to_id ? `moved to ${item.relocated_to_kind} ${short(item.relocated_to_id)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function changeDetail(row) {
  const target = row.target || {};
  const nodes = [
    element(
      "p",
      `${row.operation} · ${short(row.change_id)} · v${row.version} · target ${short(row.target_memory_id)} ` +
        `rev ${short(row.target_revision_id || "-")} · proposed by ${row.proposed_by} on ${day(row.proposed_at)}`,
      "muted"
    ),
    block("Target", element("div", target.content, "body")),
  ];
  if (target.status === "retired" && target.retirement) {
    const retirement = target.retirement;
    nodes.push(
      block(
        "Target is retired",
        pairs([
          ["kind", retirement.kind || "legacy"],
          ["retired", day(retirement.retired_at)],
          ["reason", retirement.reason],
          ["successor", retirement.superseded_by],
          ["moved to", retirement.relocated_to_id ? `${retirement.relocated_to_kind} ${retirement.relocated_to_id}` : null],
        ])
      )
    );
  }
  if (row.operation === "retire" || row.operation === "replace") {
    const destination = row.destination || {};
    nodes.push(
      block(
        "Retirement",
        pairs([
          ["kind", row.retirement_kind],
          ["reason", row.retire_reason],
          ["moved to", row.relocated_to_id ? `${row.relocated_to_kind} ${row.relocated_to_id}` : null],
          ["destination", destination.content],
          ["its scope", row.relocated_to_id ? destination.scope_name || "every scope" : null],
          ["until", destination.expires_at],
        ])
      )
    );
  }
  if (row.operation === "restore") nodes.push(block("Restore", pairs([["reason", row.restore_reason]])));
  if (row.delivery_move && row.delivery_move.length) {
    nodes.push(block("Delivery", bullets(row.delivery_move, (line) => line)));
  }
  if (row.successor) {
    const snapshot = row.successor_snapshot || {};
    nodes.push(
      block(
        "Replacement",
        pairs([
          ["candidate", `${short(row.successor.nomination_id)} v${snapshot.version}`],
          ["content", snapshot.content],
          ["scope", snapshot.scope_id],
          ["kind", snapshot.kind],
          ["evidence", (snapshot.evidence || []).join(", ") || "none"],
        ]),
        row.successor_changed
          ? element(
              "p",
              `The candidate changed after this proposal was read (now v${row.successor.version}: ` +
                `${row.successor.content}). Apply re-proposes it against the current one.`,
              "warn"
            )
          : element("p", `candidate version: ${row.successor.version}`, "muted")
      )
    );
  }
  nodes.push(
    block(
      "Evidence",
      bullets(row.evidence, (item) => `${item.kind || "reference"} ${item.id || item.ref || ""}: ${item.observation}`)
    )
  );
  if (row.conflicts && row.conflicts.length) {
    nodes.push(
      block(
        "Retired conflicts",
        bullets(row.conflicts, (item) => [
          element("div", `${short(item.memory_id)} · ${retirementText(item)}`, "conflict"),
          element("div", `retired because: ${item.retire_reason}`),
        ])
      )
    );
  }
  return nodes;
}

function renderChanges() {
  fillList("change-list", changes.rows, {
    idOf: (row) => row.change_id,
    selected: changes.selected,
    text: changeSummary,
    sub: (row) =>
      `${short(row.change_id)} · ${row.operation} · ${row.retirement_kind || "-"} · ` +
      `target ${short(row.target_memory_id)}`,
    pick: (id) => {
      changes.selected = id;
      closeForms();
      renderChanges();
      updateButtons();
    },
  });
  $("no-changes").hidden = changes.rows.length > 0;
  const row = changeRow();
  $("change-detail").hidden = !row;
  if (!row) return;
  $("change-body").replaceChildren(...changeDetail(row));
  $("change-read-row").hidden = row.blocking_conflict_ids.length === 0;
  $("change-read").checked = false;
  $("change-reason").value = "";
}

tab("changes", {
  async load() {
    changes.rows = (await call("change.list")).changes;
    changes.selected = keepSelection(changes.rows, (row) => row.change_id, changes.selected);
    renderChanges();
  },
  buttons() {
    const row = changeRow();
    const blocked = row && row.blocking_conflict_ids.length > 0 && !$("change-read").checked;
    $("change-apply").disabled = app.busy || !row || blocked;
    $("change-edit").disabled = app.busy || !row || row.operation === "redeliver";
    for (const id of ["change-decline", "change-withdraw"]) $(id).disabled = app.busy || !row;
  },
  status(status) {
    $("change-count").textContent = status.memory_changes ? `(${status.memory_changes})` : "";
  },
});

$("change-read").addEventListener("change", updateButtons);

$("change-apply").addEventListener("click", () => {
  const row = changeRow();
  act(async () => {
    const result = await call("change.apply", {
      change_id: row.change_id,
      version: row.version,
      conflict_ids: row.blocking_conflict_ids,
    });
    if (result.changed) {
      return "The target, successor, or retirement conflicts changed. Read the updated proposal and apply again.";
    }
    return "";
  }, `Applied ${row.operation} to ${short(row.target_memory_id)}.`);
});

$("change-edit").addEventListener("click", () => {
  const row = changeRow();
  const field = row.operation === "restore" ? "restore_reason" : "retire_reason";
  showForm("form-changes", {
    title: `Edit the ${field.replace("_", " ")}`,
    fields: [{ name: "reason", label: "reason", kind: "area", value: row[field] || "" }],
    onSubmit: (values) =>
      act(async () => {
        await call("change.reason", {
          change_id: row.change_id,
          version: row.version,
          target_revision_id: row.target.current_revision_id,
          reason: values.reason,
        });
      }, `Updated the proposal for ${short(row.target_memory_id)}.`),
  });
});

for (const [id, op, word] of [
  ["change-decline", "change.decline", "Declined"],
  ["change-withdraw", "change.withdraw", "Withdrew"],
]) {
  $(id).addEventListener("click", () => {
    const row = changeRow();
    const reason = required("change-reason", "why, in the reason box,");
    if (!reason) return;
    act(async () => {
      await call(op, { change_id: row.change_id, reason });
    }, `${word} ${short(row.change_id)}.`);
  });
}
