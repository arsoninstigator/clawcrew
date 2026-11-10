from python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 CLAWCREW_DB=/data/clawcrew.db

# everything the vendored provider clients need is declared in pyproject; no extra
# package is required for the api itself.
COPY pyproject.toml README.md ./
COPY clawcrew ./clawcrew
RUN pip install --no-cache-dir -e .

# the celesto OSS runtime boots local microVMs, so it needs the host's /dev/kvm.
# off by default: build with --build-arg INSTALL_CELESTO=true and map the device.
ARG INSTALL_CELESTO=false
RUN if [ "$INSTALL_CELESTO" = "true" ]; then pip install --no-cache-dir celesto; fi

RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "clawcrew.api:create_api", "--factory", "--host", "0.0.0.0", "--port", "8000"]
