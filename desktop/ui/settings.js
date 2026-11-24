// Settings: inspection/records and app/store configuration.
"use strict";

const SECTION_GROUPS = [
  ["inspect", ["health", "bootstrap", "ledger", "missed"]],
  ["manage", ["routes", "limits", "schema", "version", "general"]],
];

const settings = {
  section: "health",
  editing: null,
  bootstrapCwd: "",
  bootstrap: null,
  ledgerProject: "",
  ledgerKind: "",
  ledgerQuery: "",
  routeFilter: "all",
};

const settingsList = listPane();
settingsList.pane.classList.add("narrow");
const settingsDetail = detailPane();

function normalizeSection(id) {
  if (id === "startup" || id === "display") return "general";
  return SECTION_VIEWS[id] ? id : "health";
}

function pickSection(id) {
  guardDirty(() => {
    if (id === "topics") {
      openTopics().catch(fail);
      return;
    }
    settings.section = normalizeSection(id);
    settings.editing = null;
    reload().catch(fail);
  });
}

function renderSections() {
  fillList(
    settingsList.list,
    SECTION_GROUPS.map(([group, ids]) => ({
      title: t(`settings.group.${group}`),
      rows: ids.map((id) => ({ id, primary: t(`settings.${id}`), secondary: [t(`settings.${id}.about`)] })),
    })),
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
          const result = await spec.onSubmit(values);
          settings.editing = null;
          return result;
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
        [t("health.rules"), t("health.rules_value", { always: counts.always || 0, project: counts.project || 0, topic: counts.topic || 0 })],
        [t("health.queue"), t("health.queue_value", { ready: health.pending_ready, deferred: health.pending_deferred })],
        [t("health.tasks"), t("health.tasks_value", { a: health.tasks_active, d: health.tasks_dormant, c: health.tasks_closed })],
        [
          t("work.view.projects"),
          h("span", null, String(health.project_count), " ", button(t("missed.open"), () => openProjects().catch(fail), { kind: "quiet small" })),
        ],
        [
          t("memory.view.topics"),
          h("span", null, String(health.topic_count), " ", button(t("missed.open"), () => openTopics().catch(fail), { kind: "quiet small" })),
        ],
      ])
    ),
    section(
      t("health.recent"),
      null,
      fieldList([
        [t("health.ledger"), h("span", null, t("health.n", { n: health.ledger_30d }), " ", button(t("missed.list"), () => pickSection("ledger"), { kind: "quiet small" }))],
        [t("health.missed"), h("span", null, missedText(health), " ", button(t("missed.list"), () => pickSection("missed"), { kind: "quiet small" }))],
      ])
    ),
  ];
}

/** Where the knowledge was; `repeats` counts the mistakes that named the same place. */
function missedPlace(place) {
  const repeats = place.repeats > 1 ? t("missed.repeats", { n: place.repeats }) : null;
  if (place.kind === "source") return [t("missed.source"), [tilde(place.source), repeats].filter(Boolean).join(" · ")];
  const memory = place.kind === "memory";
  const parts = memory
    ? [excerpt(place.content, 80), named("health.route", place.route), place.status === "retired" ? t("memory.view.retired") : null, repeats]
    : [place.name, t(`work.view.${place.view}`), repeats];
  const open = memory ? () => openMemory(place.memory_id, place.status) : () => openTask(place.task_id, place.view);
  return [
    t(memory ? "missed.memory" : "missed.task"),
    h("span", null, parts.filter(Boolean).join(" · "), " ", button(t("missed.open"), () => open().catch(fail), { kind: "quiet small" })),
  ];
}

async function missedSection() {
  const { pains } = await call("settings.missed");
  const list = (rows) =>
    entries(rows, (row) => [
      [day(row.created_at), short(row.ledger_id), row.created_by && who(row.created_by)].filter(Boolean).join(" · "),
      row.what,
      ...row.places.map(missedPlace),
      [t("evidence.prevention"), row.prevention],
      row.filed_task
        ? [
            t("missed.filed"),
            h("span", null, row.filed_task_name, " ", button(t("missed.open"), () => openTask(row.filed_task, row.filed_task_view).catch(fail), { kind: "quiet small" })),
          ]
        : null,
    ]);
  const recent = pains.filter((row) => row.recent);
  const earlier = pains.filter((row) => !row.recent);
  return [
    h("h1", { class: "title", text: t("health.missed") }),
    section(t("health.recent"), recent.length, list(recent)),
    earlier.length ? section(t("missed.earlier"), earlier.length, list(earlier)) : null,
  ];
}

