# recap-asr

Сервис транскрибации + диаризации для пайплайна recap.
Нативная Windows-служба на GPU-хосте (CUDA), без Docker.

## Что делает

- `POST /v1/audio/transcriptions` (multipart, OpenAI-совместимый) →
  транскрипт (**GigaAM v3** `v3_e2e_rnnt`, русский, пунктуация из коробки) +
  сегменты-реплики с таймкодами, спикеры (pyannote speaker-diarization-3.1),
  флаг `is_owner` по эталону голоса.
- Диаризация выполняется ПЕРВОЙ и служит VAD'ом: транскрибируются только речевые
  интервалы — музыка ожидания и тишина в ASR не попадают. Длинные интервалы
  режутся на куски ≤23.5 c по самым тихим местам (short-form лимит GigaAM ~25 c).
- Записи короче `RECAP_MIN_DURATION` (5 c) не транскрибируются: ответ
  `{"segments": [], "skipped": "too_short"}`.
- `GET /health` — статус, движок, загруженность моделей, наличие эталона.
- Модели выгружаются из VRAM после каждого запроса (`RECAP_UNLOAD_AFTER`) и по
  простою (`RECAP_TTL`, 300 c) — VRAM делится с LLM у Ollama.

## Параметры запроса

| Поле | Дефолт | Смысл |
|---|---|---|
| `file` | — | аудио/видео (m4a/mp3/wav/webm/mp4/…, декодирует PyAV) |
| `language` | `ru` | GigaAM только русский |
| `diarize` | `true` | выключить диаризацию: `false` |
| `num_speakers` | — | подсказка (для звонков — 2) |

Ответ: `{text, language, duration, segments[{start,end,text,speaker}],
speakers[{label,is_owner,owner_score,talk_time}], diarization, processing_time}`.
Ошибка диаризации не валит транскрипцию: `diarization:false` + `diarization_error`.
Нечитаемый файл — HTTP 422.

## Конфигурация (env)

| Переменная | Дефолт | Смысл |
|---|---|---|
| `RECAP_MODEL` | `v3_e2e_rnnt` | модель GigaAM |
| `RECAP_GIGAAM_CACHE` | `gigaam-cache/` рядом с кодом | кэш весов GigaAM |
| `HF_HOME`, `HF_TOKEN` | — | кэш и токен Hugging Face (pyannote) |
| `RECAP_OWNER_REF` | `owner_ref.npy` рядом с кодом | эталон голоса владельца |
| `RECAP_OWNER_THRESHOLD` | `0.5` | порог косинусной близости для `is_owner` |
| `RECAP_MIN_DURATION` | `5` | секунды; короче — `skipped` |
| `RECAP_TTL`, `RECAP_UNLOAD_AFTER` | `300`, `1` | выгрузка моделей из VRAM |
| `RECAP_FFMPEG_DIR` | `bin/` рядом с кодом | каталог с `ffmpeg.exe` |
| `RECAP_HOST`, `RECAP_PORT` | `0.0.0.0`, `8000` | адрес службы |

## Установка

Каталог установки — любой (далее `<base>`), в нём: `server.py`, `enroll.py`,
`venv/`, `bin/ffmpeg.exe`, `recap-asr-service.exe` (WinSW 2.x) + `recap-asr-service.xml`.

1. Python 3.12, venv: сначала torch/torchaudio 2.8.0 с индекса cu128, затем
   `requirements.txt`, затем GigaAM (из zip-архива, если нет git) — команды в
   комментариях `requirements.txt`.
2. `bin\ffmpeg.exe` — любая статическая сборка ffmpeg. gigaam зовёт бинарь по
   имени даже для wav, а у службы нет пользовательского PATH.
3. Hugging Face: принять условия `pyannote/speaker-diarization-3.1` и
   `pyannote/segmentation-3.0`, read-токен — в `HF_TOKEN` в xml службы.
4. Эталон голоса (ниже), затем `recap-asr-service.exe install` и `start`.
5. Firewall: порт 8000/tcp только для локальной сети — аутентификации в сервисе нет.

В xml пути заданы через `%BASE%` (каталог exe), править их не нужно.

## Эталон голоса владельца

`owner_ref.npy` строится один раз:

```
venv\Scripts\python.exe enroll.py call1.m4a call2.m4a call3.m4a
```

На вход — 2–3 исходящих звонка разным контактам. Каждый диаризуется на два
спикера, спикер с повторяющимся голосовым отпечатком во всех звонках — владелец.
Скрипт печатает sanity-check (owner vs other) — разрыв должен быть явным.

## Грабли (проверено на живой установке)

- `torchaudio==2.8.0`, не новее: в 2.9 выпилен `AudioMetaData`, pyannote 3.3.2 падает на импорте.
- `huggingface_hub<1.0`: в 1.0 выпилен `use_auth_token`, который pyannote 3.3.2 передаёт.
- `matplotlib` обязателен: pyannote 3.3.2 импортирует его безусловно.
- torch ≥2.6 не грузит чекпоинты pyannote из-за дефолта `weights_only` —
  server.py ставит `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1` (источник моделей доверенный).
- **WinGet Links** (reparse-каталог в PATH) роняет любой скан PATH ошибкой 448
  «недоверенная точка подключения» — server.py вычищает его из PATH процесса.
- Кэш весов gigaam закреплён `RECAP_GIGAAM_CACHE`: дефолтный `~/.cache/gigaam`
  у службы LocalSystem уезжает в systemprofile.
- `transcribe_longform` gigaam на Windows не работает (HFValidationError на локальном
  пути с бэкслешами) — вместо него свой чанкер по речевым интервалам диаризации.

## Модели HF

- `pyannote/speaker-diarization-3.1` и её зависимость `pyannote/segmentation-3.0` —
  gated: на HF принять условия обеих, токен (read) — в env `HF_TOKEN` службы.
- `pyannote/wespeaker-voxceleb-resnet34-LM` — эмбеддинги для идентификации владельца.
- Кэш — `HF_HOME`, качается при первом обращении.
