// Review: pending candidates, admitted, turned down, put off, or reworded one at a time.
"use strict";

const review = { rows: [], selected: null };

function reviewRow() {
  return review.rows.find((row) => row.nomination_id === review.selected) || null;
}

function renderReview() {
  fillList("queue", review.rows, {
    idOf: (row) => row.nomination_id,
    selected: review.selected,
    text: (row) => row.content,
    sub: (row) =>
      `${short(row.nomination_id)} · ${row.kind} · ${row.scope_name || "-"} · ` +
      `${ageDays(row.created_at)}d`,
    dim: (row) => row.deferred_at,
    pick: (id) => {
      review.selected = id;
      closeForms();
      renderReview();
    },
  });
  $("empty").hidden = review.rows.length > 0;

  const row = reviewRow();
  $("detail").hidden = !row;
  if (!row) return;
  $("meta").textContent =
    `${row.kind} · [${row.scope_name || "-"}] · ${short(row.nomination_id)} · ` +
    `v${row.version} · waiting ${ageDays(row.created_at)} day(s)`;
  $("deferred-note").hidden = !row.deferred_at;
  $("deferred-note").textContent = `Put off earlier: ${row.defer_reason || ""}`;
  $("content").textContent = row.content;
  $("edit-form").hidden = true;
  $("content").hidden = false;

  const conflicts = row.conflict_rows || [];
  $("conflicts").hidden = conflicts.length === 0;
  $("conflict-list").replaceChildren(...conflicts.map(conflictItem));
  $("conflicts-read").checked = false;

  $("evidence-list").replaceChildren(...(row.evidence_rows || []).map(evidenceItem));

  $("reason").value = "";
}

function conflictItem(conflict) {
  const item = element("li");
  item.append(
    element(
      "div",
      `${short(conflict.memory_id)} · ${conflict.retirement_kind || "legacy"} · ` +
        `${day(conflict.retired_at)}`,
      "conflict"
    ),
    element("div", `retired because: ${conflict.retire_reason}`)
  );
  if (conflict.superseded_by) item.append(element("div", `successor: ${conflict.superseded_by}`));
  if (conflict.relocated_to_id) {
    item.append(element("div", `moved to ${conflict.relocated_to_kind}: ${conflict.relocated_to_id}`));
  }
  return item;
}

function evidenceItem(evidence) {
  const item = element("li");
  item.append(
    element("div", `${evidence.kind} · ${day(evidence.created_at)}`, "muted"),
    element("div", `what: ${evidence.what}`),
    element("div", `prevention: ${evidence.prevention}`)
  );
  return item;
}

tab("review", {
  async load() {
    const queue = await call("review.list", { include_deferred: $("include-deferred").checked });
    review.rows = queue.candidates;
    review.selected = keepSelection(review.rows, (row) => row.nomination_id, review.selected);
    renderReview();
  },
  buttons() {
    const row = reviewRow();
    const blocked = row && row.blocking_conflict_ids.length > 0 && !$("conflicts-read").checked;
    $("admit").disabled = app.busy || !row || blocked;
    for (const id of ["edit", "decline", "defer"]) $(id).disabled = app.busy || !row;
  },
  status(status) {
    $("review-count").textContent =
      `(${status.pending_ready}` +
      (status.pending_deferred ? ` · ${status.pending_deferred} deferred)` : ")");
  },
});

$("conflicts-read").addEventListener("change", updateButtons);
$("include-deferred").addEventListener("change", () => reload().catch((e) => say(errorText(e), "error")));

async function admitForm(row) {
  const choices = await loadChoices();
  const isTopic = (values) => values.delivery === "topic";
  showForm("form-review", {
    title: `Admit ${short(row.nomination_id)}`,
    fields: [
      { name: "delivery", label: "delivery", kind: "select", options: DELIVERIES, value: row.scope_id ? "scope" : "always" },
      ...topicFields(choices, "", isTopic),
    ],
    submit: "Admit",
    onSubmit: (values) =>
      act(async () => {
        const result = await call("review.admit", {
          nomination_id: row.nomination_id,
          expected_version: row.version,
          delivery: values.delivery,
          ...topicArgs(isTopic(values) ? values : {}),
          conflict_ids: row.blocking_conflict_ids,
        });
        if (result.changed === "candidate") {
          return "The candidate changed since it was shown. Read the current wording and admit again.";
        }
        if (result.changed === "conflicts") {
          return "Its retirement conflicts changed. Read the updated candidate and admit again.";
        }
        return "";
      }, `Admitted ${short(row.nomination_id)}.`),
  });
}

$("admit").addEventListener("click", () => admitForm(reviewRow()).catch((error) => say(errorText(error), "error")));

$("edit").addEventListener("click", () => {
  $("edit-text").value = reviewRow().content;
  $("content").hidden = true;
  $("edit-form").hidden = false;
  $("edit-text").focus();
});
$("edit-cancel").addEventListener("click", renderReview);
$("edit-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const row = reviewRow();
  const content = $("edit-text").value;
  act(async () => {
    await call("review.revise", { nomination_id: row.nomination_id, content });
  }, `Saved the new wording of ${short(row.nomination_id)}; it is still pending.`);
});

for (const [id, op, word] of [
  ["decline", "review.decline", "Turned down"],
  ["defer", "review.defer", "Put off"],
]) {
  $(id).addEventListener("click", () => {
    const row = reviewRow();
    const reason = required("reason", "why, in the reason box,");
    if (!reason) return;
    act(async () => {
      await call(op, { nomination_id: row.nomination_id, reason });
    }, `${word} ${short(row.nomination_id)}.`);
  });
}
