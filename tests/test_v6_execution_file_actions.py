"""E7G: complete generated mixed manifests, through the existing scratch owner."""
from dataclasses import replace
from contextlib import closing
import gc
import errno
import os
import sqlite3

import pytest
from sqlalchemy.orm import Session

from app import target_execution_database as database
from app import target_execution_coordination as stops
from app import target_execution_directories as directories
from app import target_execution_filesystem as fs
from app import target_execution_journal as journal
from app.database import make_engine
from app.maintenance import ScratchMaintenance
from app.models import (
    CatalogCollection, CatalogTitle, ExternalSubtitle, ExternalSubtitleCompatibility,
    UnresolvedExternalSubtitle, Video,
)
from app.target_execution_manifest import (
    build_execution_manifest, duplicate_execution_evidence, manifest_from_json,
    manifest_to_json, post_state_hash_from_manifest,
)
from app.target_execution_sequence import ScratchManifestExecution
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_post_state import verify_post_state
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.tools.target_plan import database_fingerprint
from test_physical_naming import NOW
from test_v6_execution_pair import STATES, assert_no_case_descriptors
from test_v6_execution_projection import expected_rows
from test_v6_execution_filesystem import seal, digest
from v6_database_case import database_rows, prepare_database


def generate(owner, path, decisions=None):
    with readonly_planner_session(path) as session:
        context = load_planner_context(session)
    snapshot = inventory_filesystem(owner.root)
    plan = plan_library(context, snapshot, windows_root=r'C:\Anime')
    manifest = build_execution_manifest(context, snapshot, plan, database_fingerprint(path),
        mount_identity=owner.mount_identity, windows_root=r'C:\Anime',
        created_at='2026-10-19T12:00:00Z', criticality_decisions=decisions)
    return manifest_from_json(manifest_to_json(manifest))


def prepare_mixed(owner, *, accounting='COMPLETE'):
    _, _, _, path = prepare_database(owner)

    def file(source, content):
        asset = owner.root / source
        asset.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        asset.write_bytes(content)
        asset.chmod(0o600)
        return asset.stat()

    copy_source = ('Duplicates/Show/Season 01/Original.MKV' if accounting == 'UNKNOWN'
        else 'old/copy/Original.MKV')
    info = file(copy_source, b'confirmed secondary video')
    file('old/source.ass', b'primary subtitle')
    unmatched_source = 'Show/extra.cs.srt' if accounting == 'UNKNOWN' else 'old/extra.cs.srt'
    file(unmatched_source, b'confirmed no match')
    file('old/subs.zip', b'preserved archive content')
    copy_parent = copy_source.rsplit('/', 1)[0]
    file(copy_parent + '/subs.zip', b'preserved archive content')
    file(copy_parent + '/Original.ass', b'secondary subtitle')
    file('old/OST/audio.flac', b'auxiliary audio')
    file('Show/Extras/notes.txt', b'already placed auxiliary')
    file('Subs/existing.zip', b'already placed archive')
    file('seznam-souboru.txt', b'approved root inventory')
    if accounting == 'INCOMPLETE':
        file('old/copy/Scan/image.png', b'unowned scan must not become duplicate evidence')
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            primary = session.get(Video, 100)
            session.add(Video(id=101, catalog_title_id=primary.catalog_title_id,
                catalog_collection_id=primary.catalog_collection_id,
                relative_path=copy_source, root_folder=copy_source.split('/')[0],
                filename='Original.MKV', size=info.st_size, mtime_ns=info.st_mtime_ns,
                file_type='episode', content_type_manual='episode', season_episode_number=1,
                local_episode_number=1, episode_number_manual_override=1,
                episode_number_verified_at=NOW, duplicate_of_video_id=100))
            sources = [(901, 'old/source.ass', 100), (903, copy_parent + '/Original.ass', 101)]
            for sub_id, source, video_id in sources:
                subtitle = ExternalSubtitle(id=sub_id, relative_path=source, codec='ass')
                session.add(subtitle)
                session.add(ExternalSubtitleCompatibility(external_subtitle=subtitle,
                    video_id=video_id, status='confirmed_compatible', match_method='manual',
                    verified_at=NOW))
            session.add(UnresolvedExternalSubtitle(id=902, relative_path=unmatched_source,
                filename='extra.cs.srt', extension='.srt', status='confirmed_no_match'))
            session.get(CatalogCollection, primary.catalog_collection_id).relative_root_path = 'old'
            session.get(CatalogTitle, primary.catalog_title_id).relative_root_path = 'old'
            session.commit()
    finally:
        engine.dispose()
    # pathlib's parents=True applies mode only to the leaf; keep every newly
    # prepared scratch parent private even under a group-writable local umask.
    for directory in owner.root.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o700)
    manifest = generate(owner, path)
    assert not manifest.payload.generation_diagnostics, manifest.payload.generation_diagnostics
    assert manifest.payload.expected_post_hash
    assert manifest.payload.duplicate_evidence[0].side_asset_accounting == accounting
    gc.collect()
    return manifest, path


