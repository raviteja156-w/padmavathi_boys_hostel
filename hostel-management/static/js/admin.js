// ============================================================
// Hostel Management System — Admin Dashboard JavaScript
// Instant client-side search/filter, delete confirmation, photo modal
// ============================================================

document.addEventListener('DOMContentLoaded', function () {
  const searchInput = document.getElementById('searchInput');
  const hostelFilter = document.getElementById('hostelFilter');
  const roomFilter = document.getElementById('roomFilter');

  function applyFilters() {
    const term = (searchInput.value || '').trim().toLowerCase();
    const hostelVal = hostelFilter.value;
    const roomVal = roomFilter.value;
    let anyVisible = false;

    document.querySelectorAll('.hostel-section').forEach(function (section) {
      const sectionHostel = section.getAttribute('data-hostel-section');
      const hostelMatches = (hostelVal === 'all' || hostelVal === sectionHostel);
      section.classList.toggle('hidden', !hostelMatches);
      if (!hostelMatches) return;

      section.querySelectorAll('.room-block').forEach(function (block) {
        const blockRoom = block.getAttribute('data-room');
        const roomMatches = (roomVal === 'all' || roomVal === blockRoom);
        block.classList.toggle('hidden', !roomMatches);
        if (!roomMatches) return;

        let visibleStudents = 0;
        const studentCards = block.querySelectorAll('.student-card');
        const totalStudents = studentCards.length;

        studentCards.forEach(function (card) {
          const name = card.getAttribute('data-name') || '';
          const contact = card.getAttribute('data-contact') || '';
          const matches = !term || name.includes(term) || contact.includes(term) || ('room ' + blockRoom).includes(term) || blockRoom === term;
          card.classList.toggle('hidden', !matches);
          if (matches) { visibleStudents++; anyVisible = true; }
        });

        const searchEmptyMsg = block.querySelector('.search-empty-msg');
        if (searchEmptyMsg) {
          searchEmptyMsg.classList.toggle('hidden', !(term && totalStudents > 0 && visibleStudents === 0));
        }
        if (totalStudents === 0 && !term) anyVisible = true; // empty rooms still "visible" by default
      });
    });

    const noResultsMsg = document.getElementById('noResultsMsg');
    if (noResultsMsg) {
      noResultsMsg.classList.toggle('hidden', !(term && !anyVisible));
    }
  }

  if (searchInput) {
    searchInput.addEventListener('input', applyFilters);
    hostelFilter.addEventListener('change', applyFilters);
    roomFilter.addEventListener('change', applyFilters);
  }
});

function openDeleteModal(actionUrl, studentName) {
  const form = document.getElementById('deleteForm');
  const nameEl = document.getElementById('deleteStudentName');
  form.action = actionUrl;
  nameEl.textContent = studentName;
  openModal('deleteModal');
}

function openPhotoModal(src, name) {
  document.getElementById('photoModalImg').src = src;
  document.getElementById('photoModalImg').alt = name;
  document.getElementById('photoModalName').textContent = name;
  openModal('photoModal');
}

// Close modal when clicking outside the box
document.addEventListener('click', function (e) {
  if (e.target.classList && e.target.classList.contains('modal-overlay')) {
    e.target.classList.add('hidden');
  }
});
