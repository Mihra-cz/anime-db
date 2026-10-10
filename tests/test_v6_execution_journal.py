"""Durable journal tests use only isolated pytest tmp_path files."""
from dataclasses import asdict, replace
import errno
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.target_execution_manifest import (
    JournalEntry, WarningAcknowledgement, can_continue_after_failure, manifest_to_json,
)
from app.target_planner_types import FilesystemEntry, LocatorBaseline
from test_v6_execution_manifest import fixture, make


def api():
    assert importlib.util.find_spec('app.target_execution_journal') is not None, \
        'Durable execution journal foundation is missing'
    return importlib.import_module('app.target_execution_journal')


def scratch_manifest(tmp_path, *, content='episode', two=False, dependency=False, primary_dependent=False):
    library = tmp_path / 'library'
    library.mkdir()
    ctx, snap = fixture(content=content)
    if two:
        first = replace(ctx.videos[0], ordinal=1) if content != 'episode' else ctx.videos[0]
        second = replace(ctx.videos[0], id=101, source='old/z.mkv',
            episode_number=2 if content == 'episode' else None,
            ordinal=2 if content != 'episode' else None)
        ctx = replace(ctx, videos=(first, second), locator_baselines=ctx.locator_baselines + (
            LocatorBaseline('video', 101, 'relative_path', 'old/z.mkv'),
            LocatorBaseline('video', 101, 'root_folder', 'old'),
        ))
        snap = replace(snap, entries=snap.entries + (FilesystemEntry('old/z.mkv', 'regular', 7, 9),))
    snap = replace(snap, root=str(library))
    result = make(ctx, snap)
    if dependency:
        actions = result.payload.actions
        payload = replace(result.payload, actions=(actions[0],
            replace(actions[1], depends_on=(actions[0].action_id,))))
        if primary_dependent:
            payload = replace(payload, actions=(payload.actions[0],
                replace(payload.actions[1], criticality='PRIMARY',
                    criticality_reason='isolated dependent PRIMARY fixture', failure_policy='STOP')))
        # This seals a deliberately constructed isolated fixture, never a production plan.
        identity = hashlib.sha256(json.dumps(
            asdict(payload), sort_keys=True,
            separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        result = replace(result, payload=payload, manifest_id=identity)
    return result


def entry(manifest, state='PENDING', index=0, **kwargs):
    return JournalEntry(manifest.manifest_id, manifest.payload.actions[index].action_id,
        index, state, **kwargs)


def complete(writer, manifest, index=0):
    for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'):
        writer.append(entry(manifest, state, index))


def test_create_is_exclusive_and_header_bound_to_exact_manifest(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        assert writer.entries == ()
        with pytest.raises(FileExistsError):
            journal.DurableJournal.create(path, manifest)
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = journal.load_journal(path, manifest)
    assert loaded.manifest_id == manifest.manifest_id
    assert loaded.entries == ()
    assert loaded.reconciliation_required is True


def test_roundtrip_preserves_explicit_states_and_requires_reconciliation_on_reopen(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        complete(writer, manifest)
        recorded = writer.entries
    loaded = journal.load_journal(path, manifest)
    assert [e.state for e in loaded.entries] == [
        'PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE']
    assert loaded.entries == recorded
    # History records assertions; even COMPLETE does not grant automatic resumption.
    assert loaded.reconciliation_required is True
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        journal.DurableJournal.create(path, manifest)
    assert path.read_bytes() == before


def test_serialization_is_deterministic_and_records_have_contiguous_sequences(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    paths = [tmp_path / 'first.jsonl', tmp_path / 'second.jsonl']
    for path in paths:
        with journal.DurableJournal.create(path, manifest) as writer:
            complete(writer, manifest)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    records = [json.loads(line) for line in paths[0].read_text().splitlines()]
    assert [r['sequence'] for r in records] == [0, 1, 2, 3, 4, 5]
    assert all(r['entry']['action_index'] == 0 for r in records[1:])


def test_action_index_order_cannot_skip_or_interleave_incomplete_actions(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path, two=True)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))
        writer.append(entry(manifest))
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))
        for state in ('FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'):
            writer.append(entry(manifest, state))
        complete(writer, manifest, 1)
    loaded = journal.load_journal(path, manifest)
    assert [e.action_index for e in loaded.entries] == [0]*5 + [1]*5
    assert {e.action_id for e in loaded.entries} == {a.action_id for a in manifest.payload.actions}


@pytest.mark.parametrize('previous,next_state', [
    (None, 'FS_APPLIED'), ('PENDING', 'PENDING'), ('PENDING', 'FS_VERIFIED'),
    ('FS_APPLIED', 'PENDING'), ('FS_APPLIED', 'DB_RECONCILED'),
    ('FS_VERIFIED', 'COMPLETE'), ('DB_RECONCILED', 'FS_VERIFIED'),
    ('COMPLETE', 'PENDING'), ('COMPLETE', 'FAILED'), ('FAILED', 'PENDING'),
])
def test_invalid_or_duplicate_transition_does_not_change_history(tmp_path, previous, next_state):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        if previous == 'FAILED':
            writer.append(entry(manifest))
            writer.append(entry(manifest, 'FAILED', failure_code='io_error'))
        elif previous:
            for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED', 'COMPLETE'):
                writer.append(entry(manifest, state))
                if state == previous:
                    break
        before = path.read_bytes()
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, next_state,
                **({'failure_code': 'io_error'} if next_state == 'FAILED' else {})))
        assert path.read_bytes() == before


