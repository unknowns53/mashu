// Settings: store health, what a session receives, scopes, topics, routes, and the schema.
"use strict";

const SECTIONS = [
  ["health", "Health/status", "capacity, queues, recent evidence, and work"],
  ["bootstrap", "Bootstrap preview", "what a session opened in a directory receives"],
  ["scopes", "Scopes", "memory homes and their opening cost"],
  ["topics", "Topics", "rules read when one kind of work begins"],
  ["routes", "Routes", "directory trees mapped to scopes or ignored"],
  ["interval", "Checkpoint interval", "how often one session may write a task's state"],
  ["schema", "Schema/migrations", "database version and pending changes"],
];

const settingsTab = { section: "health", bootstrapCwd: "" };

function renderSections() {
  fillList("settings-list", SECTIONS, {
    idOf: ([id]) => id,
    selected: settingsTab.section,
    text: ([, title]) => title,
    sub: ([, , about]) => about,
    pick: (id) => {
      settingsTab.section = id;
      closeForms();
      reload().catch((error) => say(errorText(error), "error"));
    },
  });
}

/** A table of rows, each with buttons from actions(row). */
function table(headings, rows, cells, actions) {
  const node = element("table");
  const head = element("tr");
  head.append(...headings.map((heading) => element("th", heading)), element("th"));
  node.append(head);
  for (const row of rows) {
    const line = element("tr");
    line.append(...cells(row).map((cell) => element("td", cell)));
    const buttons = element("td", undefined, "buttons");
    buttons.append(...actions(row));
    line.append(buttons);
    node.append(line);
  }
  return node;
}

function failureRoutes(health) {
  const parts = health.failure_routes
    .filter((route) => health.delivery_failures_by_route[route])
    .map((route) => `${route} ${health.delivery_failures_by_route[route]}`);
  return parts.length ? ` (${parts.join(", ")})` : "";
}

async function healthSection() {
  const health = await call("settings.health");
  const caps = health.capacities;
  const counts = health.memory_counts;
  return [
    pairs([
      [
        "schema",
        health.schema_pending.length
          ? `${health.schema_pending.length} pending: ${health.schema_pending.join(", ")}`
          : "up to date",
      ],
      ["active memories", `always ${counts.always || 0} · scope ${counts.scope || 0} · topic ${counts.topic || 0}`],
      ["memory capacity", `always ${health.memory_always_tokens}/${caps.always} · worst ${health.memory_worst_tokens}/${caps.worst}`],
      ["task cards", `${health.active_states} active · worst ${health.state_worst_tokens}/${caps.project} tokens`],
      ["temporary", `${health.temporary_count} standing · ${health.temporary_tokens}/${caps.temporary} tokens`],
      ["review queue", `${health.pending_ready} ready · ${health.pending_deferred} deferred`],
      ["evidence", `${health.ledger_30d} ledger entries in 30 days`],
      ["delivery health", `${health.delivery_failures_30d} suspected failures in 30 days${failureRoutes(health)}`],
      ["knowledge unused", `${health.knowledge_unused_30d} in 30 days`],
      ["scopes", String(health.scope_count)],
      [
        "topics",
        `${health.topic_count}` +
          (health.topic_heaviest_name
            ? ` · heaviest ${health.topic_heaviest_name} ${health.topic_heaviest_tokens}/${caps.topic} tokens`
            : ""),
      ],
      ["tasks", `${health.tasks_active} active · ${health.tasks_dormant} dormant · ${health.tasks_closed} closed`],
    ]),
  ];
}

function bootstrapSection() {
  const form = element("form", undefined, "row");
  const input = element("input");
  input.type = "text";
  input.placeholder = "a directory, as a session would open in it";
  input.value = settingsTab.bootstrapCwd;
  const go = element("button", "Preview", "primary");
  go.type = "submit";
  form.append(input, go);
  const result = element("div");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    settingsTab.bootstrapCwd = input.value.trim();
    if (!settingsTab.bootstrapCwd) return;
    try {
      result.replaceChildren(...bootstrapView(await call("settings.bootstrap", { cwd: settingsTab.bootstrapCwd })));
    } catch (error) {
      say(errorText(error), "error");
    }
  });
  return [
    element("p", "Building a preview records it as served, as an opening session would be.", "muted"),
    form,
    result,
  ];
}

