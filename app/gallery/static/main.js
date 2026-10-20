(function() {
  // the camera tabs: the arrows at the edges show only where the row can be scrolled; a tap scrolls by the visible width
  document.querySelectorAll('.camtabs-wrap').forEach(function(wrap) {
    var row = wrap.querySelector('.camtabs'), prev = wrap.querySelector('.prev'), next = wrap.querySelector('.next');
    var centred = false;
    function update() {
      if (!row.clientWidth) return;  // the panel is hidden: it is measured when it is shown (ResizeObserver)
      if (!centred) {  // the chosen camera in view
        centred = true;
        var on = row.querySelector('.active');
        if (on) row.scrollLeft = on.offsetLeft - (row.clientWidth - on.offsetWidth) / 2;
      }
      prev.hidden = row.scrollLeft < 2;
      next.hidden = row.scrollLeft + row.clientWidth > row.scrollWidth - 2;
    }
    function by(k) {
      var left = row.scrollLeft + k * row.clientWidth * 0.8;
      if (row.scrollTo) row.scrollTo({ left: left, behavior: 'smooth' }); else row.scrollLeft = left;
    }
    prev.addEventListener('click', function() { by(-1); });
    next.addEventListener('click', function() { by(1); });
    row.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    if (window.ResizeObserver) new ResizeObserver(update).observe(row);
    update();
  });

  var tabs = document.querySelectorAll('.tab[data-tab]');
  var panels = document.querySelectorAll('.panel');
  var activeTab = document.querySelector('.tab.active');
  var shown = document.querySelector('.panel:not([hidden])');
  var current = shown ? shown.getAttribute('data-panel') : 'live';  // embedded Live has no panels

  // iPhone/iPad in Safari (not the Home Screen app): the app icon in the header, a tap shows how to add it
  (function() {
    var ios = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    var standalone = window.navigator.standalone === true ||
      (window.matchMedia && matchMedia('(display-mode: standalone)').matches);
    if (!ios || standalone) return;
    var btn = document.getElementById('appbtn'), hint = document.getElementById('apphint');
    if (!btn) return;
    btn.hidden = false;
    btn.addEventListener('click', function() { hint.hidden = !hint.hidden; });
    document.getElementById('apphint-close').addEventListener('click', function() { hint.hidden = true; });
  })();

  // iOS in Low Power Mode (and some browsers) will not start video without a tap:
  // instead of an endless "Connecting…", ask for a tap and start every player on the first one
  var tapWait = null;
  var players = [];
  function waitForTap() {
    if (tapWait) return;
    tapWait = function() {
      document.removeEventListener('click', tapWait, true);
      document.removeEventListener('touchend', tapWait, true);
      tapWait = null;
      if (current === 'live') players.forEach(function(p) { p.play(); });
    };
    document.addEventListener('click', tapWait, true);
    document.addEventListener('touchend', tapWait, true);
  }

  // hls.js is loaded once, for every player that needs it
  var hlsWaiting = null;
  function withHls(cb) {
    if (window.Hls) { cb(true); return; }
    if (hlsWaiting) { hlsWaiting.push(cb); return; }
    hlsWaiting = [cb];
    var s = document.createElement('script');
    // hls.js is a file of the device itself (/static/hls.min.js?v=<hash>), not a CDN: nothing is fetched from the internet
    s.src = PAGE.hlsUrl;
    var done = function(ok) { var w = hlsWaiting; hlsWaiting = null; w.forEach(function(f) { f(ok); }); };
    s.onload = function() { done(true); };
    s.onerror = function() { done(false); };
    document.head.appendChild(s);
  }

  // candidates from the go2rtc SDP answer: where the browser tries to get the video
  function answerCandidates(sdp) {
    return (sdp || '').split('\n').filter(function(l) { return l.indexOf('a=candidate:') === 0; })
      .map(function(l) {
        var f = l.trim().split(' ');
        return f[2].toLowerCase() + ' ' + f[4] + ':' + f[5] + ' ' + (f[7] || '');
      }).join(', ') || 'none';
  }

  var STALL_MS = PAGE.webrtcFallbackMs + 15000;
  var ICE_SERVERS = PAGE.iceServers;
  // one Live player per camera: WebRTC via go2rtc (~0.3-0.5 s delay); if it does not connect, HLS
  function Player(wrap) {
    var cam = wrap.getAttribute('data-cam');
    var video = wrap.querySelector('video');
    var status = wrap.querySelector('.live-status');
    var note = wrap.parentNode.querySelector('.live-note');
    // sound (the camera's "Sound in Live"): the video starts muted (iPhone plays only that by itself), a tap turns it on
    var withAudio = wrap.hasAttribute('data-audio');
    var soundBtn = wrap.querySelector('.live-sound');
    function showSound() {
      if (!soundBtn) return;
      soundBtn.classList.toggle('on', !video.muted);
      var label = video.muted ? T['live.sound_on'] : T['live.sound_off'];
      soundBtn.setAttribute('aria-label', label);
      soundBtn.title = label;
    }
    if (soundBtn) {
      soundBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        video.muted = !video.muted;
        if (!video.muted) video.play().catch(function() {});
        showSound();
      });
      video.addEventListener('volumechange', showSound);
    }
    var hls = null, pc = null, fallbackTimer = null, retryTimer = null;
    var active = false, lastTime = -1, lastProgress = 0;

    function setStatus(text, on) {
      status.textContent = text;
      status.classList.toggle('on', !!on);
    }
    function tryPlay() {
      video.play().catch(function(err) {
        if (!err || err.name !== 'NotAllowedError') return;
        setStatus(T['js.tap_to_play'], false);
        if (note) note.textContent = T['js.tap_to_play_note'];
        waitForTap();
      });
    }
    function clearTimers() {
      clearTimeout(fallbackTimer); fallbackTimer = null;
      clearTimeout(retryTimer); retryTimer = null;
    }
    function closePc() {
      if (pc) { pc.close(); pc = null; }
    }
    // why WebRTC was abandoned: to the gallery log (journalctl -u zorcam-nvr-gallery)
    function reportFail(reason) {
      fetch('/live/webrtc-log', {
        method: 'POST', headers: { 'Content-Type': 'text/plain' }, credentials: 'same-origin',
        body: cam + ': ' + reason + ' | ' + navigator.userAgent
      }).catch(function() {});
    }

    function startWebRTC() {
      if (!window.RTCPeerConnection) { startHls(); return; }
      setStatus(T['js.connecting'], false);
      // through the Zorcam cloud: its TURN server relays the video when the Pi and the phone cannot reach each other
      var conn = new RTCPeerConnection({ iceServers: ICE_SERVERS });
      var answerSdp = '';
      pc = conn;
      if (note) note.textContent = '';
      conn.addTransceiver('video', { direction: 'recvonly' });
      // go2rtc adds the camera's sound only if the browser takes it as it is (G.711, Opus); AAC stays out
      if (withAudio) conn.addTransceiver('audio', { direction: 'recvonly' });
      var media = new MediaStream();
      conn.ontrack = function(e) {
        media.addTrack(e.track);  // video and sound in one stream, whatever order they come in
        if (video.srcObject !== media) video.srcObject = media;
        tryPlay();
      };
      conn.onconnectionstatechange = function() {
        if (conn !== pc) return;
        var st = conn.connectionState;
        if (st === 'connected') {
          clearTimeout(fallbackTimer); fallbackTimer = null;
          if (!video.paused) setStatus('LIVE', true);
        } else if (st === 'failed' || st === 'disconnected') {
          // 'disconnected' often recovers by itself (a short Wi-Fi drop), so reconnect after a pause
          setStatus(T['js.reconnecting'], false);
          clearTimeout(retryTimer);
          retryTimer = setTimeout(function() {
            if (conn !== pc || conn.connectionState === 'connected' || current !== 'live') return;
            closePc();
            startWebRTC();
          }, st === 'failed' ? 1000 : 4000);
        }
      };
      fallbackTimer = setTimeout(function() {
        if (conn !== pc || conn.connectionState === 'connected') return;
        var head = 'ICE ' + conn.iceConnectionState + '; go2rtc: ' + answerCandidates(answerSdp);
        var done = function(extra) {
          reportFail(head + (extra ? '; ' + extra : ''));
          if (conn === pc) { closePc(); startHls(); }
        };
        conn.getStats().then(function(stats) {
          var pairs = {}, local = {};
          stats.forEach(function(r) {
            if (r.type === 'candidate-pair') pairs[r.state] = (pairs[r.state] || 0) + 1;
            if (r.type === 'local-candidate') local[r.candidateType] = (local[r.candidateType] || 0) + 1;
          });
          done('pairs ' + JSON.stringify(pairs) + ', browser ' + JSON.stringify(local));
        }, function() { done(''); });
      }, PAGE.webrtcFallbackMs);

      conn.createOffer().then(function(offer) {
        return conn.setLocalDescription(offer);
      }).then(function() {
        // wait for ICE candidates (a fraction of a second on the LAN), at most 2 s
        return new Promise(function(resolve) {
          if (conn.iceGatheringState === 'complete') { resolve(); return; }
          var t = setTimeout(resolve, 2000);
          conn.addEventListener('icegatheringstatechange', function() {
            if (conn.iceGatheringState === 'complete') { clearTimeout(t); resolve(); }
          });
        });
      }).then(function() {
        return fetch('/live/webrtc?cam=' + encodeURIComponent(cam), {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: conn.localDescription.sdp,
          credentials: 'same-origin'
        });
      }).then(function(r) {
        if (!r.ok) throw new Error('signaling ' + r.status);
        return r.text();
      }).then(function(answer) {
        if (conn !== pc) return;
        answerSdp = answer;
        return conn.setRemoteDescription({ type: 'answer', sdp: answer });
      }).catch(function(err) {
        if (conn !== pc) return;
        reportFail(String(err && err.message || err));
        clearTimeout(fallbackTimer); fallbackTimer = null;
        closePc();
        startHls();
      });
    }

    function startHls() {
      setStatus(T['js.connecting_hls'], false);
      video.srcObject = null;
      var src = '/live/' + encodeURIComponent(cam) + '/stream.m3u8';
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = src;
        tryPlay();
        return;
      }
      withHls(function(loaded) {
        if (current !== 'live') return;
        if (!loaded) { setStatus(T['js.player_failed'], false); return; }
        if (!Hls.isSupported()) { setStatus(T['js.no_player'], false); return; }
        hls = new Hls({ liveSyncDurationCount: 2, liveMaxLatencyDurationCount: 5 });
        hls.on(Hls.Events.ERROR, function(e, d) { if (d.fatal) setStatus(T['js.stream_error'], false); });
        hls.loadSource(src);
        hls.attachMedia(video);
        tryPlay();
      });
    }

    function start() {
      active = true; lastTime = -1; lastProgress = Date.now();
      startWebRTC();
    }
    function stop() {
      active = false;
      clearTimers();
      closePc();
      if (hls) { hls.destroy(); hls = null; }
      video.pause();
      video.srcObject = null;
      video.removeAttribute('src');
      video.load();
    }

    video.addEventListener('playing', function() {
      var isHls = !!(hls || video.src);
      setStatus(isHls ? 'LIVE · HLS' : 'LIVE', true);
      if (note) note.textContent = isHls ? T['js.note_hls'] : T['js.note_webrtc'];
    });
    video.addEventListener('waiting', function() { setStatus(T['js.buffering'], false); });
    video.addEventListener('timeupdate', function() {
      if (video.currentTime !== lastTime) { lastTime = video.currentTime; lastProgress = Date.now(); }
    });
    // the picture has not moved for a while (stuck in Buffering after Wi-Fi -> LTE, a dead HLS or WebRTC
    // session): start over, WebRTC first; the wait covers the WebRTC attempt and the switch to HLS
    function check() {
      if (!active || tapWait || Date.now() - lastProgress < STALL_MS) return;
      reportFail('no picture for ' + Math.round((Date.now() - lastProgress) / 1000) + ' s, restarting');
      stop();
      start();
    }

    return { start: start, stop: stop, check: check, resetClock: function() { lastProgress = Date.now(); },
             play: function() { if (video.paused) video.play().catch(function() {}); } };
  }

  // Live in columns: 1, 2 or 3 on a phone, 2, 3 or 4 on a wide screen (the buttons differ by CSS); the choice is kept
  // on this device only. A choice the screen does not offer is shown as the nearest one (1 as 2 on a laptop, 4 as 3 on a phone)
  (function() {
    var grid = document.querySelector('.live-grid');
    if (!grid) return;  // one camera: no choice
    var buttons = document.querySelectorAll('.livecols [data-cols]');
    var wide = window.matchMedia('(min-width: 1024px)');
    var chosen = null;
    function apply() {
      var n = chosen || (wide.matches ? '2' : '1');
      if (wide.matches && n === '1') n = '2';
      if (!wide.matches && n === '4') n = '3';
      grid.setAttribute('data-cols', n);
      buttons.forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-cols') === n); });
    }
    try { chosen = localStorage.getItem('zorcam_live_cols'); } catch (e) {}
    if (!/^[1-4]$/.test(chosen || '')) chosen = null;
    apply();
    if (wide.addEventListener) wide.addEventListener('change', apply);
    buttons.forEach(function(b) {
      b.addEventListener('click', function() {
        chosen = b.getAttribute('data-cols');
        apply();
        try { localStorage.setItem('zorcam_live_cols', chosen); } catch (e) {}
      });
    });
  })();

  // PTZ: the buttons appear for the cameras that can turn; hold = move (repeated, the camera also stops by itself
  // after a second or two), release, a lost focus or a hidden page = stop
  (function() {
    var ctrls = document.querySelectorAll('.ptz[data-cam]');
    if (!ctrls.length) return;
    var held = null, timer = null, hover = null, REPEAT_MS = 800;
    function send(ctrl, body) {
      body.csrf = ctrl.getAttribute('data-csrf');
      return fetch('/ptz/' + encodeURIComponent(ctrl.getAttribute('data-cam')), {
        method: 'POST', credentials: 'same-origin', keepalive: true,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
    }
    function fail(ctrl, r) {
      var m = ctrl.querySelector('.ptz-msg');
      if (r && r.status === 429) return;
      m.textContent = T['js.ptz_failed'];
      clearTimeout(m.t); m.t = setTimeout(function() { m.textContent = ''; }, 3000);
    }
    function move() {
      if (!held) return;
      var ctrl = held.ctrl;
      send(ctrl, { action: 'move', dir: held.dir }).then(function(r) { if (!r.ok) fail(ctrl, r); },
                                                           function() { fail(ctrl); });
    }
    function release() {
      if (!held) return;
      var ctrl = held.ctrl;
      clearInterval(timer); timer = null;
      ctrl.querySelectorAll('.held').forEach(function(b) { b.classList.remove('held'); });
      held = null;
      send(ctrl, { action: 'stop' }).catch(function() {});
    }
    function press(ctrl, dir, btn) {
      if (held) release();
      held = { ctrl: ctrl, dir: dir };
      if (btn) btn.classList.add('held');
      move();
      timer = setInterval(move, REPEAT_MS);
    }
    function opened(ctrl) { return !ctrl.querySelector('.ptz-pad').hidden; }
    function open(ctrl, on) {
      var pad = ctrl.querySelector('.ptz-pad');
      if (!on && held && held.ctrl === ctrl) release();
      pad.hidden = !on;
      ctrl.querySelector('.ptz-toggle').setAttribute('aria-expanded', on ? 'true' : 'false');
    }
    ctrls.forEach(function(ctrl) {
      ctrl.querySelector('.ptz-toggle').addEventListener('click', function() { open(ctrl, !opened(ctrl)); });
      ctrl.querySelectorAll('.ptz-btn').forEach(function(btn) {
        btn.addEventListener('pointerdown', function(e) {
          e.preventDefault();
          try { btn.setPointerCapture(e.pointerId); } catch (x) {}
          press(ctrl, btn.getAttribute('data-dir'), btn);
        });
        ['pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave', 'blur'].forEach(function(n) {
          btn.addEventListener(n, release);
        });
        btn.addEventListener('contextmenu', function(e) { e.preventDefault(); });
      });
      ctrl.parentNode.addEventListener('pointerenter', function() { hover = ctrl; });
      fetch('/ptz/' + encodeURIComponent(ctrl.getAttribute('data-cam')), { credentials: 'same-origin' })
        .then(function(r) { return r.ok ? r.json() : {}; }).then(function(j) {
          if (!j || !j.ptz) return;
          // only the buttons the camera can do: the arrows for pan and tilt, - and + for the zoom
          var pan = j.pan !== false, zoom = j.zoom !== false;
          ctrl.querySelectorAll('.ptz-btn').forEach(function(b) {
            var z = b.getAttribute('data-dir') === 'in' || b.getAttribute('data-dir') === 'out';
            b.hidden = z ? !zoom : !pan;
          });
          ctrl.querySelector('.ptz-pad').classList.toggle('zoom-only', !pan);
          ctrl.hidden = false;
          if (!hover) hover = ctrl;
        }).catch(function() {});
    });
    document.addEventListener('visibilitychange', function() { if (document.hidden) release(); });
    window.addEventListener('pagehide', release);
    window.addEventListener('blur', release);
    // a laptop: the arrow keys and +/- move the camera under the pointer (not while typing)
    var KEYS = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', '+': 'in', '=': 'in', '-': 'out' };
    function typing() {
      var a = document.activeElement, n = a && a.tagName;
      return n === 'INPUT' || n === 'TEXTAREA' || n === 'SELECT' || (a && a.isContentEditable);
    }
    window.addEventListener('keydown', function(e) {
      var dir = KEYS[e.key];
      if (!dir || e.ctrlKey || e.metaKey || e.altKey || typing() || current !== 'live') return;
      if (!hover || hover.hidden || !hover.offsetParent || !opened(hover)) return;
      var btn = hover.querySelector('[data-dir="' + dir + '"]');
      if (!btn || btn.hidden) return;
      e.preventDefault();
      if (e.repeat || (held && held.key === e.key)) return;
      press(hover, dir, btn);
      held.key = e.key;
    });
    window.addEventListener('keyup', function(e) { if (held && held.key === e.key) release(); });
  })();

  document.querySelectorAll('.live-wrap[data-cam]').forEach(function(w) { players.push(Player(w)); });
  function startLive() { players.forEach(function(p) { p.start(); }); }
  function stopLive() { players.forEach(function(p) { p.stop(); }); }
  function restartLive() { if (current === 'live') { stopLive(); startLive(); } }
  setInterval(function() {
    if (current === 'live' && !document.hidden) players.forEach(function(p) { p.check(); });
  }, 5000);
  // back from the background: iOS has paused the video, give it time before calling it stuck
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) players.forEach(function(p) { p.resetClock(); });
  });
  // the network changed (Wi-Fi <-> LTE): the old connections lead nowhere
  window.addEventListener('online', restartLive);
  var liveReload = document.getElementById('live-reload');
  if (liveReload) liveReload.addEventListener('click', restartLive);

  // ---- motion push notifications
  (function() {
    var bell = document.getElementById('bell');
    if (!bell) return;  // live role: no notifications
    var standalone = window.navigator.standalone === true ||
      (window.matchMedia && matchMedia('(display-mode: standalone)').matches);
    var supported = window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    var reg = null;

    function setOn(on) { bell.classList.toggle('on', on); }
    function keyBytes(b64) {
      var s = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((b64.length + 3) % 4));
      var a = new Uint8Array(s.length);
      for (var i = 0; i < s.length; i++) a[i] = s.charCodeAt(i);
      return a;
    }
    function sameBytes(a, b) {
      if (a.length !== b.length) return false;
      for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
      return true;
    }
    function post(url, data) {
      data.csrf = bell.getAttribute('data-csrf');
      return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data) }).then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r; });
    }

    if (supported) {
      navigator.serviceWorker.register('/sw.js').then(function(r) {
        reg = r;
        return r.pushManager.getSubscription();
      }).then(function(sub) {
        if (!sub) { setOn(false); return; }
        // made with the key of another NVR install (a new SD card): Apple answers 400 to it, so drop it
        return fetch('/push/key', { credentials: 'same-origin' }).then(function(r) { return r.json(); }).then(function(k) {
          var have = sub.options && sub.options.applicationServerKey;
          if (k.key && have && !sameBytes(new Uint8Array(have), keyBytes(k.key))) {
            return sub.unsubscribe().then(function() { setOn(false); });
          }
          setOn(Notification.permission === 'granted');
          post('/push/subscribe', { subscription: sub.toJSON(), quiet: true }).catch(function() {});
        });
      }).catch(function() {});
      navigator.serviceWorker.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'open-live') activate('live');
      });
    }

    bell.addEventListener('click', function() {
      if (!supported) {
        if (!window.isSecureContext) alert(T['js.push_https']);
        else if (/iPhone|iPad/.test(navigator.userAgent) && !standalone) alert(T['js.push_iphone']);
        else alert(T['js.push_unsupported']);
        return;
      }
      if (!reg) { alert(T['js.push_wait']); return; }
      reg.pushManager.getSubscription().then(function(sub) {
        if (sub && Notification.permission === 'granted') {
          if (!confirm(T['js.push_off_confirm'])) return;
          return post('/push/unsubscribe', { endpoint: sub.endpoint }).catch(function() {})
            .then(function() { return sub.unsubscribe(); }).then(function() { setOn(false); });
        }
        // ask for permission right in the click handler (an iOS requirement)
        return Notification.requestPermission().then(function(perm) {
          if (perm !== 'granted') {
            alert(T['js.push_denied']);
            return;
          }
          return fetch('/push/key', { credentials: 'same-origin' }).then(function(r) { return r.json(); }).then(function(k) {
            if (!k.key) throw new Error(T['js.push_no_crypto']);
            return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(k.key) });
          }).then(function(sub) {
            return post('/push/subscribe', { subscription: sub.toJSON() });
          }).then(function() { setOn(true); });
        });
      }).catch(function(err) { alert(T['js.push_failed'] + err.message); });
    });
  })();

  // the motion frames on the clip posters: laid over the picture as it is fitted into the card (a hidden panel
  // has no size yet: this runs again when it is shown)
  function placeBoxes(root) {
    root.querySelectorAll('.card .mbox').forEach(function(m) {
      var card = m.parentNode, img = card.querySelector('img');
      if (img.complete) placeBox(card, img);
      else img.addEventListener('load', function() { placeBox(card, img); }, {once: true});
    });
  }
  placeBoxes(document);

  function activate(name) {
    if (name === current) return;
    if (current === 'live') stopLive();
    if (timelapse) timelapse.setActive(name === 'all');
    current = name;
    var tabName = name === 'videos' || name === 'all' ? 'records' : name;
    tabs.forEach(function(t) { t.classList.toggle('active', t.getAttribute('data-tab') === tabName); });
    panels.forEach(function(p) {
      p.hidden = p.getAttribute('data-panel') !== name;
      // iOS Safari does not always load lazy images of a panel that was hidden at page load
      if (!p.hidden) {
        p.querySelectorAll('img[loading=lazy]').forEach(function(img) { img.loading = 'eager'; });
        placeBoxes(p);
      }
    });
    var url = new URL(location.href);
    url.searchParams.set('tab', name);
    history.replaceState(null, '', url);
    if (name === 'live') startLive();
  }

  tabs.forEach(function(btn) {
    btn.addEventListener('click', function() {
      var name = btn.getAttribute('data-tab');
      // one-camera Live (?tab=live&cam=ID) is a separate page: the Live tab leads to all the cameras
      if (name === 'live' && document.querySelector('.panel[data-panel=live][data-single]')) {
        location.href = '/?tab=live';
        return;
      }
      if (name === 'records') {  // Records opens on Timelapse; Videos is one tap away in the switch
        if (current === 'videos' || current === 'all') return;
        name = 'all';
      }
      activate(name);
    });
  });
  document.querySelectorAll('.recseg [data-go]').forEach(function(btn) {
    btn.addEventListener('click', function() { activate(btn.getAttribute('data-go')); });
  });
  if (current === 'live') startLive();
  if (timelapse) timelapse.setActive(current === 'all');

  window.ptrBeforeReload = function() { if (current === 'live') stopLive(); };

  // clips still being recorded or encoded show the poster; once the video file is there, the card opens it
  function checkPending() {
    var cards = document.querySelectorAll('.card.pending');
    if (!cards.length) return;
    if (!document.hidden) cards.forEach(function(card) {
      fetch(card.getAttribute('data-vid'), { headers: { Range: 'bytes=0-0' }, cache: 'no-store' })
        .then(function(r) {
          if (!r.ok) return;
          card.classList.remove('pending');
          var img = card.querySelector('img');  // the poster changes if the clip failed
          img.src = img.src.split('?')[0] + '?v=' + Date.now();
        }).catch(function() {});
    });
    setTimeout(checkPending, 3000);
  }
  checkPending();

  // refresh the lists every minute, but not while watching Live or the timelapse (a reload would start it over)
  setInterval(function() {
    if (current !== 'live' && !(timelapse && current === 'all') && !document.hidden) location.reload();
  }, 60000);
})();
