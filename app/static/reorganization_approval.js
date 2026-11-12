(() => {
  'use strict';
  const validity = document.getElementById('manifest-validity');
  if (validity) {
    const initial = Number(validity.dataset.remainingSeconds);
    const received = performance.now();
    const remaining = () => {
      const seconds = Math.max(0, initial - Math.floor((performance.now() - received) / 1000));
      validity.dataset.expired = seconds ? 'no' : 'yes';
      validity.querySelector('[data-remaining]').textContent = seconds ? `${Math.floor(seconds / 60)} min ${seconds % 60} s` : 'Platnost vypršela';
      if (!seconds) document.querySelectorAll('form[action$="/prepare"] button[type=submit], form[action$="/approve"] button[type=submit], form[action$="/start"] button[type=submit]').forEach(button => { button.disabled = true; });
    };
    remaining(); setInterval(remaining, 1000);
  }
  const manifest = JSON.parse(document.getElementById('manifest-data').textContent);
  const data = manifest.payload;
  const presentation = window.ReorganizationPresentation;
  const added = new Set(JSON.parse(document.getElementById('manifest-new-directories').textContent));
  const element = (tag, text) => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  };
  const paginate = (container, initialRows, card) => {
    let page = 0, rows = initialRows;
    const render = () => {
      const total = Math.max(1, Math.ceil(rows.length / 50));
      container.querySelector('[data-count]').textContent = `Celkem ${rows.length}`;
      container.querySelector('[data-page]').replaceChildren(...rows.slice(page * 50, (page + 1) * 50).map(card));
      container.querySelector('[data-position]').textContent = `${page + 1} / ${total}`;
      container.querySelector('[data-prev]').disabled = page === 0;
      container.querySelector('[data-next]').disabled = page + 1 === total;
    };
    container.querySelector('[data-prev]').addEventListener('click', () => { page--; render(); });
    container.querySelector('[data-next]').addEventListener('click', () => { page++; render(); });
    render();
    return next => { rows = next; page = 0; render(); };
  };
  const status = row => row.criticality === 'REVIEW' ? 'REVIEW' : row.warning_ids.length ? 'WARNING' : 'READY';
  const filter = paginate(document.getElementById('manifest-actions'), data.actions, row => {
    const card = element('article', '');
    const paths = element('dl', '');
    for (const [key, value] of [['SOURCE', row.source], ['TARGET', row.target]]) {
      paths.append(element('dt', key), element('dd', value));
    }
    card.append(element('h3', `${presentation.label(row.action)} · ${presentation.label(status(row))}`), paths);
    for (const id of row.warning_ids) {
      const warning = data.warnings.find(item => item.warning_id === id);
      if (warning) card.append(element('p', presentation.describe(warning.warning_class, 'warning').title));
    }
    card.append(presentation.technical(row));
    return card;
  });
  const action = document.getElementById('manifest-action');
  const state = document.getElementById('manifest-status');
  const search = document.getElementById('manifest-search');
  const update = () => filter(data.actions.filter(row =>
    (action.value === 'ALL' || row.action === action.value) &&
    (state.value === 'ALL' || status(row) === state.value) &&
    `${row.source} ${row.target}`.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())));
  [action, state].forEach(node => node.addEventListener('change', update));
  search.addEventListener('input', update);
  paginate(document.getElementById('manifest-directories'), data.directories,
    path => element('p', `${path} · ${added.has(path) ? 'nový' : 'existující'}`));
  const selected = new Set();
  const submit = document.getElementById('manifest-prepare-submit');
  const acknowledgements = new Map(manifest.acknowledgements.map(ack => [ack.warning_id, ack]));
  const ready = () => { if (submit) submit.disabled = selected.size !== data.warnings.length || validity?.dataset.expired === 'yes'; };
  paginate(document.getElementById('manifest-warnings'), data.warnings, row => {
    const card = element('article', '');
    card.append(presentation.message(row.warning_class, 'warning'), element('p', row.source), presentation.technical(row));
    if (submit) {
      const label = element('label', 'Zkontroloval/a jsem dopad a výslovně potvrzuji toto upozornění ');
      const check = document.createElement('input');
      check.type = 'checkbox';
      check.checked = selected.has(row.warning_id);
      check.addEventListener('change', () => {
        if (check.checked) selected.add(row.warning_id); else selected.delete(row.warning_id);
        ready();
      });
      label.prepend(check);
      card.append(label);
    } else {
      const ack = acknowledgements.get(row.warning_id);
      card.append(element('p', ack ? `Potvrzeno: ${ack.actor} · ${ack.acknowledged_at}` : 'Toto upozornění zatím není potvrzené.'));
    }
    return card;
  });
  document.getElementById('manifest-prepare').addEventListener('submit', event => {
    if (selected.size !== data.warnings.length) { event.preventDefault(); return; }
    for (const warning of selected) {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'warnings'; input.value = warning;
      event.target.append(input);
    }
    submit.disabled = true;
  });
  ready();
})();
