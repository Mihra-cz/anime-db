(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('reorganization-data').textContent);
  const coverage = new Map(data.coverage.files.map(row => [row.path, row]));
  const pageSize = 50;
  const element = (tag, text) => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  };
  const fileCard = row => {
    const card = element('article', '');
    const disposition = coverage.get(row.source_relative_path)?.disposition || 'REVIEW';
    card.append(element('h3', `${row.status} · ${disposition}`));
    const paths = element('dl', '');
    for (const [label, value] of [['SOURCE', row.source_relative_path], ['TARGET', row.target_relative_path ?? 'Neurčený cíl']]) {
      paths.append(element('dt', label), element('dd', value));
    }
    card.append(paths, element('p', `${row.object_kind} #${row.object_id} · Návrh planneru: ${row.action}`));
    const messages = [
      ...row.blockers, ...row.warnings.map(text => `WARNING: ${text}`), ...row.info,
      ...(coverage.get(row.source_relative_path)?.reasons || []),
    ];
    if (messages.length) {
      const list = element('ul', '');
      [...new Set(messages)].forEach(text => list.append(element('li', text)));
      card.append(list);
    }
    return card;
  };
  const paginate = (container, rows, card) => {
    let page = 0;
    const total = Math.max(1, Math.ceil(rows.length / pageSize));
    const render = () => {
      container.querySelector('[data-count]').textContent = rows.length;
      container.querySelector('[data-page]').replaceChildren(...rows.slice(page * pageSize, (page + 1) * pageSize).map(card));
      container.querySelector('[data-position]').textContent = `${page + 1} / ${total} · celkem ${rows.length}`;
      container.querySelector('[data-prev]').disabled = page === 0;
      container.querySelector('[data-next]').disabled = page + 1 === total;
    };
    container.querySelector('[data-prev]').addEventListener('click', () => { page--; render(); });
    container.querySelector('[data-next]').addEventListener('click', () => { page++; render(); });
    render();
  };
  const groups = [...document.querySelectorAll('.reorganization-group')];
  for (const group of groups) {
    paginate(group, data.plan.records.filter(row => row.status === group.dataset.status), fileCard);
  }
  document.getElementById('reorganization-status').addEventListener('change', event => {
    for (const group of groups) {
      group.hidden = event.target.value !== 'ALL' && event.target.value !== group.dataset.status;
      if (!group.hidden && event.target.value !== 'ALL') group.open = true;
    }
  });
  for (const container of document.querySelectorAll('.reorganization-evidence')) {
    const kind = container.dataset.kind;
    const rows = kind === 'collisions' || kind === 'diagnostics' ? data.plan[kind] : data.coverage[kind];
    paginate(container, rows, row => {
      const node = element('p', typeof row === 'string' ? row :
        kind === 'directories' ? `${row.path} · ${row.disposition}${row.required_in_target ? ' · cílový adresář' : ''} · ${row.reasons.join(' · ')}` :
        kind === 'issues' ? `${row.path} · ${row.code}` : `${row.guard} · ${row.target_paths.join(' · ')}`);
      node.classList.add('reorganization-path');
      return node;
    });
  }
  document.getElementById('reorganization-download').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'reorganization-readonly-preview.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