@pytest.mark.parametrize('state', ['PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED'])
def test_failure_is_recordable_from_each_nonterminal_state(tmp_path, state):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest) as writer:
        for next_state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED'):
            writer.append(entry(manifest, next_state))
            if state == next_state:
                break
        writer.append(entry(manifest, 'FAILED', failure_code='test_failure'))
        assert writer.entries[-1].state == 'FAILED'


@pytest.mark.parametrize('change', [
    {'manifest_id': 'b'*64}, {'action_id': 'b'*64}, {'action_index': 1},
    {'action_index': True}, {'action_index': 0.0}, {'source_intact': 1},
    {'failure_code': 'not_a_failure'},
])
def test_wrong_identity_or_typed_entry_is_rejected_without_write(tmp_path, change):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        before = path.read_bytes()
        with pytest.raises((journal.JournalError, ValueError)):
            writer.append(replace(entry(manifest), **change))
        assert path.read_bytes() == before


def test_wrong_manifest_and_changed_envelope_cannot_load_history(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest):
        pass
    # A forged manifest ID fails manifest validation before any journal read.
    with pytest.raises(ValueError):
        journal.load_journal(path, replace(manifest, manifest_id='b'*64))
    (tmp_path / 'other').mkdir()
    acknowledgement = WarningAcknowledgement('w'*64, 'warning', 'video', 1,
        'human_operator', '2026-10-09T00:00:00Z')
    # Valid manifests that differ in payload, timestamp or approvals hit the header binding.
    for other in (scratch_manifest(tmp_path / 'other', two=True),
            replace(manifest, created_at='2026-10-09T00:00:00Z'),
            replace(manifest, acknowledgements=(acknowledgement,))):
        with pytest.raises(journal.ReconciliationRequired, match='binding mismatch'):
            journal.load_journal(path, other)


