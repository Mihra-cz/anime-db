const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const implementation = path.join(__dirname, '../../app/static/naming_review.js');

test('naming preview presentation is available', () => {
  assert.ok(fs.existsSync(implementation), 'Naming Review preview implementation is missing');
});

function harness() {
  const {createPreviewController} = require(implementation);
  const timers = new Map();
  const requests = [];
  const rendered = [];
  const errors = [];
  let next = 0;
  const controller = createPreviewController({
    request(text, fingerprint, signal) {
      let resolve;
      let reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      requests.push({text, fingerprint, signal, resolve, reject});
      return promise;
    },
    setTimer(fn) { timers.set(++next, fn); return next; },
    clearTimer(id) { timers.delete(id); },
    render(value) { rendered.push(value); },
    onError(value) { errors.push(value); },
  });
  return {controller, requests, rendered, errors, flush() {
    for (const [id, fn] of timers) { timers.delete(id); fn(); }
  }};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('custom preview debounces and passes unmodified text and fingerprint to server', async () => {
  const h = harness();
  h.controller.update('previous', 'basis');
  h.controller.update(' Cafe\u0301: 日本語? ', 'basis');
  assert.equal(h.requests.length, 0);
  h.flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].text, ' Cafe\u0301: 日本語? ');
  assert.equal(h.requests[0].fingerprint, 'basis');
  const result = {preview_text:'Café - 日本語', chars:10, utf8_bytes:17, valid:true};
  h.requests[0].resolve(result);
  await settle();
  assert.deepEqual(h.rendered, [result]);
});

test('older responses cannot replace newer preview even when abort is ignored', async () => {
  const h = harness();
  h.controller.update('old', 'basis'); h.flush();
  h.controller.update('new', 'basis'); h.flush();
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[1].resolve({preview_text:'new'}); await settle();
  h.requests[0].resolve({preview_text:'old'}); await settle();
  assert.deepEqual(h.rendered, [{preview_text:'new'}]);
});

test('canceling custom choice discards in-flight results and pending requests', async () => {
  const h = harness();
  h.controller.update('old', 'basis'); h.flush();
  h.controller.cancel();
  h.requests[0].resolve({preview_text:'old'}); await settle();
  h.controller.update('pending', 'basis'); h.controller.cancel(); h.flush();
  assert.deepEqual(h.rendered, []);
  assert.equal(h.requests.length, 1);
});

test('stale context and network errors remain visible without automatic saving', async () => {
  const h = harness();
  h.controller.update('custom', 'basis'); h.flush();
  h.requests[0].reject(new Error('Kontext pojmenování se změnil. Obnov stránku.'));
  await settle();
  assert.deepEqual(h.errors, ['Kontext pojmenování se změnil. Obnov stránku.']);
  assert.deepEqual(h.rendered, []);
});

test('preview strings and diagnostics render as text, without injecting markup', () => {
  const {renderPreview} = require(implementation);
  const nodes = {};
  const target = {dataset:{}, querySelector(selector) {
    return nodes[selector] ||= {textContent:''};
  }};
  renderPreview(target, {preview_text:'<img src=x>', chars:11, utf8_bytes:11,
    max_component_bytes:260, valid:false, transformations:['<b>změna</b>'],
    diagnostics:['Název je příliš dlouhý. Urči kratší název.', 'Překročeno o 5 UTF-8 bytes.']});
  assert.equal(nodes['[data-preview-text]'].textContent, '<img src=x>');
  assert.match(nodes['[data-preview-metrics]'].textContent, /260.*255/);
  assert.match(nodes['[data-preview-diagnostics]'].textContent, /Překročeno o 5/);
  assert.equal(target.dataset.valid, 'false');
});
