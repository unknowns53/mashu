// Start-up, once every page has registered itself.
"use strict";

const refresh = $("refresh");
refresh.title = t("nav.refresh");
refresh.append(icon("refresh"));
refresh.addEventListener("click", () => {
  invoke("check_now");
  reload().catch(fail);
});

listen("status", (event) => showStatus(event.payload));

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
setTimeout(checkUpdate, 3000);
setInterval(checkUpdate, 24 * 60 * 60 * 1000);
show("inbox").catch(fail);