function bootstrapView(answer) {
  const memoriesOf = (rows) => bullets(rows, (row) => `${short(row.memory_id)} ${row.content}`);
  return [
    pairs([
      ["scope", `${answer.scope || "—"} · routed ${answer.routed ? "yes" : "no"}`],
      ["schema", answer.schema_pending && answer.schema_pending.length ? `${answer.schema_pending.length} migration(s) pending` : null],
      ["review candidates", `${answer.pending} pending`],
      [
        "tokens",
        `memory ${answer.memory_tokens} · cards ${answer.card_tokens ?? answer.project_tokens} · ` +
          `temporary ${answer.temporary_tokens}`,
      ],
      ["total", `${answer.tokens}/${answer.capacity}${answer.over_budget ? " · OVER CAPACITY" : ""}`],
    ]),
    block(`Always memories (${answer.always.length})`, memoriesOf(answer.always)),
    block(`Scoped memories (${answer.scoped.length})`, memoriesOf(answer.scoped)),
    block(
      `Topics (${answer.topics.length})`,
      bullets(answer.topics, (row) => row.line),
      answer.topic_instruction && element("p", answer.topic_instruction, "muted")
    ),
    block(
      `Active task cards (${answer.states.length})`,
      bullets(answer.states, (row) => [element("div", `${row.task}  ${row.heading}`, "muted"), element("div", row.content)]),
      answer.task_instruction && element("p", answer.task_instruction, "muted")
    ),
    block(
      `Temporary context (${answer.temporary.length})`,
      bullets(answer.temporary, (row) => `until ${row.expires_at}  ${row.content}`)
    ),
  ];
}

async function scopesSection() {
  const { scopes } = await call("scope.list");
  const edit = (row) =>
    showForm("form-settings", {
      title: row ? `Edit scope ${row.name}` : "New scope",
      fields: [
        { name: "name", label: "scope name", value: row ? row.name : "" },
        { name: "summary", label: "about", kind: "area", rows: 2, value: row ? row.summary : "", hint: "optional" },
      ],
      submit: row ? "Save" : "Create",
      onSubmit: (values) =>
        act(
          async () => {
            const saved = await call("scope.save", { scope_id: row ? row.scope_id : null, ...values });
            if (saved.unchecked) return `Saved ${values.name}; the banned-pattern list was unavailable, so nothing was checked.`;
            if (saved.malformed && saved.malformed.length) return `Saved ${values.name}; some banned-pattern lines could not be compiled.`;
            return "";
          },
          `Saved scope ${values.name}.`
        ),
    });
  return [
    rowOf(button("New scope…", () => edit(null))),
    scopes.length
      ? table(
          ["scope", "memories", "pushed tokens", "about"],
          scopes,
          (row) => [row.name, String(row.n_active), String(row.push_tokens), row.summary || "—"],
          (row) => [button("Edit…", () => edit(row))]
        )
      : element("p", "No scopes yet.", "muted"),
  ];
}

async function topicsSection() {
  const [{ topics, capacity }, choices] = await Promise.all([call("topic.list"), loadChoices()]);
  const scopeChoices = [["", "every session"], ...choices.scopes.map((name) => [name, name])];
  const edit = (row) =>
    showForm("form-settings", {
      title: row ? `Edit topic ${row.name}` : "New topic",
      fields: [
        { name: "name", label: "topic name", value: row ? row.name : "", hint: "at most 40 characters" },
        { name: "trigger", label: "when to read it", value: row ? row.trigger : "", hint: "one sentence, at most 160 characters" },
        { name: "scope", label: "listed in scope", kind: "select", options: scopeChoices, value: row ? row.scope_name || "" : "" },
        ...(row
          ? [
              {
                name: "action",
                label: "action",
                value: row.action || "",
                hint: "the tool calls whose start shows its rules (delegate = Agent / Task subagent calls); empty removes it",
              },
              {
                name: "source",
                label: "source",
                value: row.source || "",
                hint: "the note or document it comes from, from the vault or repository root; empty removes it",
              },
            ]
          : []),
      ],
      submit: row ? "Save" : "Create",
      onSubmit: (values) =>
        act(
          () => call("topic.save", { topic_id: row ? row.topic_id : null, ...values, scope: values.scope || null }),
          `Saved topic ${values.name}.`
        ),
    });
  const remove = (row) =>
    confirmBox(
      `Remove topic ${row.name}?`,
      ["A topic past rules or proposals name is archived instead of deleted."],
      "Remove",
      () =>
        act(async () => {
          const removed = await call("topic.remove", { topic_id: row.topic_id });
          return removed.removed === "archived" ? `Archived topic ${row.name}; past rules or proposals name it.` : "";
        }, `Deleted topic ${row.name}.`)
    );
  return [
    rowOf(button("New topic…", () => edit(null))),
    topics.length
      ? table(
          ["topic", "listed in", "rules", "tokens", "trigger", "source"],
          topics,
          (row) => [
            row.name + (row.action ? ` · before: ${row.action}` : ""),
            row.scope_name || "every session",
            String(row.rules),
            `body ${row.body_tokens}/${capacity} · line ${row.line_tokens}`,
            row.trigger,
            row.source_text || "—",
          ],
          (row) => [button("Edit…", () => edit(row)), button("Remove", () => remove(row), "danger")]
        )
      : element("p", "No topics yet.", "muted"),
  ];
}

