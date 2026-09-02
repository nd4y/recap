#!/usr/bin/env python3
"""recap-exporter: Prometheus-метрики конвейера recap из реестра и outbox.

Читает /data/state/registry.json (его пишет workflow n8n) и каталог
/data/state/outbox, отдаёт /metrics. Только stdlib: контейнер python:alpine
без установки пакетов, скрипт монтируется файлом.

Что показывает:
  recap_registry_files{type,status}      файлов в реестре по типу и статусу
  recap_registry_bytes{type,status}      суммарный размер этих файлов
  recap_registry_retried_files{type}     файлов, у которых было больше одной попытки
  recap_registry_audio_seconds_{sum,count}{type}  длительность аудио (поле duration)
  recap_registry_asr_seconds_{sum,count}{type}    время транскрибации (поле asr_seconds)
  recap_registry_llm_seconds_{sum,count}{type}    время суммаризации (поле llm_seconds)
  recap_registry_last_transition_timestamp_seconds{status}  когда последний файл получил статус
  recap_registry_last_recording_timestamp_seconds{type}     mtime самой свежей записи
  recap_registry_updated_timestamp_seconds  mtime реестра = последний тик workflow
  recap_registry_read_error              1, если реестр есть, но не читается
  recap_outbox_files                     заметок, ждущих доставки в vault
  recap_outbox_oldest_age_seconds        возраст самой старой из них
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = os.environ.get("RECAP_STATE_DIR", "/data/state")
REGISTRY = os.path.join(STATE, "registry.json")
OUTBOX = os.path.join(STATE, "outbox")
PORT = int(os.environ.get("RECAP_EXPORTER_PORT", "9819"))

# записи реестра до появления поля type: тип по подкаталогу, как в scan.js
TYPE_BY_SUBDIR = {"CallRecords": "call", "Meetings": "meeting", "Jitsi": "meeting", "Voice": "voice_note"}
STATUSES = ("processing", "noted", "done", "failed", "skipped")


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self):
        self.lines = []

    def header(self, name, help_, kind="gauge"):
        self.lines.append("# HELP %s %s" % (name, help_))
        self.lines.append("# TYPE %s %s" % (name, kind))

    def sample(self, name, labels, value):
        if labels:
            lab = "{" + ",".join('%s="%s"' % (k, _esc(v)) for k, v in labels.items()) + "}"
        else:
            lab = ""
        if isinstance(value, float) and value == int(value):
            value = int(value)
        self.lines.append("%s%s %s" % (name, lab, value))  # str(float) в Python без потери точности

    def text(self):
        return "\n".join(self.lines) + "\n"


def entry_type(rel, rec):
    t = rec.get("type")
    if t:
        return t
    sub = rel.split("/")[0] if "/" in rel else ""
    return TYPE_BY_SUBDIR.get(sub, "generic")


def collect():
    t0 = time.time()
    m = Metrics()

    reg, read_error, reg_mtime = {}, 0, 0.0
    if os.path.exists(REGISTRY):
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                reg = json.load(f)
            reg_mtime = os.path.getmtime(REGISTRY)
        except Exception:
            reg, read_error = {}, 1

    files, size, retried = {}, {}, {}
    audio, asr, llm = {}, {}, {}
    last_transition = {}
    last_recording = {}
    types = set()
    for rel, rec in reg.items():
        if not isinstance(rec, dict):
            continue
        t = entry_type(rel, rec)
        st = rec.get("status") or "unknown"
        types.add(t)
        files[(t, st)] = files.get((t, st), 0) + 1
        size[(t, st)] = size.get((t, st), 0) + int(rec.get("size") or 0)
        if int(rec.get("attempts") or 0) > 1:
            retried[t] = retried.get(t, 0) + 1
        for field, acc in (("duration", audio), ("asr_seconds", asr), ("llm_seconds", llm)):
            v = rec.get(field)
            if isinstance(v, (int, float)):
                s, c = acc.get(t, (0.0, 0))
                acc[t] = (s + float(v), c + 1)
        ts = rec.get("claimed") if st == "processing" else rec.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            last_transition[st] = max(last_transition.get(st, 0), ts / 1000.0)
        mt = rec.get("mtime")
        if isinstance(mt, (int, float)) and mt > 0:
            last_recording[t] = max(last_recording.get(t, 0), mt / 1000.0)

    m.header("recap_registry_files", "Files in the registry by type and status")
    for t in sorted(types):
        for st in STATUSES:
            m.sample("recap_registry_files", {"type": t, "status": st}, files.get((t, st), 0))
    for (t, st), n in sorted(files.items()):
        if st not in STATUSES:
            m.sample("recap_registry_files", {"type": t, "status": st}, n)

    m.header("recap_registry_bytes", "Total size of registry files by type and status")
    for (t, st), b in sorted(size.items()):
        m.sample("recap_registry_bytes", {"type": t, "status": st}, b)

    m.header("recap_registry_retried_files", "Files that needed more than one attempt")
    for t in sorted(types):
        m.sample("recap_registry_retried_files", {"type": t}, retried.get(t, 0))

    for name, acc, help_ in (
        ("recap_registry_audio_seconds", audio, "Audio duration of processed files (registry field duration)"),
        ("recap_registry_asr_seconds", asr, "Transcription wall time (registry field asr_seconds)"),
        ("recap_registry_llm_seconds", llm, "Summarization wall time (registry field llm_seconds)"),
    ):
        m.header(name, help_, "summary")
        for t, (s, c) in sorted(acc.items()):
            m.sample(name + "_sum", {"type": t}, s)
            m.sample(name + "_count", {"type": t}, c)

    m.header("recap_registry_last_transition_timestamp_seconds", "When a file last entered the status")
    for st, ts in sorted(last_transition.items()):
        m.sample("recap_registry_last_transition_timestamp_seconds", {"status": st}, ts)

    m.header("recap_registry_last_recording_timestamp_seconds", "mtime of the newest recording seen, by type")
    for t, ts in sorted(last_recording.items()):
        m.sample("recap_registry_last_recording_timestamp_seconds", {"type": t}, ts)

    m.header("recap_registry_updated_timestamp_seconds", "mtime of registry.json (last workflow tick)")
    m.sample("recap_registry_updated_timestamp_seconds", {}, reg_mtime)
    m.header("recap_registry_read_error", "1 if registry.json exists but cannot be parsed")
    m.sample("recap_registry_read_error", {}, read_error)

    outbox_n, oldest = 0, 0.0
    if os.path.isdir(OUTBOX):
        for root, _dirs, names in os.walk(OUTBOX):
            for n in names:
                if n.startswith("."):
                    continue
                try:
                    mt = os.path.getmtime(os.path.join(root, n))
                except OSError:
                    continue
                outbox_n += 1
                oldest = mt if not oldest or mt < oldest else oldest
    m.header("recap_outbox_files", "Notes waiting in the outbox for delivery to the vault")
    m.sample("recap_outbox_files", {}, outbox_n)
    m.header("recap_outbox_oldest_age_seconds", "Age of the oldest note in the outbox")
    m.sample("recap_outbox_oldest_age_seconds", {}, (time.time() - oldest) if oldest else 0.0)

    m.header("recap_exporter_collect_seconds", "Time spent building this response")
    m.sample("recap_exporter_collect_seconds", {}, time.time() - t0)
    return m.text()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/metrics":
            body = collect().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # скрейпы в лог не пишем
        pass


if __name__ == "__main__":
    print("recap-exporter: %s on :%d" % (REGISTRY, PORT), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
