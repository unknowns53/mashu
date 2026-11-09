// Start-up, once every tab has registered itself.
"use strict";

$("refresh").addEventListener("click", () => {
  invoke("check_now");
  reload().catch((error) => say(errorText(error), "error"));
});

listen("status", (event) => showStatus(event.payload));
show("home").catch((error) => say(errorText(error), "error"));