function renderLedgerEntries(rows) {
  return section(
    t("settings.ledger"),
    rows.length,
    entries(rows, (row) => {
      const fixed = RECORDING_KINDS.includes(row.kind) ? fixedAccount(row.what) : null;
      const body = fixed ? t(`evidence.text.${fixed}`) : row.what;
      const showPrevention = fixed ? null : [t("evidence.prevention"), row.prevention];
      return [
        [day(row.created_at), short(row.ledger_id), named("evidence.kind", row.kind), row.project_name, row.created_by && who(row.created_by)].filter(Boolean).join(" · "),
        body,
        showPrevention,
      ];
    })
  );
}

async function ledgerSection() {
  const [{ entries: rows, content_limit: contentLimit }, choices] = await Promise.all([
    call("ledger.list", {
      limit: 100,
      ...(settings.ledgerProject ? { project: settings.ledgerProject } : {}),
      ...(settings.ledgerKind ? { kind: settings.ledgerKind } : {}),
      ...(settings.ledgerQuery ? { query: settings.ledgerQuery } : {}),
    }),
    loadChoices(),
  ]);
  const listSlot = h("div", null, renderLedgerEntries(rows));
  const refreshList = async () => {
    const res = await call("ledger.list", {
      limit: 100,
      ...(settings.ledgerProject ? { project: settings.ledgerProject } : {}),
      ...(settings.ledgerKind ? { kind: settings.ledgerKind } : {}),
      ...(settings.ledgerQuery ? { query: settings.ledgerQuery } : {}),
    });
    listSlot.replaceChildren(renderLedgerEntries(res.entries));
  };
  let timer = null;

  const openReport = async () => {
    const [activeTasks, dormantTasks, activeMemories] = await Promise.all([
      call("work.tasks", { activity: "active" }),
      call("work.tasks", { activity: "dormant" }),
      call("memory.list", { view: "active" }),
    ]);
    const openTasks = [...activeTasks.tasks, ...dormantTasks.tasks];
    const taskOpts = [["", t("common.none")], ...openTasks.map((row) => [row.task.task_id, `${row.task.name} (${row.task.project_name})`])];
    const memOpts = [["", t("common.none")], ...activeMemories.rows.map((row) => [row.memory_id, `${short(row.memory_id)} ${excerpt(row.content, 60)}`])];
    editInSection({
      title: t("pain.new"),
      fields: [
        { name: "what", label: t("pain.what"), kind: "area", rows: 3 },
        { name: "prevention", label: t("pain.prevention"), kind: "area", rows: 2, max: contentLimit },
        { name: "project", label: t("pain.project"), kind: "select", options: projectOptions(choices, t("common.none")), value: settings.ledgerProject },
        {
          name: "prevention_kind",
          label: t("pain.prevention_kind"),
          kind: "radio",
          options: [
            ["rule", t("pain.kind.rule")],
            ["work", t("pain.kind.work")],
          ],
          value: "rule",
        },
        { name: "task_id", label: t("pain.task"), kind: "select", options: taskOpts, value: "", show: (values) => values.prevention_kind === "work" },
        {
          kind: "group",
          label: t("pain.existing_knowledge"),
          fields: [
            { name: "violated_memory_id", label: t("pain.violated"), kind: "select", options: memOpts, value: "" },
            { name: "ignored_task_id", label: t("pain.ignored_task"), kind: "select", options: taskOpts, value: "" },
            { name: "ignored_source", label: t("pain.ignored_source"), value: "", hint: t("pain.ignored_source_hint") },
          ],
        },
      ],
      submit: t("common.add"),
      done: t("pain.reported"),
      onSubmit: async (values) => {
        const result = await call("pain.report", {
          what: values.what,
          prevention: values.prevention,
          project: values.project || null,
          prevention_kind: values.prevention_kind,
          task_id: values.prevention_kind === "work" ? values.task_id || null : null,
          violated_memory_id: values.violated_memory_id || null,
          ignored_task_id: values.ignored_task_id || null,
          ignored_source: values.ignored_source || null,
        });
        if (result.delivery_suspect) return t("pain.delivery_suspect");
        if (result.ignored_task_id || result.ignored_source) return t("pain.knowledge_unused");
        if (result.prevention_kind === "work" && result.filed_task) return { message: t("pain.work_filed"), kind: "ok" };
        if (result.nomination_existing) return { message: t("pain.candidate_joined"), kind: "ok" };
        if (result.nomination) return { message: t("pain.candidate_created"), kind: "ok" };
        return "";
      },
    });
  };

  const kindOptions = [
    ["", `${t("ledger.kind")}: ${t("common.all")}`],
    ...["incident", "friction", "explicit", "claimed", "proposed"].map((k) => [k, named("evidence.kind", k)]),
  ];
  const kindFilter = h(
    "select",
    {
      "aria-label": t("ledger.kind"),
      on: {
        change: (event) => {
          settings.ledgerKind = event.target.value;
          refreshList().catch(fail);
        },
      },
    },
    kindOptions.map(([value, label]) => h("option", { value, text: label }))
  );
  kindFilter.value = settings.ledgerKind;

  const projectFilter = h(
    "select",
    {
      "aria-label": t("filter.project"),
      on: {
        change: (event) => {
          settings.ledgerProject = event.target.value;
          refreshList().catch(fail);
        },
      },
    },
    [["", `${t("filter.project")}: ${t("common.all")}`], ...choices.projects.map((name) => [name, name])].map(([value, label]) =>
      h("option", { value, text: label })
    )
  );
  projectFilter.value = settings.ledgerProject;

  const search = searchBox((query) => {
    settings.ledgerQuery = query;
    clearTimeout(timer);
    timer = setTimeout(() => refreshList().catch(fail), 200);
  });
  search.querySelector("input").value = settings.ledgerQuery;

  return [
    sectionHead(t("settings.ledger"), button(t("pain.new"), () => openReport().catch(fail), { kind: "primary" })),
    h("div", { class: "filter-row ledger-filters" }, search, kindFilter, choices.projects.length ? projectFilter : null),
    listSlot,
  ];
}

