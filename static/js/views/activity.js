import { h } from "../dom.js";
import { icon } from "../icons.js";
import { api } from "../api.js";
import { toast } from "../components/toast.js";

// "My activity" (#/activity): what happened to the signed-in person's account, keys and
// buckets, newest first, grouped by day. GET /api/activity -> {entries: [{id, ts, area,
// action, target, actor, summary}], nextCursor, partial}. The server never sends raw audit
// details or other people's names; everything here is set as text (h()/textContent).

const AREA_ICON = {
  bucket: "bucket", key: "key", share: "users", account: "shield", profile: "user",
  cloud: "cloud", login: "lock", other: "activity",
};
const AREA_LABEL = {
  bucket: "Bucket", key: "Keys", share: "Sharing", account: "Account", profile: "Profile",
  cloud: "Cloud", login: "Sign-in", other: "Other",
};
const ACTOR = {
  you: "You", bucketeer: "Bucketeer", admin: "An administrator", owner: "The bucket's owner",
};
const PAGE = 50;

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "medium" });

const dayKey = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

function dayLabel(d) {
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (dayKey(d) === dayKey(today)) return "Today";
  if (dayKey(d) === dayKey(yesterday)) return "Yesterday";
  const opts = { weekday: "long", month: "long", day: "numeric" };
  if (d.getFullYear() !== today.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

function item(e, n) {
  const when = new Date(e.ts);
  const valid = !Number.isNaN(when.getTime());
  const area = AREA_ICON[e.area] ? e.area : "other";
  const li = h("li", { class: "act-item", "data-area": area, "data-actor": e.actor },
    h("span", { class: "act-icon", title: AREA_LABEL[area] }, icon(AREA_ICON[area])),
    h("div", { class: "act-body" },
      h("p", { class: "act-summary", text: e.summary }),
      h("p", { class: "act-meta" },
        e.target && h("span", { class: "act-target", text: e.target, title: e.target }),
        h("span", { class: "act-who", text: ACTOR[e.actor] ?? ACTOR.admin }),
        // top right on wide screens, at the end of this line on phones (css/activity.css)
        valid && h("time", { class: "act-time", datetime: e.ts, title: fullFmt.format(when), text: timeFmt.format(when) }))));
  li.style.setProperty("--i", String(Math.min(n, 12)));
  return li;
}

function skeleton() {
  const row = () => h("div", { class: "skel-row" }, h("span", { class: "skel-dot" }),
    h("div", { class: "skel-stack" }, h("div", { class: "skel-line w60" }), h("div", { class: "skel-line w40" })),
    h("span", { class: "skel-pill" }));
  return h("div", { class: "skel act-skel", "aria-busy": "true", "aria-label": "Loading your activity" }, row(), row(), row(), row());
}

function timeline() {
  const body = h("div", { class: "act-feed" });
  const note = h("p", { class: "act-note", role: "status", hidden: true },
    icon("alert"), h("span", { text: "Some entries about your account or buckets may be missing: the storage service could not be reached. Try again later." }));
  const more = h("button", { class: "btn act-more", type: "button" }, icon("down"), "Load more");
  const foot = h("div", { class: "act-foot", hidden: true }, more);
  const refresh = h("button", { class: "btn btn-ghost btn-icon", type: "button", "aria-label": "Refresh", title: "Refresh" }, icon("refresh"));
  const card = h("section", { class: "card act-card", "aria-labelledby": "act-h" },
    h("div", { class: "set-head act-head" },
      h("h2", { id: "act-h", text: "Recent activity" }),
      h("span", { class: "chip", text: "Newest first" }), refresh),
    note, body, foot);

  let cursor = null;
  let lastDay = null;
  let list = null;
  let shown = 0;
  let busy = false;

  const append = (entries) => {
    for (const e of entries) {
      const d = new Date(e.ts);
      const key = Number.isNaN(d.getTime()) ? "?" : dayKey(d);
      if (key !== lastDay || !list) {
        lastDay = key;
        const id = `act-day-${shown}`;
        list = h("ol", { class: "act-list", "aria-labelledby": id });
        body.append(h("section", { class: "act-group" },
          h("h3", { class: "act-day", id, text: key === "?" ? "Unknown date" : dayLabel(d) }), list));
      }
      list.append(item(e, shown % PAGE));
      shown += 1;
    }
  };

  const failed = (err) => h("div", { class: "set-unavail act-err", role: "alert" },
    h("p", { text: err.message || "Your activity could not be loaded." }),
    h("button", { class: "btn", type: "button", on: { click: () => load(true) } }, icon("refresh"), "Try again"));

  async function load(first) {
    if (busy) return;
    busy = true;
    if (first) {
      cursor = null; lastDay = null; list = null; shown = 0;
      body.replaceChildren(skeleton());
      foot.hidden = true;
    } else {
      // aria-busy rather than disabled: a disabled button would drop the keyboard focus
      more.setAttribute("aria-busy", "true");
      more.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Loading…");
    }
    try {
      const q = new URLSearchParams({ limit: String(PAGE) });
      if (!first && cursor != null) q.set("before_id", String(cursor));
      const res = await api(`/activity?${q}`);
      if (first) body.replaceChildren();
      note.hidden = !res.partial;
      append(res.entries);
      if (first && !res.entries.length) {
        body.replaceChildren(h("div", { class: "empty act-empty" },
          icon("activity", "empty-icon"),
          h("h3", { text: "Nothing here yet" }),
          h("p", { text: "When you create buckets, manage keys or share with colleagues, it shows up here. So do changes Bucketeer or an administrator makes to your account." }),
          h("a", { class: "btn empty-acts", href: "#/" }, icon("home"), "Go to my workspace")));
      }
      cursor = res.nextCursor;
      foot.hidden = cursor == null;
    } catch (err) {
      if (err.status === 401) return;
      if (first) body.replaceChildren(failed(err));
      else toast.error(err.message || "Could not load more activity.");
    } finally {
      busy = false;
      more.removeAttribute("aria-busy");
      more.replaceChildren(icon("down"), "Load more");
    }
  }

  more.addEventListener("click", () => load(false));
  refresh.addEventListener("click", () => load(true));
  load(true);
  return card;
}

export function renderActivity() {
  return h("main", { class: "main act", id: "main" },
    h("div", { class: "hero" },
      h("a", { class: "pf-back", href: "#/" }, icon("home"), "Back to my workspace"),
      h("span", { class: "eyebrow", text: "Your activity" }),
      h("h1", { text: "My activity" }),
      h("p", { text: "What happened to your account, keys and buckets: what you did, and what Bucketeer or an administrator did for you." })),
    timeline());
}
