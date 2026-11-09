// The review queue. Every change goes through `mashu desktop-backend`; nothing is
// decided here. Stored text is only ever set as textContent, never parsed as markup.
"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);

let candidates = [];
let selectedId = null;
let busy = false;

async function call(op, args = {}) {
  const reply = await invoke("backend", { op, args });
  if (!reply.ok) {
    throw new Error(reply.error || "refused");
  }
  return reply.result;
}

function say(text, kind = "") {
  const message = $("message");
  message.textContent = text;
  message.className = kind;
  message.hidden = !text;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function short(id) {
  return String(id).slice(0, 8);
}

function waitingDays(row) {
  const created = Date.parse(row.created_at);
  return Number.isNaN(created) ? 0 : Math.max(0, Math.floor((Date.now() - created) / 86400000));
}

function selected() {
  return candidates.find((row) => row.nomination_id === selectedId) || null;
}

function showStatus(status) {
  $("counts").textContent =
    `${status.pending_ready} waiting` +
    (status.pending_deferred ? ` · ${status.pending_deferred} deferred` : "");
  const list = $("proposal-list");
  list.replaceChildren(
    ...status.close_proposals.map((row) =>
      element("li", `${row.name} (${row.project}) — ${row.outcome}`)
    )
  );
  $("proposals").hidden = status.close_proposals.length === 0;
  if (status.schema_pending.length) {
    say(`The store is behind the code; run 'mashu admin migrate'.`, "warn");
  }
}

function renderQueue() {
  const queue = $("queue");
  queue.replaceChildren(
    ...candidates.map((row) => {
      const item = element("li");
      item.setAttribute("aria-selected", String(row.nomination_id === selectedId));
      if (row.deferred_at) item.classList.add("deferred");
      item.append(
        element("span", row.content, "line"),
        element(
          "span",
          `${short(row.nomination_id)} · ${row.kind} · ${row.scope_name || "-"} · ` +
            `${waitingDays(row)}d`,
          "sub"
        )
      );
      item.addEventListener("click", () => {
        selectedId = row.nomination_id;
        renderQueue();
        renderDetail();
      });
      return item;
    })
  );
  $("empty").hidden = candidates.length > 0;
}

function renderDetail() {
  const row = selected();
  $("detail").hidden = !row;
  if (!row) return;
  $("meta").textContent =
    `${row.kind} · [${row.scope_name || "-"}] · ${short(row.nomination_id)} · ` +
    `v${row.version} · waiting ${waitingDays(row)} day(s)`;
  $("deferred-note").hidden = !row.deferred_at;
  $("deferred-note").textContent = `Put off earlier: ${row.defer_reason || ""}`;
  $("content").textContent = row.content;
  $("edit-form").hidden = true;
  $("content").hidden = false;

  const conflicts = row.conflict_rows || [];
  $("conflicts").hidden = conflicts.length === 0;
  $("conflict-list").replaceChildren(
    ...conflicts.map((conflict) => {
      const item = element("li");
      item.append(
        element(
          "div",
          `${short(conflict.memory_id)} · ${conflict.retirement_kind || "legacy"} · ` +
            `${(conflict.retired_at || "").slice(0, 10)}`,
          "conflict"
        ),
        element("div", `retired because: ${conflict.retire_reason}`)
      );
      if (conflict.superseded_by) item.append(element("div", `successor: ${conflict.superseded_by}`));
      return item;
    })
  );
  $("conflicts-read").checked = false;

  $("evidence-list").replaceChildren(
    ...(row.evidence_rows || []).map((evidence) => {
      const item = element("li");
      item.append(
        element("div", `${evidence.kind} · ${(evidence.created_at || "").slice(0, 10)}`, "muted"),
        element("div", `what: ${evidence.what}`),
        element("div", `prevention: ${evidence.prevention}`)
      );
      return item;
    })
  );

  $("delivery").value = row.scope_id ? "scope" : "always";
  $("topic").hidden = true;
  $("reason").value = "";
  updateButtons();
}

function updateButtons() {
  const row = selected();
  const blocked = row && row.blocking_conflict_ids.length > 0 && !$("conflicts-read").checked;
  const needsTopic = $("delivery").value === "topic" && !$("topic").value;
  $("admit").disabled = busy || !row || blocked || needsTopic;
  for (const id of ["edit", "decline", "defer"]) $(id).disabled = busy || !row;
}

async function load(keepMessage = false) {
  const include = $("include-deferred").checked;
  const [queue, status] = await Promise.all([
    call("review.list", { include_deferred: include }),
    call("status"),
  ]);
  candidates = queue.candidates;
  if (!candidates.some((row) => row.nomination_id === selectedId)) {
    selectedId = candidates.length ? candidates[0].nomination_id : null;
  }
  if (!keepMessage) say("");
  showStatus(status);
  renderQueue();
  renderDetail();
}

async function act(work, done) {
  if (busy) return;
  busy = true;
  updateButtons();
  try {
    const note = await work();
    await load(true);
    say(note || done, note ? "warn" : "");
  } catch (error) {
    say(String(error.message || error), "error");
  } finally {
    busy = false;
    updateButtons();
  }
}

async function loadTopics() {
  const { topics } = await call("topic.list");
  const select = $("topic");
  select.replaceChildren(
    element("option", "choose a topic…"),
    ...topics.map((topic) => {
      const option = element("option", `${topic.name} — ${topic.trigger}`);
      option.value = topic.topic_id;
      return option;
    })
  );
  select.firstChild.value = "";
}

$("delivery").addEventListener("change", async () => {
  const wantsTopic = $("delivery").value === "topic";
  $("topic").hidden = !wantsTopic;
  if (wantsTopic) {
    try {
      await loadTopics();
    } catch (error) {
      say(String(error.message || error), "error");
    }
  }
  updateButtons();
});
$("topic").addEventListener("change", updateButtons);
$("conflicts-read").addEventListener("change", updateButtons);
$("include-deferred").addEventListener("change", () => load().catch((e) => say(String(e), "error")));
$("refresh").addEventListener("click", () => {
  invoke("check_now");
  load().catch((e) => say(String(e), "error"));
});

$("admit").addEventListener("click", () => {
  const row = selected();
  const delivery = $("delivery").value;
  act(async () => {
    const result = await call("review.admit", {
      nomination_id: row.nomination_id,
      expected_version: row.version,
      delivery,
      topic_id: delivery === "topic" ? $("topic").value : undefined,
      conflict_ids: row.blocking_conflict_ids,
    });
    if (result.changed === "candidate") {
      return "The candidate changed since it was shown. Read the current wording and admit again.";
    }
    if (result.changed === "conflicts") {
      return "Its retirement conflicts changed. Read the updated candidate and admit again.";
    }
    return "";
  }, `Admitted ${short(row.nomination_id)}.`);
});

$("edit").addEventListener("click", () => {
  const row = selected();
  $("edit-text").value = row.content;
  $("content").hidden = true;
  $("edit-form").hidden = false;
  $("edit-text").focus();
});
$("edit-cancel").addEventListener("click", renderDetail);
$("edit-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const row = selected();
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
    const row = selected();
    const reason = $("reason").value.trim();
    if (!reason) {
      say("Say why first, in the reason box.", "warn");
      $("reason").focus();
      return;
    }
    act(async () => {
      await call(op, { nomination_id: row.nomination_id, reason });
    }, `${word} ${short(row.nomination_id)}.`);
  });
}

listen("status", (event) => showStatus(event.payload));
load().catch((error) => say(String(error.message || error), "error"));
