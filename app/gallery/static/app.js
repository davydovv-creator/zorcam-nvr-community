// The clock in the phone header: the Pi's time (not the phone's), ticks in JS from the Pi's UTC offset
(function() {
  var c = document.getElementById('clock');
  if (!c) return;
  var off = parseInt(c.getAttribute('data-off'), 10) || 0;
  function two(n) { return (n < 10 ? '0' : '') + n; }
  function tick() {
    var d = new Date(Date.now() + off * 1000);
    c.textContent = two(d.getUTCHours()) + ':' + two(d.getUTCMinutes()) + ':' + two(d.getUTCSeconds());
  }
  tick();
  setInterval(tick, 1000);
})();
// The motion frame (.mbox) lies in a container (a clip card, the player frame) and is set in fractions of the picture. The
// picture is fitted into the container by object-fit (cover in cards, contain in the player): placeBox turns the
// fractions into percents of the container by the real size of the image or video; until it is known, the percents from
// the HTML stay (right if the picture fills the container)
function placeBox(host, media) {
  var m = host.querySelector('.mbox');
  if (!m) return;
  var iw = media.naturalWidth || media.videoWidth, ih = media.naturalHeight || media.videoHeight;
  var cw = host.clientWidth, ch = host.clientHeight;
  if (!iw || !ih || !cw || !ch) return;
  var k = getComputedStyle(media).objectFit === 'cover' ? Math.max(cw / iw, ch / ih) : Math.min(cw / iw, ch / ih);
  var dw = iw * k, dh = ih * k, ox = (cw - dw) / 2, oy = (ch - dh) / 2, b = JSON.parse(m.getAttribute('data-box'));
  m.style.left = (ox + b[0] * dw) / cw * 100 + '%';
  m.style.top = (oy + b[1] * dh) / ch * 100 + '%';
  m.style.width = (b[2] - b[0]) * dw / cw * 100 + '%';
  m.style.height = (b[3] - b[1]) * dh / ch * 100 + '%';
}
// Pull to refresh (the main page, Settings -> Cameras and Status): only in the app on the Home Screen, Safari and Chrome have it
// built in. window.ptrBeforeReload is called before the reload, if set
(function() {
  var standalone = window.navigator.standalone === true ||
    (window.matchMedia && matchMedia('(display-mode: standalone)').matches);
  if (!standalone) return;
  var ptr = document.getElementById('ptr');
  if (!ptr) return;
  var arrow = ptr.querySelector('svg');
  var THRESHOLD = 80, MAX = 120, SLOP = 12;
  var startY = null, pull = 0, busy = false, pulling = false;

  function show(dist) {
    var k = Math.min(dist / THRESHOLD, 1);
    ptr.style.transition = 'none';
    ptr.style.opacity = k;
    ptr.style.transform = 'translateY(' + (Math.min(dist, MAX) - 60) + 'px)';
    ptr.classList.toggle('ready', dist >= THRESHOLD);
  }
  function hide() {
    ptr.style.transition = 'transform 0.2s, opacity 0.2s';
    ptr.style.opacity = 0;
    ptr.style.transform = 'translateY(-60px)';
    ptr.classList.remove('ready');
  }

  document.addEventListener('touchstart', function(e) {
    if (busy || e.touches.length !== 1 || window.scrollY > 0) { startY = null; return; }
    startY = e.touches[0].clientY; pull = 0; pulling = false;
  }, { passive: true });
  document.addEventListener('touchmove', function(e) {
    if (startY === null) return;
    var dy = e.touches[0].clientY - startY;
    if (dy <= 0 || window.scrollY > 0) { pull = 0; hide(); return; }
    // a tap always drifts a few px: preventDefault on it makes iOS drop the click (tabs, links near the top)
    if (dy < SLOP && !pulling) return;
    pulling = true;
    pull = (dy - SLOP) * 0.5;  // resistance, like the system gesture
    e.preventDefault();
    show(pull);
  }, { passive: false });
  document.addEventListener('touchend', function() {
    if (startY === null) return;
    startY = null;
    if (pull < THRESHOLD) { hide(); return; }
    busy = true;
    ptr.classList.remove('ready');
    ptr.classList.add('loading');
    arrow.innerHTML = '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>';
    ptr.style.transition = 'transform 0.2s';
    ptr.style.transform = 'translateY(' + (THRESHOLD - 60) + 'px)';
    if (window.ptrBeforeReload) window.ptrBeforeReload();
    setTimeout(function() { location.reload(); }, 150);
  });
})();
// The mode icon in the header (device or cloud): a tap or hover shows what the page is viewed through. A second tap, a tap
// elsewhere or a few seconds close the hint.
(function() {
  var tip = null, timer = 0;
  function hide() {
    clearTimeout(timer);
    if (tip && tip.parentNode) tip.parentNode.removeChild(tip);
    tip = null;
  }
  function show(btn) {
    hide();
    tip = document.createElement('div');
    tip.className = 'modetip';
    tip.setAttribute('role', 'tooltip');
    tip.textContent = btn.getAttribute('aria-label');  // the hint text is the button's own label (en or ru)
    btn.parentNode.appendChild(tip);
    timer = setTimeout(hide, 5000);
  }
  document.addEventListener('click', function(e) {
    var btn = e.target.closest && e.target.closest('.modebtn');
    if (!btn) return hide();
    if (tip && tip.parentNode === btn.parentNode) hide(); else show(btn);
  });
  if (window.matchMedia && matchMedia('(hover: hover)').matches) {
    document.addEventListener('mouseover', function(e) {
      var btn = e.target.closest && e.target.closest('.modebtn');
      if (btn && !(tip && tip.parentNode === btn.parentNode)) show(btn);
    });
    document.addEventListener('mouseout', function(e) {
      if (e.target.closest && e.target.closest('.modebtn')) hide();
    });
  }
})();
