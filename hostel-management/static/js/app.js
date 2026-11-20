// ============================================================
// Padmavathi Boys hostel - Global JavaScript
// Mobile navigation + acknowledgement toasts (with icons) + instant page opening
// ============================================================

document.addEventListener('DOMContentLoaded', function () {
  // Mobile hamburger menu
  const hamburgerBtn = document.getElementById('hamburgerBtn');
  const mobileMenu = document.getElementById('mobileMenu');
  if (hamburgerBtn && mobileMenu) {
    hamburgerBtn.addEventListener('click', function () {
      mobileMenu.classList.toggle('hidden');
      const icon = hamburgerBtn.querySelector('i');
      icon.classList.toggle('fa-bars');
      icon.classList.toggle('fa-xmark');
    });
  }

  // Show every server-side flash message as an acknowledgement toast.
  // (FIX: this used to read window.__flashMessages, which another script filled in only AFTER this code ran,
  //  so no acknowledgement was ever shown. We now read the message list straight from the page.)
  var msgs = [];
  try {
    var el = document.getElementById('flashMessages');
    if (el) msgs = JSON.parse(el.textContent.trim()) || [];
  } catch (err) { msgs = []; }
  msgs.forEach(function (msg, i) {
    var category = msg[0], text = msg[1];
    setTimeout(function () { showToast(text, category === 'error' ? 'error' : 'success'); }, i * 140);
  });
});

// Pick an icon + short title that matches what the admin just did.
var ACK_RULES = [
  [/security check/i,                       'fa-shield-halved',            'Security check',    'error'],
  [/session expired|log in to access/i,     'fa-lock',                     'Please log in',     'error'],
  [/welcome back/i,                         'fa-right-to-bracket',         'Logged in',         'success'],
  [/logged out/i,                           'fa-right-from-bracket',       'Logged out',        'success'],
  [/restor|brought back|revert/i,           'fa-rotate-left',              'Restored',          'success'],
  [/permanently removed|permanently delet/i,'fa-trash-can-arrow-up',       'Removed for good',  'delete'],
  [/marked as left|marked as LEFT/i,        'fa-person-walking-arrow-right','Marked as left',   'delete'],
  [/no longer marked as paid/i,             'fa-rotate-left',              'Payment undone',    'warning'],
  [/marked as fully paid/i,                 'fa-circle-check',             'Fully paid',        'success'],
  [/checked out/i,                          'fa-person-walking-luggage',   'Guest checked out', 'success'],
  [/extended/i,                             'fa-calendar-plus',            'Stay extended',     'success'],
  [/guest .*added|added for/i,              'fa-suitcase-rolling',         'Guest added',       'success'],
  [/guest .*deleted/i,                      'fa-trash-can',                'Guest deleted',     'delete'],
  [/bed paused|paused/i,                    'fa-bed-pulse',                'Bed paused',        'success'],
  [/confirmed/i,                            'fa-circle-check',             'Payment confirmed', 'success'],
  [/canteen income/i,                       'fa-utensils',                 'Income added',      'success'],
  [/income entry deleted/i,                 'fa-trash-can',                'Income deleted',    'delete'],
  [/expense added/i,                        'fa-receipt',                  'Expense added',     'success'],
  [/expense deleted/i,                      'fa-trash-can',                'Expense deleted',   'delete'],
  [/note deleted/i,                         'fa-trash-can',                'Note deleted',      'delete'],
  [/note (saved|updated|cleared)/i,         'fa-note-sticky',              'Note saved',        'success'],
  [/import/i,                               'fa-file-import',              'Import finished',   'success'],
  [/deleted|removed|cleared/i,              'fa-trash-can',                'Deleted',           'delete'],
  [/payment details (updated|saved)/i,      'fa-qrcode',                   'Payment details saved','success'],
  [/payment|paid|received|recorded|\u20b9/i,'fa-indian-rupee-sign',        'Payment saved',     'success'],
  [/updated/i,                              'fa-pen-to-square',            'Updated',           'success'],
  [/saved|added|created/i,                  'fa-floppy-disk',              'Saved',             'success']
];

function ackFor(message, type) {
  var m = String(message || '');
  if (type === 'error') {
    var e = [/not found|no longer exists|no matching/i, 'fa-magnifying-glass', 'Not found'];
    if (e[0].test(m)) return { icon: e[1], title: e[2], tone: 'error' };
    if (/security check/i.test(m)) return { icon: 'fa-shield-halved', title: 'Security check', tone: 'error' };
    if (/log in/i.test(m)) return { icon: 'fa-lock', title: 'Please log in', tone: 'error' };
    return { icon: 'fa-circle-xmark', title: 'Not saved', tone: 'error' };
  }
  for (var i = 0; i < ACK_RULES.length; i++) {
    if (ACK_RULES[i][0].test(m)) return { icon: ACK_RULES[i][1], title: ACK_RULES[i][2], tone: ACK_RULES[i][3] };
  }
  if (type === 'warning') return { icon: 'fa-triangle-exclamation', title: 'Please note', tone: 'warning' };
  if (type === 'info') return { icon: 'fa-circle-info', title: 'Info', tone: 'info' };
  return { icon: 'fa-circle-check', title: 'Done', tone: 'success' };
}

