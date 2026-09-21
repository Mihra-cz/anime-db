(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AnimeEditDirty = api;
})(typeof window !== 'undefined' ? window : null, function () {
  function createDirtyRegistry(forms, snapshot, isForcedDirty = () => false) {
    const tracked = Array.from(forms);
    const baselines = new Map(tracked.map(form => [form, snapshot(form)]));
    const forcedDirty = new Set(tracked.filter(isForcedDirty));
    const listeners = new Set();
    let leaving = false;

    const dirtyForms = () => tracked.filter(
      form => forcedDirty.has(form) || snapshot(form) !== baselines.get(form)
    );
    const notify = () => listeners.forEach(listener => listener(dirtyForms()));
    return {
      forms: tracked,
      dirtyForms,
      isDirty(form) {
        return forcedDirty.has(form) || snapshot(form) !== baselines.get(form);
      },
      markSaved(form) {
        baselines.set(form, snapshot(form));
        forcedDirty.delete(form);
        notify();
      },
      markAllSaved() {
        tracked.forEach(form => baselines.set(form, snapshot(form)));
        forcedDirty.clear();
        notify();
      },
      subscribe(listener) {
        listeners.add(listener);
        listener(dirtyForms());
        return () => listeners.delete(listener);
      },
      refresh: notify,
      allowNavigation() { leaving = true; },
      navigationAllowed() { return leaving; },
    };
  }

  if (typeof document !== 'undefined') {
    const forms = Array.from(document.querySelectorAll('form[data-edit-form]'));
    if (forms.length) {
      const snapshot = form => JSON.stringify(Array.from(new FormData(form).entries()));
      const registry = createDirtyRegistry(
        forms,
        snapshot,
        form => form.dataset.dirtyOnLoad === 'true',
      );
      window.animeEditDirtyRegistry = registry;

      registry.subscribe(() => forms.forEach(form => {
        const changed = registry.isDirty(form);
        form.classList.toggle('is-dirty', changed);
        const buttons = form.querySelectorAll(
          'button[data-local-save], button[type="submit"]',
        );
        buttons.forEach(button => {
          button.classList.toggle('button-quiet', !changed);
          button.disabled = !changed;
        });
      }));

      document.addEventListener('input', registry.refresh);
      document.addEventListener('change', registry.refresh);
      document.addEventListener('submit', event => {
        if (event.target.dataset.pageSaveAllSubmit === 'true') {
          registry.allowNavigation();
          return;
        }
        const otherChanges = registry.dirtyForms().filter(
          form => form !== event.target
        );
        if (otherChanges.length && !window.confirm(
          `${otherChanges.length} další neuložené změny se při načtení stránky ztratí. Pokračovat?`
        )) {
          event.preventDefault();
          return;
        }
        registry.allowNavigation();
      });
      window.addEventListener('beforeunload', event => {
        if (registry.dirtyForms().length && !registry.navigationAllowed()) {
          event.preventDefault();
          event.returnValue = '';
        }
      });
    }
  }

  return {createDirtyRegistry};
});
