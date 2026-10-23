/* The login page: fill in the server-rendered strings from the data block and
   keep the document direction in step with the language. */
'use strict';

(function () {
  const meta = document.getElementById('panel-meta');
  let strings = {};
  try { strings = JSON.parse(meta.textContent); } catch (err) { strings = {}; }
  document.querySelectorAll('[data-s]').forEach((node) => {
    const value = strings[node.dataset.s];
    if (value) node.textContent = value;
    if (node.dataset.s === 'notice') node.hidden = !value;
  });
  if (strings.dir === 'rtl') {
    const form = document.querySelector('.login form');
    document.documentElement.dir = 'rtl';
    if (form) form.dir = 'rtl';
  }
  document.title = strings.product ? strings.product + ' — ' + strings.title : document.title;
})();
