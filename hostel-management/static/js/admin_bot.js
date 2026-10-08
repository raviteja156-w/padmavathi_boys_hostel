// Admin Bot — chat helper on the admin dashboard.
// Flows: Check Vacancy | Make Empty | Calculate per-day rent | AI Assistant (typed or spoken, English / Telugu)
(function () {
  const fab = document.getElementById('botFab');
  const panel = document.getElementById('botPanel');
  const body = document.getElementById('botBody');
  const form = document.getElementById('botForm');
  const input = document.getElementById('botInput');
  const send = document.getElementById('botSend');
  if (!fab || !panel) return;

  const micBtn = document.getElementById('botMic');
  const langBtn = document.getElementById('botLang');
  const speakBtn = document.getElementById('botSpeak');
  let assistantMode = false;  // free-text / voice chat with the AI assistant
  let autoRead = false;       // read assistant replies aloud
  let sttLang = 'en-IN';      // speech-recognition language (EN / తె toggle)
  let greeted = false;
  let pendingText = null;     // callback waiting for typed text
  let token = 0;              // bumps on restart so stale async replies are ignored

  const rupee = (n) => '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function scroll() { body.scrollTop = body.scrollHeight; }
  function botSay(html) { const d = document.createElement('div'); d.className = 'bot-msg bot'; d.innerHTML = html; body.appendChild(d); scroll(); return d; }
  function meSay(text) { const d = document.createElement('div'); d.className = 'bot-msg me'; d.textContent = text; body.appendChild(d); scroll(); }

  function chips(options) {
    // options: [{label, cls, onClick}]
    const wrap = document.createElement('div');
    wrap.className = 'bot-chips';
    options.forEach((o) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'bot-chip ' + (o.cls || '');
      b.innerHTML = o.label;
      b.addEventListener('click', () => {
        wrap.classList.add('used');
        meSay(o.say || b.textContent.trim());
        o.onClick();
      });
      wrap.appendChild(b);
    });
    body.appendChild(wrap);
    scroll();
  }

  function askText(placeholder, cb, inputmode) {
    assistantMode = false; if (micBtn) micBtn.disabled = true;   // a menu flow is taking over the text box
    pendingText = cb;
    input.disabled = false; send.disabled = false;
    input.placeholder = placeholder;
    input.inputMode = inputmode || 'text';
    input.value = '';
    setTimeout(() => input.focus(), 50);
  }
  function lockText() { if (assistantMode) return; pendingText = null; input.disabled = true; send.disabled = true; input.value = ''; input.placeholder = 'Pick an option above…'; }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const v = input.value.trim();
    if (assistantMode && !pendingText) { if (!v) return; input.value = ''; meSay(v); return askAssistant({ message: v }); }
    if (!v || !pendingText) return;
    const cb = pendingText;
    lockText();
    meSay(v);
    cb(v);
  });

  async function api(url, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    if (o.body) { o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.headers['X-CSRF-Token'] = panel.dataset.csrf; o.body = JSON.stringify(o.body); }
    try {
      const r = await fetch(url, o);
      const data = await r.json();
      if (!data.ok) return { ok: false, error: data.error || 'Something went wrong.' };
      return data;
    } catch (err) {
      return { ok: false, error: 'Could not reach the server. Please try again.' };
    }
  }

  // ---------- menu ----------
  function menu(intro) {
    if (intro) botSay(intro);
    chips([
      { label: '<i class="fa-solid fa-bed mr-1"></i> 1) Check Vacancy', say: 'Check Vacancy', onClick: flowVacancy },
      { label: '<i class="fa-solid fa-door-open mr-1"></i> 2) Make Empty', say: 'Make Empty', onClick: () => pickHostel('empty') },
      { label: '<i class="fa-solid fa-calculator mr-1"></i> 3) Calculate Per-Day Rent', say: 'Calculate Per-Day Rent', onClick: () => pickHostel('rent') },
      { label: '<i class="fa-solid fa-wand-magic-sparkles mr-1"></i> 4) AI Assistant (English / తెలుగు)', say: 'AI Assistant', onClick: enterAssistant },
    ]);
  }
  function backToMenu() { lockText(); chips([{ label: '<i class="fa-solid fa-list mr-1"></i> Back to menu', say: 'Back to menu', onClick: () => menu('What next, boss?') }]); }

  // ---------- 1) vacancy ----------
  async function flowVacancy() {
    const my = token;
    const wait = botSay('Checking all rooms…');
    const d = await api('/admin/api/bot/vacancy');
    if (my !== token) return;
    wait.remove();
    if (!d.ok) { botSay(esc(d.error)); return backToMenu(); }
    let html = '';
    d.hostels.forEach((h) => {
      html += `<div class="mt-1"><b>${esc(h.name)}</b> — ${h.vacant} vacant (${h.occupied}/${h.capacity})</div>`;
      html += '<table class="bot-table"><tr><th>Room</th><th class="n">Occupied</th><th class="n">Vacant</th></tr>';
      h.rooms.forEach((r) => {
        if (!r.assigned) {
          html += `<tr><td>${esc(r.label)}</td><td class="n" colspan="2"><span class="bot-pill na">no students assigned</span></td></tr>`;
        } else {
          const cls = r.vacant > 0 ? 'free' : 'full';
          html += `<tr><td>${esc(r.label)}</td><td class="n">${r.occupied}/${r.sharing}</td><td class="n"><span class="bot-pill ${cls}">${r.vacant}</span></td></tr>`;
        }
      });
      html += '</table>';
    });
    html += `<div class="mt-2"><b>Total beds</b> (assigned rooms): ${d.total.capacity}<br><b>Occupied:</b> ${d.total.occupied}<br><b>Vacant:</b> ${d.total.vacant}</div>`;
    botSay(html);
    backToMenu();
  }

  // ---------- shared: hostel -> room -> member ----------
  function pickHostel(mode) {
    botSay(mode === 'empty' ? 'Which hostel?' : 'Which hostel is the student in?');
    chips([
      { label: '<i class="fa-solid fa-house mr-1"></i> Old Hostel', say: 'Old Hostel', onClick: () => askRoom(mode, 'Old Hostel') },
      { label: '<i class="fa-solid fa-city mr-1"></i> New Hostel', say: 'New Hostel', onClick: () => askRoom(mode, 'New Hostel') },
    ]);
  }

  function askRoom(mode, hostel) {
    botSay(hostel === 'New Hostel' ? 'Type the room number (1-10, or <b>Hall</b>):' : 'Type the room number (1-10):');
    askText('Room number…', async (val) => {
      const my = token;
      const d = await api('/admin/api/bot/room?hostel=' + encodeURIComponent(hostel) + '&room=' + encodeURIComponent(val));
      if (my !== token) return;
      if (!d.ok) { botSay(esc(d.error)); return askRoom(mode, hostel); }
      if (!d.members.length) { botSay(`${esc(d.hostel)} ${esc(d.label)} has no members.`); return backToMenu(); }
      botSay(`<b>${esc(d.hostel)} — ${esc(d.label)}</b> (${d.occupied}/${d.sharing} occupied). ` +
        (mode === 'empty' ? 'Tap the member to make their bed empty:' : 'Tap the member:'));
      chips(d.members.map((m) => ({
        label: '<i class="fa-solid fa-user mr-1"></i> ' + esc(m.name),
        say: m.name,
        cls: mode === 'empty' ? 'danger' : '',
        onClick: () => (mode === 'empty' ? confirmEmpty(d, m) : askDays(d, m)),
      })));
    }, 'text');
  }

  // ---------- 2) make empty ----------
  function confirmEmpty(room, m) {
    botSay(`Remove <b>${esc(m.name)}</b> from ${esc(room.hostel)} ${esc(room.label)}? This deletes the student's record and cannot be undone.`);
    chips([
      { label: '<i class="fa-solid fa-trash mr-1"></i> Yes, make it empty', say: 'Yes, make it empty', cls: 'danger', onClick: async () => {
        const my = token;
        const d = await api('/admin/api/bot/make-empty', { body: { student_id: m.id } });
        if (my !== token) return;
        if (!d.ok) { botSay(esc(d.error)); return backToMenu(); }
        botSay(`Done ✅ <b>${esc(d.name)}</b> removed. ${esc(d.hostel)} ${esc(d.label)} now has <b>${d.vacant}</b> vacant bed${d.vacant === 1 ? '' : 's'} (${d.occupied}/${d.sharing}).<br><span style="color:#64748B;font-size:.8rem">Reload the dashboard to refresh the room list.</span>`);
        backToMenu();
      } },
      { label: 'Cancel', onClick: () => { botSay('Okay, nothing changed.'); backToMenu(); } },
    ]);
  }

  // ---------- 3) per-day rent ----------
  function askDays(room, m) {
    botSay(`How many days for <b>${esc(m.name)}</b>?`);
    askText('Number of days (1-31)…', async (val) => {
      const my = token;
      const d = await api('/admin/api/bot/rent/calc', { body: { student_id: m.id, days: val } });
      if (my !== token) return;
      if (!d.ok) { botSay(esc(d.error)); return askDays(room, m); }
      let html = `<b>${esc(d.name)}</b><br>Current total rent on record: ${rupee(d.monthly_rent)}<br>` +
        `Daily rent (${d.sharing}-sharing): <b>${rupee(d.per_day)}</b><br>` +
        `${d.days} day${d.days === 1 ? '' : 's'}: ${rupee(d.per_day)} × ${d.days}<br>` +
        `<b>Total rent: ${rupee(d.total)}</b>`;
      if (d.overpaid) html += `<br><span style="color:#B45309">Note: already paid ${rupee(d.amount_paid)}, which is more than this total — balance will show ₹0.</span>`;
      botSay(html);
      chips([
        { label: '<i class="fa-solid fa-floppy-disk mr-1"></i> Update total rent', say: 'Update', cls: 'primary', onClick: async () => {
          const my2 = token;
          const u = await api('/admin/api/bot/rent/update', { body: { student_id: d.student_id, days: d.days, monthly_rent: d.monthly_rent } });
          if (my2 !== token) return;
          if (!u.ok) { botSay(esc(u.error)); return backToMenu(); }
          botSay(`Updated ✅ <b>${esc(u.name)}</b>'s total rent is now <b>${rupee(u.total_rent)}</b> (balance ${rupee(u.balance)}).<br><span style="color:#64748B;font-size:.8rem">Reload the dashboard to see it in the list.</span>`);
          backToMenu();
        } },
        { label: 'Don\'t update', onClick: () => { botSay('Okay, nothing was saved.'); backToMenu(); } },
      ]);
    }, 'numeric');
  }

  // ---------- 4) AI assistant (typed or spoken; English / Telugu) ----------
  const synth = window.speechSynthesis || null;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  let teNoteShown = false;
  let rec = null;
  let listening = false;

  function speak(text, lang) {
    if (!synth || !text) return;
    synth.cancel();
    const tag = lang === 'te' ? 'te-IN' : 'en-IN';
    const u = new SpeechSynthesisUtterance(text);
    u.lang = tag;
    const voices = synth.getVoices() || [];
    const want = tag.toLowerCase();
    const v = voices.find((x) => x.lang.toLowerCase().replace('_', '-') === want) ||
              voices.find((x) => x.lang.toLowerCase().startsWith(want.slice(0, 2)));
    if (v) u.voice = v;
    else if (lang === 'te' && voices.length && !teNoteShown) {
      teNoteShown = true;
      botSay('<span style="color:#B45309;font-size:.8rem">No Telugu voice was found on this device, so the reply may not be read aloud. Install a Telugu text-to-speech voice (on Android: Google Text-to-speech) to hear Telugu.</span>');
    }
    synth.speak(u);
  }

  function assistantSay(d) {
    const node = botSay(d.html);
    if (synth && d.speak) {
      const b = document.createElement('button');
      b.type = 'button'; b.title = 'Read aloud'; b.setAttribute('aria-label', 'Read aloud');
      b.style.cssText = 'margin-left:.4rem;color:#1E3A8A;font-size:.8rem';
      b.innerHTML = '<i class="fa-solid fa-volume-high"></i>';
      b.addEventListener('click', () => speak(d.speak, d.lang));
      node.appendChild(b);
      if (autoRead) speak(d.speak, d.lang);
    }
    if (d.chips && d.chips.length) {
      chips(d.chips.map((c) => ({
        label: esc(c.label), say: c.label, cls: c.cls || '',
        onClick: () => askAssistant(Object.assign({ lang: d.lang }, c.action)),
      })));
    }
  }

  async function askAssistant(payload) {
    const my = token;
    const wait = botSay('…');
    const d = await api('/admin/api/assistant', { body: payload });
    if (my !== token) return;
    wait.remove();
    if (!d.ok) return botSay(esc(d.error));
    assistantSay(d);
  }

  function setMicUi(on) {
    listening = on;
    if (!micBtn) return;
    micBtn.classList.toggle('rec', on);
    micBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  function toggleMic() {
    if (!SR) { botSay('Voice input is not supported in this browser. Please use Chrome or Edge, or type your command.'); return; }
    if (listening && rec) { rec.stop(); return; }
    if (synth) synth.cancel();
    rec = new SR();
    rec.lang = sttLang;
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    let finalText = '';
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t; else interim += t;
      }
      input.value = (finalText + interim).trim();
    };
    rec.onerror = (e) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') botSay('Microphone permission was denied. Please allow microphone access for this site.');
      else if (e.error === 'no-speech') botSay('I did not hear anything. Tap the mic and try again.');
      else if (e.error === 'language-not-supported') botSay('This browser does not support speech recognition for the chosen language.');
      else if (e.error !== 'aborted') botSay('Voice input stopped (' + esc(e.error) + '). You can also type.');
    };
    rec.onend = () => {
      setMicUi(false);
      const v = (finalText || input.value).trim();
      if (assistantMode && v) { input.value = ''; meSay(v); askAssistant({ message: v }); }
    };
    try { rec.start(); setMicUi(true); } catch (err) { setMicUi(false); }
  }

  function enterAssistant() {
    assistantMode = true;
    input.disabled = false; send.disabled = false; pendingText = null;
    input.placeholder = 'Type or speak… / టైప్ చేయండి లేదా మాట్లాడండి…';
    input.inputMode = 'text';
    if (micBtn) micBtn.disabled = false;
    botSay('Ask me in <b>English</b> or <b>తెలుగు</b>, for example:<br>• How many vacant rooms are available?<br>• ఎన్ని గదులు ఖాళీగా ఉన్నాయి?<br>• Shift Manohar from room 2 Old Hostel to room 3 Old Hostel<br><span style="color:#64748B;font-size:.8rem">Tap the mic to speak (choose EN / తె first). I always ask you to confirm before changing anything.</span>');
    chips([{ label: '<i class="fa-solid fa-list mr-1"></i> Back to menu', say: 'Back to menu', onClick: exitAssistant }]);
    setTimeout(() => input.focus(), 50);
  }
  function exitAssistant() {
    assistantMode = false;
    if (rec && listening) rec.stop();
    if (synth) synth.cancel();
    if (micBtn) micBtn.disabled = true;
    lockText();
    menu('What next, boss?');
  }

  if (micBtn) { micBtn.disabled = true; micBtn.addEventListener('click', toggleMic); if (!SR) micBtn.title = 'Voice input needs Chrome or Edge'; }
  if (langBtn) langBtn.addEventListener('click', () => {
    sttLang = sttLang === 'en-IN' ? 'te-IN' : 'en-IN';
    langBtn.textContent = sttLang === 'en-IN' ? 'EN' : 'తె';
    langBtn.title = 'Speaking language: ' + (sttLang === 'en-IN' ? 'English' : 'Telugu');
  });
  if (speakBtn) {
    if (!synth) speakBtn.style.display = 'none';
    speakBtn.addEventListener('click', () => {
      autoRead = !autoRead;
      speakBtn.classList.toggle('on', autoRead);
      speakBtn.setAttribute('aria-pressed', autoRead ? 'true' : 'false');
      speakBtn.querySelector('i').className = autoRead ? 'fa-solid fa-volume-high' : 'fa-solid fa-volume-xmark';
      speakBtn.title = autoRead ? 'Reading replies aloud (tap to stop)' : 'Read replies aloud';
      if (!autoRead && synth) synth.cancel();
    });
  }

  // ---------- open / close ----------
  function start() { assistantMode = false; if (rec && listening) rec.stop(); if (synth) synth.cancel(); if (micBtn) micBtn.disabled = true; body.innerHTML = ''; lockText(); token++; botSay('<b>hi bosss</b> 👋 What do you want to do?'); menu(); }
  function open() { panel.classList.add('open'); fab.style.display = 'none'; if (!greeted) { greeted = true; start(); } }
  function close() { panel.classList.remove('open'); fab.style.display = ''; }
  fab.addEventListener('click', open);
  // Used by the dashboard's AI card and the sidebar's "AI Assistant" entry.
  window.openAdminBot = function (mode) { open(); if (mode === 'assistant' && !assistantMode) enterAssistant(); };
  if (/[?&]ai=1(&|$)/.test(location.search)) {
    window.addEventListener('load', function () {
      window.openAdminBot('assistant');
      try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ }
    });
  }
  document.getElementById('botClose').addEventListener('click', close);
  document.getElementById('botRestart').addEventListener('click', start);
})();
