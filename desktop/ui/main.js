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
show("inbox").catch(fail);
