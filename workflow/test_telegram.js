// Dependency-free integration checks for Code nodes; no real files or Telegram messages.
const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const source = fs.readFileSync(path.join(__dirname, 'telegram_ingest.js'), 'utf8');
const code = new AsyncFunction('require', '$env', 'console', source);
const offset = '/data/state/telegram-offset.json';
const dir = '/data/recordings/Telegram/';
const env = { TELEGRAM_BOT_TOKEN: 'test-token', TELEGRAM_CHAT_ID: '42' };
function harness(updates, broken = false) {
  const store = new Map(), calls = [];
  let failRename = false;
  const fakeFs = {
    existsSync: (p) => store.has(p),
    readFileSync: (p) => store.get(p), mkdirSync() {},
    writeFileSync: (p, v) => store.set(p, v),
    renameSync(a, b) { if (failRename && b.endsWith('.ogg')) throw Error('disk'); store.set(b, store.get(a)); store.delete(a); },
  };
  const context = { helpers: { async httpRequest(o) {
    calls.push(o);
    const method = o.url.split('/').pop();
    if (method === 'getUpdates') return { ok: true, result: updates.filter(u => u.update_id >= o.body.offset) };
    if (method === 'getFile') {
      if (broken) throw Error('secret URL test-token');
      return { ok: true, result: { file_path: 'voice/file.ogg' } };
    }
    if (method === 'sendMessage') return { ok: true, result: { message_id: 100 } };
    assert.equal(o.encoding, 'arraybuffer');
    return Buffer.from([0, 255, 128, 1]);
  } } };
  return { store, calls, setFail(v) { failRename = v; },
    run: () => code.call(context, n => n === 'fs' ? fakeFs : require(n), env, { warn() {} }) };
}
const message = (id, extra = {}) => ({ update_id: id, message: { chat: { type: 'private', id: 42 },
  message_id: id, date: 1800000000, voice: { file_id: 'voice', file_size: 4 }, ...extra } });