def run(owner, manifest, path):
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        return owner.execute(manifest, maintenance_owner=lease)


def prepare_low(owner, *, decisions=True):
    _, _, _, path = prepare_database(owner)
    for source in ('old/menu.mkv', 'old/menu.ass'):
        (owner.root / source).write_bytes(b'LOW content')
        (owner.root / source).chmod(0o600)
    info = (owner.root / 'old/menu.mkv').stat()
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            primary = session.get(Video, 100)
            session.add(Video(id=300, catalog_title_id=primary.catalog_title_id,
                catalog_collection_id=primary.catalog_collection_id,
                relative_path='old/menu.mkv', root_folder='old', filename='menu.mkv',
                size=info.st_size, mtime_ns=info.st_mtime_ns,
                file_type='menu', content_type_manual='menu'))
            subtitle = ExternalSubtitle(id=901, relative_path='old/menu.ass', codec='ass')
            session.add(subtitle)
            session.add(ExternalSubtitleCompatibility(external_subtitle=subtitle,
                video_id=300, status='confirmed_compatible', match_method='manual', verified_at=NOW))
            session.commit()
    finally:
        engine.dispose()
    decision = {('video', 300): ('LOW', 'approved preservation of menu', 'operator')} if decisions else None
    manifest = generate(owner, path, decision)
    assert not manifest.payload.generation_diagnostics, manifest.payload.generation_diagnostics
    assert {a.object_kind for a in manifest.payload.actions if a.criticality == 'LOW'} == {'video', 'subtitle'}
    gc.collect()
    return manifest, path


