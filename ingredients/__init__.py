"""ingredients: the parts the platform runs itself.

an ingredient is baked in. the platform starts it, owns its state and hands pieces of it
to an agent by capability. it is named here for what it does, never for the project it
came from: `persistent` and `ephemeral` are the machines, `browser` is a page, `schedule`
is the queue, `durableobjects` is durable coordination, `workflows` is a flow, `manage` is
the board, `kanban` is the board a crew keeps for itself, and the credential broker is how
an agent borrows a key without holding one. `crittertown/thirdparty.md` is the only file that
names the upstream projects.

the boundary this package exists to hold: **an agent vm does not get any of this.** the
platform carries ingredients because the platform is what runs them. an agent gets one
computer and one transport, and reaches everything else through the platform.
"""

PLATFORM_ONLY = True
