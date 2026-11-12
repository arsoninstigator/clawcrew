# the platform image: everything, because the platform is what runs ingredients.
# named, because the agent vm is the last stage in this file: a bare `docker build .`
# would otherwise produce the agent vm and the api container would start with no api.
from python:3.11-slim as platform

WORKDIR /app
ENV PYTHONUNBUFFERED=1 CRITTERTOWN_DB=/data/crittertown.db

COPY pyproject.toml README.md ./
# one line per package, because `COPY a b ./` copies their *contents* into the target and
# the image then builds and starts with no `api` module
COPY core ./core
COPY api ./api
COPY agents ./agents
COPY work ./work
COPY comms ./comms
COPY org ./org
COPY manage ./manage
COPY create ./create
COPY sandbox ./sandbox
COPY ingredients ./ingredients
COPY toolbox ./toolbox
COPY vendor ./vendor
# the cluster extra, because the worker and beat containers run the broker's binary
RUN pip install --no-cache-dir -e ".[schedule]"

# the persistent runtime is not installed here on purpose: it boots microVMs, so it wants
# the host's /dev/kvm and belongs on the host rather than in this image. `docker-compose.yml`
# says the same thing in a comment a deployer will actually read.

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