@pytest.mark.parametrize('accounting', ['COMPLETE', 'INCOMPLETE', 'UNKNOWN'])
def test_mixed_manifest_exact_files_db_prefixes_and_carried_evidence(tmp_path, monkeypatch, accounting):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_mixed(owner, accounting=accounting)
        immutable, original = manifest_to_json(manifest), database_rows(path)
        contents = {a.action_id: (owner.root / a.source).read_bytes() for a in manifest.payload.actions}
        inodes = {a.action_id: (owner.root / a.source).stat().st_ino for a in manifest.payload.actions}
        kinds = {(a.object_kind, a.action) for a in manifest.payload.actions}
        assert {('video', 'MOVE'), ('video', 'KEEP'), ('subtitle', 'MOVE'),
            ('subtitle', 'KEEP'), ('confirmed_no_match', 'KEEP' if accounting == 'UNKNOWN' else 'MOVE'), ('subtitle_archive', 'MOVE'),
            ('subtitle_archive', 'KEEP'), ('auxiliary', 'MOVE'), ('auxiliary', 'KEEP'),
            ('keep_root', 'KEEP')} <= kinds
        if accounting != 'UNKNOWN':
            assert {('video', 'QUARANTINE'), ('subtitle', 'QUARANTINE'),
                ('duplicate_side_asset', 'QUARANTINE')} <= kinds
        else:
            assert ('duplicate_side_asset', 'KEEP') in kinds
        committed, hashes = [], []
        verify, hashed = database._ScratchSequenceDatabase._verify_stored, database.post_state_hash_from_manifest

        def stored(db, index):
            verify(db, index)
            keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:index + 1]}
            keys |= {('unmatched_subtitle', a.object_id) for a in manifest.payload.actions[:index + 1]
                if a.object_kind == 'confirmed_no_match'}
            patches = tuple(p for p in manifest.payload.locator_patches
                if (p.object_kind, p.object_id) in keys or index == len(manifest.payload.actions) - 1)
            assert database_rows(path) == expected_rows(original, patches)
            committed.append(index)

        def post(*args, **kwargs):
            assert owner._active_index == len(manifest.payload.actions) - 1
            assert set(owner._verifications) == set(range(len(manifest.payload.actions)))
            assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
            hashes.append(True)
            return hashed(*args, **kwargs)

        monkeypatch.setattr(database._ScratchSequenceDatabase, '_verify_stored', stored)
        monkeypatch.setattr(database, 'post_state_hash_from_manifest', post)
        outcomes = run(owner, manifest, path)
        assert len(outcomes) == len(manifest.payload.actions)
        assert all(o.outcome == 'COMPLETE' for o in outcomes)
        assert set(committed) == set(range(len(outcomes))) and hashes
        assert manifest_to_json(manifest) == immutable
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
        for index, action in enumerate(manifest.payload.actions):
            target = owner.root / action.target
            assert target.read_bytes() == contents[action.action_id]
            assert target.stat().st_ino == inodes[action.action_id]
            assert (owner.root / action.source).exists() == (action.action == 'KEEP')
            assert (outcomes[index].action_id, outcomes[index].action_index,
                outcomes[index].object_kind, outcomes[index].object_id) == (
                action.action_id, index, action.object_kind, action.object_id)
        assert {p.relative_to(owner.root).as_posix() for p in owner.root.rglob('*') if p.is_file()} == {
            a.target for a in manifest.payload.actions}
        entries = journal.load_journal(owner.journal_path, manifest).entries
        assert [(e.action_index, e.state) for e in entries] == [
            (i, state) for i in range(len(outcomes)) for state in STATES]
        with readonly_planner_session(path) as session:
            context = load_planner_context(session)
        snapshot = inventory_filesystem(owner.root)
        assert post_state_hash_from_manifest(manifest, context, snapshot) == manifest.payload.expected_post_hash
        settled = verify_post_state(context, snapshot,
            execution_evidence=duplicate_execution_evidence(manifest), windows_root=r'C:\Anime')
        assert all(r.action == 'KEEP' for r in settled.records)
        duplicate = next(r.duplicate for r in settled.records if r.object_kind == 'video' and r.object_id == 101)
        assert duplicate.side_asset_accounting == accounting
        assert duplicate.purge_state == 'PRE_EXECUTION_REQUIRED'
        assert any(s.provenance == 'exclusive_secondary_directory_and_identical_primary_archive'
            for s in manifest.payload.duplicate_evidence[0].known_side_assets)
        unprovenanced = verify_post_state(context, snapshot, windows_root=r'C:\Anime')
        assert next(r.duplicate for r in unprovenanced.records
            if r.object_kind == 'video' and r.object_id == 101).side_asset_accounting == 'UNKNOWN'
        assert any(r.status == 'REVIEW' for r in unprovenanced.records)
        assert_no_case_descriptors(owner)


