// Home: what needs a decision, and how much each part of the store holds.
"use strict";

/** A card that opens a tab. Each line is text, or [text, className]. */
function homeCard(title, lines, onOpen) {
  const card = element("button", undefined, "card");
  card.type = "button";
  card.append(
    element("strong", title),
    ...lines.map((line) => (Array.isArray(line) ? element("span", line[0], line[1]) : element("span", line)))
  );
  card.addEventListener("click", () => onOpen().catch((error) => say(errorText(error), "error")));
  return card;
}

function cards(...children) {
  const node = element("div", undefined, "cards");
  node.append(...children);
  return node;
}

tab("home", {
  async load() {
    const counts = await call("home");
    const open = counts.tasks_active + counts.tasks_dormant;
    $("home-cards").replaceChildren(
      block(
        "Attention",
        cards(
          homeCard("Review ready", [`${counts.review_ready} candidate(s)`], () => show("review")),
          homeCard(
            "Review including deferred",
            [`${counts.review_ready + counts.review_deferred} candidate(s)`],
            () => {
              $("include-deferred").checked = true;
              return show("review");
            }
          ),
          homeCard("Memory changes", [`${counts.memory_changes} proposal(s)`], () => show("changes")),
          homeCard(
            "Close proposals",
            [`${counts.task_proposals} proposal(s) · ${open} open`],
            () => openWork("active")
          ),
          homeCard("Dormant tasks", [`${counts.tasks_dormant} task(s)`], () => openWork("dormant"))
        )
      ),
      block(
        "The store",
        cards(
          homeCard(
            "Memories",
            [
              `${counts.active_memories} active · ${counts.retired_memories} retired`,
              `${counts.temporary_contexts} temporary`,
            ],
            () => show("memories")
          ),
          homeCard(
            "Work",
            [
              `${counts.tasks_active} active · ${counts.tasks_dormant} dormant · ` +
                `${counts.tasks_closed} closed`,
              `${counts.projects} project(s)`,
            ],
            () => openWork("active")
          ),
          homeCard(
            "Settings & health",
            [
              `${counts.scopes} scope(s) · ${counts.routes} route(s)`,
              counts.schema_pending
                ? [`${counts.schema_pending} migration(s) pending`, "warn"]
                : "schema current",
            ],
            () => show("settings")
          )
        )
      )
    );
  },
});