(async () => {
  const h = harness([message(1, { forward_origin: { type: 'hidden_user', sender_user_name: 'Другой', date: 1 } }),
    message(2, { voice: undefined, video_note: { file_id: 'video', file_size: 4 } }),
    message(3, { chat: { type: 'private', id: 99 } }),
    message(4, { voice: { file_id: 'large', file_size: 21 * 1024 * 1024 } })]);
  await h.run();
  assert.deepEqual(h.store.get(dir+'42_1.ogg'), Buffer.from([0, 255, 128, 1]));
  assert(h.store.has(dir+'42_2.mp4'));
  assert.equal(JSON.parse(h.store.get(dir+'42_1.ogg.json')).telegram.author, 'Другой');
  assert.equal(h.calls.filter(c => c.url.endsWith('/getFile')).length, 2);
  assert.equal(JSON.parse(h.store.get(offset)).offset, 5);
  await h.run();
  assert.equal(h.calls.filter(c => c.url.endsWith('/getFile')).length, 2);
  // Crash after sidecar write, before media rename: replay repairs the download.
  const disk = harness([message(1)]); disk.setFail(true); await disk.run();
  assert(!disk.store.has(offset)); assert(!disk.store.has(dir+'42_1.ogg'));
  disk.setFail(false); await disk.run(); assert(disk.store.has(dir+'42_1.ogg'));
  // Permanent download failure is retried, then rejected without blocking later updates.
  const bad = harness([message(1)], true);
  for (let i = 0; i < 3; i++) { await bad.run(); assert(!bad.store.has(offset)); }
  await bad.run(); assert.equal(JSON.parse(bad.store.get(offset)).offset, 2);
  assert.equal(bad.calls.filter(c => c.url.endsWith('/sendMessage')).length, 1);
  const help = harness([message(1, { voice: undefined, text: '/help' })]);
  await help.run(); assert.equal(help.calls.at(-1).body.reply_parameters.message_id, 1);
  const prompt = new AsyncFunction('$', '$json', '$env', fs.readFileSync(path.join(__dirname,'build_prompt.js'),'utf8'));
  const result = await prompt(() => ({item:{json:{type:'voice_note', telegram:{kind:'voice',author:null}}}}),
    {segments:[{start:0,text:'Завтра пришлю документы.'}],duration:2}, {});
  assert(result.json.llmBody.messages[0].content.includes('Автор неизвестен'));
  assert(!result.json.llmBody.messages[0].content.includes('Реплики «Я»'));
  const save = new AsyncFunction('require','$','$json','$env','$input',fs.readFileSync(path.join(__dirname,'save_note.js'),'utf8'));
  const written = new Map([['/data/state/registry.json','{}']]);
  const saveFs = {existsSync:p=>written.has(p),readFileSync:p=>written.get(p),
    writeFileSync:(p,v)=>written.set(p,v),mkdirSync(){}};
  const prev = {meta:{type:'voice_note',rel:'Telegram/42_1.ogg',datetime:'2000-01-01T10:00',
    date:'2000-01-01',time:'10:00',telegram:{message_id:1,kind:'voice'}},asrInfo:{duration:6},hasSpeech:false,transcript:''};
  const executeSave = (response = {}, context = {}) => save.call(context, n=>n==='fs'?saveFs:require(n),()=>({item:{json:prev}}),response, {}, {item:{json:{}}});
  const noSpeech = await executeSave();
  assert(noSpeech.json.tgText.includes('Речь не распознана'));
  assert.equal(noSpeech.json.telegram.message_id,1);
  prev.skipped='too_short';
  const short = await executeSave();
  assert(short.json.skipped);assert(short.json.tgText);assert(short.json.transcriptDoc);
  prev.skipped = null; prev.hasSpeech = true; prev.t0 = Date.now() - 1000;
  prev.meta.size = 1048576; prev.asrInfo.processing_time = 2;
  prev.llmBody = { messages: [] };
  const response = (summary, usage) => ({usage,choices:[{message:{content:JSON.stringify({title:'Проверка',summary,action_items:[],participants:[]})}}]});
  const measured = await executeSave(response('Задача согласована.',{prompt_tokens:100,completion_tokens:20}));
  assert(measured.json.tgText.includes('Токены LLM: 120 (вход 100, выход 20)'));
  assert(measured.json.tgText.includes('Исходный файл: 1,00 МиБ'));
  let registry = JSON.parse(written.get('/data/state/registry.json'))[prev.meta.rel];
  assert.equal(registry.llm_total_tokens,120); assert.equal(registry.file_bytes,1048576);
  assert(registry.processing_seconds >= 3);
  const unreported = await executeSave(response('Задача согласована.'));
  assert(unreported.json.tgText.includes('API не сообщил расход'));
  const long = await executeSave(response('x'.repeat(5000),{prompt_tokens:10,completion_tokens:5}));
  assert(long.json.tgText.length <= 4000); assert(long.json.tgText.includes('Токены LLM: 15'));
  await executeSave({usage:{prompt_tokens:100,completion_tokens:5},choices:[{message:{content:'broken JSON'}}]},
    {helpers:{httpRequest:async()=>response('Повторный ответ.',{prompt_tokens:200,completion_tokens:10})}});
  registry = JSON.parse(written.get('/data/state/registry.json'))[prev.meta.rel];
  assert.equal(registry.llm_total_tokens,315); assert(registry.llm_usage_complete);
  for (const file of ['scan.js','save_note.js','prep_doc.js','telegram_ingest.js'])
    new AsyncFunction(fs.readFileSync(path.join(__dirname,file),'utf8'));
  console.log('PASS: Telegram intake, replay, speaker prompt, replies, resource footer, provider usage, retry token totals, missing usage, long message limit, JS syntax');
})().catch(e => { console.error(e); process.exit(1); });