@pytest.mark.parametrize('fault', ['accounting', 'provenance', 'omitted', 'side_path'])
def test_changed_duplicate_evidence_rejected_before_directory_evidence(tmp_path, monkeypatch, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_mixed(owner)
        evidence, = manifest.payload.duplicate_evidence
        if fault == 'accounting':
            changed = (replace(evidence, side_asset_accounting='UNKNOWN'),)
        elif fault == 'provenance':
            changed = (replace(evidence, provenance=('invented_from_quarantine_path',)),)
        elif fault == 'omitted':
            changed = ()
        else:
            side, *rest = evidence.known_side_assets
            changed = (replace(evidence, known_side_assets=(replace(side, source='Fake/side.ass'), *rest)),)
        invalid = seal(manifest, duplicate_evidence=changed)
        original, snapshot = database_rows(path), inventory_filesystem(owner.root)
        writes = []

        def first_write(*args):
            writes.append(True)
            raise OSError('directory evidence must not begin')

        monkeypatch.setattr(directories._DirectoryPreparation, 'prepare', first_write)
        with pytest.raises(stops.ScratchCoordinationStopped) as caught:
            run(owner, invalid, path)
        assert not writes
        assert caught.value.phase == 'DB_VALIDATION'
        assert 'duplicate evidence' in str(caught.value)
        assert not owner.directory_evidence_path.exists() and not owner.journal_path.exists()
        assert database_rows(path) == original and inventory_filesystem(owner.root) == snapshot


@pytest.mark.parametrize('decisions', [False, True])
def test_low_video_and_inherited_subtitle_complete_without_skips(tmp_path, decisions):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_low(owner, decisions=decisions)
        original = database_rows(path)
        outcomes = run(owner, manifest, path)
        assert len(outcomes) == len(manifest.payload.actions)
        entries = journal.load_journal(owner.journal_path, manifest).entries
        assert [(e.action_index, e.state) for e in entries] == [
            (i, state) for i in range(len(outcomes)) for state in STATES]
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
        for action in manifest.payload.actions:
            assert (owner.root / action.target).is_file()
        assert_no_case_descriptors(owner)


@pytest.mark.parametrize('fault', ['rename_before', 'rename_after', 'fsync', 'db_commit', 'journal'])
def test_middle_low_failure_preserves_complete_prefix_and_stops(tmp_path, monkeypatch, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_low(owner)
        failed_index = next(i for i, a in enumerate(manifest.payload.actions) if a.criticality == 'LOW')
        assert 0 < failed_index < len(manifest.payload.actions) - 1
        original = database_rows(path)
        attempted, applied = [], []
        rename, apply = fs._rename_noreplace, database._ScratchSequenceDatabase._apply
        commit, append, fsync = database._ScratchSequenceDatabase._commit, journal.DurableJournal.append, os.fsync

        def renamed(*args, **kwargs):
            index = owner._active_index
            attempted.append(index)
            if index == failed_index and fault == 'rename_before':
                raise OSError(errno.EIO, 'injected LOW rename failure')
            result = rename(*args, **kwargs)
            if index == failed_index and fault == 'rename_after':
                raise OSError(errno.EIO, 'uncertain LOW rename acknowledgement')
            return result

        def synced(fd):
            if owner._active_index == failed_index and fault == 'fsync' and attempted[-1:] == [failed_index]:
                raise OSError(errno.EIO, 'injected LOW post-rename fsync failure')
            return fsync(fd)

        def updated(db, index):
            applied.append(index)
            return apply(db, index)

        def committed(db, index):
            if index == failed_index and fault == 'db_commit':
                commit(db, index)
                raise OSError(errno.EIO, 'uncertain LOW DB commit acknowledgement')
            return commit(db, index)

        def appended(writer, entry):
            if entry.action_index == failed_index and fault == 'journal' and entry.state == 'COMPLETE':
                append(writer, entry)
                raise OSError(errno.EIO, 'uncertain LOW COMPLETE acknowledgement')
            return append(writer, entry)

        monkeypatch.setattr(fs, '_rename_noreplace', renamed)
        monkeypatch.setattr(os, 'fsync', synced)
        monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', updated)
        monkeypatch.setattr(database._ScratchSequenceDatabase, '_commit', committed)
        monkeypatch.setattr(journal.DurableJournal, 'append', appended)
        with pytest.raises(stops.ScratchCoordinationStopped) as caught:
            run(owner, manifest, path)
        assert caught.value.action_index == failed_index
        assert attempted and max(attempted) == failed_index and max(applied) <= failed_index
        assert not any(i > failed_index for i in owner._verifications)
        moved = fault != 'rename_before'
        committed_prefix = failed_index + int(fault in {'db_commit', 'journal'})
        keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:committed_prefix]}
        patches = tuple(p for p in manifest.payload.locator_patches if (p.object_kind, p.object_id) in keys)
        assert database_rows(path) == expected_rows(original, patches)
        entries = journal.load_journal(owner.journal_path, manifest).entries
        assert [(e.action_index, e.state) for e in entries if e.action_index < failed_index] == [
            (i, state) for i in range(failed_index) for state in STATES]
        assert all(e.action_index <= failed_index for e in entries)
        failed = manifest.payload.actions[failed_index]
        assert (owner.root / failed.target).exists() == moved
        assert (owner.root / failed.source).exists() != moved
        for action in manifest.payload.actions[failed_index + 1:]:
            assert (owner.root / action.source).is_file()
        assert_no_case_descriptors(owner)


