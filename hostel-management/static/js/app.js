// ============================================================
// Hostel Management System — Global JavaScript
// Mobile navigation + toast notifications
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

  // Render any server-side flash messages as toasts
  if (window.__flashMessages && window.__flashMessages.length) {
    window.__flashMessages.forEach(function (msg, i) {
      const category = msg[0];
      const text = msg[1];
      setTimeout(function () { showToast(text, category === 'error' ? 'error' : 'success'); }, i * 120);
    });
  }
});

function showToast(message, type) {
  type = type || 'success';
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = 'toast' + (type === 'error' ? ' toast-error' : '');
  toast.innerHTML = `
    <i class="fa-solid ${type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-check'} toast-icon"></i>
    <span class="flex-1">${escapeHtml(message)}</span>
    <i class="fa-solid fa-xmark toast-close" onclick="this.parentElement.remove()"></i>
  `;
  container.appendChild(toast);

  setTimeout(function () {
    toast.style.transition = 'opacity .3s ease, transform .3s ease';
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(-8px)';
    setTimeout(function () { toast.remove(); }, 300);
  }, 4500);
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