@pytest.mark.parametrize('damage,prefix', [
    ('truncate', 4), ('missing_newline', 4), ('checksum', 4),
    ('duplicate', 1), ('reorder', 0), ('extra_key', 4), ('middle_byte', 2),
])
def test_corrupt_history_stops_without_repair_and_preserves_diagnostics(tmp_path, damage, prefix):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        complete(writer, manifest)
    lines = path.read_bytes().splitlines(keepends=True)
    if damage == 'truncate':
        lines[-1] = lines[-1][:len(lines[-1])//2]
    elif damage == 'missing_newline':
        lines[-1] = lines[-1].rstrip(b'\n')
    elif damage == 'checksum':
        lines[-1] = lines[-1].replace(b'COMPLETE', b'PENDING ')
    elif damage == 'duplicate':
        lines.insert(2, lines[1])
    elif damage == 'reorder':
        lines[1], lines[2] = lines[2], lines[1]
    elif damage == 'middle_byte':
        lines[3] = lines[3].replace(b'FS_VERIFIED', b'FS_VERIFIEX')
    else:
        value = json.loads(lines[-1]); value['unexpected'] = True
        lines[-1] = (json.dumps(value)+'\n').encode()
    path.write_bytes(b''.join(lines))
    damaged = path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(path, manifest)
    # Only the validated prefix before the damaged record is diagnostic evidence.
    assert error.value.entries == tuple(entry(manifest, state) for state in
        ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED')[:prefix])
    assert path.read_bytes() == damaged
    with pytest.raises(FileExistsError):
        journal.DurableJournal.create(path, manifest)


def test_partial_write_poisoned_writer_and_reopen_requires_reconciliation(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        original_write = os.write
        count = 0
        def fail_mid_record(fd, data):
            nonlocal count
            if fd == writer._fd:
                count += 1
                if count == 1:
                    return original_write(fd, data[:len(data)//2])
                raise OSError(errno.EIO, 'simulated interrupted write')
            return original_write(fd, data)
        with monkeypatch.context() as patch:
            patch.setattr(journal.os, 'write', fail_mid_record)
            with pytest.raises(OSError):
                writer.append(entry(manifest))
        assert writer.entries == ()
        damaged = path.read_bytes()
        with pytest.raises(journal.ReconciliationRequired):
            writer.append(entry(manifest))
        assert path.read_bytes() == damaged
    with pytest.raises(journal.ReconciliationRequired):
        journal.load_journal(path, manifest)
    assert path.read_bytes() == damaged


def test_fsync_failure_does_not_acknowledge_complete_frame_or_allow_retry(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        original_fsync = os.fsync
        def fail_commit(fd):
            if fd == writer._fd:
                raise OSError(errno.EIO, 'simulated fsync failure')
            return original_fsync(fd)
        with monkeypatch.context() as patch:
            patch.setattr(journal.os, 'fsync', fail_commit)
            with pytest.raises(OSError):
                writer.append(entry(manifest))
        assert writer.entries == ()
        with pytest.raises(journal.ReconciliationRequired):
            writer.append(entry(manifest))
    # A whole frame cannot prove that the interrupted writer received a durable acknowledgement.
    loaded = journal.load_journal(path, manifest)
    assert loaded.entries[-1].state == 'PENDING'
    assert loaded.reconciliation_required is True


def test_crash_during_header_creation_preserves_partial_file(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    original_write = os.write
    writes = 0
    def fail_header(fd, data):
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(fd, data[:8])
        raise OSError(errno.EIO, 'header crash')
    with monkeypatch.context() as patch:
        patch.setattr(journal.os, 'write', fail_header)
        with pytest.raises(OSError):
            journal.DurableJournal.create(path, manifest)
    assert path.exists()
    damaged = path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired):
        journal.load_journal(path, manifest)
    assert path.read_bytes() == damaged


def test_other_process_cannot_read_live_writer_or_create_second_writer(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(manifest_to_json(manifest))
    path = tmp_path / 'journal.jsonl'
    script = '''
import sys
from pathlib import Path
from app.target_execution_manifest import manifest_from_json
from app.target_execution_journal import load_journal, DurableJournal, JournalBusyError
manifest=manifest_from_json(Path(sys.argv[2]).read_text())
try:
    load_journal(Path(sys.argv[1]),manifest)
except JournalBusyError:
    pass
else:
    raise AssertionError('independent reader acquired live writer journal')
try:
    DurableJournal.create(Path(sys.argv[1]),manifest)
except FileExistsError:
    pass
else:
    raise AssertionError('independent writer overwrote existing journal')
'''
    with journal.DurableJournal.create(path, manifest) as writer:
        writer.append(entry(manifest))
        process = subprocess.run([sys.executable, '-c', script, str(path), str(manifest_path)],
            capture_output=True, text=True, timeout=15)
        assert process.returncode == 0, process.stderr
    assert journal.load_journal(path, manifest).entries == (entry(manifest),)


@pytest.mark.parametrize('unsafe', ['leaf_symlink', 'ancestor_symlink', 'library', 'repo', 'recycle', 'parent_traversal', 'relative'])
def test_unsafe_creation_path_is_rejected_before_write(tmp_path, unsafe):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    if unsafe == 'leaf_symlink':
        victim = tmp_path / 'victim'; victim.write_bytes(b'preserve')
        path.symlink_to(victim)
    elif unsafe == 'ancestor_symlink':
        (tmp_path / 'alias').symlink_to(tmp_path / 'library', target_is_directory=True)
        path = tmp_path / 'alias/journal.jsonl'
    elif unsafe == 'library':
        path = Path(manifest.payload.library_root) / 'journal.jsonl'
    elif unsafe == 'repo':
        path = Path(__file__).resolve().parents[1] / 'journal-must-not-exist.jsonl'
    elif unsafe == 'recycle':
        path = tmp_path / '#recycle/journal.jsonl'
    elif unsafe == 'parent_traversal':
        path = tmp_path / 'library/../journal.jsonl'
    else:
        path = Path('journal-must-not-exist.jsonl')
    with pytest.raises((journal.JournalError, ValueError, OSError)):
        journal.DurableJournal.create(path, manifest)
    if unsafe == 'leaf_symlink':
        assert victim.read_bytes() == b'preserve'
    else:
        assert not path.exists()


def test_reader_rejects_symlink_hardlink_fifo_and_forbidden_alias(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest):
        pass
    alias = tmp_path / 'alias'
    alias.symlink_to(path)
    with pytest.raises((journal.JournalError, ValueError, OSError)):
        journal.load_journal(alias, manifest)
    hard = tmp_path / 'hard'
    os.link(path, hard)
    with pytest.raises((journal.JournalError, ValueError)):
        journal.load_journal(path, manifest)
    fifo = tmp_path / 'fifo'; os.mkfifo(fifo)
    with pytest.raises((journal.JournalError, ValueError, OSError)):
        journal.load_journal(fifo, manifest)
    with pytest.raises((journal.JournalError, ValueError)):
        journal.load_journal(hard, manifest, forbidden_files=(path,))


def test_remote_or_unknown_filesystem_is_rejected(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    import app.tools.execution_manifest as cli
    for fstype in ('cifs', 'nfs4', None):
        monkeypatch.setattr(cli, '_mount_fstype', lambda path, kind=fstype: kind)
        with pytest.raises(ValueError):
            journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest)
    assert not (tmp_path / 'journal.jsonl').exists()


@pytest.mark.parametrize('missing', ['source_intact', 'target_absent', 'primary_dependencies_clear', 'failure_journaled'])
def test_low_failure_with_missing_condition_blocks_next_action(tmp_path, missing):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True)
    conditions = dict(source_intact=True, target_absent=True,
        primary_dependencies_clear=True, failure_journaled=True)
    conditions[missing] = False
    failure = entry(manifest, 'FAILED', failure_code='io_error', **conditions)
    assert not can_continue_after_failure(manifest, failure)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest) as writer:
        writer.append(entry(manifest))
        writer.append(failure)
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))


def test_primary_failure_always_stops_even_with_all_low_conditions(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path, two=True)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest) as writer:
        writer.append(entry(manifest))
        writer.append(entry(manifest, 'FAILED', failure_code='io_error',
            source_intact=True, target_absent=True,
            primary_dependencies_clear=True, failure_journaled=True))
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))


