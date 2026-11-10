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
<title>clawcrew · pov</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#121212; color:#e8e8e8; font:14px/1.5 "Poppins",system-ui,sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid #262626; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  h1 { font-size:15px; margin:0 12px 0 0; font-weight:600; letter-spacing:.02em; }
  input, select { background:#1b1b1b; color:#e8e8e8; border:1px solid #2e2e2e; border-radius:8px; padding:7px 9px; font:inherit; }
  button { background:#a8c8f0; color:#151515; border:0; border-radius:8px; padding:7px 12px; font:inherit; font-weight:600; cursor:pointer; }
  main { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; padding:18px; align-items:start; }
  section { background:#181818; border:1px solid #262626; border-radius:12px; padding:12px; min-width:0; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:#9a9a9a; margin:0 0 10px; }
  .agent { display:flex; justify-content:space-between; gap:8px; padding:6px 8px; border-radius:8px; cursor:pointer; }
  .agent:hover { background:#212121; }
  .agent[aria-current="true"] { background:#22303f; }
  .key { color:#7d8b99; font-family:ui-monospace,monospace; font-size:11px; }
  .col { margin-bottom:10px; }
  .col h3 { font-size:11px; margin:0 0 4px; color:#8f8f8f; font-weight:600; }
  .card { background:#1f1f1f; border-left:2px solid #a8c8f0; border-radius:6px; padding:6px 8px; margin-bottom:4px; }
  .empty { color:#6d6d6d; font-size:12px; }
  li { margin-bottom:6px; }
  ul { margin:0; padding-left:16px; }
  .muted { color:#8a8a8a; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>clawcrew · pov</h1>
  <input id="crew" placeholder="crew id" size="24" />
  <input id="token" placeholder="api token (if set)" size="18" type="password" />
  <button id="load">load crew</button>
  <span id="status" class="muted"></span>
</header>
<main>
  <section>
    <h2>agents</h2>
    <div id="roster" class="empty">load a crew to see its agents</div>
  </section>
  <section>
    <h2>board</h2>
    <div id="board" class="empty">no agent selected</div>
  </section>
  <section>
    <h2>calendar</h2>
    <div id="calendar" class="empty">no agent selected</div>
  </section>
  <section>
    <h2>rooms · inbox · attention</h2>
    <div id="context" class="empty">no agent selected</div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let crewId = localStorage.getItem("clawcrew.crew") || "";
$("crew").value = crewId;
$("token").value = localStorage.getItem("clawcrew.token") || "";

function headers() {
  const token = $("token").value.trim();
  return token ? { authorization: "Bearer " + token } : {};
}
async function get(path) {
  const res = await fetch(path, { headers: headers() });
  if (!res.ok) throw new Error(path + " -> " + res.status);
  return res.json();
}
const esc = (value) => String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clock = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "";

async function loadCrew() {
  crewId = $("crew").value.trim();
  localStorage.setItem("clawcrew.crew", crewId);
  localStorage.setItem("clawcrew.token", $("token").value.trim());
  if (!crewId) return;
  $("status").textContent = "loading…";
  try {
    const data = await get("/crews/" + crewId + "/roster");
    const rows = (data.agents || []).map((agent) => `
      <div class="agent" role="button" tabindex="0" data-agent="${esc(agent.agent_id)}" aria-current="false">
        <span>${esc(agent.name)}<br><span class="key">${esc(agent.agent_key || agent.department || "")}</span></span>
        <span class="key">${agent.open_tasks} open</span>
      </div>`).join("");
    $("roster").innerHTML = rows || '<div class="empty">no agents yet</div>';
    $("roster").querySelectorAll("[data-agent]").forEach((node) => {
      const open = () => loadPov(node.dataset.agent, node);
      node.addEventListener("click", open);
      node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") open(); });
    });
    $("status").textContent = (data.agents || []).length + " agents";
  } catch (error) {
    $("status").textContent = error.message;
  }
}

async function loadPov(agentId, node) {
  document.querySelectorAll("[data-agent]").forEach((el) => el.setAttribute("aria-current", "false"));
  if (node) node.setAttribute("aria-current", "true");
  try {
    const pov = await get("/agents/" + agentId + "/pov");
    $("board").innerHTML = pov.board.columns.filter((column) => column.tasks.length).map((column) => `
      <div class="col"><h3>${esc(column.name)}</h3>
        ${column.tasks.map((task) => `<div class="card">${esc(task.title)}</div>`).join("")}
      </div>`).join("") || '<div class="empty">nothing on this board</div>';
    $("calendar").innerHTML = (pov.calendar.entries || []).slice(0, 12).map((entry) =>
      `<div class="card">${esc(clock(entry.starts_at))} · ${esc(entry.kind)} · ${esc(entry.title)}</div>`
    ).join("") || '<div class="empty">calendar is clear</div>';
    const rooms = (pov.channels || []).map((channel) => `<li>#${esc(channel.slug)} <span class="muted">${esc(channel.my_role)}</span></li>`).join("");
    const inbox = (pov.inbox.approved || []).map((message) => `<li>${esc(message.body)}</li>`).join("");
    const attention = (pov.attention.needs_you || []).map((task) => `<li>${esc(task.title)} <span class="muted">(${esc(task.column)})</span></li>`).join("");
    $("context").innerHTML = `
      <h3>rooms</h3><ul>${rooms || '<li class="empty">none</li>'}</ul>
      <h3 style="margin-top:10px">inbox <span class="muted">${pov.inbox.pending} waiting on review</span></h3><ul>${inbox || '<li class="empty">empty</li>'}</ul>
      <h3 style="margin-top:10px">needs you</h3><ul>${attention || '<li class="empty">nothing</li>'}</ul>`;
    $("status").textContent = pov.agent.name + " · " + (pov.agent.agent_key || "");
  } catch (error) {
    $("status").textContent = error.message;
  }
}

$("load").addEventListener("click", loadCrew);
if (crewId) loadCrew();
</script>
</body>
</html>
"""


GATE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>clawcrew · approval</title>
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
  <p class="kicker">clawcrew · {kicker}</p>
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
