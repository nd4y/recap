#!/usr/bin/env python3
"""Deploy recap workflows to n8n: owner setup, API key, create + activate.

Idempotent-ish: safe to rerun — reuses owner, creates workflows only if absent
(by name), otherwise updates them via PUT.
"""

import json
import os
import secrets
import sys
import urllib.request
import urllib.error

import n8n_env
_CFG = n8n_env.load()
BASE = _CFG["N8N_URL"]
HERE = os.path.dirname(os.path.abspath(__file__))
EMAIL = _CFG["N8N_OWNER_EMAIL"]
PASS_FILE = os.path.join(HERE, ".n8n-owner-password")  # gitignore this

cookie = None


def req(path, method="GET", body=None, headers=None, raw=False):
    h = {"Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        txt = resp.read().decode()
        setc = resp.headers.get_all("Set-Cookie") or []
        return resp.status, (txt if raw else (json.loads(txt) if txt else {})), setc
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), []


def load(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


# ---------- auth ----------
if os.path.exists(PASS_FILE):
    password = open(PASS_FILE).read().strip()
else:
    password = "Recap-" + secrets.token_urlsafe(12)
    with open(PASS_FILE, "w") as f:
        f.write(password)

st, body, setc = req("/rest/owner/setup", "POST", {
    "email": EMAIL, "firstName": "Owner", "lastName": "n8n", "password": password,
})
if st == 200:
    print("owner created:", EMAIL)
    cookie = "; ".join(c.split(";")[0] for c in setc)
else:
    print("owner setup skipped (status %s), logging in" % st)
    st, body, setc = req("/rest/login", "POST", {
        "email": EMAIL, "emailOrLdapLoginId": EMAIL, "password": password,
    })
    if st != 200:
        sys.exit("login failed: %s %s" % (st, body))
    cookie = "; ".join(c.split(";")[0] for c in setc)

# ---------- api key ----------
st, scopes, _ = req("/rest/api-keys/scopes")
if st == 200:
    scopes = scopes.get("data", scopes)
else:
    scopes = []
if not isinstance(scopes, list):
    scopes = []
import time as _time
st, body, _ = req("/rest/api-keys", "POST", {"label": "deploy-%d" % _time.time(), "expiresAt": None, "scopes": scopes})
if st not in (200, 201):
    sys.exit("api key creation failed: %s %s" % (st, body))
d = body.get("data", body)
apikey = d.get("rawApiKey") or d.get("apiKey")
if not apikey:
    sys.exit("no api key in response: %s" % json.dumps(body)[:500])
print("api key created")
API = {"X-N8N-API-KEY": apikey}


def api(path, method="GET", body=None):
    st, resp, _ = req("/api/v1" + path, method, body, headers=API)
    return st, resp


# ---------- error workflow ----------
error_format_js = """
if (!$env.TELEGRAM_BOT_TOKEN) { return []; }
const e = $json;
const msg = ('recap: ошибка пайплайна\\n' +
  'Workflow: ' + ((e.workflow && e.workflow.name) || '?') + '\\n' +
  'Нода: ' + ((e.execution && e.execution.lastNodeExecuted) || '?') + '\\n' +
  'Ошибка: ' + ((e.execution && e.execution.error && e.execution.error.message) || '?')).slice(0, 4000);
return [{ json: { text: msg } }];
""".strip()

err_wf = {
    "name": "recap-errors",
    "nodes": [
        {"parameters": {}, "id": "e1", "name": "Error Trigger",
         "type": "n8n-nodes-base.errorTrigger", "typeVersion": 1, "position": [0, 0]},
        {"parameters": {"jsCode": error_format_js}, "id": "e2", "name": "Format",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [220, 0]},
        {"parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({chat_id: $env.TELEGRAM_CHAT_ID, text: $json.text}) }}",
            "options": {"timeout": 30000},
        }, "id": "e3", "name": "TG alert",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [440, 0]},
    ],
    "connections": {
        "Error Trigger": {"main": [[{"node": "Format", "type": "main", "index": 0}]]},
        "Format": {"main": [[{"node": "TG alert", "type": "main", "index": 0}]]},
    },
    "settings": {"executionOrder": "v1"},
}