async function bootstrapSection() {
  const { routes } = await call("route.list");
  const input = h("input", { type: "text", placeholder: t("bootstrap.placeholder"), value: settings.bootstrapCwd });
  const result = h("div");
  const runPreview = async (cwd) => {
    settings.bootstrapCwd = cwd.trim();
    if (!settings.bootstrapCwd) return;
    try {
      settings.bootstrap = await call("settings.bootstrap", { cwd: settings.bootstrapCwd });
      result.replaceChildren(...bootstrapView(settings.bootstrap));
    } catch (error) {
      fail(error);
    }
  };
  const run = async (event) => {
    event.preventDefault();
    await runPreview(input.value);
  };
  const routeSelect = routes.length
    ? h(
        "select",
        {
          class: "bootstrap-routes",
          "aria-label": t("bootstrap.registered_folders"),
          on: {
            change: (event) => {
              if (!event.target.value) return;
              input.value = event.target.value;
              runPreview(event.target.value);
            },
          },
        },
        [
          ["", t("bootstrap.pick_folder")],
          ...routes.map((row) => [row.path_prefix, `${tilde(row.path_prefix)} (${row.project_name || t("work.folder_ignored")})`]),
        ].map(([value, label]) => h("option", { value, text: label }))
      )
    : null;
  if (routeSelect && settings.bootstrapCwd) {
    const match = routes.find((r) => tilde(r.path_prefix) === tilde(settings.bootstrapCwd) || r.path_prefix === settings.bootstrapCwd);
    if (match) routeSelect.value = match.path_prefix;
  }
  if (settings.bootstrap) result.append(...bootstrapView(settings.bootstrap));
  return [
    h("h1", { class: "title", text: t("settings.bootstrap") }),
    routeSelect ? h("div", { class: "field" }, routeSelect) : null,
    h("form", { class: "row preview-row", on: { submit: run } }, folderInput(input), h("button", { type: "submit", class: "btn primary", text: t("bootstrap.run") })),
    result,
  ];
}