def changed(record, **changes):
    action = replace(record, **changes)
    return replace(action, action_id=digest((action.object_kind, action.object_id,
        action.action, action.source, action.target)))


@pytest.mark.parametrize('fault', [
    'downgrade', 'upgrade_without_authority', 'reason', 'omitted_keep', 'omitted_patch',
    'unknown_kind', 'unresolved_kind', 'auxiliary_quarantine', 'review', 'dependency',
    'target_collision', 'new_unknown', 'unresolved_subtitle', 'suspected_duplicate', 'quarantine_chain',
])
def test_unapproved_or_incomplete_manifest_has_no_execution_effect(tmp_path, monkeypatch, fault):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_low(owner, decisions=False)
        actions = list(manifest.payload.actions)
        index = next(i for i, a in enumerate(actions) if a.object_kind == 'video' and a.object_id == 100)
        if fault == 'downgrade':
            actions[index] = replace(actions[index], criticality='LOW', failure_policy='CONTINUE_IF_SAFE')
        elif fault in {'upgrade_without_authority', 'reason'}:
            low = next(i for i, a in enumerate(actions) if a.object_kind == 'video' and a.criticality == 'LOW')
            changes = {'criticality': 'PRIMARY', 'failure_policy': 'STOP'} if fault.startswith('upgrade') else {
                'criticality_reason': 'unapproved alternative reason'}
            actions[low] = replace(actions[low], **changes)
        elif fault == 'omitted_keep':
            actions = [a for a in actions if not (a.object_kind == 'subtitle' and a.action == 'KEEP')]
        elif fault == 'omitted_patch':
            manifest = seal(manifest, locator_patches=manifest.payload.locator_patches[1:])
        elif fault in {'unknown_kind', 'unresolved_kind', 'auxiliary_quarantine'}:
            kind = {'unknown_kind': 'unknown', 'unresolved_kind': 'unresolved_subtitle',
                'auxiliary_quarantine': 'auxiliary'}[fault]
            object_id = actions[index].object_id if fault == 'unresolved_kind' else actions[index].source
            actions[index] = changed(actions[index], object_kind=kind, object_id=object_id,
                action='QUARANTINE' if fault == 'auxiliary_quarantine' else 'MOVE')
        elif fault == 'review':
            actions[index] = replace(actions[index], criticality='REVIEW', failure_policy='REVIEW')
        elif fault == 'dependency':
            actions[0] = replace(actions[0], depends_on=(actions[-1].action_id,))
        elif fault == 'quarantine_chain':
            other = next(a for a in actions if a.object_kind == 'video' and a.object_id == 300)
            actions[index] = changed(actions[index], action='QUARANTINE', target=other.source)
        else:
            if fault == 'target_collision':
                (owner.root / actions[index].target).write_bytes(b'occupied destination')
            elif fault == 'new_unknown':
                (owner.root / 'unknown.mkv').write_bytes(b'unknown without authority')
            else:
                with closing(sqlite3.connect(path)) as connection, connection:
                    if fault == 'suspected_duplicate':
                        connection.execute("UPDATE videos SET duplicate_status_manual='suspected' WHERE id=100")
                    else:
                        (owner.root / 'old/unresolved.ass').write_bytes(b'unresolved subtitle')
                        connection.execute("INSERT INTO unresolved_external_subtitles "
                            "(id,relative_path,filename,extension,status,language,created_at,updated_at) VALUES "
                            "(999,'old/unresolved.ass','unresolved.ass','.ass','unresolved','unknown',"
                            "'2026-10-19','2026-10-19')")
            manifest = generate(owner, path)
            assert manifest.payload.generation_diagnostics
            actions = list(manifest.payload.actions)
        invalid = manifest_from_json(manifest_to_json(seal(manifest, tuple(actions))))
        gc.collect()  # Finish setup's legacy fingerprint connection before measuring execution.
        original, snapshot = database_rows(path), inventory_filesystem(owner.root)
        effects = []

        def forbidden(*args, **kwargs):
            effects.append(True)
            raise AssertionError('execution authority must not be reached')

        monkeypatch.setattr(directories._DirectoryPreparation, 'prepare', forbidden)
        monkeypatch.setattr(fs, '_rename_noreplace', forbidden)
        monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', forbidden)
        with pytest.raises(stops.ScratchCoordinationStopped) as caught:
            run(owner, invalid, path)
        if fault == 'quarantine_chain':
            assert 'occupied-source chains' in str(caught.value)
        assert effects == []
        assert database_rows(path) == original and inventory_filesystem(owner.root) == snapshot
        assert not owner.directory_evidence_path.exists() and not owner.journal_path.exists()
        assert not any(p.exists() for p in owner.evidence_paths)
        assert_no_case_descriptors(owner)


