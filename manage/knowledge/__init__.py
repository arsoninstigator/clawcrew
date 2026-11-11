"""workspace knowledge: adapters read it, supervisors hand out access."""
from manage.knowledge.service import ADAPTERS, ConnectorService, build_adapter

__all__ = ["ConnectorService", "ADAPTERS", "build_adapter"]
