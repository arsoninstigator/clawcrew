"""comms: rooms whose members are agents, over a signed event log.

`events` is the wire format (nip-01/nip-29 shaped), `identity` the keypair an
agent signs with, `service` the durable log and roster, `transports` the ways
events leave and come back, and `gateway` the seam to the app's conversation.
"""

from comms.events import Event, event_id, matches
from comms.gateway import CommsGateway
from comms.service import CommsService
from comms.transports import Transport, TransportError, build_transport, TRANSPORTS

__all__ = [
    "Event",
    "event_id",
    "matches",
    "CommsService",
    "CommsGateway",
    "Transport",
    "TransportError",
    "build_transport",
    "TRANSPORTS",
]
