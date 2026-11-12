# the platform image: everything, because the platform is what runs ingredients.
from python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 CLAWCREW_DB=/data/clawcrew.db

COPY pyproject.toml README.md ./
COPY core api agents work comms org manage create sandbox ingredients toolbox ./
# the cluster extra, because the worker and beat containers run the broker's binary
RUN pip install --no-cache-dir -e ".[schedule]"

# the persistent runtime boots local microVMs, so it wants the host's /dev/kvm.
# off by default: build with --build-arg INSTALL_PERSISTENT=true and map the device.
ARG INSTALL_PERSISTENT=false
RUN if [ "$INSTALL_PERSISTENT" = "true" ]; then pip install --no-cache-dir persistent-runtime; fi

RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "api.routes:create_api", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# the agent vm: the agent's own harness, and nothing the platform owns. no ingredients,
# no toolbox, no wiring. an agent reaches the board, the browser or a flow through the
# platform, so adding an ingredient never adds weight here.
from python:3.11-slim as agent-vm

WORKDIR /agent
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir httpx pydantic
COPY pyproject.toml ./
COPY core/config.py core/errors.py ./core/
# a bare package marker, not the platform's `core/__init__.py`: that one builds the object
# graph, and the whole point of this stage is that the graph is not here.
RUN touch core/__init__.py
COPY sandbox/base.py sandbox/http.py sandbox/transport.py ./sandbox/
COPY agents/runtime.py ./agents/
CMD ["python", "-c", "print('agent vm: harness only, ask the platform for the rest')"]
