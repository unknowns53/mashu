// Settings: store health, what a session receives, scopes, topics, routes, delivery limits,
// the checkpoint interval, the schema, versions and updates, starting at login, and the window's language.
"use strict";

const SECTIONS = ["health", "bootstrap", "scopes", "topics", "routes", "limits", "interval", "schema", "version", "startup", "display"];

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

/** The pains knowledge already held should have prevented, with where that knowledge was. */
function missedText(health) {
  const routes = health.failure_routes
    .filter((route) => health.delivery_failures_by_route[route])
    .map((route) => `${named("health.route", route)} ${health.delivery_failures_by_route[route]}`);
  const parts = [];
  if (health.delivery_failures_30d) parts.push(t("health.missed_memory", { n: health.delivery_failures_30d, routes: routes.join(", ") }));
  if (health.knowledge_unused_30d) parts.push(t("health.missed_elsewhere", { n: health.knowledge_unused_30d }));
  const total = t("health.n", { n: health.missed_30d });
  return parts.length ? `${total}\n${parts.join("\n")}` : total;
}

async function healthSection() {
  const health = await call("settings.health");
  const caps = health.capacities;
  const counts = health.memory_counts;
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
        [t("health.missed"), missedText(health)],
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

/** The limits in the order a person reads them: what one session opens with, then each share. */
const LIMIT_GROUPS = [
  ["opening", ["total_capacity"]],
  ["memory", ["memory_capacity", "always_capacity", "topic_capacity", "content_limit"]],
  ["work", ["project_capacity", "task_card_capacity", "card_overflow_capacity", "task_detail_capacity", "decision_capacity"]],
  ["temporary", ["temporary_capacity"]],
];

async function limitsSection() {
  const { limits } = await call("limits.get");
  const byName = Object.fromEntries(limits.map((row) => [row.name, row]));
  const inputs = {};
  const row = (limit) => {
    const input = h("input", { type: "number", placeholder: String(limit.default) });
    input.min = String(limit.low);
    input.max = String(limit.high);
    if (limit.set) input.value = String(limit.value);
    inputs[limit.name] = input;
    const over = limit.used !== null && limit.used > limit.value;
    return h(
      "div",
      { class: "limit-row" },
      h("div", { class: "limit-name" }, h("div", { text: t(`limit.${limit.name}`) }), t(`limit.${limit.name}.about`) ? h("div", { class: "hint", text: t(`limit.${limit.name}.about`) }) : null),
      h("div", { class: "limit-input" }, input, h("span", { class: "sub", text: t(`limit.unit.${limit.unit}`) })),
      h(
        "div",
        { class: "limit-note" },
        limit.used !== null ? h("div", { class: over ? "over" : "", text: t(limit.unit === "chars" ? "limit.longest" : "limit.used", { n: limit.used }) }) : null,
        h("div", { class: "sub", text: t("limit.default", { n: limit.default }) })
      )
    );
  };
  const submit = (event) => {
    event.preventDefault();
    const changes = {};
    for (const [name, input] of Object.entries(inputs)) {
      const limit = byName[name];
      const text = input.value.trim();
      const wanted = text === "" ? null : Number(text);
      if (wanted !== null && !Number.isInteger(wanted)) {
        toast(t("limit.not_whole"), "warn");
        return;
      }
      if (wanted === null ? limit.set : !limit.set || wanted !== limit.value) changes[name] = wanted;
    }
    if (!Object.keys(changes).length) return;
    act(() => call("limits.set", { changes }), t("limit.saved"));
  };
  return [
    h("h1", { class: "title", text: t("settings.limits") }),
    h("p", { class: "sub", text: t("limit.lead") }),
    h(
      "form",
      { class: "limits", on: { submit } },
      LIMIT_GROUPS.map(([group, names]) => section(t(`limit.group.${group}`), null, h("div", { class: "limit-list" }, names.map((name) => row(byName[name]))))),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn primary", text: t("common.save") }))
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

async function versionSection() {
  const [info, place] = await Promise.all([invoke("update_check"), invoke("checkout_get")]);
  showUpdate(info);
  const latest = info.latest ? (info.available ? info.latest : t("update.current", { version: info.latest })) : info.error || t("update.unknown");
  const progress = h("p", { class: "sub" });
  const update = async () => {
    const yes = await confirmDialog({
      title: t("update.confirm", { version: info.latest }),
      lines: [t("update.confirm_detail", { path: place.path })],
      confirm: t("update.run"),
    });
    if (!yes) return;
    const stop = await listen("update-step", (event) => {
      progress.textContent = t(`update.step.${event.payload}`);
    });
    act(async () => {
      try {
        const result = await invoke("update_install", { version: info.latest });
        if (result.app === "dev_build") return t("update.dev_done");
        if (result.app === "not_published") return t("update.app_pending");
        return "";
      } finally {
        stop();
      }
    }, t("update.done"));
  };
  const input = h("input", { type: "text", value: place.path || "", placeholder: "~/MyApp/mashu" });
  const savePlace = (event) => {
    event.preventDefault();
    act(() => invoke("checkout_set", { path: input.value }), t("update.place_saved"));
  };
  return [
    h("h1", { class: "title", text: t("settings.version") }),
    h(
      "div",
      { class: "version-rows" },
      fieldList([
        [t("update.app"), info.app],
        [t("update.mashu"), info.mashu || t("update.unknown")],
        [t("update.latest"), latest],
      ]),
      h(
        "div",
        { class: "actions" },
        info.available && place.path ? button(t("update.run_to", { version: info.latest }), update, { kind: "primary" }) : null,
        button(t("update.check"), () => reload().catch(fail))
      ),
      progress,
      info.dev_build ? h("p", { class: "sub", text: t("update.dev_hint") }) : null
    ),
    section(
      t("update.place"),
      null,
      h(
        "form",
        { class: "row interval-row", on: { submit: savePlace } },
        input,
        h("button", { type: "submit", class: "btn primary", text: t("common.save") })
      ),
      h("p", { class: "sub", text: place.from_env ? t("update.from_env") : place.from_build ? t("update.from_build") : t("update.place_hint") })
    ),
  ];
}

async function startupSection() {
  const enabled = await invoke("plugin:autostart|is_enabled");
  return [
    h("h1", { class: "title", text: t("settings.startup") }),
    h(
      "label",
      { class: "check" },
      h("input", {
        type: "checkbox",
        checked: enabled,
        on: {
          change: (event) => {
            const wanted = event.target.checked;
            act(() => invoke(wanted ? "plugin:autostart|enable" : "plugin:autostart|disable"), t(wanted ? "startup.enabled" : "startup.disabled"));
          },
        },
      }),
      t("startup.login")
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
  limits: limitsSection,
  version: versionSection,
  startup: startupSection,
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
