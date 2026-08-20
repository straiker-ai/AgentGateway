"""python -m straiker_sidecar  — one process, three seams (HTTP webhook + ExtProc gRPC + ExtMCP gRPC)."""
from __future__ import annotations

import logging
import os
import sys

import uvicorn

sys.path.insert(0, os.path.dirname(__file__))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")

from config import Settings  # noqa: E402

if __name__ == "__main__":
    s = Settings()
    uvicorn.run("app:app", host=s.http_host, port=s.http_port, log_level="info", access_log=False)
