// Keyboard behavior is limited to the student card and its operation panels.
(() => {
  const panel = document.getElementById('slide-over');
  if (!panel) return;
  let opener = null;
  let returnAction = null;
  let latestSwapRequest = null;
  const scoped = () => !!panel.querySelector('[data-student-dialog]');
  const focusables = () => [...panel.querySelectorAll('button, a[href], input:not([type="hidden"]), select, textarea, summary')]
    .filter(el => !el.disabled && el.getClientRects().length);
  function close() {
    document.getElementById('slide-over-backdrop').classList.add('hidden');
    panel.classList.add('translate-x-full');
    panel.removeAttribute('role');
    panel.removeAttribute('aria-modal');
    const currentOpener = opener?.isConnected && opener.getClientRects().length ? opener : [...document.querySelectorAll('[data-student-opener][hx-target="#slide-over"]')]
      .find(el => el.getAttribute("hx-get") === opener?.getAttribute("hx-get") && el.getClientRects().length);
    currentOpener?.focus();
  }
  document.addEventListener('click', event => {
    const control = event.target.closest('[hx-get], [data-student-close], #slide-over-backdrop');
    if (!control) return;
    if (scoped() && (control.matches('[data-student-close]') || control.id === 'slide-over-backdrop')) {
      event.preventDefault(); event.stopImmediatePropagation(); close(); return;
    }
    if (control.getAttribute('hx-target') !== '#slide-over') return;
    if (!panel.contains(control)) { opener = control.querySelector('[data-student-opener]') || control; returnAction = null; }
    else if (panel.querySelector('[data-student-card]')) returnAction = control.getAttribute('hx-get');
  }, true);
  document.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target === panel) latestSwapRequest = event.detail.xhr;
  });
  document.addEventListener('htmx:afterSettle', event => {
    // An older response may settle after newer content has replaced it. That
    // content is not interactive until its own HTMX settle tasks have run.
    if (event.detail.target !== panel || event.detail.xhr !== latestSwapRequest) return;
    if (!scoped()) {
      panel.removeAttribute('role'); panel.removeAttribute('aria-modal'); panel.removeAttribute('aria-label');
      return;
    }
    panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', panel.querySelector('h2')?.textContent.trim() || 'Ученик');
    let target;
    if (panel.querySelector('[data-student-card]') && returnAction) {
      target = [...panel.querySelectorAll('[hx-get]')].find(el => el.getAttribute('hx-get') === returnAction);
      if (target?.closest('details')) target.closest('details').open = true;
    }
    target ||= panel.querySelector('input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])');
    (target || panel.querySelector("[data-student-back]") || focusables()[0])?.focus();
  });
  document.addEventListener('keydown', event => {
    if (event.target.matches('[data-student-opener][role="button"]') && ['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      event.target.click();
      return;
    }
    if (!scoped() || panel.classList.contains('translate-x-full')) return;
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const items = focusables();
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
})();
