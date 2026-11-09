// Memories: active and retired rules and standing temporary context, each read in full.
"use strict";

const memoriesTab = { view: "active", rows: [], selected: null, detail: null };

function memoryId(row) {
  return memoriesTab.view === "temporary" ? row.context_id : row.memory_id;
}

function memoryRow() {
  return memoriesTab.rows.find((row) => memoryId(row) === memoriesTab.selected) || null;
}

function deliveryLabel(row) {
  return row.delivery === "topic" ? `topic:${row.topic_name}` : row.delivery;
}

function shownMemories() {
  const query = $("memory-search").value.trim();
  return memoriesTab.rows.filter((row) =>
    matches(
      query,
      memoryId(row),
      row.content,
      row.scope_name,
      row.delivery,
      row.topic_name,
      row.retire_reason,
      row.expires_at
    )
  );
}

function renderMemoryList() {
  const rows = shownMemories();
  const temporary = memoriesTab.view === "temporary";
  fillList("memory-list", rows, {
    idOf: memoryId,
    selected: memoriesTab.selected,
    text: (row) => row.content,
    sub: (row) =>
      temporary
        ? `${short(row.context_id)} · ${row.scope_name || "every scope"} · until ${day(row.expires_at)}`
        : `${short(row.memory_id)} · ${deliveryLabel(row)} · ${row.scope_name || "-"}`,
    pick: (id) => {
      memoriesTab.selected = id;
      closeForms();
      loadMemoryDetail().catch((error) => say(errorText(error), "error"));
    },
  });
  $("no-memories").hidden = rows.length > 0;
}

async function loadMemoryDetail() {
  const row = memoryRow();
  memoriesTab.detail =
    row && memoriesTab.view !== "temporary" ? await call("memory.get", { memory_id: row.memory_id }) : null;
  renderMemoryList();
  renderMemoryDetail();
}

function renderMemoryDetail() {
  const row = memoryRow();
  const body = $("memory-body");
  const actions = $("memory-actions");
  if (!row) {
    body.replaceChildren();
    actions.replaceChildren();
    return;
  }
  if (memoriesTab.view === "temporary") {
    body.replaceChildren(
      element("p", `temporary ${short(row.context_id)}`, "muted"),
      element("div", row.content, "body"),
      pairs([
        ["scope", row.scope_name || "every scope"],
        ["until", row.expires_at],
      ])
    );
    actions.replaceChildren(
      button("Edit…", () => editTemporary(row).catch((error) => say(errorText(error), "error"))),
      button("Make it a rule", () =>
        confirmBox(`Convert temporary ${short(row.context_id)} to an active rule?`, [row.content], "Convert", () =>
          acknowledging(
            (acknowledged) => call("temporary.to_memory", { context_id: row.context_id, acknowledged }),
            `Converted ${short(row.context_id)} to a rule.`
          )
        )
      ),
      button(
        "End now",
        () =>
          confirmBox(`End temporary ${short(row.context_id)} now?`, [row.content], "End it", () =>
            act(() => call("temporary.end", { context_id: row.context_id }), `Ended ${short(row.context_id)}.`)
          ),
        "danger"
      )
    );
    return;
  }

  const detail = memoriesTab.detail || row;
  const nodes = [
    element(
      "p",
      `${short(detail.memory_id)} · ${detail.status} · created ${day(detail.created_at)} by ${detail.created_by}`,
      "muted"
    ),
    element("div", detail.content, "body"),
    pairs([
      ["delivery", deliveryLabel(detail)],
      ["scope", detail.scope_name || "-"],
      ["topic trigger", detail.delivery === "topic" ? detail.topic_trigger : null],
    ]),
  ];
  if (detail.status === "retired") {
    nodes.push(
      block(
        "Retired",
        pairs([
          ["when", detail.retired_at],
          ["kind", detail.retirement_kind || "legacy"],
          ["reason", detail.retire_reason],
          ["successor", detail.superseded_by],
          ["moved to", detail.relocated_to_id ? `${detail.relocated_to_kind} ${detail.relocated_to_id}` : null],
        ])
      )
    );
  }
  nodes.push(
    block(
      "Evidence",
      bullets(detail.basis, (item) => [
        element("div", `${item.kind} · ${day(item.created_at)} · ${item.created_by}`, "muted"),
        element("div", `what: ${item.what}`),
        element("div", `prevention: ${item.prevention}`),
      ])
    ),
    block(
      "Revisions",
      bullets(detail.revisions, (item) => [
        element("div", `${day(item.created_at)} · ${item.actor}${item.note ? ` · ${item.note}` : ""}`, "muted"),
        element("div", item.content),
      ])
    )
  );
  body.replaceChildren(...nodes);

  if (memoriesTab.view === "retired") {
    actions.replaceChildren();
    return;
  }
  actions.replaceChildren(
    button("Edit…", () => editMemory(detail)),
    button("Delivery…", () => moveMemory(detail).catch((error) => say(errorText(error), "error"))),
    button("Make it temporary…", () => memoryToTemporary(detail)),
    button("Retire…", () => retireMemory(detail), "danger")
  );
}

