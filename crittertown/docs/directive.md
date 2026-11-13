# intent, routines, gates, guardrails

four things decide whether a crew can be left alone overnight. three of them are about
stopping an agent and one is about telling it what the point is. this file is the
contract for all four, and it says which of them exist.

## intent

`org/intent.py`, tables `intents`. the sentence above the card.

a task says what to do. an intent says why, and what would make a finished task the
wrong task. an agent handed only a task optimises the task, so the crew writes its
direction down once and every prompt carries it. `render_prompt` puts the intent block
above the task line, on purpose: the card should not outrank the point.

three kinds, because they fail differently:

- `direction`: where the crew is going. single valued. a new active one supersedes the
  last, and the old row is kept with `status = superseded` so a run from last week can
  be judged against the intent that was live last week.
- `constraint`: what must not happen. not single valued. a crew's constraints accumulate
  and still apply to the agent inside it, because "no outbound email without approval"
  does not stop being true when a second constraint is added.
- `definition_of_done`: what finished means, so a run cannot declare victory on a number
  it invented.

an intent written by a person is `active`. one an agent suggests is `proposed`, steers
nothing, and waits at `POST /intent/{id}/approve`, which lets the operator rewrite the
sentence first. that is the normal case: the agent is right about the gap and wrong
about the wording.

routes: `GET|POST /crews/{id}/intent`, `POST /intent/{id}/approve|reject`.

## routines

`work/routines.py`, tables `routines` and `routine_runs`. recurring work with a receipt.

a schedule enqueues a job and forgets it, which is right for a tick and wrong for "check
the inbox every morning": a job that never ran leaves nothing behind. a routine is due on
a date and then either happened or did not. `materialise` creates the card and the
receipt together, once per (routine, date). `sweep` marks an unkept day `missed`, and it
does that once, not every night. `summary` reports kept against missed per routine, so a
routine that is eighty percent kept can be seen for what it is.

cadences: `daily`, `weekdays`, `weekly` (with a weekday), `monthly`. `hour` is the hour
it should be on the board, not a limit on when it may be done.

the nightly beat materialises every crew itself rather than through a trigger rule. the
receipt is the feature, and a row an operator can delete by accident is the wrong owner
for it.

routes: `GET|POST /crews/{id}/routines`, `POST /crews/{id}/routines/materialise`,
`POST /routines/{id}/done|pause`.

## gates

`manage/approvals`. built before either of the above and unchanged by them.

a gate is a human yes in the middle of an agent's work: a workflow parks, a card waits in
review, an approval link is signed and single use, and the run is resumed by the answer.
the invariant worth stating: a gate is the only place an agent's work becomes "approved",
and nothing else may write that word.

## guardrails

**not built.** this is the next piece, and it is the one missing for real autonomy.

the difference from a gate: a gate asks a person, a guardrail answers without one. it is
the standing limit that holds at 3am when nobody will answer. proposal, in the shape this
tree already uses:

- table `guardrails`: `team_id`, `agent_id`, `kind`, `value`, `status`, `actor`.
- kinds: `spend_per_day`, `open_cards`, `parallel_runs`, `egress_hosts`, `forbidden_tools`,
  `card_moves_per_day`. a number or a list, never prose, because a guardrail that needs
  interpreting is a gate wearing the wrong name.
- one method, `check(team_id, action, context)`, called by `dispatch`, `sandbox.ensure` and
  the egress check. it returns allowed or refused with the row that refused, so the run's
  error says which limit it hit instead of "denied".
- refused is not failed: the card goes to `blocked` with the guardrail id, and the morning
  brief counts refusals separately from errors, because they are a different problem.

## self improvement

**not built.** and it should not be built before guardrails, because an agent allowed to
rewrite its own instructions without a standing limit is the thing guardrails exist to
make safe.

the shape worth arguing about: an agent proposes, a person or a gate disposes, and the
proposal is a row rather than a silent write. that machinery is already here (`propose`
and `approve` on intents, `profile.set` with `origin`, the gate service), so the build is
small: a `lessons` table with `run_id`, `agent_id`, `statement`, `status`, and a promotion
path that takes an approved lesson and writes it into `profile.instructions` with
`origin = 'lesson:<id>'`. then the profile editor shows where an instruction came from,
which is the difference between an agent that learned and an agent that drifted.

the one rule to keep: a lesson can never edit a `constraint`. constraints are the owner's,
and an agent that can loosen its own constraint is not improving.
