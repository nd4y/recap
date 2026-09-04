// Save note: парсинг ответа LLM, заметка в vault, тексты для Telegram, реестр -> noted.
const fs = require('fs');
const path = require('path');

const prev = $('Build prompt').item.json;
const resp = $json;

const r1 = (x) => Math.round((x || 0) * 10) / 10;
// тайминги файла для метрик (recap-exporter): длительность аудио, время ASR, время LLM
const timings = { duration: r1(prev.asrInfo.duration), asr_seconds: r1(prev.asrInfo.processing_time) };

// запись короче порога (RECAP_MIN_DURATION у сервиса): ни заметки, ни Telegram —
// только отметка в реестре
if (prev.skipped) {
  const STATE0 = '/data/state/registry.json';
  const reg0 = JSON.parse(fs.readFileSync(STATE0, 'utf8'));
  reg0[prev.meta.rel] = Object.assign(reg0[prev.meta.rel] || {}, {
    status: 'skipped', reason: prev.skipped, ts: Date.now(),
  }, timings);
  fs.writeFileSync(STATE0, JSON.stringify(reg0, null, 1));
  const it0 = $input.item;
  delete it0.binary;
  it0.json = { rel: prev.meta.rel, skipped: true, tgText: '', note_path: '' };
  return it0;
}

let data = null;
const emptyCall = !prev.hasSpeech;
if (emptyCall) {
  // LLM не вызывалась: речи в записи нет (короткие звонки шлагбауму и т.п.)
  data = { title: 'Пустой звонок', summary: 'Речь не распознана.', action_items: [], participants: [] };
} else {
  // LLM регулярно кладёт в строковые значения сырые переводы строк (markdown-список
  // в summary) — для JSON.parse это управляющие символы и невалидный документ.
  // Экранируем их внутри строк, не трогая структуру.
  const escapeCtl = (s) => {
    let out = '', inStr = false, esc = false;
    for (const ch of s) {
      if (inStr) {
        if (esc) { out += ch; esc = false; continue; }
        if (ch === '\\') { out += ch; esc = true; continue; }
        if (ch === '"') { inStr = false; out += ch; continue; }
        if (ch === '\n') { out += '\\n'; continue; }
        if (ch === '\r') { continue; }
        if (ch === '\t') { out += '\\t'; continue; }
        out += ch;
      } else {
        if (ch === '"') inStr = true;
        out += ch;
      }
    }
    return out;
  };
  const tryParse = (raw) => {
    let c = (raw || '').trim().replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();
    const m = c.match(/\{[\s\S]*\}/);
    for (const cand of [c, m && m[0], escapeCtl(c), m && escapeCtl(m[0])]) {
      if (!cand) continue;
      try { return JSON.parse(cand); } catch (e) { /* следующий вариант */ }
    }
    return null;
  };
  data = tryParse(resp.choices && resp.choices[0] && resp.choices[0].message && resp.choices[0].message.content);
  if (!data || typeof data !== 'object') {
    // одна повторная попытка с ужесточённым требованием формата — gemma изредка
    // ломает JSON на длинных/разговорных транскриптах.
    // Таймаут короче лимита task runner'а (N8N_RUNNERS_TASK_TIMEOUT, в compose 600 с):
    // иначе runner убивает всю ноду, файл виснет в processing, а не уходит в фолбек
    const body = JSON.parse(JSON.stringify(prev.llmBody));
    body.messages.push({ role: 'user', content: 'Твой предыдущий ответ не был валидным JSON. Повтори ответ: СТРОГО один валидный JSON-объект указанной структуры, первый символ ответа — {.' });
    try {
      const r2 = await this.helpers.httpRequest({
        method: 'POST',
        url: `${$env.LLM_BASE_URL}/chat/completions`,
        headers: { Authorization: `Bearer ${$env.LLM_API_KEY || ''}` },
        body, json: true, timeout: 240000,
      });
      data = tryParse(r2.choices && r2.choices[0] && r2.choices[0].message && r2.choices[0].message.content);
    } catch (e) { /* ретрай не удался — ниже сработает фолбек */ }
  }
  if (!data || typeof data !== 'object') {
    data = { title: prev.meta.filename, summary: '(LLM вернула невалидный JSON, summary отсутствует)', action_items: [], participants: [] };
  }
  // время этапа LLM (включая ретрай) — от выхода из Build prompt до этой точки
  if (prev.t0) timings.llm_seconds = r1((Date.now() - prev.t0) / 1000);
}

