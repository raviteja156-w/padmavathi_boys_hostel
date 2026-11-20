/* Dashboard tour with a talking teacher (Telugu voice).
   - Opens instantly: numbers are pre-loaded and cached, and Next/Back never wait for the network.
   - "Quit tour" is always visible (panel header, paused helper, Esc key).
   - The teacher explains each point in Telugu. "Pause & act" stops the voice so the admin can edit / do real work on
     any page; "Resume" continues from the same step.
   - Read-only: it only shows numbers; every action still happens on the normal pages. */
(function () {
  'use strict';
  var KEY = 'hostelTourState', DKEY = 'hostelTourData';
  var wrap = null, panel = null, mini = null;
  var data = null, loading = false, waiters = [];
  var speakId = 0, curAudio = null, voiceNote = '';

  /* ------------------------------------------------------------ helpers */
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function rs(n) { return '\u20B9' + Math.round(Number(n) || 0).toLocaleString('en-IN'); }
  function num(n) { return String(Math.round(Number(n) || 0)); }           // plain digits are read best by the voice
  function getState() { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { return null; } }
  function setState(s) { try { if (s) sessionStorage.setItem(KEY, JSON.stringify(s)); else sessionStorage.removeItem(KEY); } catch (e) {} }
  function toast(m, t, o) { if (window.showToast) window.showToast(m, t || 'success', o); }

  /* ------------------------------------------------------------ data: memory -> cache -> network */
  function readCache(maxAgeMs) {
    try {
      var c = JSON.parse(sessionStorage.getItem(DKEY) || 'null');
      if (c && c.t && Date.now() - c.t < maxAgeMs && c.d && c.d.ok) return c.d;
    } catch (e) {}
    return null;
  }
  function fetchData(cb, silent) {
    if (cb) waiters.push(cb);
    if (loading) return;
    loading = true;
    fetch('/admin/api/tour', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (r.status === 401) throw new Error('Your session expired. Please log in again.'); return r.json(); })
      .then(function (j) {
        loading = false;
        if (!j || !j.ok) throw new Error('Could not load');
        data = j;
        try { sessionStorage.setItem(DKEY, JSON.stringify({ t: Date.now(), d: j })); } catch (e) {}
        var w = waiters; waiters = []; w.forEach(function (f) { f(true); });
      })
      .catch(function (e) {
        loading = false; waiters = [];
        if (!silent) { toast(e && e.message ? e.message : 'Could not load the tour. Check your internet.', 'error'); endTour(true); }
      });
  }
  function prefetchData() { if (!data && !readCache(60000) && !loading) fetchData(null, true); }

  /* ------------------------------------------------------------ the teacher (inline SVG: no image files to download) */
  var CHAR_SVG =
    '<svg viewBox="0 0 130 200" aria-hidden="true" focusable="false">' +
      '<ellipse cx="50" cy="196" rx="30" ry="4" fill="rgba(15,23,42,.18)"/>' +
      '<g class="tc-body">' +
        '<rect x="37" y="150" width="11" height="40" rx="3" fill="#1E293B"/><rect x="52" y="150" width="11" height="40" rx="3" fill="#1E293B"/>' +
        '<rect x="33" y="186" width="17" height="8" rx="4" fill="#0F172A"/><rect x="50" y="186" width="17" height="8" rx="4" fill="#0F172A"/>' +
        '<rect x="28" y="88" width="44" height="68" rx="12" fill="#2563EB"/>' +
        '<path d="M50 90 L44 104 L50 150 L56 104 Z" fill="#F8FAFC"/><path d="M50 96 L46 104 L50 140 L54 104 Z" fill="#DC2626"/>' +
        '<path d="M32 98 Q24 118 26 140" stroke="#2563EB" stroke-width="9" stroke-linecap="round" fill="none"/>' +
        '<circle cx="26" cy="143" r="5" fill="#F2C29B"/>' +
        '<rect x="44" y="76" width="12" height="14" rx="4" fill="#E8B48B"/>' +
        '<circle cx="50" cy="50" r="25" fill="#F2C29B"/>' +
        '<path d="M25 46 Q26 22 50 22 Q74 22 75 46 Q68 34 50 34 Q32 34 25 46 Z" fill="#1E293B"/>' +
        '<circle cx="26" cy="52" r="4" fill="#E8B48B"/><circle cx="74" cy="52" r="4" fill="#E8B48B"/>' +
        '<g class="tc-eyes"><circle cx="41" cy="49" r="3.2" fill="#0F172A"/><circle cx="59" cy="49" r="3.2" fill="#0F172A"/>' +
          '<circle cx="42" cy="48" r="1" fill="#fff"/><circle cx="60" cy="48" r="1" fill="#fff"/></g>' +
        '<circle cx="41" cy="49" r="8" fill="none" stroke="#475569" stroke-width="1.6"/><circle cx="59" cy="49" r="8" fill="none" stroke="#475569" stroke-width="1.6"/><path d="M49 49 h2" stroke="#475569" stroke-width="1.6"/>' +
        '<path d="M42 59 Q50 55 58 59 Q50 61 42 59 Z" fill="#1E293B"/>' +
        '<ellipse class="tc-mouth" cx="50" cy="65" rx="5.5" ry="3.6" fill="#7F1D1D"/>' +
        '<g class="tc-arm">' +
          '<path d="M68 98 Q82 98 92 90" stroke="#2563EB" stroke-width="9" stroke-linecap="round" fill="none"/>' +
          '<line x1="86" y1="104" x2="124" y2="40" stroke="#92400E" stroke-width="3.6" stroke-linecap="round"/>' +
          '<circle cx="124.5" cy="39" r="3.6" fill="#DC2626"/>' +
          '<circle cx="94" cy="88" r="5.5" fill="#F2C29B"/><path d="M90 90 L97 84" stroke="#E8B48B" stroke-width="2" stroke-linecap="round"/>' +
        '</g>' +
      '</g>' +
    '</svg>';

  function setTalking(on) {
    document.querySelectorAll('.tour-char').forEach(function (c) { c.classList.toggle('talking', !!on); c.classList.toggle('idle', !on); });
  }

  /* ------------------------------------------------------------ Telugu voice */
  function teluguVoice() {
    if (!('speechSynthesis' in window)) return null;
    var vs = window.speechSynthesis.getVoices() || [];
    for (var i = 0; i < vs.length; i++) if (/^te([-_]|$)/i.test(vs[i].lang)) return vs[i];
    for (var j = 0; j < vs.length; j++) if (/telugu|తెలుగు/i.test(vs[j].name)) return vs[j];
    return null;
  }
  if ('speechSynthesis' in window) { try { window.speechSynthesis.getVoices(); } catch (e) {} }

  function chunks(text) {                       // short pieces: long utterances get cut off by some browsers
    var parts = String(text).replace(/\s+/g, ' ').split(/(?<=[.!?\u0964])\s+/), out = [], cur = '';
    parts.forEach(function (p) {
      if ((cur + ' ' + p).length > 170 && cur) { out.push(cur); cur = p; } else cur = cur ? cur + ' ' + p : p;
    });
    if (cur) out.push(cur);
    return out;
  }

  function stopVoice() {
    speakId++;
    try { if ('speechSynthesis' in window) window.speechSynthesis.cancel(); } catch (e) {}
    if (curAudio) { try { curAudio.pause(); curAudio.src = ''; } catch (e) {} curAudio = null; }
    setTalking(false);
  }

  function speak(text, onDone) {
    stopVoice();
    var st = getState() || {};
    if (st.mute || !text) { if (onDone) onDone(); return; }
    var id = speakId, list = chunks(text), k = 0, v = teluguVoice();
    voiceNote = ''; paintNote();
    setTalking(true);

    function finish() { if (id !== speakId) return; setTalking(false); if (onDone) onDone(); }
    function unavailable() {
      if (id !== speakId) return;
      voiceNote = 'No Telugu voice was found on this device. Add Telugu in your phone/PC Text-to-speech settings (Google Text-to-speech). The Telugu words are written above.';
      setTalking(false); paintNote();
    }
    function nextBrowser() {
      if (id !== speakId) return;
      if (k >= list.length) return finish();
      var u = new SpeechSynthesisUtterance(list[k++]);
      u.lang = 'te-IN'; if (v) u.voice = v; u.rate = 0.92; u.pitch = 1;
      u.onend = nextBrowser;
      u.onerror = function (ev) {
        if (id !== speakId) return;
        if (ev && ev.error === 'not-allowed') { setTalking(false); voiceNote = 'Tap \u201CListen again\u201D to hear the teacher.'; paintNote(); }
        else nextBrowser();
      };
      try { window.speechSynthesis.speak(u); } catch (e) { unavailable(); }
    }
    function nextAudio() {                      // best-effort fallback when the device has no Telugu voice
      if (id !== speakId) return;
      if (k >= list.length) return finish();
      var a = new Audio('https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=te&q=' + encodeURIComponent(list[k++]));
      curAudio = a;
      a.onended = nextAudio;
      a.onerror = unavailable;
      var p = a.play(); if (p && p.catch) p.catch(unavailable);
    }
    if (v) nextBrowser(); else nextAudio();
  }

  /* ------------------------------------------------------------ the lessons: panel + Telugu words for every step */
  function when(x) { return x.days_left == null ? '' : (x.days_left < 0 ? 'left ' + (-x.days_left) + 'd ago (check out!)' : x.days_left === 0 ? 'leaves TODAY' : x.days_left === 1 ? 'leaves tomorrow' : 'leaves in ' + x.days_left + ' days'); }

  var STEPS = [
    { id: 'welcome', icon: 'fa-hand', title: 'Welcome', needsData: false,
      say: function () {
        return 'నమస్కారం! నేను మీ హాస్టల్ గైడ్\u200Cని. ఈ డాష్\u200Cబోర్డ్\u200Cలో ఉన్న ముఖ్యమైన విషయాలను ఒక్కొక్కటిగా వివరిస్తాను. ' +
               'మధ్యలో ఎప్పుడైనా ఆపి, మీ పని చేసుకోవచ్చు. తర్వాత మళ్ళీ కొనసాగించవచ్చు.';
      },
      render: function () {
        return '<div class="tour-chips"><span class="tour-chip"><i class="fa-solid fa-suitcase-rolling"></i> Guests</span><span class="tour-chip"><i class="fa-solid fa-indian-rupee-sign"></i> Payments</span>' +
               '<span class="tour-chip"><i class="fa-solid fa-bed-pulse"></i> Paused beds</span><span class="tour-chip"><i class="fa-solid fa-hourglass-half"></i> Today</span><span class="tour-chip"><i class="fa-solid fa-bed"></i> Vacancies</span></div>' +
               '<p class="tour-empty">Tap <b>Next</b> to begin. Tap <b>Pause &amp; act</b> any time to do your work, then <b>Resume</b>. Tap <b>Quit tour</b> to close.</p>';
      } },

    { id: 'guests', icon: 'fa-suitcase-rolling', title: 'Guests', needsData: true, link: '/admin/guests',
      say: function (d) {
        var g = d.guests || [];
        if (!g.length) return 'ప్రస్తుతం హాస్టల్\u200Cలో ఎవరూ అతిథులు లేరు.';
        var x = g[0], dl = x.days_left;
        var t = 'ప్రస్తుతం ' + num(g.length) + ' మంది అతిథులు హాస్టల్\u200Cలో ఉన్నారు. ' + x.name + ' గారు ';
        if (dl == null) t += 'వెళ్ళే తేదీ ఇంకా నమోదు కాలేదు. ';
        else if (dl < 0) t += num(-dl) + ' రోజుల క్రితమే వెళ్ళవలసి ఉంది, కాబట్టి చెక్ అవుట్ చేయండి. ';
        else if (dl === 0) t += 'ఈ రోజే వెళ్తున్నారు. ';
        else if (dl === 1) t += 'రేపు వెళ్తున్నారు. ';
        else t += num(dl) + ' రోజుల్లో వెళ్తారు. ';
        if (x.balance) t += 'వీరి దగ్గర ' + num(x.balance) + ' రూపాయలు బాకీ ఉంది. ';
        return t + 'మిగిలిన వివరాలు గెస్ట్స్ పేజీలో చూడవచ్చు.';
      },
      render: function (d) {
        var g = d.guests || [];
        var h = '<div class="tour-big">' + g.length + '<small>guest' + (g.length === 1 ? '' : 's') + ' staying now</small></div>';
        if (!g.length) return h + '<p class="tour-empty">No guests are staying right now.</p>';
        h += '<ul class="tour-list">';
        g.slice(0, 12).forEach(function (x) {
          h += '<li><span class="n">' + esc(x.name) + '<div class="m">' + esc(x.place || '') + (x.balance ? ' \u2022 due ' + rs(x.balance) : '') + '</div></span>' +
               '<span class="r">' + esc(x.leaves) + '<div class="m">' + esc(when(x)) + '</div></span></li>';
        });
        if (g.length > 12) h += '<li><span class="m">+ ' + (g.length - 12) + ' more on the Guests page</span></li>';
        return h + '</ul>';
      } },

    { id: 'payments', icon: 'fa-indian-rupee-sign', title: 'Payment status', needsData: true, link: '/admin/due-list',
      say: function (d) {
        var p = d.payment || {};
        return 'ఇప్పటి వరకు మొత్తం ' + num(p.total_paid) + ' రూపాయలు వసూలయ్యాయి. ఇంకా ' + num(p.total_due) + ' రూపాయలు రావలసి ఉంది. ' +
               num(p.fully_paid) + ' మంది పూర్తిగా చెల్లించారు, ' + num(p.part_paid) + ' మంది కొంత మాత్రమే చెల్లించారు, ' + num(p.not_paid) + ' మంది ఇంకా ఏమీ చెల్లించలేదు. ' +
               'గడువు దాటినవారు ' + num(p.due_now) + ' మంది, ఈ రోజు గడువు ఉన్నవారు ' + num(p.due_today) + ' మంది, రేపు గడువు ఉన్నవారు ' + num(p.due_tomorrow) + ' మంది.';
      },
      render: function (d) {
        var p = d.payment || {};
        var h = '<div class="tour-big">' + rs(p.total_paid) + '<small>paid &bull; ' + rs(p.total_due) + ' due</small></div><div class="tour-chips">' +
          '<span class="tour-chip g">' + (p.fully_paid || 0) + ' fully paid</span><span class="tour-chip a">' + (p.part_paid || 0) + ' part paid</span><span class="tour-chip r">' + (p.not_paid || 0) + ' not paid</span></div><ul class="tour-list">' +
          '<li><span class="n">Due now</span><span class="r">' + (p.due_now || 0) + ' student(s)</span></li>' +
          '<li><span class="n">Due today</span><span class="r">' + (p.due_today || 0) + '</span></li>' +
          '<li><span class="n">Due tomorrow</span><span class="r">' + (p.due_tomorrow || 0) + '</span></li>';
        if (p.left_count) h += '<li><span class="n">Paid &amp; LEFT (kept in total)</span><span class="r">' + rs(p.left_paid) + ' &bull; ' + p.left_count + '</span></li>';
        return h + '</ul>';
      } },

    { id: 'pauses', icon: 'fa-bed-pulse', title: 'Paused beds', needsData: true, link: '/admin/pause-bed',
      say: function (d) {
        var a = d.pauses || [];
        if (!a.length) return 'ప్రస్తుతం ఏ బెడ్ కూడా పాజ్ చేయబడలేదు.';
        var x = a[0];
        return num(a.length) + ' బెడ్లు పాజ్\u200Cలో ఉన్నాయి. ' + x.name + ' గారి బెడ్ ' + x.ends + ' వరకు పాజ్\u200Cలో ఉంది, ' +
               (x.remaining <= 0 ? 'ఈ రోజుతో ముగుస్తుంది.' : num(x.remaining) + ' రోజులు మిగిలి ఉన్నాయి.');
      },
      render: function (d) {
        var a = d.pauses || [];
        var h = '<div class="tour-big">' + a.length + '<small>bed' + (a.length === 1 ? '' : 's') + ' paused now</small></div>';
        if (!a.length) return h + '<p class="tour-empty">No bed is paused right now.</p>';
        h += '<ul class="tour-list">';
        a.forEach(function (x) {
          var left = x.remaining <= 0 ? 'ends today' : x.remaining === 1 ? '1 day left' : x.remaining + ' days left';
          h += '<li><span class="n">' + esc(x.name) + '<div class="m">' + esc(x.place) + '</div></span><span class="r">ends ' + esc(x.ends) + '<div class="m">' + left + '</div></span></li>';
        });
        return h + '</ul>';
      } },

    { id: 'today', icon: 'fa-hourglass-half', title: "Today's payments", needsData: true, link: '/admin/payments',
      say: function (d) {
        var pe = d.pending || [], cf = d.confirmed_today || [], t;
        if (!(d.pending_total || 0)) t = 'ధృవీకరణ కోసం ఏ చెల్లింపూ ఎదురు చూడటం లేదు. ';
        else t = 'ధృవీకరించవలసిన చెల్లింపులు ' + num(d.pending_total) + ' ఉన్నాయి. ' + (pe[0] ? 'మొదటిది ' + pe[0].name + ' గారిది, ' + num(pe[0].amount) + ' రూపాయలు. ' : '');
        return t + 'ఈ రోజు ' + num(cf.length) + ' చెల్లింపులను ధృవీకరించారు, వాటి మొత్తం ' + num(d.confirmed_today_total) + ' రూపాయలు.';
      },
      render: function (d) {
        var pe = d.pending || [], cf = d.confirmed_today || [];
        var h = '<div class="tour-big">' + (d.pending_total || 0) + '<small>to confirm</small></div>';
        if (pe.length) {
          h += '<ul class="tour-list">';
          pe.slice(0, 8).forEach(function (x) { h += '<li><span class="n">' + esc(x.name) + '<div class="m">' + esc(x.place) + '</div></span><span class="r">' + rs(x.amount) + '</span></li>'; });
          if ((d.pending_total || 0) > 8) h += '<li><span class="m">+ ' + (d.pending_total - 8) + ' more</span></li>';
          h += '</ul>';
        } else h += '<p class="tour-empty">Nothing waiting for confirmation.</p>';
        h += '<div class="tour-chips"><span class="tour-chip g">' + cf.length + ' confirmed today &bull; ' + rs(d.confirmed_today_total) + '</span></div>';
        if (cf.length) {
          h += '<ul class="tour-list">';
          cf.slice(0, 6).forEach(function (x) { h += '<li><span class="n">' + esc(x.name) + '</span><span class="r">' + rs(x.amount) + '</span></li>'; });
          h += '</ul>';
        }
        return h;
      } },

    { id: 'vacancy', icon: 'fa-bed', title: 'Vacant rooms', needsData: true, link: '/admin/vacancies',
      say: function (d) {
        var v = d.vacancy || {}, r = v.rooms || [];
        var t = 'ప్రస్తుతం ' + num(v.free) + ' ఖాళీ బెడ్లు ఉన్నాయి. ';
        if (v.empty_rooms) t += num(v.empty_rooms) + ' గదులు పూర్తిగా ఖాళీగా ఉన్నాయి. ';
        if (r[0]) t += r[0].label + ' లో ' + num(r[0].free) + ' ఖాళీ ఉంది.';
        return t;
      },
      render: function (d) {
        var v = d.vacancy || {}, r = v.rooms || [];
        var h = '<div class="tour-big">' + (v.free || 0) + '<small>free bed' + (v.free === 1 ? '' : 's') + (v.empty_rooms ? ' &bull; ' + v.empty_rooms + ' empty room' + (v.empty_rooms === 1 ? '' : 's') : '') + '</small></div>';
        if (!r.length) return h + '<p class="tour-empty">No partly-filled rooms have a free bed.</p>';
        h += '<ul class="tour-list">';
        r.forEach(function (x) { h += '<li><span class="n">' + esc(x.label) + '</span><span class="r">' + x.free + ' free of ' + x.of + '</span></li>'; });
        return h + '</ul>';
      } },

    { id: 'done', icon: 'fa-flag-checkered', title: 'All done', needsData: false,
      say: function () {
        return 'టూర్ పూర్తయింది. ఏదైనా మార్చాలంటే ఆ పేజీలోనే చేయవచ్చు. మార్పులు సేవ్ చేసిన తర్వాత మీరు అదే పేజీలో ఉంటారు. ధన్యవాదాలు!';
      },
      render: function () {
        return '<p class="tour-empty"><i class="fa-solid fa-circle-check" style="color:#16A34A"></i> Tour finished. When you save or edit something you now <b>stay on the same page</b>, and you get a message with an icon for every action.</p>';
      } }
  ];

  var PAUSE_SAY = 'సరే, ఆపాను. మీ పని చేసుకోండి. మళ్ళీ కొనసాగించాలంటే రెజ్యూమ్ నొక్కండి.';
  var LOADING_SAY = 'వివరాలు లోడ్ అవుతున్నాయి…';

  /* ------------------------------------------------------------ DOM */
  function ensureEls() {
    if (wrap) return;
    wrap = document.createElement('div'); wrap.className = 'tour-wrap'; wrap.hidden = true;
    wrap.innerHTML = '<div class="tour-char idle">' + CHAR_SVG + '</div><div class="tour-panel" role="dialog" aria-label="Dashboard tour"></div>';
    panel = wrap.querySelector('.tour-panel');
    mini = document.createElement('div'); mini.className = 'tour-mini'; mini.hidden = true;
    mini.innerHTML = '<div class="tour-char idle">' + CHAR_SVG + '</div><div class="mini-btns">' +
      '<button type="button" class="m-resume" data-t="resume"><i class="fa-solid fa-play"></i> Resume tour</button>' +
      '<button type="button" class="m-quit" data-t="end"><i class="fa-solid fa-xmark"></i> Quit tour</button></div>';
    document.body.appendChild(wrap); document.body.appendChild(mini);
  }

  function hideAll() {
    stopVoice();
    if (wrap) wrap.hidden = true;
    if (mini) mini.hidden = true;
    document.body.classList.remove('tour-open');
  }

  function paintNote() {
    var n = panel && panel.querySelector('.tour-note-slot');
    if (n) n.innerHTML = voiceNote ? '<div class="tour-note"><i class="fa-solid fa-volume-xmark"></i> ' + esc(voiceNote) + '</div>' : '';
  }
  function muteLabel(s) { return '<i class="fa-solid ' + (s.mute ? 'fa-volume-xmark' : 'fa-volume-high') + '"></i> ' + (s.mute ? 'Voice off' : 'Voice on'); }
  function stepIndex(s) { return Math.max(0, Math.min((s && s.step) || 0, STEPS.length - 1)); }

  function draw(s) {
    var i = stepIndex(s), st = STEPS[i], last = i === STEPS.length - 1, ready = !st.needsData || !!data;
    var dots = ''; STEPS.forEach(function (_, k) { dots += '<i class="' + (k === i ? 'on' : '') + '"></i>'; });
    var body = ready ? st.render(data || {}) : '<div class="tour-skel" style="width:40%;height:1.8rem"></div><div class="tour-skel"></div><div class="tour-skel" style="width:80%"></div>';
    var say = ready ? st.say(data || {}) : LOADING_SAY;
    panel.innerHTML =
      '<div class="tour-head"><span class="t-ico"><i class="fa-solid ' + st.icon + '"></i></span>' +
        '<div><div class="t-title">' + esc(st.title) + '</div><div class="t-sub">Step ' + (i + 1) + ' of ' + STEPS.length + '</div></div>' +
        '<div class="tour-dots">' + dots + '</div>' +
        '<button type="button" class="tour-quit" data-t="end" aria-label="Quit tour"><i class="fa-solid fa-xmark"></i> Quit tour</button></div>' +
      '<div class="tour-say">' + esc(say) +
        '<span class="tour-nov"><button type="button" data-t="speak" style="font-weight:800;color:#92400E"><i class="fa-solid fa-volume-high"></i> Listen again</button>' +
        ' &nbsp;&bull;&nbsp; <button type="button" data-t="mute" style="font-weight:800;color:#92400E">' + muteLabel(s) + '</button></span></div>' +
      '<div class="tour-note-slot"></div>' +
      '<div class="tour-body">' + body + '</div>' +
      '<div class="tour-foot">' +
        '<button type="button" class="tour-btn warn" data-t="pause"><i class="fa-solid fa-pause"></i> Pause &amp; act</button>' +
        '<button type="button" class="tour-btn" data-t="prev"' + (i === 0 ? ' disabled' : '') + '><i class="fa-solid fa-chevron-left"></i> Back</button>' +
        '<button type="button" class="tour-btn pri" data-t="' + (last ? 'end' : 'next') + '">' + (last ? 'Finish <i class="fa-solid fa-check"></i>' : 'Next <i class="fa-solid fa-chevron-right"></i>') + '</button>' +
      '</div>';
    paintNote();
    return ready ? say : null;
  }

  /* speakNow is true only right after a tap (browsers allow voice only after a tap) */
  function show(speakNow) {
    ensureEls();
    var s = getState();
    if (!s || !s.active) { hideAll(); return; }
    if (s.paused) { wrap.hidden = true; mini.hidden = false; document.body.classList.remove('tour-open'); return; }
    mini.hidden = true; wrap.hidden = false; document.body.classList.add('tour-open');
    var st = STEPS[stepIndex(s)];
    if (st.needsData && !data) {
      var cached = readCache(5 * 60000);
      if (cached) { data = cached; fetchData(refreshed, true); }       // show cached numbers now, refresh quietly
    }
    var say = draw(s);
    if (st.needsData && !data) { fetchData(function () { show(speakNow); }); return; }
    if (speakNow && say) speak(say);
  }

  function refreshed() {                 // fresh numbers arrived in the background: update the panel, never interrupt the voice
    var s = getState(); if (!s || !s.active || s.paused || !panel || wrap.hidden) return;
    var st = STEPS[stepIndex(s)], b = panel.querySelector('.tour-body');
    if (b && st.needsData && data) b.innerHTML = st.render(data);
  }

  /* ------------------------------------------------------------ actions */
  function go(delta) {
    var s = getState(); if (!s) return;
    s.step = Math.max(0, Math.min((s.step || 0) + delta, STEPS.length - 1)); setState(s);
    show(true);                          // instant: data is already in memory; only the voice restarts
  }

  function pause() {
    var s = getState() || { step: 0 };
    var st = STEPS[stepIndex(s)];
    s.active = true; s.paused = true; setState(s);
    stopVoice(); show(false);
    speak(PAUSE_SAY);
    toast('Tour paused. Do your work, then tap \u201CResume tour\u201D.', 'info', { icon: 'fa-pause', title: 'Tour paused' });
    if (st && st.link && location.pathname !== st.link) { setTimeout(function () { location.href = st.link; }, 1500); }
  }

  function resume() {
    var s = getState() || { step: 0 };
    s.active = true; s.paused = false; setState(s);
    data = null; try { sessionStorage.removeItem(DKEY); } catch (e) {}   // numbers may have changed while the admin worked
    show(true);
  }

  function endTour(silent) {
    setState(null); data = null; try { sessionStorage.removeItem(DKEY); } catch (e) {}
    hideAll();
    if (!silent) toast('The tour was closed. You can start it again any time from \u201CTake a Tour\u201D.', 'info', { icon: 'fa-flag-checkered', title: 'Tour closed' });
  }

  function start() {
    var cached = readCache(60000); if (cached) data = cached;
    setState({ active: true, paused: false, step: 0, mute: !!(getState() || {}).mute });
    show(true);
    if (!data) fetchData(null, true);    // load the numbers while the teacher is already greeting
  }

  function replay() {
    var s = getState(); if (!s) return;
    if (s.mute) { s.mute = false; setState(s); var b = panel.querySelector('[data-t="mute"]'); if (b) b.innerHTML = muteLabel(s); }
    var st = STEPS[stepIndex(s)];
    if (!st.needsData || data) speak(st.say(data || {}));
  }

  function toggleMute() {
    var s = getState(); if (!s) return;
    s.mute = !s.mute; setState(s);
    var b = panel.querySelector('[data-t="mute"]'); if (b) b.innerHTML = muteLabel(s);
    if (s.mute) stopVoice(); else replay();
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-tour-start]'); if (b) { e.preventDefault(); start(); return; }
    var t = e.target.closest('[data-t]');
    if (!t || !((panel && panel.contains(t)) || (mini && mini.contains(t)))) return;
    var a = t.getAttribute('data-t');
    if (a === 'next') go(1);
    else if (a === 'prev') go(-1);
    else if (a === 'pause') pause();
    else if (a === 'resume') resume();
    else if (a === 'end') endTour();
    else if (a === 'speak') replay();
    else if (a === 'mute') toggleMute();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var s = getState(); if (!s || !s.active || s.paused) return;
    var tg = e.target && e.target.tagName;
    if (tg === 'INPUT' || tg === 'TEXTAREA' || tg === 'SELECT') return;
    endTour();
  });

  // warm the numbers up before the admin even taps "Take a Tour"
  ['mouseover', 'touchstart', 'focusin'].forEach(function (ev) {
    document.addEventListener(ev, function (e) { if (e.target.closest && e.target.closest('[data-tour-start]')) prefetchData(); }, { passive: true });
  });

  function init() {
    ensureEls();
    show(false);
    var s = getState();
    if (s && s.active && !s.paused) { voiceNote = 'Tap \u201CListen again\u201D to hear the teacher on this page.'; paintNote(); }
    if (document.querySelector('[data-tour-start]')) {
      var idle = window.requestIdleCallback || function (f) { return setTimeout(f, 1200); };
      idle(prefetchData);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.addEventListener('pageshow', function (e) { if (e.persisted) show(false); });
  window.addEventListener('pagehide', function () { stopVoice(); });
})();