def test_uncertain_middle_quarantine_preserves_mixed_prefix_and_all_files(tmp_path, monkeypatch):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_mixed(owner)
        failed_index = next(i for i, a in enumerate(manifest.payload.actions)
            if a.object_kind == 'duplicate_side_asset')
        assert 0 < failed_index < len(manifest.payload.actions) - 1
        original = database_rows(path)
        contents = {a.action_id: (owner.root / a.source).read_bytes() for a in manifest.payload.actions}
        attempted, applied = [], []
        rename, apply = fs._rename_noreplace, database._ScratchSequenceDatabase._apply

        def renamed(*args, **kwargs):
            attempted.append(owner._active_index)
            result = rename(*args, **kwargs)
            if owner._active_index == failed_index:
                raise OSError(errno.EIO, 'uncertain quarantine rename acknowledgement')
            return result

        def updated(db, index):
            applied.append(index)
            return apply(db, index)

        monkeypatch.setattr(fs, '_rename_noreplace', renamed)
        monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', updated)
        with pytest.raises(stops.ScratchFilesystemReconciliationRequired) as caught:
            run(owner, manifest, path)
        assert caught.value.action_index == failed_index and caught.value.reconciliation_required
        assert max(attempted) == failed_index and applied == list(range(failed_index))
        keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:failed_index]}
        patches = tuple(p for p in manifest.payload.locator_patches if (p.object_kind, p.object_id) in keys)
        assert database_rows(path) == expected_rows(original, patches)
        entries = journal.load_journal(owner.journal_path, manifest).entries
        assert [(e.action_index, e.state) for e in entries] == [
            (i, state) for i in range(failed_index) for state in STATES] + [(failed_index, 'PENDING')]
        for index, action in enumerate(manifest.payload.actions):
            locator = action.target if index <= failed_index else action.source
            assert (owner.root / locator).read_bytes() == contents[action.action_id]
        assert len([p for p in owner.root.rglob('*') if p.is_file()]) == len(manifest.payload.actions)
        assert_no_case_descriptors(owner)
