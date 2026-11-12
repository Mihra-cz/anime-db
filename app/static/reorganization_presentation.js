(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('reorganization-messages').textContent);
  const element = (tag, text = '') => { const node = document.createElement(tag); node.textContent = text; return node; };
  const describe = (code, severity = 'review') => {
    let key = code;
    if (key.startsWith('inventory_diagnostic:')) key = key.slice('inventory_diagnostic:'.length);
    key = key.split(':')[0];
    const message = data.messages[key] || (key.startsWith('duplicate_relation_') ? data.messages.duplicate_relation_ : null);
    return {...(message || data.fallbacks[severity] || data.fallbacks.review), code, severity};
  };
  const technical = value => {
    const detail = element('details');
    detail.className = 'reorganization-technical';
    detail.append(element('summary', 'Technické podrobnosti'), element('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2)));
    return detail;
  };
  const message = (code, severity = 'review') => {
    const value = describe(code, severity), node = element('div');
    node.className = 'reorganization-message ' + severity;
    node.append(element('h4', `${({error:'Blokující problém',review:'Ke kontrole',warning:'Upozornění',info:'Informace'})[severity]} · ${value.title}`),
      element('p', value.reason), element('p', 'Dopad: ' + value.impact), element('p', value.action));
    return node;
  };
  window.ReorganizationPresentation = {element, describe, technical, message,
    label: key => data.labels[key] || 'Další stav'};
})();
