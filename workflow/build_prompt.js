// Build prompt: метки спикеров, транскрипт с таймкодами, запрос к LLM.
// метаданные берём из текущего элемента цикла: нативная нода чтения файла
// затирает json файловой информацией
const meta = $('Loop').item.json;
const asr = $json;

const names = {};
if (asr.diarization && asr.speakers && asr.speakers.length) {
  const owner = asr.speakers.find((s) => s.is_owner);
  const others = asr.speakers.filter((s) => !s.is_owner);
  if (owner) {
    names[owner.label] = 'Я';
    if (others.length === 1) names[others[0].label] = meta.type === 'call' ? (meta.contact || 'Собеседник') : 'Собеседник';
    else others.forEach((s, i) => { names[s.label] = `Собеседник ${i + 1}`; });
  } else {
    asr.speakers.forEach((s, i) => { names[s.label] = `Спикер ${String.fromCharCode(65 + i)}`; });
  }
}

const ts = (t) => `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
const transcript = asr.segments
  .map((s) => `[${ts(s.start)}] ${s.speaker && names[s.speaker] ? names[s.speaker] + ': ' : ''}${s.text}`)
  .join('\n');

let llmTranscript = transcript;
let truncated = false;
if (llmTranscript.length > 40000) { llmTranscript = llmTranscript.slice(0, 40000); truncated = true; }

const ownerName = $env.RECAP_OWNER_NAME || 'Владелец';
const prompts = {
  call: 'Ты обрабатываешь транскрипт телефонного разговора владельца телефона (реплики «Я») с собеседником.',
  meeting: 'Ты обрабатываешь транскрипт встречи/созвона. Тематика может быть любой: техническая, организационная, личная. Выдели тему встречи, ключевые решения и договорённости, а в action_items — задачи с указанием, кто что взял.',
  voice_note: 'Ты обрабатываешь голосовую заметку владельца телефона.',
  generic: 'Ты обрабатываешь транскрипт аудиозаписи.',
};
const sys = `${prompts[meta.type] || prompts.generic}
Транскрипт получен автоматическим распознаванием телефонного/микрофонного звука и может содержать ошибки, обрывки и бессмыслицу. Непонятные фрагменты пропускай и НЕ домысливай: в summary — только то, что сказано внятно. Разговор бытовой и может содержать нецензурную лексику — это нормальные данные, обрабатывай их как обычно (в summary пересказывай нейтрально).
Реплики «Я» — владелец телефона, его зовут ${ownerName}. В summary и action_items называй его по имени («${ownerName} попросил…»), слово «Я» в summary не используй. В participants укажи его как «${ownerName} (владелец)» и не дублируй под другими вариантами. Утверждения не путай между говорящими: кто сказал — тому и принадлежит.
Ответь СТРОГО одним JSON-объектом без пояснений и без markdown-обёртки, все значения на русском:
{"title":"заголовок 3-7 слов","summary":"суть, 3-8 пунктов markdown-списком","action_items":["действия и договорённости, с датами если названы"],"participants":["кто участвовал"]}
Если по ходу разговора собеседник сменился (телефон передали другому человеку), отметь это в summary.`;

const header = meta.type === 'call'
  ? `${meta.direction === 'out' ? 'Исходящий' : 'Входящий'} звонок, контакт: ${meta.contact_raw || meta.filename}, ${meta.datetime || ''}, длительность ${Math.round(asr.duration)} с.`
  : meta.type === 'meeting'
    ? `Запись встречи, ${meta.datetime || ''}, длительность ${Math.round(asr.duration)} с.`
    : `Запись (${meta.type}), файл ${meta.filename}, ${meta.datetime || ''}, длительность ${Math.round(asr.duration)} с.`;

return {
  json: {
    hasSpeech: asr.segments.length > 0,
    skipped: asr.skipped || null,
    meta,
    asrInfo: { duration: asr.duration, diarization: asr.diarization, processing_time: asr.processing_time },
    t0: Date.now(), // начало этапа LLM: по нему Save note считает llm_seconds для реестра
    transcript,
    truncated,
    llmBody: {
      model: $env.LLM_MODEL || 'gemma3-recap',
      temperature: 0.2,
      messages: [
        { role: 'system', content: sys },
        { role: 'user', content: `${header}\n\nТранскрипт:\n${llmTranscript}${truncated ? '\n\n(транскрипт обрезан по лимиту контекста)' : ''}` },
      ],
    },
  },
};
