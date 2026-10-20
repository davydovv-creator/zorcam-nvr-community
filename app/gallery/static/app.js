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
// a clip with metadata (the watch page and the window over the timelapse; root is where its player is): the frame and the
// Motion badge are on while currentTime >= the motion time (the frame is also on the poster before playback starts); the
// mark on the scale under the video is the motion time, a tap seeks to it
// The system controls of a clip (iOS keeps them for seconds and has no setting for that): while it plays they go away
// a second after the last touch, a tap on the video brings them back; paused, they stay, so that there is something to press
var CONTROLS_HIDE_MS = 1000;
function autoHideControls(video) {
  var timer = 0;
  function later() {
    clearTimeout(timer);
    timer = setTimeout(function() { if (!video.paused) video.controls = false; }, CONTROLS_HIDE_MS);
  }
  function show() { video.controls = true; later(); }
  ['touchstart', 'mousemove', 'click'].forEach(function(ev) { video.addEventListener(ev, show, { passive: true }); });
  ['playing', 'seeking', 'volumechange'].forEach(function(ev) { video.addEventListener(ev, later); });
  video.addEventListener('pause', function() { clearTimeout(timer); video.controls = true; });
}
function initMotionClip(root) {
  root.querySelectorAll('video[controls]').forEach(autoHideControls);
  var frame = root.querySelector('.vframe');
  if (!frame) return;
  var video = frame.querySelector('video'), mbox = frame.querySelector('.mbox'), badge = frame.querySelector('.mbadge');
  var scale = root.querySelector('.mscale'), tick = scale && scale.querySelector('.mtick');
  var prog = scale && scale.querySelector('.mprog');
  var at = frame.hasAttribute('data-motion') ? parseFloat(frame.getAttribute('data-motion')) : null;
  var started = false;
  function update() {
    var on = at !== null && video.currentTime >= at;
    if (mbox) mbox.classList.toggle('on', on || (!started && !!video.getAttribute('poster')));  // before the start - over the poster
    if (badge) badge.classList.toggle('on', on);
    if (prog && video.duration) prog.style.width = Math.min(100, video.currentTime / video.duration * 100) + '%';
  }
  function ready() {
    placeBox(frame, video);
    if (scale && video.duration) {
      tick.style.left = Math.min(100, at / video.duration * 100) + '%';
      scale.classList.add('ready');
    }
    update();
  }
  video.addEventListener('playing', function() { started = true; update(); });
  video.addEventListener('seeked', function() { started = true; update(); });
  video.addEventListener('timeupdate', update);
  video.addEventListener('loadedmetadata', ready);
  video.addEventListener('durationchange', ready);
  video.addEventListener('resize', ready);
  window.addEventListener('resize', ready);
  if (tick) tick.addEventListener('click', function() { video.currentTime = Math.min(at, video.duration || at); });
  ready();
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
// The mode icon in the header (device or cloud): a tap or hover shows what the page is viewed through, and on the device
// a link to the cloud. A tap closes the hint again on a second tap, a tap elsewhere or after a few seconds; a hover keeps
// it while the pointer is on the icon or on the hint, so its link can be reached.
(function() {
  var shown = null, timer = 0, leave = 0;
  function hide() {
    clearTimeout(timer);
    clearTimeout(leave);
    if (shown) shown.hidden = true;
    shown = null;
  }
  function show(btn, auto) {
    hide();
    shown = btn.parentNode.querySelector('.modetip');
    shown.hidden = false;
    if (auto) timer = setTimeout(hide, 5000);
  }
  function zone(e) {
    return e.target && e.target.closest ? e.target.closest('.modebtn, .modetip') : null;
  }
  document.addEventListener('click', function(e) {
    if (e.target.closest && e.target.closest('.modetip')) return;  // the link in the hint goes on
    var btn = e.target.closest && e.target.closest('.modebtn');
    if (!btn) return hide();
    if (shown && shown.parentNode === btn.parentNode) hide(); else show(btn, true);
  });
  if (window.matchMedia && matchMedia('(hover: hover)').matches) {
    document.addEventListener('mouseover', function(e) {
      var el = zone(e);
      if (!el) return;
      clearTimeout(leave);
      if (el.classList.contains('modebtn') && (!shown || shown.parentNode !== el.parentNode)) show(el, false);
    });
    document.addEventListener('mouseout', function(e) {
      var from = zone(e), to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest('.modebtn, .modetip') : null;
      if (from && !to) leave = setTimeout(hide, 300);
    });
  }
})();
// The on-screen keyboard (iPhone): while a field has focus the tab bar is hidden, otherwise iOS lifts it above the keyboard,
// and after the keyboard closes leaves it in the middle of the screen until the next scroll. A scroll to the same place
// on blur makes iOS put the fixed elements back.
(function() {
  if (!window.matchMedia || !matchMedia('(pointer: coarse)').matches) return;
  var root = document.documentElement, timer = 0;
  function typing(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.tagName === 'TEXTAREA' || el.isContentEditable) return true;
    if (el.tagName !== 'INPUT') return false;
    return !/^(checkbox|radio|button|submit|reset|range|color|file|image|hidden)$/.test(el.type);
  }
  document.addEventListener('focusin', function(e) {
    if (!typing(e.target) || window.innerWidth >= 1024) return;
    clearTimeout(timer);
    root.classList.add('kbd');
  });
  document.addEventListener('focusout', function() {
    clearTimeout(timer);
    // the focus moving to the next field is not a keyboard close
    timer = setTimeout(function() {
      if (typing(document.activeElement)) return;
      root.classList.remove('kbd');
      window.scrollTo(window.scrollX, window.scrollY);
    }, 100);
  });
})();
