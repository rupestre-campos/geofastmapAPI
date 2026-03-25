/**
 * Format job timestamps (ISO 8601 UTC from API) for display in the user's locale and timezone.
 */
(function (global) {
  function formatJobTime(iso) {
    if (iso == null || iso === '') return '—';
    var s = String(iso).trim();
    var d = new Date(s);
    if (isNaN(d.getTime())) {
      return s.replace('T', ' ').substring(0, 19);
    }
    return d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'medium' });
  }

  function applyJobTimes(root) {
    root = root || document;
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('[data-job-time]').forEach(function (el) {
      var iso = el.getAttribute('data-job-time');
      el.textContent = formatJobTime(iso);
    });
  }

  global.GeofastmapFormatJobTime = formatJobTime;
  global.GeofastmapApplyJobTimes = applyJobTimes;
})(typeof window !== 'undefined' ? window : this);
