const test = require('node:test');
const assert = require('node:assert/strict');

const {createDirtyRegistry} = require('../../app/static/local_edit_dirty.js');

function form(value = '') {
  return {value};
}

function registry(forms) {
  return createDirtyRegistry(forms, item => item.value);
}

test('A: unchanged forms produce zero dirty changes', () => {
  const state = registry([form('title'), form('video')]);
  assert.equal(state.dirtyForms().length, 0);
});

test('B/C: title and video forms use the same dirty contract', () => {
  const title = form('title');
  const video = form('video');
  const state = registry([title, video]);

  title.value = 'title changed';
  assert.deepEqual(state.dirtyForms(), [title]);
  title.value = 'title';
  video.value = 'video changed';
  assert.deepEqual(state.dirtyForms(), [video]);
});

test('D/E: three dirty sections count down when one value is restored', () => {
  const title = form('title');
  const first = form('first');
  const second = form('second');
  const state = registry([title, first, second]);

  title.value = 'title changed';
  first.value = 'first changed';
  second.value = 'second changed';
  assert.equal(state.dirtyForms().length, 3);

  first.value = 'first';
  assert.equal(state.dirtyForms().length, 2);
});

test('F: saving one form preserves other dirty forms', () => {
  const title = form('title');
  const first = form('first');
  const second = form('second');
  const state = registry([title, first, second]);
  title.value = 'title changed';
  first.value = 'first changed';
  second.value = 'second changed';

  state.markSaved(first);

  assert.deepEqual(state.dirtyForms(), [title, second]);
});

test('G: successful save and reload starts with zero dirty forms', () => {
  const title = form('title changed');
  const video = form('video changed');
  const state = registry([title, video]);
  title.value = 'saved title';
  video.value = 'saved video';
  state.markAllSaved();
  assert.equal(state.dirtyForms().length, 0);

  const reloaded = registry([form('saved title'), form('saved video')]);
  assert.equal(reloaded.dirtyForms().length, 0);
});
