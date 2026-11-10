"""self-hosted workflow tools, plugged in over their own apis.

n8n and dify are source-available, not Apache/MIT, so no code of theirs is copied
here: their *export formats* are imported in `clawcrew/importers.py`, and this
package talks to **your own instance** over the api that instance already publishes.
that distinction is the whole reason the two paths are separate files: an importer
reads a json file you own, a client calls a daemon you run.

both clients are thin on purpose. they translate one vendor's request/response shape
into plain dicts and raise `ProviderFailure`, so a workflow node never sees httpx.
"""

from __future__ import annotations

from ..config import Settings
from . import dify, n8n

__all__ = ["ServiceRegistry", "build_services", "dify", "n8n"]


class ServiceRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.n8n = n8n.make(settings.n8n_base_url, settings.n8n_api_key)
        self.dify = dify.make(settings.dify_base_url, settings.dify_api_key, settings.dify_user)

    def status(self) -> dict:
        return {"n8n": self.n8n.status(), "dify": self.dify.status()}


def build_services(settings: Settings | None = None) -> ServiceRegistry:
    return ServiceRegistry(settings or Settings.from_env())
