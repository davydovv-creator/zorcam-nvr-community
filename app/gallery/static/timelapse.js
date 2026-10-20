// ---- timelapse (Timelapse tab): the frames of a period at 15 fps, x1/x2/x4 skip frames, x0.25/x0.5 slow every frame down
// (the labels on the buttons are real-time multipliers, data-speed keeps the factor)
// the player of the Timelapse tab: null when the page has none (embedded Live)
var timelapse = (function() {
  var root = document.querySelector('.tl[data-cam]');
  if (!root) return null;
  var cam = root.getAttribute('data-cam');
  var canvas = root.querySelector('.tl-canvas'), ctx = canvas.getContext('2d');
  var timeEl = root.querySelector('.tl-time'), msg = root.querySelector('.tl-msg');
  var playBtn = root.querySelector('.tl-play'), range = root.querySelector('.tl-range');
  var strip = root.querySelector('.tl-strip');
  var FPS = 15, AHEAD = 24, PARALLEL = 5;  // frames kept ahead of the current one, requests at a time
  var frames = [], times = null, base = '', zone = '', cache = {}, loading = 0;  // frames: the file names
  var cur = -1, goal = 0, speed = 1, playing = false, active = false, dragging = false, started = false;
  var last = 0, stalled = 0, raf = 0, token = 0, period = 'today', motions = [], marks = [], markBox = null, relayout = 0;
  // pos: where frame j stands on the strip and the slider, in frames from the first one; a pause in the recording
  // (the camera was off) takes its real length there, so the strip shows it as a gap. total: the end of the strip
  var pos = null, total = 0, ival = 0;  // ival: the snapshot interval of the period, seconds
  // the edges of the day: the camera was off from midnight to the first frame (lead) or from the last frame to the end of
  // the day / to now (tail); then pos[0] > 0 and total > pos[last]. t0, tEnd: the time of the strip's two ends; nowT: the Pi's now
  var lead = false, tail = false, t0 = 0, tEnd = 0, nowT = 0;
  // standing in a gap (the player walks it at the pace of the frames, a grey screen): gapTo is the frame after it, gp the
  // place in pos units; -1 outside a gap
  var gapTo = -1, gp = -1;
  var videos = [], clipLen = 10, vlink = null;  // videos: {n: name, t: start} of the period, for the link over the strip
  var daysBox = root.querySelector('.tl-days'), calBox = root.querySelector('.tl-cal');
  var day = '', days = [], info = {}, cells = {}, today = '', daysRaw = '', daysAt = 0;  // day: the chosen calendar day, '' = a period
  var LANG = document.documentElement.lang || 'en';
  // opened from a motion push (?at=YYYYMMDD_HHMMSS, checked by the server): that day, paused at the motion
  var at = /^\d{8}_\d{6}$/.test(root.getAttribute('data-at') || '') ? root.getAttribute('data-at') : '';
  var note = root.querySelector('.tl-note'), modal = root.querySelector('.tl-modal');
  var modalBody = modal.querySelector('.tl-modal-body'), modalTime = modal.querySelector('.tl-modal-time');
  var modalDl = modal.querySelector('.tl-modal-dl'), modalFrom = null;
  var clipPrev = modal.querySelector('.tl-clip-prev'), clipNext = modal.querySelector('.tl-clip-next');
  var batch = [], batchAt = 0;  // the clips of the dot (earlier first) and the open one

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function stamp(t) {
    var d = new Date(t * 1000);
    return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + ' ' +
      pad(d.getUTCDate()) + '.' + pad(d.getUTCMonth() + 1) + '.' + d.getUTCFullYear();
  }
  // The reply is only the file names. The format is known to zorcam_config.parse_frame_name; here just the time and
  // the _m mark of YYYYMMDD_HHMMSS_mmm[_m].jpg. The time is the Pi's own, as in the names, hence Date.UTC.
  function timeOf(n) {
    return Date.UTC(+n.substr(0, 4), +n.substr(4, 2) - 1, +n.substr(6, 2), +n.substr(9, 2), +n.substr(11, 2), +n.substr(13, 2)) / 1000;
  }
  function isMotion(n) { return n.charCodeAt(19) === 95; }  // '_' after the milliseconds; '.' otherwise
  // the place and the time of frame k on the strip; -1 is the start of the strip, frames.length its end (the edges of the gaps)
  function pAt(k) { return k < 0 ? 0 : k >= frames.length ? total : pos[k]; }
  function tAt(k) { return k < 0 ? (lead ? t0 : times[0]) : k >= frames.length ? (tail ? tEnd : times[k - 1]) : times[k]; }
  function frac(j) { return total ? pAt(j) / total : 0; }  // 0 ... 1 along the strip
  // the frame nearest to the place p (in the units of pos); inside a gap — the nearer of its two edges
  function frameAt(p) {
    var lo = 0, hi = frames.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (pos[mid] < p) lo = mid + 1; else hi = mid; }
    return lo && p - pos[lo - 1] < pos[lo] - p ? lo - 1 : lo;
  }
  function hideLink() { if (vlink) { vlink.remove(); vlink = null; } }
  // above the slider at frame j: the videos of the motion there (each starts shortly before the motion and lasts clip
  // seconds), at most MAX_LINKS rows; more of them: the last row leads to the list of videos of the camera. The
  // window is widened to what a finger covers on the strip (7 px each way), so that clips of one dot of a day are all here.
  var MAX_LINKS = 4;
  function showLink(j) {
    hideLink();
    var T0 = times[j], w = strip.clientWidth;
    if (!isMotion(frames[j]) || !w) return false;
    var ex = 7 * (tAt(frames.length) - tAt(-1)) / w;
    var found = videos.filter(function(v) { return T0 >= v.t - 5 - ex && T0 <= v.t + clipLen + 30 + ex; });
    if (!found.length) return false;
    var more = found.length > MAX_LINKS;
    // the whole batch of the dot, earlier first: the window of a clip turns the pages through it
    var all = found.slice().sort(function(p, q) { return p.t - q.t; });
    found.sort(function(p, q) { return Math.abs(T0 - p.t) - Math.abs(T0 - q.t); });
    found = found.slice(0, MAX_LINKS).sort(function(p, q) { return p.t - q.t; });
    var box = document.createElement('div');
    box.className = 'tl-vlist';
    function row(href, text, label, clip) {
      var a = document.createElement('a');
      a.className = 'tl-vlink';
      a.href = href;
      a.textContent = text;
      if (label) a.setAttribute('aria-label', label);
      if (clip) a.addEventListener('click', function(e) {  // the clip opens over the player; a new tab keeps the page
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button) return;
        e.preventDefault();
        var k = all.map(function(v) { return v.n; });
        openClip(clip, a, k, k.indexOf(clip));
      });
      box.appendChild(a);
    }
    found.forEach(function(v) {
      var at = v.n.substr(9, 2) + ':' + v.n.substr(11, 2) + ':' + v.n.substr(13, 2);
      row('/watch/' + encodeURIComponent(cam) + '/' + encodeURIComponent(v.n), T['tl.video'] + ' ' + at + ' ›',
        T['tl.video_open'] + ', ' + at, v.n);
    });
    if (more) row('/?tab=videos&cam=' + encodeURIComponent(cam), T['tl.all_videos'] + ' ›');
    strip.appendChild(box);
    // by the thumb, but inside the player: the strip is 7 px inside it on both sides
    var half = box.offsetWidth / 2, at = frac(j) * w;
    box.style.left = Math.max(half - 7, Math.min(w + 7 - half, at)) + 'px';
    // the list stands on the lower edge of the picture (8 px up), so that the thumb and the slider stay free
    var stage = root.querySelector('.tl-stage');
    box.style.bottom = 'calc(100% + ' + Math.round(strip.getBoundingClientRect().top - stage.getBoundingClientRect().bottom + 8) + 'px)';
    vlink = box;
    return true;
  }
  // the place p (pos units) strictly inside a gap: the frame after it (frames.length for the tail); -1 if p is on a frame
  // or between close ones
  function gapAt(p) {
    if (lead && p < pos[0]) return 0;
    if (tail && p > pos[frames.length - 1]) return frames.length;
    var lo = 0, hi = frames.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (pos[mid] < p) lo = mid + 1; else hi = mid; }
    return lo && p > pos[lo - 1] && p < pos[lo] && pos[lo] - pos[lo - 1] > 1 ? lo : -1;
  }
  function say(text) { msg.textContent = text || ''; }

  // The clip over the player: the same markup and script as the watch page (the fragment of /watch/<camera>/<clip>),
  // the timelapse under it stays paused on its frame.
  var clipToken = 0, RATIO_KEY = 'zorcam_clip_ratio_' + cam;
  // The proportion of the camera's clips (the main stream): the place for the clip is kept in it from the start, the poster
  // (a frame of the second stream, often of another proportion) is stretched to it, so the window does not jump on load
  function clipRatio() {
    try { var r = parseFloat(localStorage.getItem(RATIO_KEY)); if (r > 0.2 && r < 5) return r; } catch (e) {}
    return 16 / 9;
  }
  function fitClip(video) {
    video.style.aspectRatio = String(clipRatio());
    video.classList.add('pre');
    // registered before initMotionClip: the frame of motion is placed after the proportion is set
    video.addEventListener('loadedmetadata', function() {
      if (!video.videoWidth || !video.videoHeight) return;
      var r = video.videoWidth / video.videoHeight;
      video.style.aspectRatio = String(r);
      video.classList.remove('pre');
      try { localStorage.setItem(RATIO_KEY, String(r)); } catch (e) {}
    });
  }
  // the arrows stand at the middle of the picture (not of the whole body, where the scale of motion is under it)
  function placeArrows() {
    var v = modalBody.querySelector('video') || modalBody.querySelector('.tl-modal-ph');
    var top = v && v.offsetHeight ? v.offsetTop + v.offsetHeight / 2 : modalBody.offsetHeight / 2;
    clipPrev.style.top = clipNext.style.top = top + 'px';
  }
  function openClip(name, from, list, idx, quiet) {
    if (list) { batch = list; batchAt = Math.max(0, idx); }
    var mine = ++clipToken, ts = timeOf(name), url = '/watch/' + encodeURIComponent(cam) + '/' + encodeURIComponent(name);
    pause();
    modalFrom = from || null;
    modalBody.innerHTML = '';
    var ph = document.createElement('div');
    ph.className = 'tl-modal-ph';
    ph.style.aspectRatio = String(clipRatio());
    modalBody.appendChild(ph);
    modalTime.textContent = stamp(ts) + (zone ? ' ' + zone : '') + (batch.length > 1 ? ' · ' + (batchAt + 1) + '/' + batch.length : '');
    clipPrev.hidden = batch.length < 2 || batchAt <= 0;
    clipNext.hidden = batch.length < 2 || batchAt >= batch.length - 1;
    placeArrows();
    modalDl.href = '/vid/' + encodeURIComponent(cam) + '/' + encodeURIComponent(name + '.mp4') + '?dl=1';
    modal.hidden = false;
    document.body.classList.add('modal-open');
    var cur = document.activeElement;
    if (!quiet || !cur || !modal.contains(cur) || cur.hidden) modal.querySelector('.tl-modal-x').focus();
    fetch(url + '?frag=1', { credentials: 'same-origin' })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function(html) {
        if (mine !== clipToken) return;
        modalBody.innerHTML = html;  // built by the server from the escaped parts (watch_media)
        var video = modalBody.querySelector('video');
        if (video) {
          // without the poster: the clip is opened by a tap and plays at once, the poster would only flash before it
          video.removeAttribute('poster');
          fitClip(video);
          video.addEventListener('loadedmetadata', placeArrows);
        }
        placeArrows();
        initMotionClip(modalBody);
        if (video) { var pl = video.play(); if (pl && pl.catch) pl.catch(function() {}); }
      })
      .catch(function() {
        if (mine !== clipToken) return;
        var p = document.createElement('p');
        modalBody.innerHTML = '';
        p.className = 'tl-modal-err';
        p.textContent = T['tl.clip_error'];
        modalBody.appendChild(p);
      });
  }
  function closeClip() {
    if (modal.hidden) return;
    clipToken++;
    modal.hidden = true;
    modalBody.innerHTML = '';  // the video stops with its element
    document.body.classList.remove('modal-open');
    if (modalFrom && document.contains(modalFrom)) modalFrom.focus();
    modalFrom = null;
  }
  // d = -1: the earlier clip of the batch, 1: the later one
  function turnClip(d) {
    var k = batchAt + d;
    if (modal.hidden || k < 0 || k >= batch.length) return;
    batchAt = k;
    openClip(batch[k], modalFrom, null, 0, true);
  }
  // a swipe over the clip: to the left (the finger goes right to left) — the later one, to the right — the earlier one;
  // not from the scale of motion and not from the bar of the native controls at the bottom of the video
  var swipe = null;
  modalBody.addEventListener('touchstart', function(e) {
    swipe = null;
    if (e.touches.length !== 1 || batch.length < 2) return;
    var tc = e.touches[0], el = e.target;
    if (el.closest && el.closest('.mscale')) return;
    if (el.tagName === 'VIDEO' && tc.clientY > el.getBoundingClientRect().bottom - 56) return;
    swipe = { x: tc.clientX, y: tc.clientY };
  }, { passive: true });
  modalBody.addEventListener('touchmove', function(e) { if (e.touches.length > 1) swipe = null; }, { passive: true });
  modalBody.addEventListener('touchend', function(e) {
    var s = swipe, tc = e.changedTouches[0];
    swipe = null;
    if (!s || !tc) return;
    var dx = tc.clientX - s.x, dy = tc.clientY - s.y;
    if (Math.abs(dx) >= 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) turnClip(dx < 0 ? 1 : -1);
  }, { passive: true });
  modalBody.addEventListener('touchcancel', function() { swipe = null; }, { passive: true });
  clipPrev.addEventListener('click', function() { turnClip(-1); });
  clipNext.addEventListener('click', function() { turnClip(1); });
  modal.addEventListener('click', function(e) {  // the dark field around the clip, the cross
    if (e.target === modal || (e.target.closest && e.target.closest('.tl-modal-x'))) closeClip();
  });

  function dropFrame(j) {
    var e = cache[j];
    delete cache[j];
    if (e && e.state === 'loading') {
      // counted out here: Safari does not always fire onerror for a cancelled image, and a stuck counter stops loading
      e.img.onload = e.img.onerror = null;
      loading--;
      e.img.src = '';
    }
  }
  // x1, x2, x4 skip frames (the step), x0.25 and x0.5 show every frame, only slower: FPS * speed
  function stepOf() { return speed < 1 ? 1 : speed; }
  // what is wanted now: the goal and AHEAD frames after it (fewer when slow); everything else is let go (memory on the phone)
  function wanted() {
    var list = [], ahead = speed < 1 ? AHEAD / 2 : AHEAD;
    for (var i = goal, k = 0; i < frames.length && k <= ahead; i += stepOf(), k++) list.push(i);
    return list;
  }
  function pump() {
    if (!frames.length) return;
    var list = wanted(), keep = {};
    list.forEach(function(j) { keep[j] = true; });
    Object.keys(cache).forEach(function(k) { if (!keep[k]) dropFrame(k); });
    for (var n = 0; n < list.length && loading < PARALLEL; n++) {
      if (!cache[list[n]]) fetchFrame(list[n]);
    }
  }
  function fetchFrame(j) {
    var e = { img: new Image(), state: 'loading' };
    cache[j] = e;
    loading++;
    e.img.onload = function() {
      loading--;
      if (cache[j] !== e) { pump(); return; }
      e.state = 'ready';
      if (!playing) present();
      pump();
    };
    e.img.onerror = function() {
      loading--;
      if (cache[j] === e) e.state = 'bad';  // a frame removed meanwhile: skipped
      pump();
    };
    e.img.src = base + frames[j];
  }

  function setPosition(j) {
    timeEl.textContent = stamp(times[j]) + (zone ? ' ' + zone : '');  // the Pi's time zone: MSK or UTC+03:00
    root.classList.toggle('motion', isMotion(frames[j]));
    if (!dragging) range.value = Math.round(pos[j]);
    range.style.setProperty('--p', frac(j) * 100 + '%');
  }
  // draws the goal frame if it has arrived; a frame that failed is passed over
  function present() {
    if (gapTo >= 0) return false;  // the grey screen of a gap stays until the player leaves it
    var e = cache[goal];
    while (e && e.state === 'bad') {
      if (goal >= frames.length - 1) { cur = goal; pause(); return false; }  // the last frame is gone: the end
      goal = Math.min(frames.length - 1, goal + stepOf());
      e = cache[goal];
      pump();
    }
    if (!e || e.state !== 'ready') return false;
    if (canvas.width !== e.img.naturalWidth) { canvas.width = e.img.naturalWidth; canvas.height = e.img.naturalHeight; }
    ctx.drawImage(e.img, 0, 0, canvas.width, canvas.height);
    cur = goal;
    setPosition(cur);
    say('');
    stalled = 0;
    pump();
    return true;
  }

  // the gap before frame k at the place p: a grey screen with the notice, the time goes on; the frames after it load meanwhile
  function showGap(k, p) {
    gapTo = k; gp = p; goal = Math.min(k, frames.length - 1); cur = k - 1; stalled = 0;
    ctx.fillStyle = '#6e6e6a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    timeEl.textContent = stamp(Math.min(tAt(k), tAt(k - 1) + (p - pAt(k - 1)) * ival)) + (zone ? ' ' + zone : '');
    root.classList.remove('motion');
    if (!dragging) range.value = Math.round(p);
    range.style.setProperty('--p', p / total * 100 + '%');
    say(T['tl.no_frames']);
    pump();
  }
  function stepGap(ts) {
    var p = gp + stepOf();
    if (p < pAt(gapTo)) { showGap(gapTo, p); last = ts; return; }
    if (gapTo >= frames.length) { showGap(gapTo, total); pause(); return; }  // the tail is walked: grey at the end, stopped
    goal = gapTo; gapTo = -1; gp = -1;  // the gap is over: on to the frame after it
    pump();
    if (present()) last = ts;
  }
  function tick(ts) {
    raf = playing && active ? requestAnimationFrame(tick) : 0;
    if (ts - last < 1000 / (FPS * Math.min(speed, 1)) - 3) return;
    if (gapTo >= 0) { stepGap(ts); return; }
    if (cur === goal) {
      if (cur >= frames.length - 1) {
        if (!tail) { pause(); return; }
        gapTo = frames.length; gp = pos[cur]; stepGap(ts);  // the camera was off after the last frame
        return;
      }
      var nx = Math.min(frames.length - 1, cur + stepOf());
      for (var i = cur; i < nx; i++) if (pos[i + 1] - pos[i] > 1) { nx = i; break; }  // never skip over the start of a gap
      if (nx === cur) { gapTo = cur + 1; gp = pos[cur]; stepGap(ts); return; }
      goal = nx;
      pump();
    }
    if (present()) { last = ts; return; }
    if (!stalled) stalled = ts;
    else if (ts - stalled > 500) say(T['js.buffering']);
  }
  function play() {
    if (!frames.length) return;
    hideLink();
    // from the true end (the tail walked, or the last frame with no tail) the play starts again; mid-tail it goes on
    if (gapTo >= frames.length ? gp >= total : !tail && cur >= frames.length - 1) seek(0);
    playing = true;
    root.classList.add('playing');
    playBtn.setAttribute('aria-label', T['tl.pause']);
    if (!raf && active) raf = requestAnimationFrame(tick);
  }
  function pause() {
    playing = false;
    root.classList.remove('playing');
    playBtn.setAttribute('aria-label', T['tl.play']);
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }
  function seek(j) {
    if (!frames.length) return;
    gapTo = -1; gp = -1;
    goal = Math.max(0, Math.min(frames.length - 1, j));
    setPosition(goal);
    last = 0;
    pump();
    if (!playing) present();
  }

  // The speed buttons are real-time multipliers: the default (every snapshot, factor 1) runs FPS * the snapshot interval
  // times faster than life, the others are 0.25 ... 4 of it. The interval is the median gap between neighbouring frames
  // (the camera's setting, undisturbed by pauses in the recording); under 2 frames it is unknown: plain factors.
  function snapshotInterval() {
    var gaps = [];
    for (var i = 1; times && i < times.length; i++) if (times[i] > times[i - 1]) gaps.push(times[i] - times[i - 1]);
    if (!gaps.length) return 0;
    gaps.sort(function(a, b) { return a - b; });
    var m = gaps.length >> 1;
    return gaps.length % 2 ? gaps[m] : (gaps[m - 1] + gaps[m]) / 2;
  }
  function compact(x) {  // 1500 -> 1.5k: five buttons must fit in 390 px
    return x >= 1000 ? String(Math.round(x / 100) / 10) + 'k' : String(x);
  }
  function labelSpeeds() {
    var every = snapshotInterval() * FPS;
    root.querySelectorAll('[data-speed]').forEach(function(b) {
      var f = parseFloat(b.getAttribute('data-speed'));
      if (!every) { b.textContent = '×' + f; b.removeAttribute('title'); b.removeAttribute('aria-label'); return; }
      var n = compact(Math.max(1, Math.round(every * f)));
      b.textContent = '×' + n;
      b.title = n + T['tl.realtime'];
      b.setAttribute('aria-label', n + T['tl.realtime']);
    });
  }
  function resetSpeed() {  // the default (every snapshot) on every load of a period or a day
    speed = 1;
    root.querySelectorAll('[data-speed]').forEach(function(o) {
      o.classList.toggle('active', o.getAttribute('data-speed') === '1');
    });
  }

  // A pause longer than this many intervals between snapshots (and than a minute) is a gap on the strip: the camera
  // was off or the recording stood. Shorter ones (a reconnect) keep one frame's step, as before.
  var GAP_INTERVALS = 3, GAP_MIN = 60;
  // one pass over the names: the times and the places on the strip, the gaps, the red motion marks, the places where
  // the hour or the date changes (an hour inside a gap stands where it is in time)
  function buildStrip() {
    strip.innerHTML = '';
    markBox = null;
    motions = []; marks = [];
    var n = frames.length, run = null, i;
    times = new Float64Array(n);
    pos = new Float64Array(n);
    for (i = 0; i < n; i++) times[i] = timeOf(frames[i]);
    var every = ival = snapshotInterval();
    function span(cls, a, b) {
      var m = document.createElement('span');
      m.className = cls;
      m.style.left = a / total * 100 + '%';
      m.style.width = (b - a) / total * 100 + '%';
      strip.appendChild(m);
    }
    var limit = every ? Math.max(GAP_INTERVALS * every, GAP_MIN) : 0;
    // the day starts at midnight: the time before the first frame is a gap only if the camera recorded on an earlier day
    // (otherwise it did not exist yet, or the old records are cleaned up); the day ends at 23:59:59, today — now
    var dstr = frames[0].substr(0, 4) + '-' + frames[0].substr(4, 2) + '-' + frames[0].substr(6, 2);
    t0 = Math.floor(times[0] / 86400) * 86400;
    tEnd = nowT ? Math.min(t0 + 86399, nowT) : times[n - 1];
    lead = !!limit && times[0] - t0 > limit && days.some(function(it) { return it.d < dstr && it.frames > 0; });
    tail = !!limit && tEnd - times[n - 1] > limit;
    if (lead) pos[0] = (times[0] - t0) / every;
    for (i = 1; i < n; i++) {
      var gap = times[i] - times[i - 1];
      pos[i] = pos[i - 1] + (every && gap > limit ? gap / every : 1);
    }
    total = pos[n - 1] + (tail ? (tEnd - times[n - 1]) / every : 0);
    // the gap before frame k lies between k - 1 and k; before the first and after the last one only at the edges of the day
    var kFrom = lead ? 0 : 1, kTo = tail ? n : n - 1;
    for (i = kFrom; i <= kTo; i++) if (pAt(i) - pAt(i - 1) > 1) span('g', pAt(i - 1), pAt(i));
    for (i = kFrom; i <= kTo; i++) {
      var ta = tAt(i - 1), tb = tAt(i), h0 = Math.floor(ta / 3600), h1 = Math.floor(tb / 3600);
      for (var h = h0 + 1; h <= h1; h++) {  // every hour boundary between the two frames
        var t = Math.max(ta, Math.min(tb, h * 3600));
        var at = pAt(i - 1) + (tb > ta ? (t - ta) / (tb - ta) : 1) * (pAt(i) - pAt(i - 1));
        marks.push({ p: at, h: h % 24, day: h % 24 === 0, t: h * 3600 });
      }
    }
    function flush() { if (run) span('m', pos[run[0]], pos[run[1]]); run = null; }
    if (total) for (i = 0; i < n; i++) {
      if (!isMotion(frames[i])) { flush(); continue; }
      motions.push(i);
      if (run && run[1] === i - 1) run[1] = i; else { flush(); run = [i, i]; }
    }
    flush();
    layoutMarks();
  }

  // Day and hour marks under the strip, by frame index like the motion marks. A day line is always drawn; hour lines
  // not closer than 5 px (every 1, 2, 3, 6 or 12 hours, as the width allows); labels not closer than 30 px: the
  // hour labels are dropped first, then the day labels that would overlap each other.
  var STEPS = [1, 2, 3, 6, 12], MIN_LINE = 5, MIN_LABEL = 30;
  function spaced(xs, min) {
    for (var k = 1; k < xs.length; k++) if (xs[k] - xs[k - 1] < min) return false;
    return true;
  }
  function layoutMarks() {
    if (markBox) { markBox.remove(); markBox = null; }
    var w = strip.clientWidth;
    if (!frames.length || !w) return;
    var x = function(m) { return total ? m.p / total * w : 0; };
    var lines = null, step = 0;
    STEPS.some(function(s) {
      var pick = marks.filter(function(m) { return m.day || m.h % s === 0; });
      if (!spaced(pick.map(x), MIN_LINE)) return false;
      lines = pick; step = s;
      return true;
    });
    if (!lines) lines = marks.filter(function(m) { return m.day; });
    var days = [], edge = -1e9;
    lines.forEach(function(m) { if (m.day && x(m) - edge >= MIN_LABEL) { days.push(m); edge = x(m); } });
    var hours = [];
    if (step) STEPS.some(function(s) {
      if (s < step || s % step) return false;
      var pick = lines.filter(function(m) { return !m.day && m.h % s === 0; });
      var xs = pick.concat(days).map(x).sort(function(a, b) { return a - b; });
      if (!pick.length || !spaced(xs, MIN_LABEL)) return false;
      hours = pick;
      return true;
    });
    markBox = document.createElement('span');
    markBox.className = 'tl-marks';
    function put(cls, m, text) {
      var e = document.createElement('span');
      e.className = cls;
      e.style.left = (total ? m.p / total * 100 : 0) + '%';
      if (text) e.textContent = text;
      markBox.appendChild(e);
      return e;
    }
    // the first and the last frame: a line like the hour lines and the time under it (a date too when the period
    // crosses midnight), the first label grows to the right of its line and the last to the left, so that none leaves the strip
    var a = stamp(tAt(-1)), b = stamp(tAt(frames.length));
    var dates = a.slice(9, 14) !== b.slice(9, 14);
    var end = function(s) { return (dates ? s.slice(9, 14) + ' ' : '') + s.slice(0, 5); };
    var first = { p: 0 }, finish = { p: total || 1 };
    put('tl-tick', first); put('tl-tick', finish);
    var e0 = put('tl-lab edge first', first, end(a)), e1 = put('tl-lab edge last', finish, end(b));
    strip.appendChild(markBox);  // in the document the widths are known
    var taken = [{ a: 0, b: e0.offsetWidth }, { a: w - e1.offsetWidth, b: w }];
    lines.forEach(function(m) { put(m.day ? 'tl-tick day' : 'tl-tick', m); });
    // a label that would touch the first or the last one is dropped (its line stays), the end labels never
    function put2(m, text) {
      var e = put('tl-lab', m, text), c = x(m), half = e.offsetWidth / 2;
      if (taken.some(function(r) { return c + half + 3 > r.a && c - half - 3 < r.b; })) e.remove();
    }
    days.forEach(function(m) {
      var d = new Date(m.t * 1000);
      put2(m, pad(d.getUTCDate()) + '.' + pad(d.getUTCMonth() + 1));
    });
    hours.forEach(function(m) { put2(m, pad(m.h)); });
  }

  // The calendar: one button per day from the oldest with snapshots or videos to today, built once per reply of ?days=1.
  // A day without snapshots is dimmed and not tappable. Two fixed slots under the number: the timelapse
  // (there are snapshots) and the videos; a fact that is false leaves its icon faint.
  var SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round">';
  // a stack of frames with the play triangle over it
  var TIMELAPSE = SVG + '<path d="M7 5h12M5 8h14"/><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M10.5 13.5v5l4-2.5z" fill="currentColor"/></svg>';
  var VIDEO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M3 8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM15 11l6-3v8l-6-3"/></svg>';
  function markDay() {
    Object.keys(cells).forEach(function(d) {
      var on = d === day;
      cells[d].classList.toggle('sel', on);
      cells[d].setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  function reveal(d, smooth) {
    var c = cells[d];
    if (!c || !daysBox.clientWidth) return;
    var left = c.parentNode.offsetLeft - (daysBox.clientWidth - c.parentNode.offsetWidth) / 2;
    if (daysBox.scrollTo) daysBox.scrollTo({ left: left, behavior: smooth ? 'smooth' : 'auto' }); else daysBox.scrollLeft = left;
  }
  function buildDays() {
    var wdf = new Intl.DateTimeFormat(LANG, { weekday: 'short', timeZone: 'UTC' });
    var mof = new Intl.DateTimeFormat(LANG, { month: 'short', timeZone: 'UTC' });
    var fullf = new Intl.DateTimeFormat(LANG, { day: 'numeric', month: 'short', timeZone: 'UTC' });
    var nf = new Intl.NumberFormat(LANG), year = +today.slice(0, 4), month = -1;
    var frag = document.createDocumentFragment();
    cells = {}; info = {};
    days.forEach(function(it) {
      var p = it.d.split('-'), dt = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
      info[it.d] = it;
      var w = document.createElement('span'), mo = document.createElement('span'), b = document.createElement('button');
      w.className = 'tl-dw';
      mo.className = 'mo';
      if (dt.getUTCMonth() !== month) {
        month = dt.getUTCMonth();
        mo.textContent = mof.format(dt).replace(/[.]$/, '') + (+p[0] !== year ? ' ' + p[0].slice(2) : '');
      }
      b.type = 'button';
      b.className = 'tl-day' + (it.d === today ? ' today' : '');
      b.setAttribute('data-d', it.d);
      b.innerHTML = '<span class="wd"></span><span class="num"></span><span class="mk">' +
        '<i class="tm' + (it.frames ? ' on' : '') + '">' + TIMELAPSE + '</i>' +
        '<i class="vd' + (it.clips ? ' on' : '') + '">' + (it.clips ? '<b class="cnt"></b>' : '') + VIDEO + '</i></span>';
      if (it.clips) b.querySelector('.cnt').textContent = it.clips > 99 ? '99+' : it.clips;
      b.children[0].textContent = wdf.format(dt).replace(/[.]$/, '');
      b.children[1].textContent = +p[2];
      var label = fullf.format(dt) + ', ' + (it.frames ? T['tl.day_frames'] + ' ' + nf.format(it.frames) : T['tl.day_none']) +
        ', ' + (it.clips ? T['tl.day_clips'] + ' ' + nf.format(it.clips) : T['tl.day_no_clips']);
      b.setAttribute('aria-label', label);
      b.title = label;
      if (!it.frames) { b.setAttribute('aria-disabled', 'true'); b.tabIndex = -1; }
      cells[it.d] = b;
      w.appendChild(mo);
      w.appendChild(b);
      frag.appendChild(w);
    });
    daysBox.innerHTML = '';
    daysBox.appendChild(frag);
    calBox.hidden = !days.length;
    markDay();
    reveal(day || today, false);
  }
  function fetchDays() {
    return fetch(root.getAttribute('data-api') + '?days=1', { credentials: 'same-origin', cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function(text) {
        daysAt = Date.now();
        if (text === daysRaw) return;  // nothing new: the row stays as it is, with its scroll position
        var data = JSON.parse(text);
        daysRaw = text; days = data.days || []; today = data.today || '';
        buildDays();
      });
  }
  daysBox.addEventListener('click', function(e) {
    var b = e.target.closest ? e.target.closest('.tl-day') : null;
    if (b && b.getAttribute('aria-disabled') !== 'true') load(period, b.getAttribute('data-d'));
  });
  root.querySelectorAll('.tl-calnav').forEach(function(b) {
    b.addEventListener('click', function() {
      var by = daysBox.clientWidth * 0.7 * (b.classList.contains('prev') ? -1 : 1);
      if (daysBox.scrollBy) daysBox.scrollBy({ left: by, behavior: 'smooth' }); else daysBox.scrollLeft += by;
    });
  });
  // [ and ]: the previous or next day that has snapshots; with a period chosen, [ goes to the newest such day
  function stepDay(dir) {
    var list = days.filter(function(it) { return it.frames > 0; }), cur = day || '9999', pick = null;
    for (var i = dir < 0 ? list.length - 1 : 0; i >= 0 && i < list.length; i += dir < 0 ? -1 : 1) {
      if (dir < 0 ? list[i].d < cur : list[i].d > cur) { pick = list[i]; break; }
    }
    if (pick) { load(period, pick.d); reveal(pick.d, true); }
  }

  // The address of a push (?cam=…&at=…) is used once: a reload must not jump back to that moment
  function forgetAt() {
    try {
      var url = new URL(location.href);
      url.searchParams.delete('cam');
      url.searchParams.delete('at');
      history.replaceState(null, '', url);
    } catch (e) {}
  }
  // the first motion frame at or after the moment t (seconds, as in times); none: the nearest frame of the day
  function frameFor(t) {
    for (var i = 0; i < frames.length; i++) if (times[i] >= t && isMotion(frames[i])) return i;
    var lo = 0, hi = frames.length - 1;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (times[mid] < t) lo = mid + 1; else hi = mid; }
    return lo && t - times[lo - 1] < times[lo] - t ? lo - 1 : lo;
  }

  // p: a period (always today: since midnight); d: a calendar day YYYY-MM-DD instead of it.
  // mode 'at': the day of a push, 'gone': today instead of it, the notice stays
  function load(p, d, mode) {
    var mine = ++token;
    d = d || '';
    day = d;
    if (!mode) note.textContent = '';
    if (!d) period = p;
    markDay();
    if (Date.now() - daysAt > 300000) fetchDays().catch(function() {});  // the list ages: midnight, new videos
    pause();
    Object.keys(cache).forEach(dropFrame);
    hideLink(); videos = [];
    frames = []; times = null; pos = null; total = 0; lead = tail = false; marks = []; markBox = null; cur = -1; goal = 0; stalled = 0; gapTo = -1; gp = -1;
    resetSpeed(); labelSpeeds();
    range.max = 0; range.value = 0; range.disabled = true;
    strip.innerHTML = ''; timeEl.textContent = '';
    root.classList.remove('motion');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    say(T['tl.loading']);
    fetch(root.getAttribute('data-api') + (d ? '?day=' + d : '?period=' + p), { credentials: 'same-origin', cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(data) {
        if (mine !== token) return;
        videos = (data.videos || []).map(function(n) { return { n: n, t: timeOf(n) }; });
        clipLen = data.clip || 10;
        frames = data.frames; base = data.base; zone = (data.tz && data.tz.label) || '';
        nowT = data.now || 0;
        if (mode === 'at' && !frames.length) {  // the day is gone (cleanup): today and a notice
          note.textContent = T['tl.deleted'].replace('{when}', at.substr(6, 2) + '.' + at.substr(4, 2) + ' ' +
            at.substr(9, 2) + ':' + at.substr(11, 2));
          forgetAt();
          load('today', '', 'gone');
          return;
        }
        if (mode) forgetAt();
        if (!frames.length) { say(T['tl.empty']); return; }  // "Loading…" stays until the first frame is drawn
        buildStrip();
        range.max = Math.round(total); range.disabled = false;
        labelSpeeds();
        goal = 0;
        if (mode === 'at') seek(frameFor(timeOf(at)));  // stands paused on the motion; it plays on the play button
        else pump();  // the first frame only: it plays on the play button, not by itself
      })
      .catch(function() { if (mine === token) { say(T['tl.error']); if (mode) forgetAt(); } });
  }

  root.querySelectorAll('[data-speed]').forEach(function(b) {
    b.addEventListener('click', function() {
      speed = parseFloat(b.getAttribute('data-speed'));
      root.querySelectorAll('[data-speed]').forEach(function(o) { o.classList.toggle('active', o === b); });
      pump();
    });
  });
  playBtn.addEventListener('click', function() { if (playing) pause(); else play(); });

  // the slider: while it is held the player stands, on release it goes on if it was playing
  var resume = false;
  function hold() { if (dragging) return; dragging = true; resume = playing; pause(); }
  function release() { if (!dragging) return; dragging = false; if (resume) play(); resume = false; }
  range.addEventListener('pointerdown', hold);
  range.addEventListener('touchstart', hold, { passive: true });
  range.addEventListener('input', function() {
    hold();
    var v = parseInt(range.value, 10), j = frameAt(v), k = gapAt(v);
    if (k >= 0) { showGap(k, v); return; }  // inside a gap: stands there, grey
    // a step of the keys inside a gap would snap back to the same frame: over the gap, to the next one
    if (j === cur && v !== Math.round(pos[cur])) j = Math.max(0, Math.min(frames.length - 1, cur + (v > pos[cur] ? 1 : -1)));
    seek(j);
  });
  range.addEventListener('change', release);
  window.addEventListener('pointerup', release);
  window.addEventListener('touchend', release);
  window.addEventListener('touchcancel', release);

  // a tap on the strip: to that place, to the nearest motion mark if one is within a finger's width
  strip.addEventListener('click', function(e) {
    if (!frames.length || (e.target.closest && e.target.closest('.tl-vlist'))) return;
    hideLink();
    var r = strip.getBoundingClientRect();
    var x = Math.max(0, Math.min(r.width, e.clientX - r.left)), j = frameAt(x / r.width * total), best = 14;
    motions.forEach(function(m) {
      var d = Math.abs(frac(m) * r.width - x);
      if (d < best) { best = d; j = m; }
    });
    var k = best === 14 ? gapAt(x / r.width * total) : -1;
    if (k >= 0 && frac(k) * r.width - x > 3 && x - frac(k - 1) * r.width > 3) { pause(); showGap(k, x / r.width * total); return; }
    seek(j);
    if (showLink(j)) pause();  // the link would be stale as soon as the frames moved on
  });
  document.addEventListener('click', function(e) { if (!strip.contains(e.target) && !modal.contains(e.target)) hideLink(); });

  window.addEventListener('resize', function() {  // also the turn of the phone
    clearTimeout(relayout);
    relayout = setTimeout(layoutMarks, 120);
  });

  document.addEventListener('visibilitychange', function() { if (document.hidden) pause(); });

  // keyboard (a laptop), only while the timelapse is on screen and the focus is not in a field: Space plays or pauses,
  // the arrows step a frame (Shift: 10), Home and End go to the ends, [ and ] change the day. The slider keeps its own arrows and Home/End;
  // a focused button or link keeps its Space
  document.addEventListener('keydown', function(e) {
    if (!modal.hidden) {
      if (e.key === 'Escape') { closeClip(); e.preventDefault(); }
      else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !e.altKey && !e.ctrlKey && !e.metaKey) {
        turnClip(e.key === 'ArrowLeft' ? -1 : 1);
        e.preventDefault();
      }
      return;
    }
    if (!active || e.altKey || e.ctrlKey || e.metaKey) return;
    var el = e.target, tag = (el && el.tagName) || '';
    if ((el && el.isContentEditable) || tag === 'TEXTAREA' || tag === 'SELECT' || (tag === 'INPUT' && el !== range)) return;
    var k = e.key, step = e.shiftKey ? 10 : 1;
    if (e.code === 'BracketLeft' || e.code === 'BracketRight') {  // by the key, not the letter: the same on the Russian layout
      stepDay(e.code === 'BracketLeft' ? -1 : 1);
      e.preventDefault();
      return;
    }
    if (!frames.length) return;
    if (k === ' ' || k === 'Spacebar') {
      if (tag === 'BUTTON' || tag === 'A') return;
      if (playing) pause(); else play();
    } else if (el !== range && (k === 'ArrowLeft' || k === 'ArrowRight')) {
      pause();
      seek(goal + (k === 'ArrowLeft' ? -step : step));
    } else if (el !== range && (k === 'Home' || k === 'End')) {
      pause();
      seek(k === 'Home' ? 0 : frames.length - 1);
    } else {
      return;
    }
    e.preventDefault();
  });

  return {
    setActive: function(on) {
      active = on;
      if (!on) { pause(); closeClip(); return; }  // the clip does not play on behind another tab
      if (started) return;
      started = true;
      say(T['tl.loading']);
      // the calendar first (it is cached on the Pi): the newest day that has snapshots opens, today as the period
      var go = function() {
        if (at) { day = at.substr(0, 4) + '-' + at.substr(4, 2) + '-' + at.substr(6, 2); load(period, day, 'at'); return; }
        var newest = '';
        days.forEach(function(it) { if (it.frames > 0) newest = it.d; });
        load(period, newest === today ? '' : newest);
      };
      fetchDays().then(go, go);
    }
  };
})();
