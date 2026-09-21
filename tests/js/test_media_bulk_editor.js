const test = require('node:test');
const assert = require('node:assert/strict');

const {readMediaBulkState} = require('../../app/static/media_bulk_editor.js');

test('bulk state is derived only from current page choices and requested values', () => {
  const choices = [{checked: false}, {checked: false}];
  const selects = [{value: ''}, {value: ''}];

  assert.deepEqual(readMediaBulkState(choices, selects), {
    selectedCount: 0,
    hasRequestedChanges: false,
    canSubmit: false,
  });

  choices[1].checked = true;
  selects[0].value = 'ja';
  assert.deepEqual(readMediaBulkState(choices, selects), {
    selectedCount: 1,
    hasRequestedChanges: true,
    canSubmit: true,
  });

  choices[1].checked = false;
  assert.equal(readMediaBulkState(choices, selects).canSubmit, false);
});

test('a newly rendered page starts with no selection', () => {
  const firstPage = [{checked: true}, {checked: true}];
  assert.equal(readMediaBulkState(firstPage, [{value: 'none'}]).selectedCount, 2);

  const nextPage = [{checked: false}, {checked: false}];
  assert.deepEqual(readMediaBulkState(nextPage, [{value: ''}]), {
    selectedCount: 0,
    hasRequestedChanges: false,
    canSubmit: false,
  });
});
