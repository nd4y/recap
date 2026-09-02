#!/usr/bin/env python3
import json, os, sys, urllib.request, urllib.error
import n8n_env
_CFG = n8n_env.load()
BASE = _CFG["N8N_URL"]
HERE = os.path.dirname(os.path.abspath(__file__))
EMAIL = _CFG["N8N_OWNER_EMAIL"]
password = open(os.path.join(HERE, ".n8n-owner-password")).read().strip()
cookie = None
def req(path, method="GET", body=None):
    h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    resp = urllib.request.urlopen(r, timeout=30)
    return json.loads(resp.read().decode() or "{}"), resp.headers.get_all("Set-Cookie") or []
body, setc = req("/rest/login", "POST", {"email": EMAIL, "emailOrLdapLoginId": EMAIL, "password": password})
cookie = "; ".join(c.split(";")[0] for c in setc)
eid = sys.argv[1] if len(sys.argv) > 1 else "3"
body, _ = req("/rest/executions/%s" % eid)
d = body.get("data", body)
raw = d.get("data")
if isinstance(raw, str):
    try: raw = json.loads(raw)
    except Exception: pass
out = os.path.join(HERE, "exec_dump.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False, indent=1)
print("saved", out)
