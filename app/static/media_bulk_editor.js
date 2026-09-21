(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AnimeMediaBulk = api;
})(typeof window !== 'undefined' ? window : null, function () {
  function readMediaBulkState(choices, selects) {
    const selectedCount = Array.from(choices).filter(choice => choice.checked).length;
    const hasRequestedChanges = Array.from(selects).some(select => select.value);
    return {
      selectedCount,
      hasRequestedChanges,
      canSubmit: selectedCount > 0 && hasRequestedChanges,
    };
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
      const choices = Array.from(document.querySelectorAll('[data-media-select]'));
      const form = document.querySelector('[data-media-bulk-form]');
      if (!form) return;
      const selects = Array.from(form.querySelectorAll('select'));
      const submit = form.querySelector('[data-media-bulk-submit]');
      const counter = form.querySelector('[data-selected-count]');
      const update = () => {
        const state = readMediaBulkState(choices, selects);
        counter.textContent = `Vybráno: ${state.selectedCount} videí`;
        submit.disabled = !state.canSubmit;
      };
      choices.forEach(choice => choice.addEventListener('change', update));
      selects.forEach(select => select.addEventListener('change', update));
      form.addEventListener('submit', event => {
        const state = readMediaBulkState(choices, selects);
        if (!state.canSubmit) event.preventDefault();
      });
      update();
    });
  }

  return {readMediaBulkState};
});
