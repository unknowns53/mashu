// Settings: store health, what a session receives, scopes, topics, routes, the checkpoint
// interval, the schema, and the window's language.
"use strict";

const SECTIONS = ["health", "bootstrap", "scopes", "topics", "routes", "interval", "schema", "display"];

const settings = { section: "health", editing: null, bootstrapCwd: "", bootstrap: null };

const settingsList = listPane();
settingsList.pane.classList.add("narrow");
const settingsDetail = detailPane();

function pickSection(id) {
  settings.section = id;
  settings.editing = null;
  reload().catch(fail);
}

function renderSections() {
  fillList(
    settingsList.list,
    [{ rows: SECTIONS.map((id) => ({ id, primary: t(`settings.${id}`), secondary: [t(`settings.${id}.about`)] })) }],
    { selected: settings.section, onPick: pickSection }
  );
}

/** A form in place of the section, closed by saving or cancelling. */
function editInSection(formSpec) {
  settings.editing = formSpec;
  renderEditing();
}

function renderEditing() {
  const spec = settings.editing;
  settingsDetail.mark(`${settings.section}:edit`);
  settingsDetail.inner.replaceChildren(
    form({
      ...spec,
      onCancel: () => {
        settings.editing = null;
        reload().catch(fail);
      },
      onSubmit: (values) =>
        act(async () => {
          const note = await spec.onSubmit(values);
          settings.editing = null;
          return note;
        }, spec.done),
    })
  );
}

/** A table whose rows each end in buttons from ops(row). */
function table(headings, rows, cells, ops) {
  return h(
    "table",
    { class: "table" },
    h("thead", null, h("tr", null, headings.map((heading) => h("th", { text: heading })), h("th"))),
    h(
      "tbody",
      null,
      rows.map((row) =>
        h(
          "tr",
          null,
          cells(row).map(([text, kind]) => h("td", { class: kind || "", text })),
          h("td", { class: "ops" }, ops(row))
        )
      )
    )
  );
}

function sectionHead(title, ...actions) {
  return h("div", { class: "row heading-row" }, h("h1", { class: "title", text: title }), h("span", { class: "spacer" }), actions);
}

// ---------------------------------------------------------------- sections

async function healthSection() {
  const health = await call("settings.health");
  const caps = health.capacities;
  const counts = health.memory_counts;
  const failures = health.failure_routes
    .filter((route) => health.delivery_failures_by_route[route])
    .map((route) => `${route} ${health.delivery_failures_by_route[route]}`);
  return [
    h("h1", { class: "title", text: t("settings.health") }),
    section(
      t("health.capacity"),
      null,
      h(
        "div",
        { class: "meters" },
        meterRow(t("health.always"), health.memory_always_tokens, caps.always),
        meterRow(t("health.worst"), health.memory_worst_tokens, caps.worst),
        meterRow(t("health.cards"), health.state_worst_tokens, caps.project),
        meterRow(t("health.temporary"), health.temporary_tokens, caps.temporary),
        health.topic_heaviest_name ? meterRow(t("health.topic", { name: health.topic_heaviest_name }), health.topic_heaviest_tokens, caps.topic) : null
      )
    ),
    section(
      t("health.counts"),
      null,
      fieldList([
        [t("settings.schema"), health.schema_pending.length ? t("schema.pending", { n: health.schema_pending.length }) : t("health.schema_ok")],
        [t("health.rules"), t("health.rules_value", { always: counts.always || 0, scope: counts.scope || 0, topic: counts.topic || 0 })],
        [t("health.queue"), t("health.queue_value", { ready: health.pending_ready, deferred: health.pending_deferred })],
        [t("health.tasks"), t("health.tasks_value", { a: health.tasks_active, d: health.tasks_dormant, c: health.tasks_closed })],
        [t("settings.scopes"), String(health.scope_count)],
        [t("settings.topics"), String(health.topic_count)],
      ])
    ),
    section(
      t("health.recent"),
      null,
      fieldList([
        [t("health.ledger"), t("health.n", { n: health.ledger_30d })],
        [t("health.failures"), t("health.n", { n: health.delivery_failures_30d }) + (failures.length ? `（${failures.join(", ")}）` : "")],
        [t("health.unused"), t("health.n", { n: health.knowledge_unused_30d })],
      ])
    ),
  ];
}

function bootstrapSection() {
  const input = h("input", { type: "text", placeholder: t("bootstrap.placeholder"), value: settings.bootstrapCwd });
  const result = h("div");
  const run = async (event) => {
    event.preventDefault();
    settings.bootstrapCwd = input.value.trim();
    if (!settings.bootstrapCwd) return;
    try {
      settings.bootstrap = await call("settings.bootstrap", { cwd: settings.bootstrapCwd });
      result.replaceChildren(...bootstrapView(settings.bootstrap));
    } catch (error) {
      fail(error);
    }
  };
  if (settings.bootstrap) result.append(...bootstrapView(settings.bootstrap));
  return [
    h("h1", { class: "title", text: t("settings.bootstrap") }),
    h("p", { class: "sub", text: t("bootstrap.note") }),
    h("form", { class: "row preview-row", on: { submit: run } }, input, h("button", { type: "submit", class: "btn primary", text: t("bootstrap.run") })),
    result,
  ];
}

