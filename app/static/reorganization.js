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
  const reviewMessages = {
    duplicate_side_asset_association_unresolved: 'Archiv je u potvrzených duplicitních videí. Chybí rozhodnutí, zda patří konkrétní kopii, nebo se má zachovat samostatně v Subs.',
    archive_review_source_changed: 'Zdrojový archiv se od rozhodnutí změnil nebo již není dostupný. Ověřte jej a rozhodnutí znovu uložte, změňte nebo zrušte.',
    archive_review_context_changed: 'Archiv již nepatří do původní kolekce nebo podporovaného umístění. Rozhodnutí je nutné znovu posoudit.',
    archive_review_duplicate_changed: 'Vybrané video nebo jeho potvrzená duplicitní vazba se změnily. Původní rozhodnutí zůstává uložené, ale planner jej nepoužije.',
    archive_review_target_invalid: 'Cíl uloženého rozhodnutí již není platný nebo koliduje s jiným souborem. Rozhodnutí změňte nebo zrušte; technický důvod je v detailu.',
  };
  const archiveForm = (source, review) => {
    const container = element('div', '');
    const button = element('button', review.decision ? 'Změnit / zrušit rozhodnutí' : 'Vyřešit');
    button.type = 'button';
    button.setAttribute('aria-expanded', 'false');
    const form = element('form', '');
    form.method = 'post';
    form.action = '/reorganization/archive-review';
    form.hidden = true;
    form.className = 'reorganization-archive-form';
    const hidden = (name, value) => {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = name; input.value = value;
      form.append(input);
      return input;
    };
    hidden('source', source);
    hidden('fingerprint', review.fingerprint);
    const choice = hidden('choice', review.decision?.choice || 'unresolved');
    const secondary = hidden('secondary_video_id', review.decision?.secondary_video_id || '');
    const options = element('fieldset', '');
    options.append(element('legend', 'Jak archiv zachovat'));
    if (review.decision?.choice === 'secondary') {
      options.append(element('p', `Uložené přiřazení: kopie #${review.decision.secondary_video_id}: ${review.decision.secondary_source} → primární video #${review.decision.primary_video_id}: ${review.decision.primary_source}`));
    }
    const selected = review.decision?.choice === 'secondary' ? `secondary:${secondary.value}` : choice.value;
    const option = (value, text, disabled = false) => {
      const label = element('label', '');
      const radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'archive-option'; radio.value = value;
      radio.required = true;
      radio.checked = value === selected; radio.disabled = disabled;
      radio.addEventListener('change', () => {
        choice.value = value.startsWith('secondary:') ? 'secondary' : value;
        secondary.value = value.startsWith('secondary:') ? value.split(':')[1] : '';
      });
      label.append(radio, element('span', text));
      options.append(label);
    };
    options.append(element('p', 'A. Přiřadit konkrétní potvrzené duplicitní kopii. Archiv se zachová v Duplicates se svým původním názvem.'));
    for (const video of review.candidates) {
      option(`secondary:${video.id}`, `Kopie #${video.id}: ${video.source} → primární video #${video.primary_id}: ${video.primary_source}`);
    }
    if (!review.candidates.length) options.append(element('p', 'V aktuální kolekci není dostupná platná potvrzená duplicitní kopie.'));
    option('standalone', 'B. Zachovat jako samostatný archiv titulků v Subs s původním názvem.', !review.standalone_allowed);
    if (review.standalone_reason) options.append(element('p', review.standalone_reason));
    option('unresolved', 'C. Nechat nerozhodnuté (zrušit uložené rozhodnutí).');
    form.append(options, element('p', 'Ukládá se pouze rozhodnutí do lokální appdata. Soubor se nepřesouvá. Planner znovu ověří aktuální vazby a cílové kolize.'));
    const save = element('button', 'Uložit rozhodnutí a obnovit náhled');
    save.type = 'submit';
    form.append(save);
    button.addEventListener('click', () => {
      form.hidden = !form.hidden;
      button.setAttribute('aria-expanded', String(!form.hidden));
    });
    container.append(button, form);
    return container;
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
      if (row.status === 'REVIEW') {
        const detail = element('details', '');
        detail.append(element('summary', 'Technické důvody'), list);
        card.append(detail);
      } else card.append(list);
    }
    const review = data.archive_reviews?.[row.source_relative_path];
    if (review) {
      const explanation = row.blockers.map(code => reviewMessages[code]).filter(Boolean);
      if (explanation.length) card.append(element('p', explanation.join(' ')));
      else if (review.decision) card.append(element('p', 'Pro archiv je uložené explicitní lidské rozhodnutí.'));
      card.append(archiveForm(row.source_relative_path, review));
    } else if (row.status === 'REVIEW') {
      const note = element('p', 'Tato položka se neřeší přiřazením archivu. ');
      const link = element('a', 'Otevřít kontrolu hierarchie a autority videí');
      link.href = '/hierarchy-review';
      const codes = row.blockers.join(' ');
      if (/naming|name_choice|physical_name/.test(codes)) {
        link.href = '/naming-review'; link.textContent = 'Otevřít Naming Review';
      } else if (/layout/.test(codes)) {
        link.href = '/naming-review/layout'; link.textContent = 'Otevřít Layout Review';
      } else if (/subtitle|compatibility/.test(codes) || /subtitle/.test(row.object_kind)) {
        link.href = '/media-check'; link.textContent = 'Otevřít kontrolu médií a kompatibility titulků';
      } else if (/unknown|auxiliary/.test(row.object_kind)) {
        note.append(document.createTextNode('Neznámý nebo nejednoznačný fyzický asset vyžaduje ruční audit zdrojového souboru; automatické přiřazení není dostupné.'));
        card.append(note);
        return card;
      }
      note.append(link);
      card.append(note);
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
