(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AnimeNamingReview = api;
  if (root && typeof document !== 'undefined') api.initialize(document, root);
})(typeof window !== 'undefined' ? window : null, function () {
  function createPreviewController({request, render, onError, setTimer = setTimeout,
    clearTimer = clearTimeout, delay = 250}) {
    let timer;
    let abort;
    let generation = 0;
    function cancel() {
      generation += 1;
      clearTimer(timer);
      if (abort) abort.abort();
    }
    return {
      cancel,
      update(text, fingerprint) {
        cancel();
        const current = generation;
        timer = setTimer(async () => {
          abort = new AbortController();
          try {
            const preview = await request(text, fingerprint, abort.signal);
            if (generation === current) render(preview);
          } catch (error) {
            if (generation === current && error.name !== 'AbortError') {
              onError(error.message || 'Náhled se nepodařilo načíst. Zkus upravit název znovu.');
            }
          }
        }, delay);
      },
    };
  }

  function renderPreview(target, preview) {
    target.dataset.valid = String(preview.valid);
    target.querySelector('[data-preview-text]').textContent =
      preview.preview_text === null ? 'Náhled není dostupný.' : preview.preview_text;
    const measures = [
      `${preview.chars ?? '—'} znaků · prefix ${preview.utf8_bytes ?? '—'} / 255 UTF-8 bytes`,
    ];
    if (preview.max_component_bytes != null && preview.max_component_bytes !== preview.utf8_bytes) {
      measures.push(`Nejdelší známý název souboru: ${preview.max_component_bytes} / 255 UTF-8 bytes`);
    }
    target.querySelector('[data-preview-metrics]').textContent = measures.join(' · ');
    target.querySelector('[data-preview-transformations]').textContent =
      (preview.transformations || []).join(' · ');
    target.querySelector('[data-preview-diagnostics]').textContent =
      (preview.diagnostics || []).join(' · ');
    target.querySelector('[data-preview-status]').textContent = '';
  }

  function initialize(document, window) {
    const forms = Array.from(document.querySelectorAll('form[data-preview-url]'));
    forms.forEach(form => {
      const input = form.querySelector('[data-naming-custom]');
      const customChoice = form.querySelector('[data-naming-custom-choice]');
      const controls = form.querySelector('[data-naming-custom-controls]');
      const target = form.querySelector('[data-naming-preview]');
      const status = target.querySelector('[data-preview-status]');
      const fingerprint = form.querySelector('input[name="fingerprint"]').value;
      const controller = createPreviewController({
        async request(text, basis, signal) {
          const body = new URLSearchParams({fingerprint:basis, custom_text:text});
          const response = await window.fetch(form.dataset.previewUrl, {
            method:'POST', body, signal, credentials:'same-origin',
            headers:{Accept:'application/json'},
          });
          const result = await response.json();
          if (!response.ok) {
            throw new Error(typeof result.detail === 'string' ? result.detail :
              'Náhled se nepodařilo načíst. Obnov stránku a zkontroluj název znovu.');
          }
          return result;
        },
        render(result) { renderPreview(target, result); },
        onError(message) { status.textContent = message; },
      });
      function updatePreview() {
        status.textContent = 'Načítám náhled…';
        target.querySelector('[data-preview-text]').textContent = 'Čeká na aktuální náhled.';
        target.querySelector('[data-preview-metrics]').textContent = '';
        target.querySelector('[data-preview-transformations]').textContent = '';
        target.querySelector('[data-preview-diagnostics]').textContent = '';
        controller.update(input.value, fingerprint);
      }
      function updateChoice() {
        controls.hidden = !customChoice.checked;
        input.required = customChoice.checked;
        if (customChoice.checked) updatePreview();
        else controller.cancel();
      }
      controls.hidden = !customChoice.checked;
      input.required = customChoice.checked;
      form.addEventListener('change', event => {
        if (event.target.name === 'candidate_key') updateChoice();
      });
      input.addEventListener('input', () => {
        if (customChoice.checked) updatePreview();
      });
    });
    const registry = window.animeEditDirtyRegistry;
    const summary = document.querySelector('[data-naming-dirty]');
    if (registry) registry.subscribe(dirty => {
      const changed = forms.filter(form => dirty.includes(form));
      if (summary) {
        summary.textContent = changed.length ? `Neuložené změny: ${changed.length}.` : 'Žádné neuložené změny.';
        summary.classList.toggle('has-changes', changed.length > 0);
      }
      forms.forEach(form => {
        form.querySelector('[data-naming-form-dirty]').textContent = registry.isDirty(form) ?
          'Neuložená změna.' : 'Žádné neuložené změny.';
      });
    });
  }

  return {createPreviewController, renderPreview, initialize};
});