function bootstrapView(answer) {
  const rules = (rows) => entries(rows, (row) => [null, row.content]);
  return [
    h("p", { class: "sub", text: `${t("bootstrap.area")}: ${answer.scope || t("bootstrap.unrouted")}` }),
    h(
      "div",
      { class: "meters preview-total" },
      meterRow(`${t("bootstrap.total")}${answer.over_budget ? `（${t("bootstrap.over")}）` : ""}`, answer.tokens, answer.capacity)
    ),
    section(t("bootstrap.always"), answer.always.length, rules(answer.always)),
    section(t("bootstrap.scoped"), answer.scoped.length, rules(answer.scoped)),
    section(t("bootstrap.topics"), answer.topics.length, entries(answer.topics, (row) => [null, row.line])),
    section(t("bootstrap.cards"), answer.states.length, entries(answer.states, (row) => [row.heading, row.content])),
    section(t("bootstrap.temporary"), answer.temporary.length, entries(answer.temporary, (row) => [t("temporary.until", { when: day(row.expires_at) }), row.content])),
  ];
}

async function scopesSection() {
  const { scopes } = await call("scope.list");
  const edit = (row) =>
    editInSection({
      title: row ? row.name : t("scope.new"),
      fields: [
        { name: "name", label: t("scope.name"), value: row ? row.name : "" },
        { name: "summary", label: t("scope.summary"), kind: "area", rows: 2, value: row ? row.summary : "" },
      ],
      submit: row ? t("common.save") : t("common.create"),
      done: t("scope.saved"),
      onSubmit: async (values) => {
        const saved = await call("scope.save", { scope_id: row ? row.scope_id : null, ...values });
        if (saved.unchecked) return t("scope.unchecked");
        if (saved.malformed && saved.malformed.length) return t("scope.malformed");
        return "";
      },
    });
  return [
    sectionHead(t("settings.scopes"), button(t("scope.new"), () => edit(null))),
    table(
      [t("scope.name"), t("scope.rules"), t("scope.tokens"), t("scope.summary")],
      scopes,
      (row) => [
        [row.name, "name"],
        [String(row.n_active), row.n_active ? "num" : "num zero"],
        [`${row.push_tokens} token`, row.push_tokens ? "num" : "num zero"],
        [row.summary || "", "muted"],
      ],
      (row) => [button(t("common.edit"), () => edit(row), { kind: "quiet small" })]
    ),
  ];
}

async function topicsSection() {
  const [{ topics, capacity }, choices] = await Promise.all([call("topic.list"), loadChoices()]);
  const edit = (row) =>
    editInSection({
      title: row ? row.name : t("topic.new"),
      fields: [
        { name: "name", label: t("topic.name"), value: row ? row.name : "", max: 40 },
        { name: "trigger", label: t("topic.trigger"), value: row ? row.trigger : "", hint: t("topic.trigger_hint"), max: 160 },
        { name: "scope", label: t("topic.area"), kind: "select", options: areaOptions(choices, t("topic.area_all")), value: row ? row.scope_name || "" : "" },
        ...(row
          ? [
              { name: "action", label: t("topic.action"), value: row.action || "", hint: t("topic.action_hint") },
              { name: "source", label: t("topic.source"), value: row.source || "", hint: t("topic.source_hint") },
            ]
          : []),
      ],
      submit: row ? t("common.save") : t("common.create"),
      done: t("topic.saved"),
      onSubmit: (values) => call("topic.save", { topic_id: row ? row.topic_id : null, ...values, scope: values.scope || null }).then(() => ""),
    });
  const remove = async (row) => {
    const yes = await confirmDialog({ title: t("topic.remove_confirm"), lines: [row.name, t("topic.remove_note")], confirm: t("common.remove"), danger: true });
    if (!yes) return;
    act(async () => {
      const removed = await call("topic.remove", { topic_id: row.topic_id });
      return removed.removed === "archived" ? t("topic.archived") : "";
    }, t("topic.removed"));
  };
  return [
    sectionHead(t("settings.topics"), button(t("topic.new"), () => edit(null))),
    table(
      [t("topic.name"), t("topic.trigger"), t("topic.rules"), t("topic.tokens")],
      topics,
      (row) => [
        [row.name + (row.scope_name ? `\n${row.scope_name}` : ""), "name"],
        [row.trigger, "muted"],
        [String(row.rules), "num"],
        [`${row.body_tokens} / ${capacity}`, "num"],
      ],
      (row) => [button(t("common.edit"), () => edit(row), { kind: "quiet small" }), button(t("common.remove"), () => remove(row), { kind: "quiet small danger" })]
    ),
  ];
}