function editMemory(row) {
  showForm("form-memories", {
    title: `Edit ${short(row.memory_id)}`,
    fields: [{ name: "content", label: "rule", kind: "area", value: row.content }],
    onSubmit: (values) =>
      acknowledging(
        (acknowledged) => call("memory.revise", { memory_id: row.memory_id, content: values.content, acknowledged }),
        `Revised ${short(row.memory_id)}.`
      ),
  });
}

function scopeOptions(choices, none) {
  return [["", none], ...choices.scopes.map((name) => [name, name])];
}

async function moveMemory(row) {
  const choices = await loadChoices();
  const isTopic = (values) => values.delivery === "topic";
  showForm("form-memories", {
    title: `Delivery of ${short(row.memory_id)}`,
    fields: [
      { name: "delivery", label: "delivery", kind: "select", options: DELIVERIES, value: row.delivery },
      {
        name: "scope",
        label: "scope",
        kind: "select",
        options: scopeOptions(choices, "none"),
        value: row.scope_name || "",
        hint: "An always rule may keep the scope it belongs to.",
        show: (values) => !isTopic(values),
      },
      ...topicFields(choices, row.topic_id, isTopic),
    ],
    onSubmit: (values) =>
      act(
        () =>
          call("memory.delivery", {
            memory_id: row.memory_id,
            delivery: values.delivery,
            scope: isTopic(values) ? null : values.scope || null,
            ...topicArgs(isTopic(values) ? values : {}),
          }),
        `Moved ${short(row.memory_id)} to ${values.delivery}.`
      ),
  });
}

function memoryToTemporary(row) {
  if (row.delivery === "topic") {
    say("A topic rule cannot become temporary; move it to always or scope first.", "warn");
    return;
  }
  showForm("form-memories", {
    title: `Make ${short(row.memory_id)} temporary`,
    fields: [{ name: "days", label: "days", kind: "number", step: "0.5", value: 7, hint: "more than 0, at most 14" }],
    submit: "Convert",
    onSubmit: (values) =>
      act(
        () => call("memory.to_temporary", { memory_id: row.memory_id, days: values.days }),
        `Converted ${short(row.memory_id)} to temporary context.`
      ),
  });
}

