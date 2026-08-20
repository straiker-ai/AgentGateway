#!/usr/bin/env python3
"""Render gateway/config/agentgateway.yaml for deployment: replace the dev `keys:` block with the real
per-user list from deploy/agent-keys.env. Prints the rendered YAML to stdout. Pure text transform so the
committed reference config stays readable and the keys stay out of git."""
import re, sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
cfg = open(os.path.join(HERE, "..", "gateway", "config", "agentgateway.yaml")).read()
keys = []
for line in open(os.path.join(HERE, "agent-keys.env")):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    user, key = line.split("=", 1)
    keys.append((user.strip(), key.strip()))
if not keys:
    sys.exit("no keys in deploy/agent-keys.env")
block = "\n".join(f"      - key: {k}\n        metadata: {{ user: {u}, team: straiker, app: {u}, straiker_app: \"\" }}" for u, k in keys)
new, n = re.subn(r"      keys:\n      - key: dev-key-1\n        metadata: \{ user: [^}]+\}\n", "      keys:\n" + block + "\n", cfg)
if n != 1:
    sys.exit(f"expected exactly one dev keys block, found {n}")
sys.stdout.write(new)