async function routesSection() {
  const [{ routes }, choices] = await Promise.all([call("route.list"), loadChoices()]);
  const edit = (row) =>
    editInSection({
      title: row ? tilde(row.path_prefix) : t("route.new"),
      fields: [
        { name: "path", label: t("route.path"), value: row ? row.path_prefix : "" },
        { name: "scope", label: t("route.area"), kind: "select", options: areaOptions(choices, t("route.ignored")), value: row ? row.scope_name || "" : "" },
      ],
      submit: row ? t("common.save") : t("common.add"),
      done: t("route.saved"),
      onSubmit: (values) => call("route.save", { route_id: row ? row.route_id : null, path: values.path, scope: values.scope || null }).then(() => ""),
    });
  const remove = async (row) => {
    const yes = await confirmDialog({ title: t("route.remove_confirm"), lines: [tilde(row.path_prefix)], confirm: t("common.remove"), danger: true });
    if (yes) act(() => call("route.remove", { path: row.path_prefix }), t("route.removed"));
  };
  const sorted = [...routes].sort((a, b) => a.path_prefix.localeCompare(b.path_prefix));
  return [
    sectionHead(t("settings.routes"), button(t("route.new"), () => edit(null))),
    table(
      [t("route.path"), t("route.area")],
      sorted,
      (row) => [[tilde(row.path_prefix), "mono"], row.scope_name ? [row.scope_name, "name"] : [t("route.ignored"), "muted"]],
      (row) => [button(t("common.edit"), () => edit(row), { kind: "quiet small" }), button(t("common.remove"), () => remove(row), { kind: "quiet small danger" })]
    ),
  ];
}

async function intervalSection() {
  const { minutes, max } = await call("interval.get");
  const input = h("input", { type: "number", value: String(minutes) });
  input.min = "0";
  input.max = String(max);
  const submit = (event) => {
    event.preventDefault();
    const wanted = Number(input.value);
    if (!Number.isInteger(wanted)) {
      toast(t("interval.not_whole"), "warn");
      return;
    }
    act(() => call("interval.set", { minutes: wanted }), t("interval.saved", { n: wanted }));
  };
  return [
    h("h1", { class: "title", text: t("settings.interval") }),
    h("p", { class: "sub", text: minutes === 0 ? t("interval.none") : t("interval.every", { n: minutes }) }),
    h(
      "form",
      { class: "row interval-row", on: { submit } },
      input,
      h("span", { class: "sub", text: t("interval.minutes") }),
      h("button", { type: "submit", class: "btn primary", text: t("common.save") })
    ),
  ];
}

async function schemaSection() {
  const { pending } = await call("schema.status");
  const nodes = [h("h1", { class: "title", text: t("settings.schema") })];
  if (!pending.length) return [...nodes, h("p", { class: "sub", text: t("schema.ok") })];
  return [
    ...nodes,
    h("div", { class: "callout warn" }, h("p", { text: t("schema.pending", { n: pending.length }) })),
    entries(pending, (name) => [null, name]),
    h(
      "div",
      { class: "actions" },
      button(
        t("schema.apply"),
        async () => {
          const yes = await confirmDialog({ title: t("schema.apply_confirm"), list: pending, confirm: t("schema.apply") });
          if (!yes) return;
          act(async () => {
            const { applied } = await call("schema.migrate");
            return applied.length ? "" : t("schema.ok");
          }, t("schema.applied"));
        },
        { kind: "primary" }
      )
    ),
  ];
}

function displaySection() {
  return [
    h("h1", { class: "title", text: t("settings.display") }),
    section(
      t("display.language"),
      null,
      h(
        "div",
        { class: "radio-list narrow" },
        LANGUAGES.map(([value, label]) =>
          h(
            "label",
            null,
            h("input", {
              type: "radio",
              name: "language",
              value,
              checked: value === language,
              on: {
                change: () => {
                  setLanguage(value);
                  location.reload();
                },
              },
            }),
            h("span", null, h("div", { class: "what", text: label }))
          )
        )
      )
    ),
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
  display: async () => displaySection(),
};

page("settings", {
  nav: "nav.settings",
  icon: "settings",
  root: h("div", { class: "page" }, settingsList.pane, settingsDetail.pane),
  list: settingsList.list,
  selected: () => settings.section,
  pick: pickSection,
  async load() {
    renderSections();
    if (settings.editing) return renderEditing();
    settingsDetail.mark(settings.section);
    settingsDetail.inner.replaceChildren(...(await SECTION_VIEWS[settings.section]()).filter(Boolean));
  },
});
