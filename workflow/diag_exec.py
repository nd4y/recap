#!/usr/bin/env python3
"""Show recent n8n executions and the error of the newest failed one."""
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
    if cookie:
        h["Cookie"] = cookie
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        return resp.status, json.loads(resp.read().decode() or "{}"), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500], []

st, body, setc = req("/rest/login", "POST", {"email": EMAIL, "emailOrLdapLoginId": EMAIL, "password": password})
if st != 200:
    sys.exit("login failed: %s %s" % (st, body))
cookie = "; ".join(c.split(";")[0] for c in setc)

st, body, _ = req("/rest/executions?limit=15")
if st != 200:
    sys.exit("executions list failed: %s %s" % (st, body))
data = body.get("data", body)
results = data.get("results", data if isinstance(data, list) else [])
failed_id = None
for e in results:
    line = "%s %s %s started=%s stopped=%s" % (e.get("id"), e.get("workflowName") or e.get("workflowId"), e.get("status"), e.get("startedAt"), e.get("stoppedAt"))
    print(line)
    if e.get("status") in ("error", "failed", "crashed") and not failed_id:
        failed_id = e.get("id")

if failed_id:
    st, body, _ = req("/rest/executions/%s" % failed_id)
    if st == 200:
        d = body.get("data", body)
        raw = d.get("data")
        print("\n--- failed execution %s ---" % failed_id)
        print("workflow:", (d.get("workflowData") or {}).get("name"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                print("raw data string:", raw[:1500]); raw = None
        if isinstance(raw, dict):
            rd = raw.get("resultData", {})
            print("lastNodeExecuted:", rd.get("lastNodeExecuted"))
            print("error:", json.dumps(rd.get("error"), ensure_ascii=False)[:2000])
        elif isinstance(raw, list):
            s = json.dumps(raw, ensure_ascii=False)
            import re
            print("lastNodeExecuted refs:", re.findall(r'"lastNodeExecuted","(\d+)"', s)[:3])
            msgs = re.findall(r'"message":"((?:[^"\\]|\\.){0,300})"', s)
            seen = []
            for m in msgs:
                if m not in seen:
                    seen.append(m)
            for m in seen[:8]:
                print("msg:", m)
            stacks = re.findall(r'"description":"((?:[^"\\]|\\.){0,300})"', s)
            for m in stacks[:3]:
                print("desc:", m)
            # node name via index refs is fiddly; dump names present
            print("nodes mentioned:", [n for n in ["Scan","Read audio","ASR","Build prompt","LLM","Save note","TG configured","TG message","Prep doc","TG doc","Finalize"] if '"%s"' % n in s])
    else:
        print("fetch failed exec:", st, str(body)[:300])