def test_safe_low_failure_allows_only_next_independent_action(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest) as writer:
        writer.append(entry(manifest))
        failure = entry(manifest, 'FAILED', failure_code='io_error',
            source_intact=True, target_absent=True,
            primary_dependencies_clear=True, failure_journaled=True)
        writer.append(failure)
        assert can_continue_after_failure(manifest, failure)
        writer.append(entry(manifest, index=1))
        assert writer.entries[-1].action_index == 1


def test_dependencies_must_be_complete_before_start(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True, dependency=True)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest) as writer:
        writer.append(entry(manifest))
        writer.append(entry(manifest, 'FAILED', failure_code='io_error',
            source_intact=True, target_absent=True,
            primary_dependencies_clear=True, failure_journaled=True))
        # Standalone LOW contract permits continuation, dependency still cannot be assumed complete.
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))


def test_review_criticality_cannot_enter_lifecycle(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    payload = replace(manifest.payload, actions=(replace(manifest.payload.actions[0],
        criticality='REVIEW', criticality_reason='isolated review fixture', failure_policy='REVIEW'),))
    # Seals a deliberately constructed isolated fixture, never a production plan.
    identity = hashlib.sha256(json.dumps(asdict(payload), sort_keys=True,
        separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    manifest = replace(manifest, payload=payload, manifest_id=identity)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        before = path.read_bytes()
        with pytest.raises(journal.JournalError, match='REVIEW'):
            writer.append(entry(manifest))
        assert path.read_bytes() == before
        assert writer.entries == ()


def test_out_of_band_append_poisons_live_writer_without_repair(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        writer.append(entry(manifest))
        with path.open('ab') as stream:  # Advisory lock: same-user raw write to scratch data.
            stream.write(b'foreign\n')
        damaged = path.read_bytes()
        for _ in range(2):
            with pytest.raises(journal.ReconciliationRequired, match='outside writer|interrupted'):
                writer.append(entry(manifest, 'FS_APPLIED'))
        assert writer.entries == (entry(manifest),)
        assert path.read_bytes() == damaged
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(path, manifest)
    assert error.value.entries == (entry(manifest),)


def test_manifest_and_scratch_library_are_immutable(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    source = Path(manifest.payload.library_root) / 'original.mkv'
    source.write_bytes(b'untouched library fixture')
    db = tmp_path / 'source.db'; db.write_bytes(b'untouched DB fixture')
    manifest_path = tmp_path / 'manifest.json'; manifest_path.write_text(manifest_to_json(manifest))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (source, db, manifest_path)}
    immutable = manifest_to_json(manifest)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest,
            forbidden_files=(db, manifest_path)) as writer:
        complete(writer, manifest)
    assert manifest_to_json(manifest) == immutable
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == values for p, values in before.items())


def test_parent_replacement_stops_writer_before_any_new_bytes(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    parent = tmp_path / 'runtime'; parent.mkdir(mode=0o700)
    path = parent / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        original = tmp_path / 'original-runtime'
        parent.rename(original)  # Isolated runtime directory, never the library.
        parent.mkdir(mode=0o700)
        before = (original / 'journal.jsonl').read_bytes()
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest))
        assert (original / 'journal.jsonl').read_bytes() == before
        assert not path.exists()


def test_parent_permissions_and_new_hardlink_stop_live_writer(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    parent = tmp_path / 'runtime'; parent.mkdir(mode=0o700)
    path = parent / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        before = path.read_bytes()
        parent.chmod(0o777)
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest))
        assert path.read_bytes() == before
    parent.chmod(0o700)
    other = parent / 'other.jsonl'
    with journal.DurableJournal.create(other, manifest) as writer:
        before = other.read_bytes()
        os.link(other, parent / 'alias')
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest))
        assert other.read_bytes() == before


