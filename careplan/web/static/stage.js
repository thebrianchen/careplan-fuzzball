// Stage view: live visit capture + streamed card + boundary log. Talks to careplan.serve:
//   POST /visit/<case>/turn?speaker=   (16 kHz s16le PCM)   POST /visit/<case>/reset
//   GET  /stream/<case>[?live=1]       (server-sent events: step, tool, item, rejected, done, end)
(() => {
  const CASE = document.getElementById('visit').dataset.case;
  const S = { blocked: ['Blocked', 'bad'], at_risk: ['At risk', 'warn'], likely_ok: ['Likely OK', 'ok'] };
  const $ = id => document.getElementById(id);
  const log = $('log'), turns = $('turns'), vstat = $('vstat');
  let ncross = 0, nlocal = 0, nturns = 0, es = null, t0 = Date.now();
  const fields = new Set();

  // ---- mic: hold a button, speak, release; downsample to 16 kHz in the browser.
  let ctx, proc, chunks = [], rec = false;
  async function mic() {
    if (ctx) return;
    if (!window.isSecureContext || !navigator.mediaDevices) {
      throw new Error('microphone needs https or localhost (open this page directly, not inside a preview frame)');
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream);
    proc = ctx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = e => { if (rec) chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    src.connect(proc); proc.connect(ctx.destination);
  }
  function down(f32, from) {
    const r = from / 16000, n = Math.floor(f32.length / r), out = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      let s = 0, c = 0;
      for (let j = Math.floor(i * r); j < Math.floor((i + 1) * r) && j < f32.length; j++) { s += f32[j]; c++; }
      const v = Math.max(-1, Math.min(1, c ? s / c : 0)); out[i] = v < 0 ? v * 32768 : v * 32767;
    }
    return out;
  }
  async function stop(btn) {
    if (!rec) return;
    rec = false; btn.classList.remove('on');
    if (!chunks.length) { vstat.textContent = 'nothing recorded'; return; }
    const total = chunks.reduce((a, c) => a + c.length, 0), f = new Float32Array(total);
    let o = 0; for (const c of chunks) { f.set(c, o); o += c.length; } chunks = [];
    const pcm = down(f, ctx.sampleRate);
    vstat.textContent = `transcribing ${(pcm.length / 16000).toFixed(1)} s on the private box…`;
    const r = await fetch(`/visit/${CASE}/turn?speaker=${btn.dataset.speaker}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: pcm.buffer });
    if (!r.ok) { vstat.textContent = `transcription failed (${r.status})`; return; }
    const t = await r.json(); nturns++;
    const d = document.createElement('div'); d.className = 'turn';
    d.innerHTML = `<b>${t.speaker}</b>${esc(t.text)}`; turns.appendChild(d); turns.scrollTop = turns.scrollHeight;
    vstat.textContent = `${nturns} turn(s) captured`;
  }
  // Click once to start, click again to stop (toggle). Holding also works: release stops it.
  let active = null;
  async function start(b) {
    try { await mic(); } catch (err) { vstat.textContent = 'mic unavailable: ' + (err.message || err.name); console.error(err); return; }
    if (ctx.state === 'suspended') await ctx.resume();
    chunks = []; rec = true; active = b; b.classList.add('on'); vstat.textContent = `listening (${b.dataset.speaker})… click again to stop`;
  }
  for (const b of document.querySelectorAll('button.talk')) {
    let pressedAt = 0;
    b.addEventListener('mousedown', e => { e.preventDefault(); if (rec && active === b) { stop(b); active = null; } else if (!rec) { pressedAt = Date.now(); start(b); } });
    b.addEventListener('mouseup', () => { if (rec && active === b && Date.now() - pressedAt > 600) { stop(b); active = null; } });  // a long hold releases
    b.addEventListener('touchstart', e => { e.preventDefault(); if (rec && active === b) { stop(b); active = null; } else if (!rec) start(b); }, { passive: false });
  }
  document.addEventListener('keydown', e => {             // space bar toggles the patient mic; D toggles the doctor's
    if (e.target.tagName === 'INPUT') return;
    const b = e.code === 'Space' ? document.querySelector('[data-speaker=patient]') : e.key.toLowerCase() === 'd' ? document.querySelector('[data-speaker=doctor]') : null;
    if (!b) return; e.preventDefault();
    if (rec && active === b) { stop(b); active = null; } else if (!rec) start(b);
  });
  $('clip').onclick = async () => {           // fallback: a recorded line through the real pipeline
    vstat.textContent = 'transcribing the recorded line on the private box…';
    const r = await fetch(`/visit/${CASE}/clip?name=turn_T02&speaker=patient`, { method: 'POST' });
    if (!r.ok) { vstat.textContent = `no recorded line (${r.status})`; return; }
    const t = await r.json(); nturns++;
    const d = document.createElement('div'); d.className = 'turn'; d.innerHTML = `<b>${t.speaker}</b>${esc(t.text)}`; turns.appendChild(d);
    vstat.textContent = `${nturns} turn(s) captured`;
  };
  $('reset').onclick = async () => { await fetch(`/visit/${CASE}/reset`, { method: 'POST' }); turns.innerHTML = ''; nturns = 0; vstat.textContent = 'visit cleared'; };

  // ---- the check
  $('go').onclick = () => {
    if (es) return;
    $('recbadge').classList.remove('on');
    $('go').disabled = true; t0 = Date.now();
    vstat.textContent = nturns ? 'checking the plan: written visit + what was just said' : 'checking the plan against the written visit';
    es = new EventSource(`/stream/${CASE}${nturns ? '?live=append' : ''}`);   // live lines land on top of the visit so far
    es.onmessage = onmsg;
  };
  $('replay').onclick = () => {              // fallback: a saved run of the same pipeline, replayed with its real timing
    if (es) return;
    $('go').disabled = true; t0 = Date.now();
    vstat.textContent = 'recorded run (saved earlier from this same pipeline)';
    es = new EventSource(`/stream/${CASE}?replay=1`);
    es.onmessage = onmsg;
  };
  setInterval(() => { if (es) $('clock').textContent = ((Date.now() - t0) / 1000).toFixed(0) + ' s'; }, 500);

  function row(cls, t, html) {
    const d = document.createElement('div'); d.className = 'row ' + cls;
    d.innerHTML = `<span class='t'>${t} s</span><span class='what'>${html}</span>`; log.appendChild(d); log.scrollTop = log.scrollHeight;
  }
  function onmsg(m) {
    const ev = JSON.parse(m.data);
    if (ev.type === 'recorded') { $('recbadge').classList.add('on'); return; }
    if (ev.type === 'tool') {
      if (ev.tool === 'find_resources') {
        ncross++; Object.keys(ev.args || {}).forEach(k => fields.add(k));
        row('cross', ev.t, `<span class='tag b1'>→ public box</span>${esc(JSON.stringify(ev.args))}${ev.n_results != null ? ` → ${ev.n_results} records` : ''}`);
        $('ncross').textContent = ncross; $('nfields').textContent = [...fields].join(', ');
      } else {
        nlocal++; row('local', ev.t, `<span class='tag b2'>the private box</span>${ev.tool}${ev.args && ev.args.appt_ref ? ' ' + ev.args.appt_ref : ''}${ev.prefetched ? ' (pre-loaded)' : ''}`);
        $('nlocal').textContent = nlocal;
      }
    }
    if (ev.type === 'item') {
      const it = ev.item, d = $('i-' + it.item_id); if (!d) return;
      const s = S[it.status] || [it.status, 'wait'];
      d.classList.add('on'); d.style.borderColor = `var(--${s[1]})`;
      d.querySelector('.pill').outerHTML = `<span class='pill ${s[1]}'>${s[0]}</span>`;
      d.querySelector('.why').textContent = (it.barriers || []).map(b => b.barrier.replace('_', ' ') + ': ' + b.reason).join(' · ');
      const a = (it.alternatives || [])[0]; if (a) d.querySelector('.alt').innerHTML = `<b>Try:</b> ${esc(a.text)}`;
    }
    if (ev.type === 'end') {
      es.close(); es = null; $('go').disabled = false;
      $('end').innerHTML = ev.ok ? `<a href='/card/${CASE}'>Full card with evidence →</a> · <a href='/summary/${CASE}'>Patient summary →</a>` : `<span style='color:var(--bad)'>${esc(ev.error || 'failed')}</span>`;
    }
  }
  // beat 3: the red-team task array's live tally (partial shard results on the private box)
  async function redteam() {
    try { const r = await (await fetch('/redteam/live')).json();
      const a = r.single_agent, b = r.split;
      $('rt').textContent = (a.runs || b.runs) ? `single agent ${a.leaked}/${a.runs} leaked · split ${b.leaked}/${b.runs}` : 'not running';
    } catch (e) { /* keep last value */ }
  }
  redteam(); setInterval(redteam, 5000);
  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
})();
