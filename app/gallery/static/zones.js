// motion zones: a tap or a stroke over the grid paints cells; a stroke sets every cell it crosses to the value opposite
// to the first one (paint or erase), so it never flickers. The cells go to the hidden field "cells" (0/1 row by row).
(function() {
  var grid = document.querySelector('.zonegrid');
  if (!grid) return;
  var cols = +grid.getAttribute('data-cols'), rows = +grid.getAttribute('data-rows');
  var field = document.querySelector('input[name=cells]');
  var mode = document.querySelector('select[name=mode]');
  var warn = document.getElementById('zones-warn');
  var cells = [], value = null, last = null;

  for (var i = 0; i < cols * rows; i++) {
    var c = document.createElement('i');
    if (field.value.charAt(i) === '1') c.className = 'on';
    grid.appendChild(c);
    cells.push(c);
  }

  function sync() {
    var s = '', n = 0;
    cells.forEach(function(c) { var on = c.classList.contains('on'); s += on ? '1' : '0'; if (on) n++; });
    field.value = s;
    warn.hidden = !(mode.value === 'exclude' && n === cells.length);
  }

  function cellAt(x, y) {
    var r = grid.getBoundingClientRect();
    var col = Math.floor((x - r.left) / r.width * cols), row = Math.floor((y - r.top) / r.height * rows);
    return Math.max(0, Math.min(cols - 1, col)) + Math.max(0, Math.min(rows - 1, row)) * cols;
  }

  function paint(i) {
    cells[i].classList.toggle('on', value);
  }

  // a quick stroke skips cells between two events: walk the segment in half-cell steps
  function stroke(x, y) {
    var r = grid.getBoundingClientRect(), step = Math.max(1, Math.min(r.width / cols, r.height / rows) / 2);
    var n = Math.max(1, Math.ceil(Math.hypot(x - last[0], y - last[1]) / step));
    for (var k = 1; k <= n; k++) paint(cellAt(last[0] + (x - last[0]) * k / n, last[1] + (y - last[1]) * k / n));
    last = [x, y];
    sync();
  }

  grid.addEventListener('pointerdown', function(e) {
    if (e.button) return;
    e.preventDefault();
    grid.setPointerCapture(e.pointerId);
    value = !cells[cellAt(e.clientX, e.clientY)].classList.contains('on');
    last = [e.clientX, e.clientY];
    paint(cellAt(e.clientX, e.clientY));
    sync();
  });
  grid.addEventListener('pointermove', function(e) { if (last) stroke(e.clientX, e.clientY); });
  ['pointerup', 'pointercancel'].forEach(function(name) {
    grid.addEventListener(name, function() { last = null; });
  });
  grid.addEventListener('contextmenu', function(e) { e.preventDefault(); });

  document.getElementById('zones-clear').addEventListener('click', function() {
    cells.forEach(function(c) { c.classList.remove('on'); });
    sync();
  });
  mode.addEventListener('change', sync);
  sync();
})();
