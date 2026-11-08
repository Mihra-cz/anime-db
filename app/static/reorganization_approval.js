(() => {
  'use strict';
  const manifest = JSON.parse(document.getElementById('manifest-data').textContent);
  const data = manifest.payload;
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
    card.append(element('h3', `${row.action} · ${status(row)} · ${row.object_kind} #${row.object_id}`), paths,
      element('p', `${row.expected_size} B · mtime_ns ${row.expected_mtime_ns} · ${row.criticality} · ${row.failure_policy}`));
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
  const ready = () => { if (submit) submit.disabled = selected.size !== data.warnings.length; };
  paginate(document.getElementById('manifest-warnings'), data.warnings, row => {
    const card = element('article', '');
    card.append(element('p', `${row.warning_class} · ${row.object_kind} #${row.object_id} · ${row.source}`),
      element('p', `Warning ID: ${row.warning_id}`));
    if (submit) {
      const label = element('label', 'Výslovně potvrzuji tento warning ');
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
      card.append(element('p', ack ? `Potvrzeno: ${ack.actor} · ${ack.acknowledged_at}` : 'Nepotvrzeno — STOP'));
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