const fmtDur = (s) => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const dt = prev.meta.datetime || '';
const [d, t] = dt.split('T');
const year = (d || '0000').slice(0, 4);
const sanitize = (s) => String(s).replace(/[:\\/\*\?"<>\|#\^\[\]\r\n]/g, '').trim().slice(0, 80);
const title = sanitize(data.title || prev.meta.filename) || 'Запись';

// Заметка пишется в outbox; в vault её переносит задача планировщика NAS
// (stack/deliver-outbox.sh) через штатный API NAS — прямые записи в ФС клиент
// синхронизации NAS не видит (проверено), а операции через API генерят события синка.
const vaultDir = `/data/vault/Notes/Recordings/${year}`;
const outDir = `/data/state/outbox/${year}`;
fs.mkdirSync(outDir, { recursive: true });
const baseName = `${d} ${(t || '').replace(':', '')} — ${title}`;
let name = `${baseName}.md`;
let n = 2;
while (fs.existsSync(path.join(outDir, name)) || fs.existsSync(path.join(vaultDir, name))) {
  name = `${baseName} -${n}.md`; n++;
}
const outFile = path.join(outDir, name);
const full = path.join(vaultDir, name); // финальный путь в vault — он идёт в реестр

const fm = [
  '---',
  `type: ${prev.meta.type}`,
  `date: ${dt}`,
  ...(prev.meta.type === 'call'
    ? [`direction: ${prev.meta.direction}`, `contact: "${prev.meta.contact || ''}"`, `region: "${prev.meta.region || ''}"`]
    : []),
  `duration: "${fmtDur(prev.asrInfo.duration)}"`,
  `source: "${prev.meta.rel}"`,
  `tags: [recording/${prev.meta.type}]`,
  '---',
].join('\n');

const actionsMd = (data.action_items || []).map((a) => `- [ ] ${a}`).join('\n');
const participantsMd = (data.participants || []).length ? `Участники: ${(data.participants || []).join(', ')}\n\n` : '';
const note = `${fm}\n\n# ${title}\n\n${participantsMd}## Суть\n\n${data.summary || ''}\n\n${actionsMd ? `## Действия\n\n${actionsMd}\n\n` : ''}## Транскрипт\n\n${prev.transcript}\n`;
fs.writeFileSync(outFile, note);

const STATE = '/data/state/registry.json';
const reg = JSON.parse(fs.readFileSync(STATE, 'utf8'));
reg[prev.meta.rel] = Object.assign(reg[prev.meta.rel] || {}, { status: 'noted', note_path: full, ts: Date.now() }, timings);
fs.writeFileSync(STATE, JSON.stringify(reg, null, 1));

const typeLabel = { meeting: 'Встреча', voice_note: 'Голосовая заметка', generic: 'Запись' };
const tgTitle = prev.meta.type === 'call'
  ? `${prev.meta.direction === 'out' ? 'Исходящий звонок' : 'Входящий звонок'} — ${prev.meta.contact_raw || prev.meta.filename}`
  : `${typeLabel[prev.meta.type] || 'Запись'} — ${title}`;
let tgText = `${tgTitle}\n${prev.meta.date || ''} ${prev.meta.time || ''}, ${fmtDur(prev.asrInfo.duration)}\n\n${data.summary || ''}`;
if (actionsMd) tgText += `\n\nДействия:\n${(data.action_items || []).map((a) => `- ${a}`).join('\n')}`;
if (tgText.length > 4000) tgText = tgText.slice(0, 3990) + '…';
if (emptyCall) tgText = ''; // пустые звонки в Telegram не шлём (IF-нода отсеет)
// отсечка по возрасту записи: старые (бэклог) — только в vault, без Telegram
const maxAgeDays = parseFloat($env.RECAP_TG_MAX_AGE_DAYS || '2');
const recTs = Date.parse(prev.meta.datetime || '');
if (!isNaN(recTs) && (Date.now() - recTs) / 86400e3 > maxAgeDays) tgText = '';

const it = $input.item;
delete it.binary;
it.json = {
  rel: prev.meta.rel,
  note_path: full,
  title,
  tgText,
  transcriptDoc: `${tgTitle}\n${prev.meta.date || ''} ${prev.meta.time || ''}\n\n${prev.transcript}\n`,
};
return it;