async function routesSection() {
  const [{ routes }, choices] = await Promise.all([call("route.list"), loadChoices()]);
  const edit = (row) =>
    showForm("form-settings", {
      title: row ? `Edit route ${row.path_prefix}` : "New route",
      fields: [
        { name: "path", label: "directory path", value: row ? row.path_prefix : "" },
        {
          name: "scope",
          label: "scope",
          kind: "select",
          options: [["", "ignored on purpose"], ...choices.scopes.map((name) => [name, name])],
          value: row ? row.scope_name || "" : "",
        },
      ],
      submit: row ? "Save" : "Add",
      onSubmit: (values) =>
        act(
          () => call("route.save", { route_id: row ? row.route_id : null, path: values.path, scope: values.scope || null }),
          `Routed ${values.path} to ${values.scope || "nothing (ignored)"}.`
        ),
    });
  const remove = (row) =>
    confirmBox(`Remove route ${row.path_prefix}?`, [], "Remove", () =>
      act(() => call("route.remove", { path: row.path_prefix }), `Removed ${row.path_prefix}.`)
    );
  return [
    rowOf(button("New route…", () => edit(null))),
    routes.length
      ? table(
          ["directory", "scope"],
          routes,
          (row) => [row.path_prefix, row.scope_name || "ignored"],
          (row) => [button("Edit…", () => edit(row)), button("Remove", () => remove(row), "danger")]
        )
      : element("p", "No routes yet.", "muted"),
  ];
}

async function intervalSection() {
  const { minutes, max } = await call("interval.get");
  const form = element("form", undefined, "row");
  const input = element("input");
  input.type = "number";
  input.min = "0";
  input.max = String(max);
  input.value = String(minutes);
  const save = element("button", "Save", "primary");
  save.type = "submit";
  form.append(input, element("span", "minutes (0 for no limit)", "muted"), save);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const wanted = Number(input.value);
    if (!Number.isInteger(wanted)) {
      say(`${input.value} is not a whole number of minutes.`, "warn");
      return;
    }
    act(() => call("interval.set", { minutes: wanted }), `Checkpoint interval set to ${wanted} minutes.`);
  });
  return [
    element(
      "p",
      minutes === 0
        ? "One session may write a task's state as often as it likes."
        : `Once every ${minutes} minutes. A second write inside that time is refused whatever it carries, ` +
            "and the session's next MCP call reads a changed value."
    ),
    form,
  ];
}

async function schemaSection() {
  const { pending } = await call("schema.status");
  if (!pending.length) return [element("p", "Schema is up to date.")];
  return [
    element("p", `${pending.length} migration(s) pending`, "warn"),
    bullets(pending, (name) => name),
    rowOf(button(
        "Apply migrations…",
        () =>
          confirmBox(`Apply ${pending.length} pending migration(s)?`, pending, "Apply", () =>
            act(async () => {
              const { applied } = await call("schema.migrate");
              return applied.length ? "" : "The schema was already up to date.";
            }, "Applied the pending migrations.")
          ),
        "primary"
      )),
  ];
}

const SECTION_VIEWS = {
  health: healthSection,
  bootstrap: async () => bootstrapSection(),
  scopes: scopesSection,
  topics: topicsSection,
  routes: routesSection,
  interval: intervalSection,
  schema: schemaSection,
};

tab("settings", {
  async load() {
    renderSections();
    const [, title] = SECTIONS.find(([id]) => id === settingsTab.section);
    $("settings-body").replaceChildren(element("h2", title, "title"), ...(await SECTION_VIEWS[settingsTab.section]()));
  },
});
