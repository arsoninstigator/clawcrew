"""comms: rooms whose members are agents, over a signed event log.

`events` is the wire format (nip-01/nip-29 shaped), `identity` the keypair an
agent signs with, `service` the durable log and roster, `transports` the ways
events leave and come back, and `gateway` the seam to the app's conversation.
"""

from .events import Event, event_id, matches
from .gateway import CommsGateway
from .service import CommsService
from .transports import Transport, TransportError, build_transport, TRANSPORTS

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
