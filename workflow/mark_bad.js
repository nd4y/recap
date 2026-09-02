// Mark bad: ASR-нода упала на этом файле. 4xx = файл битый -> failed навсегда;
// прочее (timeout, connection refused, 5xx) = транзиент -> вернуть в очередь,
// не сжигая попытку (GPU-хост может быть просто выключен).
// Переход в failed — отдельное сообщение в Telegram (если бот настроен).
const fs = require('fs');
const meta = $('Loop').item.json;
const err = ($json && $json.error) || {};
const http = parseInt(err.httpCode || err.status || 0) || 0;
const msg = String(err.message || err.description || 'unknown').slice(0, 300);

const STATE = '/data/state/registry.json';
const reg = JSON.parse(fs.readFileSync(STATE, 'utf8'));
const r = reg[meta.rel] || {};
const permanent = http >= 400 && http < 500;
if (permanent) {
  r.status = 'failed';
  r.error = msg;
  r.ts = Date.now();
} else {
  delete r.status;
  delete r.claimed;
  r.attempts = Math.max((r.attempts || 1) - 1, 0);
  r.last_error = msg;
}
reg[meta.rel] = r;
fs.writeFileSync(STATE, JSON.stringify(reg, null, 1));

if (permanent && $env.TELEGRAM_BOT_TOKEN && $env.TELEGRAM_CHAT_ID) {
  try {
    await this.helpers.httpRequest({
      method: 'POST',
      url: `https://api.telegram.org/bot${$env.TELEGRAM_BOT_TOKEN}/sendMessage`,
      body: { chat_id: $env.TELEGRAM_CHAT_ID, text: `recap: файл помечен failed\n${meta.rel}\nASR: ${msg}` },
      json: true, timeout: 30000,
    });
  } catch (e) { /* алерт не должен ронять тик */ }
}
return { json: { rel: meta.rel, permanent, error: msg } };