// showToast(message, type, opts) - type: success | error | warning | info ; opts: {title, icon}
function showToast(message, type, opts) {
  type = type || 'success';
  opts = opts || {};
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const a = ackFor(message, type);
  const icon = opts.icon || a.icon, title = opts.title || a.title, tone = a.tone;
  const ms = tone === 'error' ? 7000 : 4200;

  // keep the screen tidy: at most 4 messages at a time
  while (container.children.length >= 4) container.firstChild.remove();

  const toast = document.createElement('div');
  toast.className = 'toast ack ack-' + tone + (tone === 'error' ? ' toast-error' : '');
  toast.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  toast.innerHTML =
    '<span class="ack-ico"><i class="fa-solid ' + icon + '"></i></span>' +
    '<span class="ack-txt"><b>' + escapeHtml(title) + '</b><span>' + escapeHtml(message) + '</span></span>' +
    '<i class="fa-solid fa-xmark toast-close" aria-label="Close"></i>' +
    '<span class="ack-bar" style="animation-duration:' + ms + 'ms"></span>';
  toast.querySelector('.toast-close').addEventListener('click', function () { toast.remove(); });
  container.appendChild(toast);

  setTimeout(function () {
    toast.style.transition = 'opacity .25s ease, transform .25s ease';
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(-8px)';
    setTimeout(function () { toast.remove(); }, 260);
  }, ms);
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Generic modal helpers (shared across pages)
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('hidden');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('hidden');
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay:not(.hidden)').forEach(function (m) {
      m.classList.add('hidden');
    });
  }
});

// COPY buttons (payment number): <button class="copy-btn" data-copy="...">
document.addEventListener('click', function (e) {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  const text = btn.getAttribute('data-copy') || '';
  function done() { showToast('Copied: ' + text, 'success'); }
  function fallback() {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (err) { showToast('Could not copy. Please copy manually.', 'error'); }
    document.body.removeChild(ta);
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, fallback);
  } else {
    fallback();
  }
});


// ============================================================
// Fast & clear feedback (added): busy buttons, stay-where-you-were scroll, offline hint
// ============================================================
(function () {
  // 1) While a form is saving: show "Saving..." on the button and stop double taps.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM' || e.defaultPrevented) return;
    if ((form.method || '').toLowerCase() !== 'post' || form.hasAttribute('data-no-busy')) return;
    if (form.target === '_blank' || form.hasAttribute('download')) return;
    var btn = e.submitter || form.querySelector('button[type=submit], input[type=submit]');
    // remember where the page was scrolled, so the page after saving opens at the same spot
    try { sessionStorage.setItem('hostelScroll', JSON.stringify({ p: location.pathname, y: window.scrollY, t: Date.now() })); } catch (err) {}
    if (!btn || btn.classList.contains('is-busy')) return;
    setTimeout(function () {                       // after the browser has collected the form (keeps button values)
      if (e.defaultPrevented || btn.disabled) return;   // cancelled by confirm(), or already handled by the page itself
      btn.classList.add('is-busy');
      btn.setAttribute('aria-busy', 'true');
      if (btn.tagName === 'BUTTON') {
        btn.dataset.oldHtml = btn.innerHTML;
        btn.innerHTML = '<span class="busy-spin"></span>Saving...';
      }
      btn.disabled = true;
      setTimeout(function () { resetBusy(btn); }, 20000);   // safety: never stay stuck
    }, 0);
  }, true);

  function resetBusy(btn) {
    if (!btn || !btn.classList.contains('is-busy')) return;
    btn.classList.remove('is-busy'); btn.removeAttribute('aria-busy'); btn.disabled = false;
    if (btn.dataset.oldHtml) { btn.innerHTML = btn.dataset.oldHtml; delete btn.dataset.oldHtml; }
  }
  window.addEventListener('pageshow', function () {            // back button / cached page
    document.querySelectorAll('.is-busy').forEach(resetBusy);
  });

  // 2) After an action the page reloads: put the scroll back where it was (same page only).
  document.addEventListener('DOMContentLoaded', function () {
    try {
      var s = JSON.parse(sessionStorage.getItem('hostelScroll') || 'null');
      sessionStorage.removeItem('hostelScroll');
      if (s && s.p === location.pathname && Date.now() - s.t < 30000 && !location.hash) {
        window.scrollTo(0, s.y);
      }
    } catch (err) {}
  });

  // 3) Offline / slow network hint
  window.addEventListener('offline', function () { if (window.showToast) showToast('No internet. Your last action may not have saved.', 'error'); });
  window.addEventListener('online', function () { if (window.showToast) showToast('Back online.', 'success'); });
})();


// ============================================================
// Instant page opening: start loading a page the moment the admin touches / hovers its link
// ============================================================
(function () {
  var done = {};
  function prefetch(a) {
    if (!a || a.target === '_blank' || a.hasAttribute('download')) return;
    var u;
    try { u = new URL(a.href, location.href); } catch (e) { return; }
    if (u.origin !== location.origin || u.pathname === location.pathname && u.search === location.search) return;
    if (/\/(logout|delete|pdf|export|confirm|restore|purge)/.test(u.pathname)) return;   // never prefetch actions / files
    var key = u.pathname + u.search;
    if (done[key]) return; done[key] = 1;
    var l = document.createElement('link'); l.rel = 'prefetch'; l.href = u.href; l.as = 'document';
    document.head.appendChild(l);
  }
  var t;
  document.addEventListener('mouseover', function (e) { var a = e.target.closest && e.target.closest('a[href]'); if (!a) return; clearTimeout(t); t = setTimeout(function () { prefetch(a); }, 60); }, { passive: true });
  document.addEventListener('touchstart', function (e) { prefetch(e.target.closest && e.target.closest('a[href]')); }, { passive: true });
})();
