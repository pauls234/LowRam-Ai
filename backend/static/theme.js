/* Applies the remembered theme before the first paint (no flash). The server
 * setting (section "ui") is authoritative and re-applied by app.js. */
(function () {
  'use strict';
  try {
    var theme = window.localStorage.getItem('lowram.theme');
    if (theme === 'light' || theme === 'dark') document.documentElement.setAttribute('data-theme', theme);
  } catch (_) { /* storage unavailable: follow the system */ }
})();
