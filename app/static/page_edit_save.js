(() => {
  const bar = document.getElementById('hierarchy-save-bar');
  const registry = window.animeEditDirtyRegistry;
  if (!bar || !registry) return;
  const forms = registry.forms.filter(form => form.dataset.saveKind);
  const button = document.getElementById('hierarchy-save-all');
  const count = document.getElementById('hierarchy-dirty-count');
  const feedback = document.getElementById('hierarchy-save-feedback');
  const TITLE_HIERARCHY_FIELDS = [
    'part_type_manual', 'season_number_manual', 'season_label_manual',
    'part_number_manual', 'sort_order_manual', 'numbering_mode',
    'episode_start_offset',
  ];

  function plural(value) {
    return value === 1 ? 'neuložená změna' :
      value >= 2 && value <= 4 ? 'neuložené změny' : 'neuložených změn';
  }

  function eligibleDirtyForms() {
    return registry.dirtyForms().filter(form => forms.includes(form));
  }

  function update() {
    const dirty = eligibleDirtyForms();
    count.textContent = `${dirty.length} ${plural(dirty.length)}`;
    button.disabled = dirty.length === 0;
  }

  function value(data, name) {
    return String(data.get(name) ?? '');
  }

  function section(form) {
    const data = new FormData(form);
    const kind = form.dataset.saveKind;
    const id = Number(form.dataset.saveId);
    if (kind === 'variant_group') return {kind, id, values: {
      catalog_title_id: Number(value(data, 'catalog_title_id')),
      manual_label: value(data, 'manual_label'),
      release_source: value(data, 'release_source'),
      content_variant: value(data, 'content_variant'),
      note: value(data, 'note'),
    }};
    if (kind === 'recap_position') return {kind, id, values: {
      manual_episode_number: value(data, 'manual_episode_number'),
    }};
    if (kind === 'title_hierarchy') {
      // Same fields as the local submit: an input disabled because the part
      // type does not use it is omitted, never sent as an empty value.
      const values = {hierarchy_verified: data.has('hierarchy_verified')};
      TITLE_HIERARCHY_FIELDS.forEach(name => {
        if (data.has(name)) values[name] = value(data, name);
      });
      return {kind, id, values};
    }
    if (kind === 'video_hierarchy') return {kind, id, values: {
      catalog_title_id: Number(form.dataset.catalogTitleId),
      content_type: value(data, 'content_type'),
      manual_episode_number: value(data, 'manual_episode_number'),
      media_part_number: value(data, 'media_part_number'),
    }};
    throw new Error('Nepodporovaná editační sekce.');
  }

  function showError(message) {
    feedback.textContent = message;
    feedback.classList.add('error');
  }

  button.addEventListener('click', () => {
    const changed = eligibleDirtyForms();
    if (!changed.length) return;
    const invalid = changed.find(form => !form.reportValidity());
    if (invalid) {
      showError('Opravte nejprve neplatnou editační sekci.');
      invalid.scrollIntoView({block: 'center'});
      return;
    }
    let sections;
    try {
      sections = changed.map(section);
    } catch (error) {
      showError(error.message || 'Hromadné uložení nelze připravit.');
      return;
    }
    const submit = document.createElement('form');
    submit.method = 'post';
    submit.action = bar.dataset.saveEndpoint;
    submit.dataset.pageSaveAllSubmit = 'true';
    const fields = {
      payload_json: JSON.stringify({sections}),
      return_to: `${window.location.pathname}${window.location.search}${window.location.hash}`,
    };
    Object.entries(fields).forEach(([name, fieldValue]) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = fieldValue;
      submit.append(input);
    });
    document.body.append(submit);
    registry.allowNavigation();
    submit.submit();
  });

  registry.subscribe(update);
})();
