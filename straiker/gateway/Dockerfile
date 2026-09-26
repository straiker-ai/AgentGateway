# Straiker x agentgateway — ONE container: the pinned agentgateway binary + the Straiker sidecar.
# Why one container: App Runner exposes a single HTTP/1.1 port and has no gRPC ingress, and two
# of the three Straiker seams (ExtProc, ExtMCP) are gRPC. So gRPC stays on loopback inside the
# container and only agentgateway's HTTP listener is exposed. Same shape as the Kong shared gateway.
#
# The upstream image is distroless (no shell) — we COPY the binary out of it into a python base.
# Forked agentgateway with the NATIVE Straiker guard (first-class "Straiker" in the UI dropdown).
# Built from straiker-ai/AgentGateway (docker build -t straiker-agentgateway-fork:local).
ARG AGW_VERSION=v1.4.1-straiker
ARG AGW_BASE=straiker-agentgateway-fork:local
FROM ${AGW_BASE} AS agw

FROM python:3.12-slim
ARG AGW_VERSION=v1.4.1
ENV AGENTGATEWAY_VERSION=${AGW_VERSION} PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY --from=agw /app/agentgateway /usr/local/bin/agentgateway
WORKDIR /srv
COPY straiker_sidecar/requirements.txt /srv/straiker_sidecar/requirements.txt
RUN pip install --no-cache-dir -r /srv/straiker_sidecar/requirements.txt
COPY straiker_sidecar /srv/straiker_sidecar
COPY gateway/config /srv/config
COPY gateway/entrypoint.sh /entrypoint.sh
COPY lab/tasks_server.py /srv/tasks_server.py
COPY lab/claims_server.py /srv/claims_server.py
COPY lab/knowledge_server.py /srv/knowledge_server.py
RUN chmod +x /entrypoint.sh && useradd -r -u 10001 agw && chown -R agw:agw /srv
USER agw
# App Runner routes to ONE port; agentgateway's HTTP listener binds it. gRPC/HTTP sidecar = loopback only.
EXPOSE 3000
ENTRYPOINT ["/entrypoint.sh"]
