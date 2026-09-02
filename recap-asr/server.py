"""recap-asr — speech-to-text + diarization service for the recap pipeline.

OpenAI-compatible endpoint: POST /v1/audio/transcriptions (multipart).
Extras on top of the OpenAI schema: speaker labels per segment and owner
identification against a reference voice embedding.

ASR: GigaAM v3 (v3_e2e_rnnt, русский язык, пунктуация из коробки). Диаризация
идёт ПЕРВОЙ и служит заодно VAD'ом: транскрибируются только речевые интервалы,
музыка ожидания и тишина в ASR не попадают.

Runs natively on Windows (no Docker) as a WinSW service, torch cu128.
"""

import gc
import logging
import os
import tempfile
import threading
import time
import wave

_HERE = os.path.dirname(os.path.abspath(__file__))

# WinGet Links — reparse-каталог: любой скан PATH по нему падает с OSError 448
# («недоверенная точка подключения»), поэтому вычищаем его из PATH процесса.
# gigaam зовёт ffmpeg по имени (даже для wav) — у службы (LocalSystem) нет
# пользовательского PATH, поэтому каталог бинаря задаётся явно: RECAP_FFMPEG_DIR
# или подкаталог bin/ рядом с этим файлом.
_FFMPEG_DIR = os.environ.get("RECAP_FFMPEG_DIR") or os.path.join(_HERE, "bin")
os.environ["PATH"] = os.pathsep.join(
    ([_FFMPEG_DIR] if os.path.isdir(_FFMPEG_DIR) else [])
    + [p for p in os.environ.get("PATH", "").split(os.pathsep) if "WinGet" not in p]
)

# pyannote checkpoints predate torch 2.6 weights_only default and fail to load
# under it; models come only from the official pyannote repos in our own HF cache
os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
# без серий *_created рядом с каждым счётчиком: они удваивают выдачу /metrics без пользы
os.environ.setdefault("PROMETHEUS_DISABLE_CREATED_SERIES", "true")

import torch  # noqa: E402  (import order is deliberate)
import numpy as np  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
import av  # noqa: E402  (PyAV: декод m4a/webm/mp3/... в float32 16 kHz mono)
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest  # noqa: E402

