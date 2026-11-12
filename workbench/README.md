# workbench

where an agent composes its own steps out of `toolbox/` entries.

not built yet, and deliberately empty until the shape is settled. the question this root
answers is what an agent does *between* turns, when the step it needs is not a workflow node
the platform already has. the rule will be the same as everywhere else here: what a person
deploys is a toolbox entry, what an agent assembles from those for one job is a workbench
step, and neither is an ingredient, because the platform does not run it.
