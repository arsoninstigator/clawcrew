"""the services the platform carries itself.

`ingredients/` is baked in: the platform runs it and hands it to an agent by capability.
`toolbox/` is called: someone else's instance, over its own api, never copied here.

so the board (project management) and flows (workflows) are ingredients, while the two
workflow engines are toolbox: you run them, the platform calls them, and their export
formats are imported by `agents/importers.py` rather than vendored.
"""

from __future__ import annotations

from core.config import Settings
from ingredients.workflows import client as workflows_client
from ingredients.projectmanagement.manage import client as manage_client
from toolbox import firecrawl
from toolbox.workflows import dify, n8n

__all__ = ["ServiceRegistry", "build_services", "dify", "firecrawl", "manage", "n8n", "workflows"]


class ServiceRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.n8n = n8n.make(settings.n8n_base_url, settings.n8n_api_key)
        self.dify = dify.make(settings.dify_base_url, settings.dify_api_key, settings.dify_user)
        self.firecrawl = firecrawl.make(
            getattr(settings, "firecrawl_api_key", ""),
            getattr(settings, "firecrawl_base_url", "https://api.firecrawl.dev/v1"),
        )
        self.workflows = workflows_client.make(getattr(settings, "workflows_base_url", ""),
                                        getattr(settings, "workflows_api_key", ""))
        self.manage = manage_client.make(getattr(settings, "manage_base_url", ""),
                                         getattr(settings, "manage_api_key", ""))

    def status(self) -> dict:
        return {
            "n8n": self.n8n.status(),
            "dify": self.dify.status(),
            "firecrawl": self.firecrawl.status(),
            "workflows": self.workflows.status(),
            "manage": self.manage.status(),
        }


def build_services(settings: Settings | None = None) -> ServiceRegistry:
    return ServiceRegistry(settings or Settings.from_env())
