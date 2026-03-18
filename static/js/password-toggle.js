/**
 * Toggle password visibility for inputs inside .password-wrap when .js-password-toggle is clicked.
 */
(function () {
  function init() {
    document.querySelectorAll('.js-password-toggle').forEach(function (btn) {
      var wrap = btn.closest('.password-wrap');
      if (!wrap) return;
      var input = wrap.querySelector('input');
      var eye = wrap.querySelector('.icon-eye');
      var eyeOff = wrap.querySelector('.icon-eye-off');
      if (!input || !eye || !eyeOff) return;
      btn.addEventListener('click', function () {
        if (input.type === 'password') {
          input.type = 'text';
          eye.style.display = 'none';
          eyeOff.style.display = 'inline';
          btn.setAttribute('aria-label', 'Hide password');
        } else {
          input.type = 'password';
          eye.style.display = 'inline';
          eyeOff.style.display = 'none';
          btn.setAttribute('aria-label', 'Show password');
        }
      });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
