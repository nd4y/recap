"""Connection settings for the n8n helper scripts.

Read from `.n8n.env` next to this file (KEY=VALUE, gitignored; see `.n8n.env.example`),
overridable by environment variables of the same name.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    cfg = {"N8N_TIMEZONE": "UTC"}
    path = os.path.join(HERE, ".n8n.env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    for k in ("N8N_URL", "N8N_OWNER_EMAIL", "N8N_TIMEZONE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    missing = [k for k in ("N8N_URL", "N8N_OWNER_EMAIL") if not cfg.get(k)]
    if missing:
        raise SystemExit("set %s in %s or in the environment" % (", ".join(missing), path))
    cfg["N8N_URL"] = cfg["N8N_URL"].rstrip("/")
    return cfg