# ---------- main workflow ----------
main_wf = {
    "name": "recap-pipeline",
    "nodes": [
        {"parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}},
         "id": "n1", "name": "Every 5 min",
         "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [-400, 0]},
        {"parameters": {"jsCode": load("telegram_ingest.js")}, "id": "tg-in", "name": "Telegram intake",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [-300, 0]},
        {"parameters": {"jsCode": load("scan.js")}, "id": "n2", "name": "Scan",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [-180, 0]},
        {"parameters": {"options": {}}, "id": "n3", "name": "Loop",
         "type": "n8n-nodes-base.splitInBatches", "typeVersion": 3, "position": [40, 0]},
        {"parameters": {"operation": "read", "fileSelector": "={{ $json.path }}", "options": {}},
         "id": "n4", "name": "Read audio",
         "type": "n8n-nodes-base.readWriteFile", "typeVersion": 1, "position": [260, 100]},
        {"parameters": {
            "method": "POST",
            "url": "={{ $env.RECAP_ASR_URL }}/v1/audio/transcriptions",
            "sendBody": True, "contentType": "multipart-form-data",
            "bodyParameters": {"parameters": [
                {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "data"},
                {"name": "num_speakers", "value": "={{ $('Loop').item.json.type === 'call' ? '2' : '' }}"},
                {"name": "diarize", "value": "={{ $('Loop').item.json.type === 'voice_note' ? 'false' : 'true' }}"},
                {"name": "skip_short", "value": "={{ $('Loop').item.json.telegram ? 'false' : 'true' }}"},
            ]},
            "options": {"timeout": 900000},
        }, "id": "n5", "name": "ASR",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [480, 100],
         "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 5000,
         "onError": "continueRegularOutput"},
        {"parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [
                    {"id": "ok1", "leftValue": "={{ $json.segments !== undefined }}", "rightValue": "",
                     "operator": {"type": "boolean", "operation": "true", "singleValue": True}},
                ],
                "combinator": "and",
            },
            "options": {},
        }, "id": "n16", "name": "ASR ok",
         "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [590, 100]},
        {"parameters": {"mode": "runOnceForEachItem", "jsCode": load("mark_bad.js")},
         "id": "n17", "name": "Mark bad",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [590, 260]},
        {"parameters": {"mode": "runOnceForEachItem", "jsCode": load("build_prompt.js")},
         "id": "n6", "name": "Build prompt",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [700, 100]},
        {"parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [
                    {"id": "hs1", "leftValue": "={{ $json.hasSpeech }}", "rightValue": "",
                     "operator": {"type": "boolean", "operation": "true", "singleValue": True}},
                ],
                "combinator": "and",
            },
            "options": {},
        }, "id": "n15", "name": "Has speech",
         "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [810, 100]},
        {"parameters": {
            "method": "POST",
            "url": "={{ $env.LLM_BASE_URL }}/chat/completions",
            "sendHeaders": True,
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $env.LLM_API_KEY }}"},
            ]},
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.llmBody) }}",
            "options": {"timeout": 600000},
        }, "id": "n7", "name": "LLM",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [920, 100],
         "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 10000},
        {"parameters": {"mode": "runOnceForEachItem", "jsCode": load("save_note.js")},
         "id": "n8", "name": "Save note",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [1140, 100]},
        {"parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [
                    {"id": "c1", "leftValue": "={{ $env.TELEGRAM_BOT_TOKEN }}", "rightValue": "",
                     "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}},
                    {"id": "c2", "leftValue": "={{ $json.tgText }}", "rightValue": "",
                     "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}},
                ],
                "combinator": "and",
            },
            "options": {},
        }, "id": "n9", "name": "TG configured",
         "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [1360, 100]},
        {"parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({chat_id: $env.TELEGRAM_CHAT_ID, text: $json.tgText, ...($json.telegram ? {reply_parameters: {message_id: $json.telegram.message_id, allow_sending_without_reply: true}} : {})}) }}",
            "options": {"timeout": 30000},
        }, "id": "n10", "name": "TG message",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [1580, 0],
         "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 5000},
        {"parameters": {"mode": "runOnceForEachItem", "jsCode": load("prep_doc.js")},
         "id": "n11", "name": "Prep doc",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [1800, 0]},
        {"parameters": {
            "method": "POST",
            "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendDocument",
            "sendBody": True, "contentType": "multipart-form-data",
            "bodyParameters": {"parameters": [
                {"parameterType": "formBinaryData", "name": "document", "inputDataFieldName": "document"},
                {"name": "chat_id", "value": "={{ $env.TELEGRAM_CHAT_ID }}"},
                {"name": "reply_to_message_id", "value": "={{ $json.message_id }}"},
            ]},
            "options": {"timeout": 60000},
        }, "id": "n12", "name": "TG doc",
         "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [2020, 0],
         "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 5000},
        {"parameters": {"mode": "runOnceForEachItem", "jsCode": load("finalize.js")},
         "id": "n13", "name": "Finalize",
         "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [2240, 100]},
        {"parameters": {}, "id": "n14", "name": "Done",
         "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": [260, -140]},
    ],
    "connections": {
        "Every 5 min": {"main": [[{"node": "Telegram intake", "type": "main", "index": 0}]]},
        "Telegram intake": {"main": [[{"node": "Scan", "type": "main", "index": 0}]]},
        "Scan": {"main": [[{"node": "Loop", "type": "main", "index": 0}]]},
        "Loop": {"main": [
            [{"node": "Done", "type": "main", "index": 0}],
            [{"node": "Read audio", "type": "main", "index": 0}],
        ]},
        "Read audio": {"main": [[{"node": "ASR", "type": "main", "index": 0}]]},
        "ASR": {"main": [[{"node": "ASR ok", "type": "main", "index": 0}]]},
        "ASR ok": {"main": [
            [{"node": "Build prompt", "type": "main", "index": 0}],
            [{"node": "Mark bad", "type": "main", "index": 0}],
        ]},
        "Mark bad": {"main": [[{"node": "Loop", "type": "main", "index": 0}]]},
        "Build prompt": {"main": [[{"node": "Has speech", "type": "main", "index": 0}]]},
        "Has speech": {"main": [
            [{"node": "LLM", "type": "main", "index": 0}],
            [{"node": "Save note", "type": "main", "index": 0}],
        ]},
        "LLM": {"main": [[{"node": "Save note", "type": "main", "index": 0}]]},
        "Save note": {"main": [[{"node": "TG configured", "type": "main", "index": 0}]]},
        "TG configured": {"main": [
            [{"node": "TG message", "type": "main", "index": 0}],
            [{"node": "Finalize", "type": "main", "index": 0}]],
        },
        "TG message": {"main": [[{"node": "Prep doc", "type": "main", "index": 0}]]},
        "Prep doc": {"main": [[{"node": "TG doc", "type": "main", "index": 0}]]},
        "TG doc": {"main": [[{"node": "Finalize", "type": "main", "index": 0}]]},
        "Finalize": {"main": [[{"node": "Loop", "type": "main", "index": 0}]]},
    },
    # успешные экзекьюшены не сохраняются: 288 пустых тиков в сутки с полными данными
    # раздували SQLite и участвовали в заклине мьютекса записи; для разбора остаются
    # ошибочные (целиком) и история в реестре/метриках
    "settings": {"executionOrder": "v1", "timezone": _CFG["N8N_TIMEZONE"],
                 "saveDataSuccessExecution": "none", "saveDataErrorExecution": "all"},
}