function retireMemory(row) {
  showForm("form-memories", {
    title: `Retire ${short(row.memory_id)}`,
    fields: [
      {
        name: "kind",
        label: "kind",
        kind: "select",
        options: [
          ["invalidated", "invalidated — it was wrong"],
          ["superseded", "superseded — another rule replaces it"],
          ["out_of_scope", "out_of_scope — it does not belong here"],
          ["relocated", "relocated — it lives in temporary context now"],
        ],
      },
      {
        name: "successor",
        label: "successor Memory id",
        hint: "the first 8 characters are enough",
        show: (values) => values.kind === "superseded",
      },
      {
        name: "destination",
        label: "Temporary Context id",
        hint: "the first 8 characters are enough",
        show: (values) => values.kind === "relocated",
      },
      { name: "reason", label: "reason", kind: "area", rows: 3 },
    ],
    submit: "Retire",
    onSubmit: (values) =>
      act(
        () =>
          call("memory.retire", {
            memory_id: row.memory_id,
            kind: values.kind,
            reason: values.reason,
            successor: values.kind === "superseded" ? values.successor : null,
            destination: values.kind === "relocated" ? values.destination : null,
          }),
        `Retired ${short(row.memory_id)}.`
      ),
  });
}

async function editTemporary(row) {
  const choices = await loadChoices();
  showForm("form-memories", {
    title: `Edit temporary ${short(row.context_id)}`,
    fields: [
      { name: "content", label: "context", kind: "area", value: row.content },
      { name: "scope", label: "scope", kind: "select", options: scopeOptions(choices, "every scope"), value: row.scope_name || "" },
    ],
    onSubmit: (values) =>
      act(
        () => call("temporary.revise", { context_id: row.context_id, content: values.content, scope: values.scope || null }),
        `Revised temporary ${short(row.context_id)}.`
      ),
  });
}

async function rememberNew() {
  const choices = await loadChoices();
  const isTopic = (values) => values.delivery === "topic";
  showForm("form-memories", {
    title: "Remember a rule",
    fields: [
      { name: "content", label: "rule", kind: "area", rows: 3, hint: "at most 120 characters; the rule only" },
      { name: "why", label: "why", kind: "area", rows: 3, hint: "optional: reason, measurements, dates, history" },
      { name: "delivery", label: "delivery", kind: "select", options: DELIVERIES, value: "always" },
      {
        name: "scope",
        label: "scope",
        kind: "select",
        options: scopeOptions(choices, "none"),
        show: (values) => values.delivery === "scope",
      },
      ...topicFields(choices, "", isTopic),
    ],
    submit: "Remember",
    onSubmit: (values) =>
      acknowledging(
        (acknowledged) =>
          call("memory.remember", {
            content: values.content,
            why: values.why,
            delivery: values.delivery,
            scope: values.delivery === "scope" ? values.scope || null : null,
            ...topicArgs(isTopic(values) ? values : {}),
            acknowledged,
          }),
        "Remembered."
      ),
  });
}

function temporaryNew() {
  showForm("form-memories", {
    title: "Record temporary context",
    fields: [
      { name: "content", label: "context", kind: "area", rows: 3 },
      { name: "days", label: "days", kind: "number", step: "0.5", value: 3, hint: "more than 0, at most 14" },
    ],
    submit: "Record",
    onSubmit: (values) =>
      act(() => call("temporary.add", { content: values.content, days: values.days }), "Recorded."),
  });
}

tab("memories", {
  async load() {
    memoriesTab.rows = (await call("memory.list", { view: memoriesTab.view })).rows;
    memoriesTab.selected = keepSelection(shownMemories(), memoryId, memoriesTab.selected);
    await loadMemoryDetail();
  },
});

viewButtons("memory-views", (view) => {
  memoriesTab.view = view;
  memoriesTab.selected = null;
  closeForms();
  reload().catch((error) => say(errorText(error), "error"));
});
$("memory-search").addEventListener("input", () => {
  memoriesTab.selected = keepSelection(shownMemories(), memoryId, memoriesTab.selected);
  loadMemoryDetail().catch((error) => say(errorText(error), "error"));
});
$("memory-new").addEventListener("click", () => rememberNew().catch((error) => say(errorText(error), "error")));
$("temporary-new").addEventListener("click", temporaryNew);
