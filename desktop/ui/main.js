// Start-up, once every page has registered itself.
"use strict";

const refresh = $("refresh");
refresh.title = t("nav.refresh");
refresh.append(icon("refresh"));
refresh.addEventListener("click", () => {
  guardDirty(() => {
    invoke("check_now");
    reload().catch(fail);
  });
});

let lastInboxSignature = null;
function inboxSignature(status) {
  if (!status) return "";
  const closes = (status.close_proposals || []).map((row) => `${row.task_id}:${row.proposed_at || ""}`).join(",");
  return `${status.pending_ready}:${status.pending_deferred}:${status.memory_changes}:${closes}`;
}

listen("status", (event) => {
  const nextSig = inboxSignature(event.payload);
  const changed = lastInboxSignature !== null && nextSig !== lastInboxSignature;
  lastInboxSignature = nextSig;
  showStatus(event.payload);
  if (changed && app.current === "inbox" && !app.busy && !hasDirtyForm()) {
    reload().catch(fail);
  }
});

/** Ask for a newer release at start and once a day; the Settings page shows the details. */
async function checkUpdate() {
  try {
    showUpdate(await invoke("update_check"));
  } catch {
    // Without a checkout or a network there is nothing to offer.
  }
}

const updateNote = $("update-note");
updateNote.addEventListener("click", () => {
  settings.section = "version";
  show("settings").catch(fail);
});
invoke("checkout_get").then((place) => {
  if (!place.ready) {
    showSetup("checkout");
    return;
  }
  noteAfterSetup();
  setTimeout(checkUpdate, 3000);
  setInterval(checkUpdate, 24 * 60 * 60 * 1000);
  show("inbox").catch(fail);
});