function bootstrapView(answer) {
  const entryWithOpen = (label, onOpen, body) => [
    h("div", { class: "entry-head" }, h("span", { class: "when", text: label || "" }), button(t("missed.open"), () => onOpen().catch(fail), { kind: "quiet small" })),
    body,
  ];
  return [
    h("p", { class: "sub", text: `${t("bootstrap.area")}: ${answer.project || t("bootstrap.unrouted")}` }),
    h(
      "div",
      { class: "meters preview-total" },
      meterRow(`${t("bootstrap.total")}${answer.over_budget ? `（${t("bootstrap.over")}）` : ""}`, answer.tokens, answer.capacity)
    ),
    section(t("bootstrap.always"), answer.always.length, entries(answer.always, (row) => entryWithOpen(null, () => openMemory(row.memory_id, "active"), row.content))),
    section(t("bootstrap.project_rules"), answer.project_rules.length, entries(answer.project_rules, (row) => entryWithOpen(null, () => openMemory(row.memory_id, "active"), row.content))),
    section(t("bootstrap.topics"), answer.topics.length, entries(answer.topics, (row) => entryWithOpen(null, () => openTopicMemories(row.topic), row.line))),
    section(t("bootstrap.cards"), answer.states.length, entries(answer.states, (row) => entryWithOpen(row.heading, () => openTask(row.task_id, "active"), row.content))),
    section(t("bootstrap.temporary"), answer.temporary.length, entries(answer.temporary, (row) => entryWithOpen(t("temporary.until", { when: day(row.expires_at) }), () => openTemporary(row.context_id), row.content))),
  ];
}

/** Folders mapped to projects or recorded as outside every project; both can be changed here. */
async function routesSection() {
  const [{ routes }, choices] = await Promise.all([call("route.list"), loadChoices()]);
  const edit = (row) =>
    editInSection({
      title: row ? tilde(row.path_prefix) : t("route.new"),
      fields: [
        { name: "path", label: t("route.path"), kind: "folder", value: row ? row.path_prefix : "" },
        {
          name: "project",
          label: t("route.project"),
          kind: "select",
          options: [["", t("route.ignored")], ...choices.projects.map((name) => [name, name])],
          value: row ? row.project_name || "" : "",
        },
      ],
      submit: row ? t("common.save") : t("common.add"),
      done: t("route.saved"),
      onSubmit: (values) =>
        call("route.save", { route_id: row ? row.route_id : null, path: values.path, project: values.project || null }).then(() => ""),
    });
  const remove = async (row) => {
    const yes = await confirmDialog({ title: t("route.remove_confirm"), lines: [tilde(row.path_prefix)], confirm: t("common.remove"), danger: true });
    if (yes) act(() => call("route.remove", { path: row.path_prefix }), t("route.removed"));
  };
  const filterSeg = segmented(
    [
      ["all", t("route.filter_all")],
      ["ignored", t("route.filter_ignored")],
    ],
    settings.routeFilter,
    (val) => {
      settings.routeFilter = val;
      reload().catch(fail);
    }
  );
  const filtered = settings.routeFilter === "ignored" ? routes.filter((r) => !r.project_name) : routes;
  const sorted = [...filtered].sort((a, b) => a.path_prefix.localeCompare(b.path_prefix));
  return [
    sectionHead(t("settings.routes"), filterSeg, button(t("route.new"), () => edit(null))),
    h("p", { class: "sub", text: t("route.precedence_note") }),
    sorted.length
      ? table(
          [t("route.path"), t("route.project")],
          sorted,
          (row) => [
            [tilde(row.path_prefix), "mono"],
            [row.project_name || t("work.folder_ignored"), row.project_name ? "" : "muted"],
          ],
          (row) => [button(t("common.edit"), () => edit(row), { kind: "quiet small" }), button(t("common.remove"), () => remove(row), { kind: "quiet small danger" })]
        )
      : h("p", { class: "empty-note", text: t("memory.empty") }),
  ];
}

/** The limits in the order a person reads them: what one session opens with, then each share. */
const LIMIT_GROUPS = [
  ["opening", ["total_capacity"]],
  ["memory", ["memory_capacity", "always_capacity", "topic_capacity", "content_limit"]],
  ["work", ["project_capacity", "task_card_capacity", "card_overflow_capacity", "task_detail_capacity", "ruling_count", "ruling_chars", "ruling_words_chars"]],
  ["temporary", ["temporary_capacity"]],
];

