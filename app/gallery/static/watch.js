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


initMotionClip(document);
