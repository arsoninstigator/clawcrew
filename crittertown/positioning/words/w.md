# the nine w's

the c words say what a crew does. these say what the town is for, and to whom, and what it
refuses. the form is borrowed from `kqrla/clawcrew`'s `directive/positioning.md`, which
wrote the nine dimensions down with a column for evidence and a column for how sure we are;
that habit is the useful part, so it is kept. a positioning document that reads as complete
on day one is a positioning document that was invented.

| dimension | this project | evidence | status |
| --- | --- | --- | --- |
| **who** | one to three people with work that will not fit in one context window: a solo operator, a small studio, a team that wants agents on shifts rather than one chat tab | the shape of every entry point (`/ask`, the board, the brief) assumes an operator who delegates rather than types | user-stated |
| **what** | a backend for crews of agents that take shifts, hold cards, sign their own work, and leave an audit trail a non-ai third party can read | `agents/`, `work/`, `org/`, `comms/`, the gate and brief rows, 649 tests | verified |
| **when** | now, because agent runtimes got cheap enough that the binding constraint moved from compute to oversight, and oversight is the part nobody ships | persistent and ephemeral providers exist; the brief exists to make a night legible | partial |
| **where** | python and one sqlite file, or postgres on supabase or insforge, on your own kvm host for computers, with the browser in your own container | `docs/backend.md`, `docker-compose.yml`, `crittertown/docs/schema.postgres.sql` | verified |
| **why** | a small team's month goes to coordination: who is on, what is blocked, what happened overnight. a town that coordinates itself returns that month | no external evidence yet; the claim has not been measured against somebody's actual week | gap |
| **why-not** | it cannot decide what the business is, cannot invent demand, cannot approve spending on your behalf, and will not hold your provider keys: a credential is brokered, scoped and revocable, never handed over | `ingredients/credentials/credential_broker`, `docs/backend.md` on delegation up the command chain | verified |
| **world** | other agent stacks orchestrate tools inside one loop. this one is judged by whether a crew survives a week of shifts unattended, and by whether the morning says what happened | the brief, the board's append-only log, run rows that keep a failure instead of swallowing it | partial |
| **way** | one crew, one board, one brief, one night first. the second crew is a config change and the second business is a new team row, not a rewrite | `POST /crews`, the overnight pack, the c words' `where it lives` column | plan |
| **wire** | the board, the morning brief and basecamp are the message layer: findings post where people already look, instead of a dashboard nobody opens | `channels`, `comms`, `work/brief.py`, the a2a digest rule | plan |

## the gaps that matter

**why** is the one with no evidence at all, and it is the one that decides the project. "coordination
eats the month" is a belief until it is measured against somebody's calendar. it stays a gap rather
than being softened into a partial.

**wire** is a plan. the brief is built and delivered to a channel, and basecamp is a name with a
comms layer under it, so the audit trail is legible in principle and unread in practice. saying so is
cheaper than pretending otherwise.

## what this is not

not a pitch. nothing here is written to convince a reader who has not already decided. the reader is
the crew itself at 3am, deciding whether a thing it wants to do is allowed, and the person who has to
read that decision in the morning.

the letters are not decoration either. `c.md` beside this file is the verb list; this is the frame
those verbs hang on.
