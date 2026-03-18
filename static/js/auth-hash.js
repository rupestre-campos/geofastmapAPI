/**
 * Hash password fields with SHA-256 before submit so only the hash is sent to the backend.
 * Backend stores bcrypt(hash) and verifies with bcrypt.verify(hash, stored).
 */
(function () {
  var PASSWORD_FIELD_NAMES = ['password', 'current_password', 'new_password', 'new_password_confirm'];

  function bufferToHex(buffer) {
    var arr = new Uint8Array(buffer);
    var hex = '';
    for (var i = 0; i < arr.length; i++) {
      var h = arr[i].toString(16);
      hex += h.length === 1 ? '0' + h : h;
    }
    return hex;
  }

  function sha256Hex(str) {
    return window.crypto.subtle
      .digest('SHA-256', new TextEncoder().encode(str))
      .then(bufferToHex);
  }

  function hashPasswordFields(form) {
    var promises = [];
    var fields = [];
    PASSWORD_FIELD_NAMES.forEach(function (name) {
      var input = form.querySelector('input[name="' + name + '"]');
      if (input && input.type === 'password' && input.value) {
        fields.push(input);
        promises.push(sha256Hex(input.value));
      }
    });
    if (promises.length === 0) return Promise.resolve();
    return Promise.all(promises).then(function (hashes) {
      hashes.forEach(function (hash, i) {
        fields[i].value = hash;
      });
    });
  }

  function init() {
    if (!window.crypto || !window.crypto.subtle) return;
    var forms = document.querySelectorAll('.js-auth-password-hash');
    forms.forEach(function (form) {
      form.addEventListener('submit', function (e) {
        if (form.dataset.hashed === '1') return;
        e.preventDefault();
        var newPass = form.querySelector('input[name="new_password"]');
        var newPassConfirm = form.querySelector('input[name="new_password_confirm"]');
        if (newPass && newPassConfirm && newPass.value !== newPassConfirm.value) {
          var msg = document.getElementById('password-mismatch');
          if (msg) msg.style.display = 'block';
          else alert('New password and confirmation do not match.');
          return;
        }
        var msg = document.getElementById('password-mismatch');
        if (msg) msg.style.display = 'none';
        hashPasswordFields(form).then(function () {
          form.dataset.hashed = '1';
          form.submit();
        });
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