async function limitsSection() {
  const { limits } = await call("limits.get");
  const byName = Object.fromEntries(limits.map((row) => [row.name, row]));
  const inputs = {};
  const initialValues = {};
  const formNode = h("form", { class: "limits" });
  const syncDirty = () => {
    const dirty = Object.entries(inputs).some(([name, input]) => input.value !== initialValues[name]);
    formNode.dataset.dirty = dirty ? "1" : "";
  };
  const row = (limit) => {
    const input = h("input", { type: "number", placeholder: String(limit.default) });
    input.min = String(limit.low);
    input.max = String(limit.high);
    if (limit.set) input.value = String(limit.value);
    inputs[limit.name] = input;
    initialValues[limit.name] = input.value;
    input.addEventListener("input", syncDirty);
    const over = limit.used !== null && limit.used !== undefined && limit.used > limit.value;
    return h(
      "div",
      { class: "limit-row" },
      h("div", { class: "limit-name" }, h("div", { text: t(`limit.${limit.name}`) }), t(`limit.${limit.name}.about`) ? h("div", { class: "hint", text: t(`limit.${limit.name}.about`) }) : null),
      h("div", { class: "limit-input" }, input, h("span", { class: "sub", text: t(`limit.unit.${limit.unit}`) })),
      h(
        "div",
        { class: "limit-note" },
        limit.used !== null && limit.used !== undefined
          ? h("div", { class: over ? "over" : "", text: t(limit.unit === "chars" ? "limit.longest" : "limit.used", { n: limit.used }) })
          : null,
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
  const hasCustom = limits.some((l) => l.set);
  const resetAll = async () => {
    const yes = await confirmDialog({
      title: t("limit.reset_confirm"),
      confirm: t("limit.reset_all"),
      danger: true,
    });
    if (!yes) return;
    const changes = Object.fromEntries(limits.filter((l) => l.set).map((l) => [l.name, null]));
    act(() => call("limits.set", { changes }), t("limit.reset_done"));
  };
  formNode.addEventListener("submit", submit);
  append(formNode, [
    LIMIT_GROUPS.map(([group, names]) => section(t(`limit.group.${group}`), null, h("div", { class: "limit-list" }, names.filter((name) => byName[name]).map((name) => row(byName[name]))))),
    h(
      "div",
      { class: "form-actions" },
      h("button", { type: "submit", class: "btn primary", text: t("common.save") }),
      hasCustom ? button(t("limit.reset_all"), resetAll, { kind: "quiet" }) : null
    ),
  ]);
  return [h("h1", { class: "title", text: t("settings.limits") }), h("p", { class: "sub", text: t("limit.lead") }), formNode];
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
  const mashu = info.mashu || (/unknown op/.test(info.mashu_error || "") ? t("update.mashu_old") : info.mashu_error || t("update.unknown"));
  const progress = h("p", { class: "sub" });
  const check = button(t("update.check"), async () => {
    check.disabled = true;
    check.textContent = t("update.checking");
    await reload().catch(fail);
  });
  const update = async () => {
    const yes = await confirmDialog({
      title: t("update.confirm", { version: info.latest }),
      lines: [t("update.confirm_detail", { path: info.checkout })],
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
  const input = h("input", { type: "text", value: place.path || info.checkout || "", placeholder: "~/MyApp/mashu" });
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
        [t("update.mashu"), mashu],
        [t("update.latest"), latest],
      ]),
      h(
        "div",
        { class: "actions" },
        info.available && info.checkout ? button(t("update.run_to", { version: info.latest }), update, { kind: "primary" }) : null,
        check
      ),
      h("p", { class: "sub", text: t("update.checked", { time: clock(app.updateChecked || new Date()) }) }),
      info.checkout ? null : h("p", { class: "sub", text: t("update.no_checkout") }),
      progress,
      info.dev_build ? h("p", { class: "sub", text: t("update.dev_hint") }) : null
    ),
    section(
      t("update.place"),
      null,
      h(
        "form",
        { class: "row interval-row", on: { submit: savePlace } },
        folderInput(input),
        h("button", { type: "submit", class: "btn primary", text: t("common.save") })
      ),
      h("p", { class: "sub", text: place.from_env ? t("update.from_env") : place.from_build ? t("update.from_build") : t("update.place_hint") })
    ),
  ];
}

async function generalSection() {
  const enabled = await invoke("plugin:autostart|is_enabled");
  return [
    h("h1", { class: "title", text: t("settings.general") }),
    section(
      t("settings.startup"),
      null,
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
      )
    ),
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
  missed: missedSection,
  ledger: ledgerSection,
  bootstrap: bootstrapSection,
  routes: routesSection,
  limits: limitsSection,
  version: versionSection,
  general: generalSection,
  schema: schemaSection,
};

page("settings", {
  nav: "nav.settings",
  icon: "settings",
  root: h("div", { class: "page" }, settingsList.pane, settingsDetail.pane),
  list: settingsList.list,
  selected: () => settings.section,
  pick: pickSection,
  async load() {
    settings.section = normalizeSection(settings.section);
    renderSections();
    if (settings.editing) return renderEditing();
    settingsDetail.mark(settings.section);
    settingsDetail.inner.replaceChildren(...(await SECTION_VIEWS[settings.section]()).filter(Boolean));
  },
});