log = logging.getLogger("recap-asr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HOST = os.environ.get("RECAP_HOST", "0.0.0.0")
PORT = int(os.environ.get("RECAP_PORT", "8000"))
ASR_MODEL = os.environ.get("RECAP_MODEL", "v3_e2e_rnnt")
# фиксированный кэш весов: у службы (LocalSystem) свой ~, дефолтный ~/.cache/gigaam
# уводил модель в systemprofile и качал её повторно
GIGAAM_CACHE = os.environ.get("RECAP_GIGAAM_CACHE") or os.path.join(_HERE, "gigaam-cache")
DEFAULT_LANGUAGE = os.environ.get("RECAP_LANGUAGE", "ru")  # GigaAM — только русский
# записи короче порога не обрабатываются вовсе (сброшенные и случайные звонки):
# мгновенный ответ skipped без загрузки моделей; 0 = выкл
MIN_DURATION = float(os.environ.get("RECAP_MIN_DURATION", "5"))
MODEL_TTL = int(os.environ.get("RECAP_TTL", "300"))  # seconds idle before VRAM unload
# выгрузка моделей после каждого запроса: VRAM делится с LLM у Ollama
UNLOAD_AFTER = os.environ.get("RECAP_UNLOAD_AFTER", "1").lower() not in ("0", "false", "no")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
OWNER_REF = os.environ.get("RECAP_OWNER_REF") or os.path.join(_HERE, "owner_ref.npy")
OWNER_THRESHOLD = float(os.environ.get("RECAP_OWNER_THRESHOLD", "0.5"))
DIARIZATION_MODEL = os.environ.get("RECAP_DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1")
EMBEDDING_MODEL = os.environ.get("RECAP_EMBEDDING_MODEL", "pyannote/wespeaker-voxceleb-resnet34-LM")
SAMPLE_RATE = 16000
# short-form лимит GigaAM ~25 c: куски держим <=23.5 c
CHUNK_MAX = 23.5
CHUNK_CUT_FROM = 16.0

app = FastAPI(title="recap-asr")

_lock = threading.Lock()  # GPU work is serialized
_models = {"asr": None, "diarization": None, "embedding": None}
_last_used = 0.0
_owner_ref = None

# Prometheus-метрики на /metrics (без аутентификации, как и остальной сервис — только LAN)
M_REQUESTS = Counter("recap_asr_requests_total", "Transcription requests by result", ["result"])
M_AUDIO = Counter("recap_asr_audio_seconds_total", "Audio duration received, by result", ["result"])
M_STAGE = Counter("recap_asr_stage_seconds_total", "Wall time by processing stage", ["stage"])
M_REQUEST_TIME = Histogram(
    "recap_asr_request_seconds", "Wall time of a transcription request",
    buckets=(5, 10, 20, 30, 60, 120, 300, 600, 900),
)
M_SEGMENTS = Counter("recap_asr_segments_total", "Transcript segments produced")
M_OWNER = Counter("recap_asr_owner_total", "Owner identification outcome per diarized request", ["result"])
M_INFLIGHT = Gauge("recap_asr_inflight", "Requests being processed or waiting for the GPU lock")
M_MODEL_LOADED = Gauge("recap_asr_model_loaded", "1 while the model is loaded in VRAM", ["model"])
for _r in ("ok", "skipped", "undecodable", "error"):
    M_REQUESTS.labels(_r)
    M_AUDIO.labels(_r)
for _s in ("decode", "diarization", "asr"):
    M_STAGE.labels(_s)
for _o in ("found", "not_found", "no_reference"):
    M_OWNER.labels(_o)
for _m in _models:
    M_MODEL_LOADED.labels(_m).set(0)


# свой роут, а не app.mount("/metrics", make_asgi_app()): mount отвечает на /metrics
# редиректом 307 на /metrics/, и скрейперу без follow_redirects достаётся пустой ответ
@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def decode_audio(path, sampling_rate=SAMPLE_RATE):
    """Любой контейнер/кодек (m4a, mp3, webm, mp4, ...) -> float32 mono @ sampling_rate."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    frames = []
    with av.open(path, metadata_errors="ignore") as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio stream")
        for frame in container.decode(stream):
            frame.pts = None
            for out in resampler.resample(frame):
                frames.append(out.to_ndarray())
        for out in resampler.resample(None):  # flush
            frames.append(out.to_ndarray())
    if not frames:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(frames, axis=1).reshape(-1)
    return pcm.astype(np.float32) / 32768.0


def _load_owner_ref():
    global _owner_ref
    if _owner_ref is None and os.path.isfile(OWNER_REF):
        _owner_ref = np.load(OWNER_REF)
        log.info("owner reference loaded from %s", OWNER_REF)
    return _owner_ref


def _get_asr():
    if _models["asr"] is None:
        import gigaam

        log.info("loading GigaAM %s", ASR_MODEL)
        _models["asr"] = gigaam.load_model(ASR_MODEL, download_root=GIGAAM_CACHE)
        M_MODEL_LOADED.labels("asr").set(1)
    return _models["asr"]


def _get_diarization():
    if _models["diarization"] is None:
        from pyannote.audio import Pipeline

        log.info("loading diarization pipeline %s", DIARIZATION_MODEL)
        pipe = Pipeline.from_pretrained(DIARIZATION_MODEL, use_auth_token=HF_TOKEN or None)
        pipe.to(torch.device("cuda"))
        _models["diarization"] = pipe
        M_MODEL_LOADED.labels("diarization").set(1)
    return _models["diarization"]


def _get_embedding():
    if _models["embedding"] is None:
        from pyannote.audio import Inference, Model

        log.info("loading embedding model %s", EMBEDDING_MODEL)
        model = Model.from_pretrained(EMBEDDING_MODEL, use_auth_token=HF_TOKEN or None)
        _models["embedding"] = Inference(model, window="whole", device=torch.device("cuda"))
        M_MODEL_LOADED.labels("embedding").set(1)
    return _models["embedding"]


def _touch():
    global _last_used
    _last_used = time.time()


def _unload_models(reason):
    if any(_models.values()):
        log.info("unloading models from VRAM (%s)", reason)
        for k in _models:
            _models[k] = None
            M_MODEL_LOADED.labels(k).set(0)
        gc.collect()
        torch.cuda.empty_cache()


def _unloader():
    while True:
        time.sleep(30)
        with _lock:
            if _last_used and time.time() - _last_used > MODEL_TTL:
                _unload_models("idle %ds" % MODEL_TTL)


threading.Thread(target=_unloader, daemon=True).start()


def _cosine(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


def speaker_embedding(audio, annotation, label, max_seconds=30.0):
    """Embedding of one diarized speaker: concatenated crops of their turns."""
    chunks, total = [], 0.0
    for seg in annotation.label_timeline(label):
        if seg.duration < 0.4:
            continue
        s, e = int(seg.start * SAMPLE_RATE), int(seg.end * SAMPLE_RATE)
        chunks.append(audio[s:e])
        total += seg.duration
        if total >= max_seconds:
            break
    if total < 1.5:
        return None
    wav = torch.from_numpy(np.concatenate(chunks))[None, :]
    emb = _get_embedding()({"waveform": wav, "sample_rate": SAMPLE_RATE})
    return np.asarray(emb).flatten()


def diarize(audio, num_speakers=None):
    """Run diarization; returns (annotation, speakers_meta)."""
    pipe = _get_diarization()
    wav = torch.from_numpy(audio)[None, :]
    kwargs = {}
    if num_speakers:
        kwargs["num_speakers"] = num_speakers
    ann = pipe({"waveform": wav, "sample_rate": SAMPLE_RATE}, **kwargs)

    ref = _load_owner_ref()
    speakers = []
    for label in ann.labels():
        talk = sum(seg.duration for seg in ann.label_timeline(label))
        emb = speaker_embedding(audio, ann, label)
        score = _cosine(emb, ref) if (emb is not None and ref is not None) else None
        speakers.append({"label": label, "talk_time": round(talk, 1), "owner_score": score, "embedding": emb})

    # highest score above threshold wins; everything else is not the owner
    scored = [s for s in speakers if s["owner_score"] is not None]
    best = max(scored, key=lambda s: s["owner_score"], default=None)
    for s in speakers:
        s["is_owner"] = bool(best and s is best and best["owner_score"] >= OWNER_THRESHOLD)
    M_OWNER.labels(
        "no_reference" if ref is None else ("found" if any(s["is_owner"] for s in speakers) else "not_found")
    ).inc()
    return ann, speakers


# ---------------------------------------------------------------- chunking


def _quietest_cut(audio, lo_s, hi_s, win=0.2):
    """Точка разреза: центр самого тихого win-окна в диапазоне [lo_s, hi_s]."""
    lo, hi, w = int(lo_s * SAMPLE_RATE), int(hi_s * SAMPLE_RATE), int(win * SAMPLE_RATE)
    hi = min(hi, len(audio))
    best, best_e = hi, None
    for s in range(lo, max(hi - w, lo + 1), w // 2):
        e = float(np.abs(audio[s:s + w]).sum())
        if best_e is None or e < best_e:
            best, best_e = s + w // 2, e
    return best / SAMPLE_RATE


def _split_long(audio, start, end):
    """Режет интервал на куски <=CHUNK_MAX по самым тихим местам."""
    out, pos = [], start
    while end - pos > CHUNK_MAX + 0.5:
        cut = _quietest_cut(audio, pos + CHUNK_CUT_FROM, pos + CHUNK_MAX)
        out.append((pos, cut))
        pos = cut
    out.append((pos, end))
    return out


def _speech_ranges(annotation, audio, pad=0.25, bridge=0.8):
    """Чанки для ASR из речевых интервалов диаризации (она же VAD): паузы,
    музыка ожидания и тишина в транскрибацию не попадают."""
    dur = len(audio) / SAMPLE_RATE
    regions = []
    for seg in annotation.get_timeline().support():
        a = max(0.0, seg.start - pad)
        b = min(dur, seg.end + pad)
        if regions and a - regions[-1][1] <= bridge:
            regions[-1] = (regions[-1][0], b)
        else:
            regions.append((a, b))

    ranges = []
    for a, b in regions:
        if b - a <= CHUNK_MAX:
            # приклеиваем к предыдущему чанку, если суммарно влезает
            if ranges and b - ranges[-1][0] <= CHUNK_MAX and a - ranges[-1][1] <= bridge:
                ranges[-1] = (ranges[-1][0], b)
            else:
                ranges.append((a, b))
        else:
            ranges.extend(_split_long(audio, a, b))
    return ranges


def _full_ranges(audio):
    """Чанки по всему файлу (режим без диаризации)."""
    dur = len(audio) / SAMPLE_RATE
    return _split_long(audio, 0.0, dur)


def _transcribe_ranges(audio, ranges):
    """GigaAM по кускам; словные таймкоды смещаются в координаты целого файла."""
    asr = _get_asr()
    chunks = []
    for a, b in ranges:
        seg = audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)]
        if len(seg) < int(0.3 * SAMPLE_RATE):
            continue
        pcm = (np.clip(seg, -1.0, 1.0) * 32767).astype(np.int16)
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm.tobytes())
            res = asr.transcribe(tmp, word_timestamps=True)
        finally:
            os.unlink(tmp)
        text = (res.text or "").strip()
        if not text:
            continue
        words = [{"t": w.text, "s": round(w.start + a, 2), "e": round(w.end + a, 2)}
                 for w in (res.words or [])]
        chunks.append({"start": round(a, 2), "end": round(b, 2), "text": text, "words": words})
    return chunks


# ---------------------------------------------------------------- turns


def segments_to_turns(chunks, annotation, merge_gap=1.2):
    """Гибридная разбивка на реплики. Чанк, целиком принадлежащий одному спикеру,
    сохраняется как есть (с пунктуацией и связностью); чанк со сменой говорящего
    внутри (перебивания) режется по словам."""
    turns = [(seg, label) for seg, _, label in annotation.itertracks(yield_label=True)]

    def word_speaker(ws, we):
        best, best_ov = None, 0.0
        for t, label in turns:
            ov = min(we, t.end) - max(ws, t.start)
            if ov > best_ov:
                best, best_ov = label, ov
        if best is None and turns:
            c = (ws + we) / 2
            best = min(turns, key=lambda tl: min(abs(c - tl[0].start), abs(c - tl[0].end)))[1]
        return best

    units = []
    for ch in chunks:
        words = ch["words"]
        if not words:
            units.append({"start": ch["start"], "end": ch["end"], "text": ch["text"], "speaker": None})
            continue
        spks = [word_speaker(w["s"], w["e"]) for w in words]
        if len(set(spks)) == 1:
            units.append({"start": words[0]["s"], "end": words[-1]["e"],
                          "text": ch["text"], "speaker": spks[0]})
        else:
            cur = None
            for w, sp in zip(words, spks):
                if cur and cur["speaker"] == sp:
                    cur["end"] = w["e"]
                    cur["text"] += " " + w["t"]
                else:
                    if cur:
                        units.append(cur)
                    cur = {"start": w["s"], "end": w["e"], "text": w["t"], "speaker": sp}
            if cur:
                units.append(cur)

    out = []
    for u in units:
        u["text"] = u["text"].strip()
        if out and out[-1]["speaker"] == u["speaker"] and u["start"] - out[-1]["end"] <= merge_gap:
            out[-1]["end"] = u["end"]
            out[-1]["text"] += " " + u["text"]
        else:
            out.append(u)
    out = [s for s in out if s["text"].strip(" -—–.,!?…")]
    for i, seg in enumerate(out):
        seg["id"] = i
        seg["start"] = round(seg["start"], 2)
        seg["end"] = round(seg["end"], 2)
    return out


@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine": "gigaam/%s" % ASR_MODEL,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "models_loaded": {k: v is not None for k, v in _models.items()},
        "owner_ref": os.path.isfile(OWNER_REF),
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(None),  # accepted for OpenAI compatibility, ignored
    language: str = Form(None),  # accepted for compatibility; GigaAM is ru-only
    response_format: str = Form("verbose_json"),
    diarize_flag: str = Form("true", alias="diarize"),
    # str, не int: n8n шлёт multipart-поля строками, пустая строка не должна давать 422
    num_speakers: str = Form(None),
):
    t0 = time.time()
    suffix = os.path.splitext(file.filename or "audio.m4a")[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        path = tmp.name
    try:
        audio = decode_audio(path, sampling_rate=SAMPLE_RATE)
    except Exception as e:
        # битый/недокачанный файл — это проблема входа (4xx), а не сервиса (5xx):
        # клиент по коду отличает «файл плохой, пометить failed» от «повторить позже»
        log.warning("undecodable audio %s: %s", file.filename, e)
        M_REQUESTS.labels("undecodable").inc()
        return JSONResponse({"error": "undecodable audio: %s" % e}, status_code=422)
    finally:
        os.unlink(path)
    M_STAGE.labels("decode").inc(time.time() - t0)
    duration = len(audio) / SAMPLE_RATE

    if MIN_DURATION and duration < MIN_DURATION:
        log.info("skipped %s: %.1fs < %.1fs", file.filename, duration, MIN_DURATION)
        M_REQUESTS.labels("skipped").inc()
        M_AUDIO.labels("skipped").inc(duration)
        return JSONResponse({
            "task": "transcribe", "language": DEFAULT_LANGUAGE,
            "duration": round(duration, 2), "text": "", "segments": [],
            "speakers": [], "diarization": False, "skipped": "too_short",
            "processing_time": round(time.time() - t0, 1),
        })

    want_diarization = str(diarize_flag).lower() not in ("false", "0", "no", "")
    ns = int(num_speakers) if num_speakers and str(num_speakers).strip().isdigit() else None

    M_INFLIGHT.inc()
    try:
        with _lock:
            _touch()
            speakers_out, diar_ok, diar_err, ann = [], False, None, None
            if want_diarization:
                t_diar = time.time()
                try:
                    ann, speakers = diarize(audio, num_speakers=ns)
                    speakers_out = [
                        {k: v for k, v in s.items() if k != "embedding"} for s in speakers
                    ]
                    diar_ok = True
                except Exception as e:  # diarization failure must not kill transcription
                    log.exception("diarization failed")
                    diar_err = str(e)
                M_STAGE.labels("diarization").inc(time.time() - t_diar)

            t_asr = time.time()
            ranges = _speech_ranges(ann, audio) if diar_ok else _full_ranges(audio)
            chunks = _transcribe_ranges(audio, ranges)
            M_STAGE.labels("asr").inc(time.time() - t_asr)

            if diar_ok:
                segments = segments_to_turns(chunks, ann)
            else:
                segments = [
                    {"id": i, "start": ch["start"], "end": ch["end"], "text": ch["text"]}
                    for i, ch in enumerate(chunks)
                ]
            _touch()
            if UNLOAD_AFTER:
                _unload_models("after request")
    except Exception:
        M_REQUESTS.labels("error").inc()
        M_AUDIO.labels("error").inc(duration)
        raise
    finally:
        M_INFLIGHT.dec()

    M_REQUESTS.labels("ok").inc()
    M_AUDIO.labels("ok").inc(duration)
    M_SEGMENTS.inc(len(segments))
    M_REQUEST_TIME.observe(time.time() - t0)

    body = {
        "task": "transcribe",
        "language": DEFAULT_LANGUAGE,
        "duration": round(duration, 2),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "speakers": speakers_out,
        "diarization": diar_ok,
        "processing_time": round(time.time() - t0, 1),
    }
    if diar_err:
        body["diarization_error"] = diar_err
    log.info(
        "transcribed %s: %.0fs audio in %.1fs, %d segments, diarization=%s",
        file.filename, duration, body["processing_time"], len(segments), diar_ok,
    )
    return JSONResponse(body)


if __name__ == "__main__":
    log.info("recap-asr starting on %s:%d (engine=gigaam/%s)", HOST, PORT, ASR_MODEL)
    uvicorn.run(app, host=HOST, port=PORT, workers=1)
