// Page of a clip or a frame. On a clip a swipe to the left is the next (older, as in the grid) one, to the right the previous
// (newer) one; at the screen edges a swipe is not caught: there is the system gesture "back" in Safari; the move is by
// location.replace: "Back" returns to the gallery instead of paging through the clips backwards.
(function() {
  var box = document.getElementById('media'), el = box.firstElementChild;
  var newer = box.dataset.newer, older = box.dataset.older;
  if (!newer && !older) return;
  var x0 = null, y0 = 0, dx = 0, horizontal = false;
  box.addEventListener('touchstart', function(e) {
    var t = e.touches[0];
    x0 = (e.touches.length === 1 && t.clientX > 24 && t.clientX < innerWidth - 24) ? t.clientX : null;
    y0 = t.clientY; dx = 0; horizontal = false;
  }, {passive: true});
  box.addEventListener('touchmove', function(e) {
    if (x0 === null) return;
    dx = e.touches[0].clientX - x0;
    var dy = e.touches[0].clientY - y0;
    if (!horizontal && Math.abs(dx) > 12) horizontal = Math.abs(dx) > Math.abs(dy) * 1.5;
    if (!horizontal) return;
    var target = dx < 0 ? older : newer;
    el.style.transition = 'none';
    el.style.transform = 'translateX(' + (target ? dx : dx / 4) + 'px)';
  }, {passive: true});
  box.addEventListener('touchend', function() {
    if (x0 === null || !horizontal) { x0 = null; return; }
    var target = dx < 0 ? older : newer;
    el.style.transition = '';
    if (target && Math.abs(dx) > 60) {
      el.style.transform = 'translateX(' + (dx < 0 ? -innerWidth : innerWidth) + 'px)';
      el.style.opacity = 0;
      location.replace(target);
    } else {
      el.style.transform = '';
    }
    x0 = null;
  });
})();


// a clip with metadata: the frame and the Motion badge are on while currentTime >= the motion time (the frame
// is also on the poster before playback starts); the mark on the scale under the video is the motion time, a tap seeks to it
(function() {
  var frame = document.querySelector('.vframe');
  if (!frame) return;
  var video = frame.querySelector('video'), mbox = frame.querySelector('.mbox'), badge = frame.querySelector('.mbadge');
  var scale = document.querySelector('.mscale'), tick = scale && scale.querySelector('.mtick');
  var prog = scale && scale.querySelector('.mprog');
  var at = frame.hasAttribute('data-motion') ? parseFloat(frame.getAttribute('data-motion')) : null;
  var started = false;
  function update() {
    var on = at !== null && video.currentTime >= at;
    if (mbox) mbox.classList.toggle('on', on || !started);
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
})();
