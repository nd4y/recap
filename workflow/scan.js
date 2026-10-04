// Scan: обход /data/recordings, отбор кандидатов, claim в реестре, парсинг метаданных.
const fs = require('fs');
const path = require('path');

const REC = '/data/recordings';
const STATE = '/data/state/registry.json';
// + видео-контейнеры: записи встреч (Jitsi пишет .webm), звук достаёт PyAV на стороне ASR
const AUDIO = new Set(['.m4a', '.mp3', '.wav', '.ogg', '.opus', '.amr', '.aac', '.flac', '.wma', '.webm', '.mp4', '.mkv']);
const BATCH = parseInt($env.RECAP_BATCH || '10');
const now = Date.now();

let reg = {};
try { reg = JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch (e) {}

const files = [];
(function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name.startsWith('.')) continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else files.push(p);
  }
})(REC);

const candidates = [];
const exhausted = []; // файлы, у которых кончились попытки на этом тике
for (const p of files) {
  if (!AUDIO.has(path.extname(p).toLowerCase())) continue;
  const st = fs.statSync(p);
  const rel = path.relative(REC, p).replace(/\\/g, '/');
  const r = reg[rel];
  if (r) {
    if (['done', 'failed', 'skipped'].includes(r.status)) continue;
    if (r.status === 'processing' && now - (r.claimed || 0) < 2 * 3600e3) continue;
    if ((r.attempts || 0) >= 3) {
      // три claim'а подряд протухли (workflow падал после ASR): failed навсегда
      r.status = 'failed';
      r.error = r.error || r.last_error || 'attempts exhausted';
      r.ts = now;
      exhausted.push(rel);
      continue;
    }
  }
  if (!rel.startsWith('Telegram/') && now - st.mtimeMs < 3 * 60e3) continue; // возможно, ещё дозаливается
  candidates.push({ path: p, rel, size: st.size, mtimeMs: st.mtimeMs });
}

candidates.sort((a, b) => a.mtimeMs - b.mtimeMs);
const batch = candidates.slice(0, BATCH);

const pad = (n) => String(n).padStart(2, '0');
// Явный сдвиг локального времени от UTC в часах (RECAP_TZ_OFFSET_HOURS, без DST):
// task runner n8n не наследует TZ контейнера, поэтому getHours() врёт — считаем руками
const TZ_OFFSET_MS = parseFloat($env.RECAP_TZ_OFFSET_HOURS || '0') * 3600e3;
const localParts = (ms) => {
  const d = new Date(ms + TZ_OFFSET_MS);
  return {
    date: `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`,
    time: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`,
  };
};
function parseMeta(f) {
  const parts = f.rel.split('/');
  const sub = parts.length > 1 ? parts[0] : '';
  // все записи встреч (Jitsi, Teams и прочие) лежат в одном подкаталоге Meetings
  const typeMap = { CallRecords: 'call', Meetings: 'meeting', Voice: 'voice_note', Telegram: 'voice_note' };
  const type = typeMap[sub] || 'generic';
  const base = path.basename(f.rel).replace(/\.[^.]+$/, '');
  const meta = { type, filename: base };
  if (sub === 'Telegram') {
    const sidecar = JSON.parse(fs.readFileSync(f.path + '.json', 'utf8'));
    meta.telegram = sidecar.telegram;
  }
  const m = base.match(/^(.*?)[ _]?(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})_(out|inc)$/);
  if (type === 'call' && m) {
    meta.date = m[2];
    meta.time = `${m[3]}:${m[4]}`;
    meta.datetime = `${m[2]}T${m[3]}:${m[4]}`;
    meta.direction = m[6];
    meta.contact_raw = m[1].trim().replace(/ @$/, '');
    const cm = meta.contact_raw.match(/^(.+?) \((.+?)\)/);
    if (cm) { meta.contact = cm[1].trim(); meta.region = cm[2].trim(); }
    else { meta.contact = meta.contact_raw; meta.region = ''; }
  } else {
    // Jitsi кладёт UTC-таймштамп в имя (..._2026-09-01T08_24_55.582Z.webm) — он
    // авторитетнее mtime; конвертируется в локальное время по RECAP_TZ_OFFSET_HOURS
    const iso = base.match(/(\d{4})-(\d{2})-(\d{2})T(\d{2})_(\d{2})_(\d{2})/);
    const ms = iso
      ? Date.UTC(+iso[1], +iso[2] - 1, +iso[3], +iso[4], +iso[5], +iso[6])
      : (meta.telegram && meta.telegram.received_date ? meta.telegram.received_date * 1000 : f.mtimeMs);
    const lp = localParts(ms);
    meta.date = lp.date;
    meta.time = lp.time;
    meta.datetime = `${meta.date}T${meta.time}`;
  }
  return meta;
}

const metas = batch.map(parseMeta);
batch.forEach((f, i) => {
  reg[f.rel] = Object.assign(reg[f.rel] || {}, {
    status: 'processing', claimed: now,
    attempts: ((reg[f.rel] || {}).attempts || 0) + 1,
    size: f.size, mtime: f.mtimeMs,
    type: metas[i].type, // для метрик (recap-exporter): тип без повторного разбора пути
  });
});
fs.mkdirSync('/data/state', { recursive: true });
fs.writeFileSync(STATE, JSON.stringify(reg, null, 1));

// переход в failed по исчерпанию попыток — сообщение в Telegram (если бот настроен)
if (exhausted.length && $env.TELEGRAM_BOT_TOKEN && $env.TELEGRAM_CHAT_ID) {
  for (const rel of exhausted) {
    try {
      await this.helpers.httpRequest({
        method: 'POST',
        url: `https://api.telegram.org/bot${$env.TELEGRAM_BOT_TOKEN}/sendMessage`,
        body: { chat_id: $env.TELEGRAM_CHAT_ID, text: `recap: файл помечен failed после 3 попыток\n${rel}\n${reg[rel].error}` },
        json: true, timeout: 30000,
      });
    } catch (e) { /* алерт не должен ронять тик */ }
  }
}

return batch.map((f, i) => ({ json: Object.assign({}, f, metas[i]) }));