def upsert(wf):
    st, resp = api("/workflows?limit=100")
    if st != 200:
        sys.exit("list workflows failed: %s %s" % (st, resp))
    existing = {w["name"]: w["id"] for w in resp.get("data", [])}
    if wf["name"] in existing:
        wid = existing[wf["name"]]
        st, resp = api("/workflows/%s" % wid, "PUT", wf)
        if st != 200:
            sys.exit("update %s failed: %s %s" % (wf["name"], st, json.dumps(resp)[:800]))
        print("updated", wf["name"], wid)
        return wid
    st, resp = api("/workflows", "POST", wf)
    if st not in (200, 201):
        sys.exit("create %s failed: %s %s" % (wf["name"], st, json.dumps(resp)[:800]))
    print("created", wf["name"], resp["id"])
    return resp["id"]


err_id = upsert(err_wf)
main_wf["settings"]["errorWorkflow"] = err_id
main_id = upsert(main_wf)

for wid, name in [(main_id, "recap-pipeline")]:
    st, resp = api("/workflows/%s/activate" % wid, "POST")
    if st != 200:
        sys.exit("activate %s failed: %s %s" % (name, st, json.dumps(resp)[:500]))
    print("activated", name)

print("MAIN_ID=%s ERR_ID=%s" % (main_id, err_id))
print("owner password in", PASS_FILE)
