const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Runs the real hierarchy_fields.js, local_edit_dirty.js and page_edit_save.js
// against a minimal DOM.  FakeFormData follows the browser rule the title form
// contract depends on: disabled controls are not submitted.

class FakeNode {
  constructor(tagName, attributes = {}, children = []) {
    this.tagName = tagName;
    this.attributes = attributes;
    this.children = children;
    this.value = attributes.value ?? '';
    this.checked = false;
    this.hidden = false;
    this.disabled = false;
    this.required = false;
    this.listeners = {};
    this.dataset = Object.fromEntries(
      Object.entries(attributes)
        .filter(([name]) => name.startsWith('data-'))
        .map(([name, value]) => [
          name.slice(5).replace(/-(\w)/g, (_, letter) => letter.toUpperCase()),
          value,
        ]),
    );
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
    };
  }

  *walk() {
    for (const child of this.children) {
      yield child;
      yield* child.walk();
    }
  }

  querySelectorAll(selector) {
    return [...this.walk()].filter(node => matches(node, selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  dispatch(type, target = this) {
    (this.listeners[type] || []).forEach(listener => listener({target}));
  }

  append(child) { this.children.push(child); }
  reportValidity() { return true; }
  submit() { this.submitted = true; }
}

function matches(node, selector) {
  return selector.split(',').some(part => {
    const match = part.trim().match(
      /^([a-z]+)?(?:\.([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/,
    );
    if (!match) throw new Error(`Unsupported selector: ${part}`);
    const [, tag, className, attribute, value] = match;
    if (tag && node.tagName !== tag) return false;
    if (className && !(node.attributes.class || '').split(/\s+/).includes(className)) {
      return false;
    }
    if (!attribute) return true;
    if (!(attribute in node.attributes)) return false;
    return value === undefined || node.attributes[attribute] === value;
  });
}

class FakeFormData {
  constructor(form) {
    this.items = [];
    for (const node of form.walk()) {
      const name = node.attributes.name;
      if (!name || node.disabled || !['input', 'select'].includes(node.tagName)) continue;
      if (node.attributes.type === 'checkbox' && !node.checked) continue;
      this.items.push([name, node.value]);
    }
  }

  entries() { return this.items[Symbol.iterator](); }
  has(name) { return this.items.some(([key]) => key === name); }
  get(name) {
    const item = this.items.find(([key]) => key === name);
    return item ? item[1] : null;
  }
}

const CONDITIONAL = ['season_number_manual', 'season_label_manual', 'part_number_manual'];
// Same table as BROWSER_VISIBLE in tests/test_hierarchy_title_form_contract.py,
// which pins the backend relevance of the title form to it.
const VISIBLE = {
  '': [],
  season: CONDITIONAL,
  part: ['season_number_manual', 'part_number_manual'],
  ...Object.fromEntries(
    ['film', 'ova', 'special', 'preview', 'recap', 'bonus', 'other'].map(type => [
      type, ['season_number_manual', 'season_label_manual'],
    ]),
  ),
};

function titlePage(values = {}) {
  const input = (name, value = '', attributes = {}) =>
    new FakeNode('input', {name, value, ...attributes});
  const fields = {
    part_type_manual: new FakeNode('select', {name: 'part_type_manual'}),
    season_number_manual: input('season_number_manual', values.season),
    season_label_manual: input('season_label_manual', values.label),
    part_number_manual: input('part_number_manual', values.part),
    sort_order_manual: input('sort_order_manual'),
    hierarchy_verified: input('hierarchy_verified', 'true', {type: 'checkbox'}),
    numbering_mode: new FakeNode('select', {name: 'numbering_mode'}),
    episode_start_offset: input('episode_start_offset'),
  };
  fields.part_type_manual.value = values.partType || '';
  fields.hierarchy_verified.checked = Boolean(values.partType);
  fields.numbering_mode.value = 'auto';
  const submit = new FakeNode('button', {type: 'submit'});
  const form = new FakeNode('form', {
    class: 'hierarchy-title-editor structural-fields-form',
    'data-edit-form': '', 'data-save-kind': 'title_hierarchy', 'data-save-id': '7',
  }, [
    new FakeNode('label', {}, [fields.part_type_manual]),
    new FakeNode('label', {'data-season-number': ''}, [fields.season_number_manual]),
    new FakeNode('label', {'data-season-label': ''}, [fields.season_label_manual]),
    new FakeNode('label', {'data-part-number': ''}, [fields.part_number_manual]),
    new FakeNode('label', {}, [fields.sort_order_manual]),
    new FakeNode('label', {}, [fields.hierarchy_verified]),
    new FakeNode('label', {}, [fields.numbering_mode]),
    new FakeNode('label', {}, [fields.episode_start_offset]),
    input('return_to', '/hierarchy-review/1/titles/7', {type: 'hidden'}),
    submit,
  ]);
  const saveAll = new FakeNode('button', {id: 'hierarchy-save-all'});
  const body = new FakeNode('body', {}, [
    new FakeNode('div', {
      id: 'hierarchy-save-bar', 'data-save-endpoint': '/hierarchy-review/1/save-all',
    }),
    new FakeNode('span', {id: 'hierarchy-dirty-count'}),
    new FakeNode('p', {id: 'hierarchy-save-feedback'}),
    saveAll,
    form,
  ]);
  const document = {
    body,
    querySelectorAll: selector => body.querySelectorAll(selector),
    getElementById: id => [...body.walk()].find(node => node.attributes.id === id) || null,
    createElement: tagName => new FakeNode(tagName),
    addEventListener: (type, listener) => body.addEventListener(type, listener),
  };
  const window = {
    location: {pathname: '/hierarchy-review/1/titles/7', search: '', hash: ''},
    addEventListener() {},
    confirm: () => true,
  };
  const context = vm.createContext({document, window, FormData: FakeFormData});
  for (const script of ['hierarchy_fields.js', 'local_edit_dirty.js', 'page_edit_save.js']) {
    vm.runInContext(
      fs.readFileSync(path.join(__dirname, '../../app/static', script), 'utf8'),
      context,
    );
  }

  return {
    form, fields, submit, saveAll, registry: window.animeEditDirtyRegistry,
    set(name, value) {
      fields[name].value = value;
      fields[name].dispatch('change');
      body.dispatch('change', fields[name]);
    },
    submitted() {
      return [...new FakeFormData(form).entries()].map(([name]) => name);
    },
    saveAllPayload() {
      saveAll.dispatch('click');
      const request = body.children.find(node => node.submitted);
      const payload = request.children.find(node => node.name === 'payload_json');
      return JSON.parse(payload.value);
    },
  };
}

test('hidden conditional inputs are exactly the ones the backend does not require', () => {
  for (const [partType, visible] of Object.entries(VISIBLE)) {
    const page = titlePage({partType});
    assert.deepEqual(
      page.submitted().filter(name => CONDITIONAL.includes(name)), visible, partType,
    );
    for (const name of CONDITIONAL) {
      const hidden = !visible.includes(name);
      assert.equal(page.fields[name].disabled, hidden, `${partType} ${name}`);
      assert.equal(page.form.querySelector(`[data-${
        {season_number_manual: 'season-number', season_label_manual: 'season-label',
         part_number_manual: 'part-number'}[name]
      }]`).hidden, hidden);
    }
  }
});

test('numbering-only edits toggle dirty state and restoring them cleans it', () => {
  const page = titlePage({season: '2', label: 'S2'});
  assert.equal(page.registry.dirtyForms().length, 0);
  assert.equal(page.submit.disabled, true);
  assert.equal(page.saveAll.disabled, true);

  page.set('numbering_mode', 'absolute');
  assert.deepEqual(Array.from(page.registry.dirtyForms()), [page.form]);
  assert.equal(page.submit.disabled, false);
  assert.equal(page.saveAll.disabled, false);
  page.set('numbering_mode', 'auto');
  assert.equal(page.registry.dirtyForms().length, 0);
  assert.equal(page.submit.disabled, true);

  page.set('episode_start_offset', '12');
  assert.deepEqual(Array.from(page.registry.dirtyForms()), [page.form]);
  page.set('episode_start_offset', '');
  assert.equal(page.registry.dirtyForms().length, 0);
});

test('hidden conditional inputs never create dirty state', () => {
  const page = titlePage({partType: 'part', season: '2', label: 'S2', part: '2'});
  page.fields.season_label_manual.value = 'changed while hidden';
  page.set('episode_start_offset', '');
  assert.equal(page.registry.dirtyForms().length, 0);

  page.set('part_type_manual', 'season');
  assert.equal(page.registry.dirtyForms().length, 1);
  page.set('part_type_manual', 'part');
  assert.equal(page.registry.dirtyForms().length, 0);
});

test('Save All sends the same title fields as the local submit', () => {
  for (const [values, expected] of [
    [{}, ['part_type_manual', 'sort_order_manual', 'numbering_mode', 'episode_start_offset']],
    [{partType: 'part', season: '2', label: 'S2', part: '2'}, [
      'part_type_manual', 'season_number_manual', 'part_number_manual',
      'sort_order_manual', 'numbering_mode', 'episode_start_offset',
    ]],
  ]) {
    const page = titlePage(values);
    page.set('numbering_mode', 'absolute');
    page.set('episode_start_offset', '12');
    const [section] = page.saveAllPayload().sections;

    assert.equal(section.kind, 'title_hierarchy');
    assert.equal(section.id, 7);
    assert.equal(section.values.hierarchy_verified, Boolean(values.partType));
    const {hierarchy_verified, ...fields} = section.values;
    assert.deepEqual(Object.keys(fields).sort(), [...expected].sort());
    assert.deepEqual(
      page.submitted().filter(name => !['return_to', 'hierarchy_verified'].includes(name)).sort(),
      [...expected].sort(),
    );
    assert.equal(fields.numbering_mode, 'absolute');
    assert.equal(fields.episode_start_offset, '12');
  }
});
