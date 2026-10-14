// the tabs row scrolls when the labels do not fit: the current tab must be in sight
(function() {
  var a = document.querySelector('.subtabs a.active');
  if (a) a.parentNode.scrollLeft = a.offsetLeft - (a.parentNode.clientWidth - a.offsetWidth) / 2;
})();

// entered but not saved: leaving by a link (Back, Cancel, another tab) asks first
(function() {
  var forms = [].slice.call(document.querySelectorAll('form[method=POST]'));
  function state() {
    return forms.map(function(f) {
      return [].map.call(f.elements, function(el) {
        return el.name ? el.name + '=' + (el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value) : '';
      }).join('&');
    }).join('|');
  }
  var saved = state(), leaving = false;
  forms.forEach(function(f) { f.addEventListener('submit', function() { leaving = true; }); });
  document.addEventListener('click', function(e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || leaving || state() === saved) return;
    if (!confirm(T['js.unsaved'])) { e.preventDefault(); e.stopPropagation(); }
  }, true);
})();

// long actions (ONVIF search takes seconds): a spinner and data-busy text instead of the label, no second tap
(function() {
  function busy(el) {
    if (el.classList.contains('busy')) return false;
    el.dataset.label = el.innerHTML;
    el.innerHTML = '<span class="spinner"></span>' + el.getAttribute('data-busy');
    el.classList.add('busy');
    return true;
  }
  document.addEventListener('click', function(e) {
    var a = e.target.closest && e.target.closest('a[data-busy]');
    if (!a || e.defaultPrevented) return;
    if (!busy(a)) e.preventDefault();
  });
  document.querySelectorAll('form').forEach(function(f) {
    var b = f.querySelector('button[data-busy]');
    if (b) f.addEventListener('submit', function(e) { if (!busy(b)) e.preventDefault(); });
  });
  // Back in Safari restores the page from its cache as it was: put the labels back
  window.addEventListener('pageshow', function() {
    document.querySelectorAll('.busy').forEach(function(el) {
      el.innerHTML = el.dataset.label; el.classList.remove('busy');
    });
  });
})();

// the language of this device: the choice is kept in a cookie (for a year; "auto" removes it), the page reloads
document.querySelectorAll('select[data-lang-pick]').forEach(function(s) {
  s.addEventListener('change', function() {
    document.cookie = s.getAttribute('data-cookie') + '=' + (s.value === 'auto' ? '; max-age=0' : s.value + '; max-age=31536000') +
      '; path=/; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : '');
    location.reload();
  });
});

// a field of the camera form is active only while its switch is on (an inactive one is not sent: the values stay as they were)
document.querySelectorAll('[data-needs]').forEach(function(el) {
  var sw = el.form.querySelector('input[name="' + el.getAttribute('data-needs') + '"]');
  function sync() { el.disabled = !sw.checked; el.closest('.row').classList.toggle('off', !sw.checked); }
  sw.addEventListener('change', sync);
  sync();
});

// Notifications: a switch goes to the server at once (fetch), on an error it goes back
(function() {
  var all = document.querySelector('input[data-notify=all]');
  if (!all) return;
  var csrf = all.closest('section').getAttribute('data-csrf');
  var rows = [].slice.call(document.querySelectorAll('input[data-notify]')).filter(function(e) { return e !== all; });
  function sync() {
    rows.forEach(function(e) { e.disabled = !all.checked; e.closest('.row').classList.toggle('off', !all.checked); });
  }
  function send(el, cam) {
    var on = el.checked;
    fetch('/settings/notify', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({csrf: csrf, camera: cam, on: on})})
      .then(function(r) { if (!r.ok) throw 0; })
      .catch(function() { el.checked = !on; sync(); alert(T['notify.error']); });
  }
  all.addEventListener('change', function() { sync(); send(all, 'all'); });
  rows.forEach(function(e) { e.addEventListener('change', function() { send(e, e.getAttribute('data-notify')); }); });
  sync();
})();

// the camera's frame: no frame on the card (404) - remove the broken picture, the "No frame" plate stays
document.querySelectorAll('img.camframe-img').forEach(function(img) {
  function drop() { img.remove(); }
  img.addEventListener('error', drop);
  if (img.complete && !img.naturalWidth) drop();
});

// a page that updates itself (the disk is being set up, the cloud code waits): data-reload="ms"
(function() {
  var r = document.querySelector('[data-reload]');
  if (r) setTimeout(function() { location.reload(); }, parseInt(r.getAttribute('data-reload'), 10));
})();

// "Saved" and the wizard steps: go on by themselves, replacing the page in the history (otherwise "Back" returns to
// the page "Saved"); <meta http-equiv="refresh"> next to the element is the fallback without JavaScript
(function() {
  var r = document.querySelector('[data-replace]');
  if (r) setTimeout(function() { location.replace(r.getAttribute('data-replace')); }, parseInt(r.getAttribute('data-after'), 10));
})();
