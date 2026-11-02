// links without login: "Copy" puts the link on the clipboard, "Share" opens the system share sheet (shown only where
// the browser has one)
(function() {
  function absolute(link) { return new URL(link, location.href).href; }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    // plain HTTP at home: no clipboard API, the old way
    return new Promise(function(resolve, reject) {
      var box = document.createElement('textarea');
      box.value = text;
      box.setAttribute('readonly', '');
      box.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(box);
      box.select();
      box.setSelectionRange(0, text.length);
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(box);
      if (ok) resolve(); else reject(new Error('copy'));
    });
  }
  document.querySelectorAll('[data-copy]').forEach(function(btn) {
    var label = btn.textContent;
    btn.addEventListener('click', function() {
      copyText(absolute(btn.getAttribute('data-copy'))).then(function() {
        btn.textContent = T['share.copied'];
        setTimeout(function() { btn.textContent = label; }, 2000);
      }).catch(function() {});
    });
  });
  document.querySelectorAll('[data-share]').forEach(function(btn) {
    if (!navigator.share) return;
    btn.hidden = false;
    btn.addEventListener('click', function() {
      navigator.share({ url: absolute(btn.getAttribute('data-share')) }).catch(function() {});
    });
  });
})();