def test_forked_writer_rejects_append_without_waiting_for_inherited_mutex(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        before = path.read_bytes()
        writer._mutex.acquire()
        pid = os.fork()
        if pid == 0:
            # The child must never return into the pytest runner, whatever it raises.
            code = 1
            try:
                import signal
                signal.alarm(3)
                writer.append(entry(manifest))
            except journal.JournalError:
                code = 0
            except BaseException:
                code = 2
            finally:
                os._exit(code)
        try:
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        finally:
            writer._mutex.release()
        assert path.read_bytes() == before


def test_lost_whole_tail_frame_never_grants_resumption(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        complete(writer, manifest)
    path.write_bytes(b''.join(path.read_bytes().splitlines(keepends=True)[:-1]))
    loaded = journal.load_journal(path, manifest)
    assert loaded.entries[-1].state == 'DB_RECONCILED'
    assert loaded.reconciliation_required is True


def test_directory_fsync_failure_is_not_successful_creation(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    original_fsync = os.fsync
    def fail_directory(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'directory persistence failed')
        return original_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(journal.os, 'fsync', fail_directory)
        with pytest.raises(OSError):
            journal.DurableJournal.create(path, manifest)
    assert path.exists()  # Evidence preserved; no automatic cleanup/retry.
    with pytest.raises(FileExistsError):
        journal.DurableJournal.create(path, manifest)


def rewrite_frames(path, modify):
    """Forge internally checksummed scratch data to exercise semantic replay validation."""
    frames = [json.loads(line) for line in path.read_text().splitlines()]
    modify(frames)
    previous = None
    lines = []
    for frame in frames:
        body = {k: v for k, v in frame.items() if k != 'sha256'}
        if 'previous_sha256' in body:
            body['previous_sha256'] = previous
        raw = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        previous = hashlib.sha256(raw.encode()).hexdigest()
        lines.append(json.dumps({**body, 'sha256': previous},
            sort_keys=True, separators=(',', ':'), ensure_ascii=True)+'\n')
    path.write_text(''.join(lines))


@pytest.mark.parametrize('damage', [
    'boolean_version', 'wrong_manifest', 'wrong_action', 'boolean_index',
    'skip_transition', 'nonfailure_evidence', 'unknown_entry_field',
])
def test_valid_checksums_do_not_override_strict_binding_types_or_lifecycle(tmp_path, damage):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        complete(writer, manifest)
    def modify(frames):
        if damage == 'boolean_version':
            frames[0]['version'] = True
        elif damage == 'wrong_manifest':
            frames[1]['entry']['manifest_id'] = 'b'*64
        elif damage == 'wrong_action':
            frames[1]['entry']['action_id'] = 'b'*64
        elif damage == 'boolean_index':
            frames[1]['entry']['action_index'] = False
        elif damage == 'skip_transition':
            frames[2]['entry']['state'] = 'FS_VERIFIED'
        elif damage == 'nonfailure_evidence':
            frames[1]['entry']['source_intact'] = True
        else:
            frames[1]['entry']['ignore_failure'] = True
    rewrite_frames(path, modify)
    before = path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired):
        journal.load_journal(path, manifest)
    assert path.read_bytes() == before


def test_deeply_nested_corrupt_frame_is_reconciliation_not_parser_crash(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        writer.append(entry(manifest))
    with path.open('ab') as stream:  # Isolated scratch damage within the frame size limit.
        stream.write(b'[' * 30000 + b']' * 30000 + b'\n')
    damaged = path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(path, manifest)
    assert error.value.entries == (entry(manifest),)
    assert path.read_bytes() == damaged


def test_losing_all_records_leaves_ambiguous_header_not_resumption_permission(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        complete(writer, manifest)
    path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
    loaded = journal.load_journal(path, manifest)
    assert loaded.entries == ()
    assert loaded.reconciliation_required is True


def test_short_and_interrupted_writes_finish_one_durable_record(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        original_write = os.write
        attempts = 0
        def short_write(fd, data):
            nonlocal attempts
            if fd != writer._fd:
                return original_write(fd, data)
            attempts += 1
            if attempts == 1:
                raise InterruptedError(errno.EINTR, 'interrupted syscall')
            return original_write(fd, data[:11])
        with monkeypatch.context() as patch:
            patch.setattr(journal.os, 'write', short_write)
            writer.append(entry(manifest))
        assert writer.entries == (entry(manifest),)
    assert journal.load_journal(path, manifest).entries == (entry(manifest),)


def test_low_failure_with_primary_dependent_never_continues(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True,
        dependency=True, primary_dependent=True)
    failure = entry(manifest, 'FAILED', failure_code='io_error', source_intact=True,
        target_absent=True, primary_dependencies_clear=True, failure_journaled=True)
    assert not can_continue_after_failure(manifest, failure)
    with journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest) as writer:
        writer.append(entry(manifest))
        writer.append(failure)
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))


def test_real_process_exit_releases_lock_but_keeps_partial_frame(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    manifest_path = tmp_path / 'manifest.json'; manifest_path.write_text(manifest_to_json(manifest))
    path = tmp_path / 'journal.jsonl'
    script = '''
import os,sys
from pathlib import Path
from app.target_execution_manifest import JournalEntry,manifest_from_json
from app.target_execution_journal import DurableJournal
import app.target_execution_journal as journal
manifest=manifest_from_json(Path(sys.argv[2]).read_text())
writer=DurableJournal.create(Path(sys.argv[1]),manifest)
write=os.write
def crash(fd,data):
    write(fd,data[:len(data)//2])
    os._exit(73)
journal.os.write=crash
writer.append(JournalEntry(manifest.manifest_id,manifest.payload.actions[0].action_id,0,'PENDING'))
'''
    process = subprocess.run([sys.executable, '-c', script, str(path), str(manifest_path)],
        capture_output=True, text=True, timeout=15)
    assert process.returncode == 73, process.stderr
    before = path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(path, manifest)
    assert error.value.entries == ()
    assert path.read_bytes() == before


@pytest.mark.parametrize('fstype', ['tmpfs', 'ramfs'])
def test_volatile_memory_filesystem_cannot_claim_durable_journal(tmp_path, monkeypatch, fstype):
    journal = api()
    manifest = scratch_manifest(tmp_path)
    import app.tools.execution_manifest as cli
    monkeypatch.setattr(cli, '_mount_fstype', lambda path: fstype)
    with pytest.raises(journal.JournalError):
        journal.DurableJournal.create(tmp_path / 'journal.jsonl', manifest)
    assert not (tmp_path / 'journal.jsonl').exists()


@pytest.mark.parametrize('predecessor', ['PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED'])
def test_low_continuation_requires_direct_durable_pending_to_failed(tmp_path, predecessor):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True)
    path = tmp_path / 'journal.jsonl'
    failure = entry(manifest, 'FAILED', failure_code='io_error', source_intact=True,
        target_absent=True, primary_dependencies_clear=True, failure_journaled=True)
    # The same context-free entry meets necessary conditions in every history.
    assert can_continue_after_failure(manifest, failure)
    with journal.DurableJournal.create(path, manifest) as writer:
        for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED'):
            writer.append(entry(manifest, state))
            if state == predecessor:
                break
        writer.append(failure)
        diagnostic = writer.entries
        before = path.read_bytes()
        if predecessor == 'PENDING':
            complete(writer, manifest, 1)
            assert writer.entries[-1].state == 'COMPLETE'
        else:
            # Boolean assertions cannot undo already recorded FS/DB progress.
            with pytest.raises(journal.JournalError, match='STOP'):
                writer.append(entry(manifest, index=1))
            assert writer.entries == diagnostic
            assert path.read_bytes() == before
        recorded = writer.entries
    loaded = journal.load_journal(path, manifest)
    assert loaded.entries == recorded
    assert loaded.reconciliation_required is True
    if predecessor != 'PENDING':
        assert loaded.entries[-1] == failure  # Keep late failure for diagnosis.


@pytest.mark.parametrize('predecessor', ['FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED'])
def test_replay_rejects_checksummed_continuation_after_late_low_failure(tmp_path, predecessor):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True)
    path = tmp_path / 'journal.jsonl'
    with journal.DurableJournal.create(path, manifest) as writer:
        for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED', 'DB_RECONCILED'):
            writer.append(entry(manifest, state))
            if state == predecessor:
                break
        writer.append(entry(manifest, 'FAILED', failure_code='io_error', source_intact=True,
            target_absent=True, primary_dependencies_clear=True, failure_journaled=True))
        diagnostic = writer.entries
    def add_unsafe_continuation(frames):
        frames.append({'sequence': len(frames), 'previous_sha256': frames[-1]['sha256'],
            'entry': asdict(entry(manifest, index=1))})
    # Valid checksums/identities do not make this legacy unsafe transition valid.
    rewrite_frames(path, add_unsafe_continuation)
    damaged = path.read_bytes()
    with pytest.raises(journal.ReconciliationRequired) as error:
        journal.load_journal(path, manifest)
    assert error.value.entries == diagnostic
    assert path.read_bytes() == damaged


def test_unacknowledged_low_failure_never_allows_continuation(tmp_path, monkeypatch):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True)
    path = tmp_path / 'journal.jsonl'
    failure = entry(manifest, 'FAILED', failure_code='io_error', source_intact=True,
        target_absent=True, primary_dependencies_clear=True, failure_journaled=True)
    assert can_continue_after_failure(manifest, failure)
    with journal.DurableJournal.create(path, manifest) as writer:
        writer.append(entry(manifest))
        original_fsync = os.fsync
        def fail_failure_barrier(fd):
            if fd == writer._fd:
                raise OSError(errno.EIO, 'unacknowledged failure record')
            return original_fsync(fd)
        with monkeypatch.context() as patch:
            patch.setattr(journal.os, 'fsync', fail_failure_barrier)
            with pytest.raises(OSError):
                writer.append(failure)
        assert writer.entries == (entry(manifest),)
        uncertain = path.read_bytes()
        with pytest.raises(journal.ReconciliationRequired):
            writer.append(entry(manifest, index=1))
        assert path.read_bytes() == uncertain
    loaded = journal.load_journal(path, manifest)
    assert loaded.entries == (entry(manifest), failure)
    assert loaded.reconciliation_required is True
    with pytest.raises(FileExistsError):
        journal.DurableJournal.create(path, manifest)
    assert path.read_bytes() == uncertain


def test_failed_entry_without_history_is_not_continuation_authority(tmp_path):
    journal = api()
    manifest = scratch_manifest(tmp_path, content='op', two=True)
    path = tmp_path / 'journal.jsonl'
    failure = entry(manifest, 'FAILED', failure_code='io_error', source_intact=True,
        target_absent=True, primary_dependencies_clear=True, failure_journaled=True)
    assert can_continue_after_failure(manifest, failure)
    with journal.DurableJournal.create(path, manifest) as writer:
        before = path.read_bytes()
        with pytest.raises(journal.JournalError):
            writer.append(failure)
        with pytest.raises(journal.JournalError):
            writer.append(entry(manifest, index=1))
        assert writer.entries == ()
        assert path.read_bytes() == before
