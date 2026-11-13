"""the one page that ships with the backend.

"pov switching only works with the api" was a fair complaint, so here is the smallest
honest answer: a served page, no build step, no framework, that lists the crew's
agents and renders one agent's point of view from `/agents/{id}/pov`. it is a
switcher, not a product: the real canvas comes later.

it is inlined as a string rather than a template file so `pip install .` cannot ship a
package that 404s its own page.
"""

POV_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>crittertown · pov</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#121212; color:#e8e8e8; display:grid;
         grid-template-columns:216px 1fr; min-height:100vh;
         font:14px/1.5 ui-sans-serif,system-ui,sans-serif; }
  aside { border-right:1px solid #242424; padding:14px 12px; background:#141414;
          display:flex; flex-direction:column; gap:6px; min-height:100vh; }
  .brand { font-weight:650; letter-spacing:.02em; margin-bottom:8px; }
  .label { font-size:10px; text-transform:uppercase; letter-spacing:.09em; color:#7f7f7f;
           margin:12px 0 4px; }
  .pick { display:block; width:100%; text-align:left; background:transparent; color:#d6d6d6;
          border:1px solid transparent; border-radius:7px; padding:5px 7px; font:inherit;
          cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .pick:hover { background:#1d1d1d; }
  .pick[aria-current="true"] { background:#1f2a36; border-color:#2d4054; color:#eaf2fb; }
  main { display:flex; flex-direction:column; min-width:0; }
  header { display:flex; gap:14px; align-items:center; flex-wrap:wrap;
           padding:12px 18px; border-bottom:1px solid #242424; position:sticky; top:0;
           background:#121212; z-index:2; }
  select, input, textarea { background:#1a1a1a; color:#e8e8e8; border:1px solid #2c2c2c;
           border-radius:7px; padding:6px 8px; font:inherit; }
  textarea { width:100%; min-height:64px; font-family:ui-monospace,monospace; font-size:12px; }
  button { background:#22303f; color:#dbe8f6; border:1px solid #34485e; border-radius:7px;
           padding:5px 10px; font:inherit; cursor:pointer; }
  button:hover { background:#2a3b4d; }
  button.warn { background:#3a2326; border-color:#5a3338; color:#f3d7d9; }
  nav { display:flex; gap:4px; flex-wrap:wrap; }
  nav a { color:#9fb0c0; text-decoration:none; padding:5px 9px; border-radius:7px;
          border:1px solid transparent; font-size:13px; }
  nav a[aria-current="page"] { background:#22303f; border-color:#34485e; color:#eaf2fb; }
  #view { padding:18px; display:grid; gap:14px; align-content:start; }
  .row { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px;
         align-items:start; }
  .panel { background:#171717; border:1px solid #262626; border-radius:11px; padding:12px;
           min-width:0; }
  .panel h2 { font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:#8b8b8b;
              margin:0 0 9px; font-weight:600; }
  .card { background:#1d1d1d; border-left:2px solid #7fa8d4; border-radius:6px; padding:7px 9px;
          margin-bottom:6px; font-size:13px; }
  .card .meta { color:#7f8b96; font-size:11px; }
  .kv { margin-bottom:10px; }
  .kv .name { color:#93a3b1; font-size:11px; font-family:ui-monospace,monospace; }
  .muted { color:#7d7d7d; font-size:12px; }
  code { font-family:ui-monospace,monospace; font-size:11px; color:#8fa6bd; }
  #status { padding:6px 18px 18px; }
  .bad { color:#e6a2a8; }
  .good { color:#9ccfa6; }
  footer { margin-top:auto; }
</style>
</head>
<body>
<aside>
  <div class="brand">crittertown</div>
  <div class="label">crews</div>
  <div id="crews" class="list"></div>
  <div class="label">channels</div>
  <div id="channels" class="list"></div>
  <div class="label">pages</div>
  <a class="pick" href="#/isolation">isolation</a>
</aside>
<main>
  <header>
    <span class="label" style="margin:0">viewing as</span>
    <select id="agentSelect"></select>
    <code id="agentKey"></code>
    <nav id="tabs"></nav>
  </header>
  <div id="view"></div>
  <div id="status" class="muted"></div>
</main>
<script>
// every route this page calls, as a template. the suite reads this table and checks
// each one against the openapi document, so the shell cannot drift from the api it renders.
var R = {
  crews: "crews",
  channels: "crews/{team}/channels",
  agents: "crews/{team}/agents",
  board: "agents/{agent}/board",
  channelBoard: "channels/{channel}/board",
  calendar: "agents/{agent}/calendar",
  inbox: "agents/{agent}/inbox",
  pov: "agents/{agent}/pov",
  profile: "agents/{agent}/profile",
  profileEntry: "agents/{agent}/profile/{namespace}/{key}",
  consistency: "agents/{agent}/consistency",
  computer: "agents/{agent}/computer",
  grants: "agents/{agent}/grants",
  grant: "agents/{agent}/grants/{kind}/{resource}",
  isolation: "crews/{team}/isolation",
  moveTask: "tasks/{task}/move",
  routines: "crews/{team}/routines",
  routineDone: "routines/{routine}/done",
  routinePause: "routines/{routine}/pause",
  materialise: "crews/{team}/routines/materialise",
  guardrails: "crews/{team}/guardrails",
  guardrail: "guardrails/{rail}"
};

var NAMESPACES = ["skills","scope","memory","instructions","knowledge","perms","tools","vertical","spawn"];
var VIEWS = ["board","calendar","inbox","runs","profile","computer","access","routines","guardrails"];

var state = { crews: [], team: null, channels: [], agents: [], agent: null, view: "board" };

function fill(path, params) {
  return "/" + path.replace(/\{(\w+)\}/g, function (_m, k) {
    return encodeURIComponent((params || {})[k] === undefined ? "" : params[k]);
  });
}

async function api(path, params, opts) {
  var res = await fetch(fill(path, params), opts);
  var text = await res.text();
  var data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!res.ok) {
    var err = new Error((data && (data.message || data.error)) || ("http " + res.status));
    err.data = data; err.status = res.status; throw err;
  }
  return data;
}

function el(tag, attrs, kids) {
  var node = document.createElement(tag);
  Object.keys(attrs || {}).forEach(function (k) {
    if (k === "text") { node.textContent = attrs[k]; }
    else if (k === "class") { node.className = attrs[k]; }
    else if (k.slice(0, 2) === "on") { node.addEventListener(k.slice(2), attrs[k]); }
    else { node.setAttribute(k, attrs[k]); }
  });
  (kids || []).forEach(function (kid) {
    node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  });
  return node;
}

function when(seconds) {
  if (!seconds) return "";
  return new Date(Number(seconds) * 1000).toLocaleString();
}

function say(text, bad) {
  var box = document.getElementById("status");
  box.textContent = text || "";
  box.className = bad ? "muted bad" : "muted";
}

// ---- rail -------------------------------------------------------------
function renderRail() {
  var crews = document.getElementById("crews");
  crews.textContent = "";
  state.crews.forEach(function (team) {
    crews.appendChild(el("button", {
      class: "pick", "aria-current": state.team && team.id === state.team.id ? "true" : "false",
      text: team.name, onclick: function () { pickTeam(team); }
    }));
  });
  var channels = document.getElementById("channels");
  channels.textContent = "";
  state.channels.forEach(function (channel) {
    channels.appendChild(el("button", {
      class: "pick", text: "# " + channel.name,
      onclick: function () { openChannel(channel); }
    }));
  });
  if (!state.team) channels.appendChild(el("div", { class: "muted", text: "pick a crew" }));
  else if (!state.channels.length) channels.appendChild(el("div", { class: "muted", text: "no channels yet" }));
}

function renderHeader() {
  var select = document.getElementById("agentSelect");
  select.textContent = "";
  if (!state.agents.length) {
    select.appendChild(el("option", { text: "no agents in this crew" }));
  }
  state.agents.forEach(function (agent) {
    select.appendChild(el("option", { value: agent.id, text: agent.name + " · " + (agent.role || "agent") }));
  });
  if (state.agent) select.value = state.agent.id;
  select.onchange = function () {
    var found = state.agents.filter(function (a) { return a.id === select.value; })[0];
    if (found) { state.agent = found; renderHeader(); renderView(); }
  };
  document.getElementById("agentKey").textContent = state.agent ? (state.agent.agent_key || "") : "";

  var tabs = document.getElementById("tabs");
  tabs.textContent = "";
  VIEWS.forEach(function (view) {
    tabs.appendChild(el("a", {
      href: "#", "aria-current": state.view === view ? "page" : "false", text: view,
      onclick: function (event) { event.preventDefault(); state.view = view; renderHeader(); renderView(); }
    }));
  });
}

// ---- views ------------------------------------------------------------
async function renderView() {
  var view = document.getElementById("view");
  view.textContent = "";
  if (!state.agent) { view.appendChild(el("div", { class: "muted", text: "no agent to render" })); return; }
  var params = { agent: state.agent.id, team: state.team ? state.team.id : "" };
  try {
    if (state.view === "board") await renderBoard(view, params);
    else if (state.view === "calendar") await renderCalendar(view, params);
    else if (state.view === "inbox") await renderInbox(view, params);
    else if (state.view === "runs") await renderRuns(view, params);
    else if (state.view === "profile") await renderProfile(view, params);
    else if (state.view === "computer") await renderComputer(view, params);
    else if (state.view === "access") await renderAccess(view, params);
    else if (state.view === "routines") await renderRoutines(view, params);
    else if (state.view === "guardrails") await renderGuardrails(view, params);
  } catch (err) {
    say(err.message, true);
    view.appendChild(el("div", { class: "panel" }, [
      el("h2", { text: "this view could not be read" }),
      el("div", { class: "muted", text: err.message })
    ]));
  }
}

async function renderBoard(view, params) {
  var board = await api(R.board, params);
  var row = el("div", { class: "row" });
  (board.columns || []).forEach(function (column) {
    var panel = el("div", { class: "panel" }, [el("h2", { text: column.name + " · " + column.tasks.length })]);
    column.tasks.forEach(function (task) {
      var move = el("select", {});
      (board.columns || []).forEach(function (other) {
        move.appendChild(el("option", { value: other.name, text: "move to " + other.name }));
      });
      move.value = column.name;
      move.onchange = async function () {
        try {
          await api(R.moveTask, { task: task.id }, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ column: move.value, actor: state.agent.name })
          });
          say("moved " + task.id + " to " + move.value);
          renderView();
        } catch (err) { say(err.message, true); }
      };
      panel.appendChild(el("div", { class: "card" }, [
        el("div", { text: task.title }),
        el("div", { class: "meta", text: task.id + " · " + (task.assignee_agent_id || "unassigned") }),
        move
      ]));
    });
    if (!column.tasks.length) panel.appendChild(el("div", { class: "muted", text: "empty" }));
    row.appendChild(panel);
  });
  view.appendChild(row);
  view.appendChild(el("div", { class: "muted",
    text: board.open + " open of " + board.total + " cards. moves go through the board endpoints, so they are audited like any other write." }));
}

async function renderCalendar(view, params) {
  var calendar = await api(R.calendar, params);
  var panel = el("div", { class: "panel" }, [el("h2", { text: "next " + calendar.days + " days" })]);
  (calendar.entries || []).forEach(function (entry) {
    panel.appendChild(el("div", { class: "card" }, [
      el("div", { text: entry.title }),
      el("div", { class: "meta", text: (entry.kind || "block") + " · " + when(entry.starts_at) })
    ]));
  });
  if (!calendar.entries || !calendar.entries.length) panel.appendChild(el("div", { class: "muted", text: "nothing scheduled" }));
  view.appendChild(panel);

  var title = el("input", { placeholder: "what is it", style: "min-width:200px" });
  var starts = el("input", { type: "datetime-local" });
  var add = el("div", { class: "panel" }, [
    el("h2", { text: "add a block" }),
    el("div", { style: "display:flex; gap:8px; flex-wrap:wrap" }, [title, starts, el("button", {
      text: "add", onclick: async function () {
        if (!title.value || !starts.value) { say("a block needs a title and a start", true); return; }
        try {
          await api(R.calendar, params, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ title: title.value, starts_at: starts.value })
          });
          say("added, and the event is on the agent's calendar like any other write");
          renderView();
        } catch (err) { say(err.message, true); }
      }
    })])
  ]);
  view.appendChild(add);
}

async function renderInbox(view, params) {
  var inbox = await api(R.inbox, params);
  var panel = el("div", { class: "panel" }, [el("h2", { text: "waiting for " + state.agent.name })]);
  (inbox.messages || []).forEach(function (message) {
    panel.appendChild(el("div", { class: "card" }, [
      el("div", { text: message.sentence || message.summary || message.subject || JSON.stringify(message) }),
      el("div", { class: "meta", text: message.kind || message.from || "" })
    ]));
  });
  if (!inbox.messages || !inbox.messages.length) panel.appendChild(el("div", { class: "muted", text: "nothing waiting" }));
  view.appendChild(panel);
}

async function renderRuns(view, params) {
  var pov = await api(R.pov, params);
  var panel = el("div", { class: "panel" }, [el("h2", { text: "recent runs" })]);
  (pov.runs || []).forEach(function (run) {
    panel.appendChild(el("div", { class: "card" }, [
      el("div", { text: (run.status || "") + " · " + (run.provider || "") }),
      el("div", { class: "meta", text: (run.id || "") + " " + when(run.created_date) })
    ]));
  });
  if (!pov.runs || !pov.runs.length) panel.appendChild(el("div", { class: "muted", text: "no runs yet" }));
  view.appendChild(panel);
  view.appendChild(el("div", { class: "muted",
    text: "as of " + when(pov.as_of) + ". pov is a projection over rows, not a login: nothing here changes what " + state.agent.name + " may do." }));
}

async function renderProfile(view, params) {
  var profile = await api(R.profile, params);
  var names = profile.namespaces || {};
  var head = el("div", { class: "panel" }, [
    el("h2", { text: "profile · revision " + (profile.revision || 0) }),
    el("div", { class: "muted" }, [
      el("span", { text: "fingerprint " }),
      el("code", { text: profile.fingerprint || "" })
    ])
  ]);
  view.appendChild(head);

  var row = el("div", { class: "row" });
  NAMESPACES.forEach(function (namespace) {
    var entries = names[namespace] || {};
    var panel = el("div", { class: "panel" }, [el("h2", { text: namespace })]);
    var keys = Object.keys(entries);
    if (!keys.length) panel.appendChild(el("div", { class: "muted", text: "empty" }));
    keys.forEach(function (key) {
      var box = el("textarea", {});
      box.value = JSON.stringify(entries[key], null, 2);
      var status = el("div", { class: "muted" });
      panel.appendChild(el("div", { class: "kv" }, [
        el("div", { class: "name", text: key }),
        box,
        el("div", { style: "display:flex; gap:6px; align-items:center; margin-top:4px" }, [
          el("button", { text: "replace", onclick: async function () {
            var value;
            try { value = JSON.parse(box.value); } catch (e) { value = box.value; }
            try {
              await api(R.profileEntry, { agent: params.agent, namespace: namespace, key: key }, {
                method: "PUT", headers: { "content-type": "application/json" },
                body: JSON.stringify({ value: value, origin: "owner" })
              });
              status.textContent = "stored exactly this value";
              var check = await api(R.consistency, { agent: params.agent });
              status.textContent = "stored · " + (check.ok ? "consistent" : check.problems.length + " problems");
              status.className = check.ok ? "muted good" : "muted bad";
              head.querySelector("code").textContent = check.fingerprint || "";
            } catch (err) { status.textContent = err.message; status.className = "muted bad"; }
          }}),
          status
        ])
      ]));
    });
    row.appendChild(panel);
  });
  view.appendChild(row);
  view.appendChild(el("div", { class: "muted",
    text: "a save replaces the value it shows. there is no merged preview, because a merged preview would teach you something false about what the agent gets. grants are on their own page." }));
}

async function renderComputer(view, params) {
  var computer = await api(R.computer, params);
  var panel = el("div", { class: "panel" }, [el("h2", { text: "computer" })]);
  if (computer.error) {
    panel.appendChild(el("div", { class: "card" }, [
      el("div", { text: computer.message || computer.error }),
      el("div", { class: "meta", text: JSON.stringify(computer.details || {}) })
    ]));
  } else {
    panel.appendChild(el("pre", { class: "muted", style: "white-space:pre-wrap; margin:0",
      text: JSON.stringify(computer, null, 2) }));
  }
  view.appendChild(panel);
}

async function renderRoutines(view, params) {
  var data = await api(R.routines, { team: params.team });
  var panel = el("div", { class: "panel" }, [el("h2", { text: "routines" })]);
  var today = data.today;
  (data.routines || []).forEach(function (routine) {
    var run = routine.run || {};
    var line = el("div", { class: "card" }, [
      el("div", { text: routine.name }),
      el("div", { class: "meta", text: routine.cadence +
        (routine.due_today ? " · due " + today : " · not due today") +
        " · " + (run.status || "no receipt") })
    ]);
    if (run.status && run.status !== "done") {
      line.appendChild(el("button", { text: "mark done", onclick: async function () {
        try {
          await api(R.routineDone, { routine: routine.id },
            { method: "POST", headers: { "content-type": "application/json" },
              body: JSON.stringify({ notes: "marked done in the shell" }) });
          say("receipt closed: " + routine.name);
          renderView();
        } catch (err) { say(err.message, true); }
      }}));
    }
    if (routine.status === "active") {
      line.appendChild(el("button", { class: "warn", text: "pause", onclick: async function () {
        try {
          await api(R.routinePause, { routine: routine.id },
            { method: "POST", headers: { "content-type": "application/json" },
              body: JSON.stringify({ status: "paused" }) });
          say("paused " + routine.name);
          renderView();
        } catch (err) { say(err.message, true); }
      }}));
    }
    panel.appendChild(line);
  });
  if (!data.routines || !data.routines.length) {
    panel.appendChild(el("div", { class: "muted", text: "no routines. a routine is what the crew owes every day, and the receipt is the point." }));
  }
  panel.appendChild(el("button", { text: "materialise today", onclick: async function () {
    try {
      var out = await api(R.materialise, { team: params.team }, { method: "POST" });
      say("created " + (out.created || []).length + " card(s)");
      renderView();
    } catch (err) { say(err.message, true); }
  }}));
  view.appendChild(panel);
  view.appendChild(el("div", { class: "muted",
    text: "a missed routine reads as missed, not as absent: sweep writes the miss, so the summary has a denominator." }));
}

async function renderGuardrails(view, params) {
  var data = await api(R.guardrails, { team: params.team });
  var panel = el("div", { class: "panel" }, [el("h2", { text: "guardrails" })]);
  var today = data.summary || {};
  var refused = today.refused || {};
  var names = Object.keys(refused);
  panel.appendChild(el("div", { class: "meta", text: names.length
    ? "refused today: " + names.map(function (k) { return k + " " + refused[k]; }).join(", ")
    : "nothing refused today" }));
  (data.limits || []).forEach(function (limit) {
    panel.appendChild(el("div", { class: "card" }, [
      el("div", { text: limit.kind + " = " + JSON.stringify(limit.value) }),
      el("div", { class: "meta", text: (limit.agent_id || "crew-wide") + " · " + limit.status }),
      el("button", { class: "warn", text: "pause", onclick: async function () {
        try {
          await api(R.guardrail, { rail: limit.id }, { method: "DELETE" });
          say("paused " + limit.kind);
          renderView();
        } catch (err) { say(err.message, true); }
      }})
    ]));
  });
  if (!data.limits || !data.limits.length) {
    panel.appendChild(el("div", { class: "muted", text: "no limits. an agent with no limits is a gate nobody is standing at." }));
  }
  view.appendChild(panel);
  view.appendChild(el("div", { class: "muted",
    text: "a limit answers without a person. it refuses before a card moves, and the refusal is counted apart from a failure." }));
}

async function renderAccess(view, params) {
  var data = await api(R.grants, params);
  var panel = el("div", { class: "panel" }, [el("h2", { text: "grants" })]);
  (data.grants || []).forEach(function (grant) {
    panel.appendChild(el("div", { class: "card" }, [
      el("div", { text: grant.kind + " · " + grant.resource }),
      el("div", { class: "meta", text: (grant.scopes || []).join(", ") || "no scopes" }),
      el("button", { class: "warn", text: "revoke", onclick: async function () {
        try {
          await api(R.grant, { agent: params.agent, kind: grant.kind, resource: grant.resource },
            { method: "DELETE" });
          say("revoked " + grant.kind + " " + grant.resource);
          renderView();
        } catch (err) { say(err.message, true); }
      }})
    ]));
  });
  if (!data.grants || !data.grants.length) panel.appendChild(el("div", { class: "muted", text: "no grants" }));
  view.appendChild(panel);
  view.appendChild(el("div", { class: "muted",
    text: "perms is not a profile key. what an agent may reach is a grant, and a grant is revocable without touching the profile." }));
}

// ---- boot -------------------------------------------------------------
async function pickTeam(team) {
  state.team = team;
  state.channels = [];
  state.agents = [];
  state.agent = null;
  renderRail();
  try {
    var channels = await api(R.channels, { team: team.id });
    state.channels = channels.channels || [];
    var agents = await api(R.agents, { team: team.id });
    state.agents = agents.agents || [];
    state.agent = state.agents[0] || null;
  } catch (err) { say(err.message, true); }
  renderRail();
  renderHeader();
  renderView();
}

async function openChannel(channel) {
  state.view = "board";
  renderHeader();
  try {
    var board = await api(R.channelBoard, { channel: channel.id });
    say("channel " + channel.name + ": " + (board.total || 0) + " cards");
  } catch (err) { say(err.message, true); }
}

async function boot() {
  renderHeader();
  try {
    var data = await api(R.crews, {});
    state.crews = data.crews || data || [];
  } catch (err) { say(err.message, true); return; }
  renderRail();
  if (state.crews.length) await pickTeam(state.crews[0]);
  else { say("no crews yet. POST /crews and this page has something to show."); renderHeader(); }
}

boot();
</script>
</body>
</html>
"""


ISOLATION_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>crittertown · isolation</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#121212; color:#e8e8e8; padding:22px;
         font:14px/1.55 ui-sans-serif,system-ui,sans-serif; max-width:1000px; }
  h1 { font-size:15px; margin:0 0 4px; font-weight:650; }
  h2 { font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:#8b8b8b;
       margin:20px 0 8px; font-weight:600; }
  select { background:#1a1a1a; color:#e8e8e8; border:1px solid #2c2c2c; border-radius:7px;
           padding:6px 8px; font:inherit; }
  .card { background:#171717; border:1px solid #262626; border-radius:11px; padding:11px;
          margin-bottom:8px; }
  .row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  ul { margin:0; padding-left:16px; }
  code { font-family:ui-monospace,monospace; font-size:11px; color:#8fa6bd; }
  .muted { color:#7d7d7d; font-size:12px; }
  .bad { color:#e6a2a8; }
  .good { color:#9ccfa6; }
</style>
</head>
<body>
<h1>isolation</h1>
<div class="muted">two agents that drifted into the same profile is the failure this layer
exists to prevent, so it gets a page instead of a line in a report.</div>
<div style="margin-top:12px"><select id="team"></select></div>
<div id="out"></div>
<script>
var R = { crews: "crews", isolation: "crews/{team}/isolation" };

function fill(path, params) {
  return "/" + path.replace(/\{(\w+)\}/g, function (_m, k) { return encodeURIComponent(params[k] || ""); });
}
async function api(path, params) {
  var res = await fetch(fill(path, params));
  var data = await res.json();
  if (!res.ok) throw new Error((data && (data.message || data.error)) || ("http " + res.status));
  return data;
}
function el(tag, text, cls) {
  var node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

async function show(teamId) {
  var out = document.getElementById("out");
  out.textContent = "";
  var report = await api(R.isolation, { team: teamId });
  var row = el("div");
  row.className = "row";

  var left = el("div", null, "card");
  left.appendChild(el("h2", null, "identical profiles"));
  var pairs = report.identical_profiles || [];
  if (!pairs.length) left.appendChild(el("div", "none · every agent has its own profile", "muted good"));
  pairs.forEach(function (pair) {
    var line = el("div", null, "card");
    line.appendChild(el("div", JSON.stringify(pair)));
    left.appendChild(line);
  });

  var right = el("div", null, "card");
  right.appendChild(el("h2", null, "shared rows"));
  var shared = report.shared_profiles || [];
  if (!shared.length) right.appendChild(el("div", "none", "muted"));
  shared.forEach(function (row2) { right.appendChild(el("div", JSON.stringify(row2), "muted")); });

  row.appendChild(left);
  row.appendChild(right);
  out.appendChild(row);

  var agents = el("div", null, "card");
  agents.appendChild(el("h2", null, "agents and the namespaces they hold"));
  (report.agents || []).forEach(function (agent) {
    var line = el("div");
    line.appendChild(el("code", " " + (agent.agent_key || "") + " "));
    line.appendChild(el("span", (agent.name || agent.id) + " · " + (agent.role || "")));
    line.appendChild(el("div", (agent.namespaces || []).join(", "), "muted"));
    agents.appendChild(line);
  });
  out.appendChild(agents);
}

async function boot() {
  var pick = document.getElementById("team");
  try {
    var data = await api(R.crews, {});
    var crews = data.crews || data || [];
    if (!crews.length) {
      document.getElementById("out").appendChild(el("div", "no crews yet", "muted"));
      return;
    }
    crews.forEach(function (team) {
      var option = document.createElement("option");
      option.value = team.id;
      option.textContent = team.name;
      pick.appendChild(option);
    });
    pick.onchange = function () { show(pick.value).catch(function (e) {
      document.getElementById("out").textContent = e.message;
    }); };
    await show(crews[0].id);
  } catch (err) {
    document.getElementById("out").textContent = err.message;
  }
}
boot();
</script>
</body>
</html>
"""



GATE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>crittertown · approval</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#121212;
         color:#e8e8e8; font:15px/1.55 "Poppins",system-ui,sans-serif; padding:24px; }}
  .card {{ width:100%; max-width:520px; background:#181818; border:1px solid #262626;
           border-radius:14px; padding:22px; }}
  .kicker {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#8f8f8f; margin:0 0 10px; }}
  h1 {{ font-size:19px; line-height:1.35; margin:0 0 10px; }}
  p {{ margin:0 0 14px; color:#bdbdbd; }}
  form {{ margin:0; }}
  button {{ width:100%; background:{colour}; color:#141414; border:0; border-radius:10px;
            padding:13px 16px; font:inherit; font-weight:600; cursor:pointer; }}
  .note {{ font-size:12px; color:#8a8a8a; margin-top:14px; }}
  .done {{ border-left:3px solid {colour}; padding-left:12px; color:#dcdcdc; }}
  code {{ background:#111; border:1px solid #262626; border-radius:6px; padding:1px 5px; font-size:12px; }}
</style>
</head>
<body>
<div class="card">
  <p class="kicker">crittertown · {kicker}</p>
  <h1>{title}</h1>
  {body}
</div>
</body>
</html>
"""


def gate_page(title: str, body_html: str, decision: str = "approve", kicker: str = "approval") -> str:
    """the whole no-login experience: a sentence, a button, a receipt."""
    colour = "#a8c8f0" if decision != "reject" else "#f0a8a8"
    return GATE_PAGE.format(kicker=kicker, title=title, body=body_html, colour=colour)
