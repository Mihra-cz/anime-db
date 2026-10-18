"""E7D locator-only DB preparation; unsupported physical actions stay isolated."""
from dataclasses import replace
import gc
import sqlite3

import pytest
from sqlalchemy.orm import Session

from app import target_execution_database as database
from app.database import make_engine
from app.maintenance import ScratchMaintenance
from app.models import CatalogCollection, CatalogTitle, ExternalSubtitle, UnresolvedExternalSubtitle, Video
from app.target_execution_manifest import LocatorPatch, build_execution_manifest, manifest_to_json
from app.target_execution_sequence import ScratchManifestExecution
from app.target_planner_filesystem import inventory_filesystem, plan_library
from app.target_planner_service import load_planner_context, readonly_planner_session
from app.tools.target_plan import database_fingerprint
from test_v6_execution_filesystem import seal
from test_v6_execution_sequence import prepare_sequence
from v6_database_case import database_rows


TABLES = {'video': Video.__table__, 'subtitle': ExternalSubtitle.__table__,
    'unmatched_subtitle': UnresolvedExternalSubtitle.__table__,
    'collection': CatalogCollection.__table__, 'title': CatalogTitle.__table__}


def expected_rows(original, patches):
    result = dict(original)
    for kind, table in TABLES.items():
        names = list(table.columns.keys())
        rows = []
        for original_row in original[table.name]:
            row = list(original_row)
            for patch in patches:
                if patch.object_kind == kind and patch.object_id == row[names.index('id')]:
                    assert original_row[names.index(patch.field)] == patch.before
                    row[names.index(patch.field)] = patch.after
            rows.append(tuple(row))
        result[table.name] = tuple(rows)
    return result


def fresh_manifest(owner, path, windows_root=None):
    with readonly_planner_session(path) as session:
        context = load_planner_context(session)
    snapshot = inventory_filesystem(owner.root)
    return build_execution_manifest(context, snapshot, plan_library(context, snapshot, windows_root=windows_root),
        database_fingerprint(path), mount_identity=owner.mount_identity, windows_root=windows_root,
        created_at='2026-10-18T12:00:00Z')


@pytest.fixture
def container_case(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, path = prepare_sequence(owner)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE catalog_collections SET relative_root_path='legacy'")
            connection.execute("UPDATE catalog_titles SET relative_root_path='legacy/season'")
        manifest = fresh_manifest(owner, path)
        assert not manifest.payload.generation_diagnostics
        assert {'collection', 'title'} <= {p.object_kind for p in manifest.payload.locator_patches}
        yield owner, manifest, path


@pytest.mark.parametrize('last_keep', [False, True])
def test_containers_commit_with_last_action_only_after_all_physical_verification(container_case, monkeypatch, last_keep):
    owner, manifest, path = container_case
    if last_keep:
        (owner.root / 'seznam-souboru.txt').write_bytes(b'preserved inventory')
        (owner.root / 'seznam-souboru.txt').chmod(0o600)
        manifest = fresh_manifest(owner, path)
        assert manifest.payload.actions[-1].action == 'KEEP'
    original = database_rows(path)
    apply, hashed = database._ScratchSequenceDatabase._apply, database.post_state_hash_from_manifest
    verified, hashes = [], []
    def applied(db, index):
        assert set(owner._verifications) == set(range(index + 1))
        apply(db, index)
    def stored_hash(*args):
        assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)
        hashes.append(True)
        return hashed(*args)
    verify = database._ScratchSequenceDatabase._verify_stored
    def stored(db, index):
        verify(db, index)
        keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:index + 1]}
        patches = tuple(p for p in manifest.payload.locator_patches
            if (p.object_kind, p.object_id) in keys or index == len(manifest.payload.actions) - 1)
        assert database_rows(path) == expected_rows(original, patches)
        verified.append(index)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_apply', applied)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_verify_stored', stored)
    monkeypatch.setattr(database, 'post_state_hash_from_manifest', stored_hash)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        outcomes = owner.execute(manifest, maintenance_owner=lease)
    assert all(outcome.outcome == 'COMPLETE' for outcome in outcomes)
    assert set(verified) == set(range(len(manifest.payload.actions))) and hashes
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


def test_projection_uses_only_sealed_patches_for_each_r0_prefix(container_case):
    _, manifest, path = container_case
    project = getattr(database, '_locator_prefixes', None)
    assert callable(project), 'E7D sealed locator prefix projection is missing'
    original = database_rows(path)
    states, stages, counts = project(original, manifest)
    assert states[0] == original and counts[0] == 0
    assert states[-1] == expected_rows(original, manifest.payload.locator_patches)
    for index, action in enumerate(manifest.payload.actions):
        if index < len(manifest.payload.actions) - 1:
            assert states[index + 1]['catalog_collections'] == original['catalog_collections']
            assert states[index + 1]['catalog_titles'] == original['catalog_titles']
        if action.action == 'KEEP' and index < len(manifest.payload.actions) - 1:
            assert stages[index] == () and counts[index + 1] == counts[index]


@pytest.fixture
def all_locators(container_case):
    owner, _, path = container_case
    from app.models import ExternalSubtitleCompatibility
    from test_physical_naming import NOW
    for source in ('old/source.ass', 'old/no-match.ass', 'old/subtitles.zip'):
        (owner.root / source).write_bytes(b'preserve all content')
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            subtitle = ExternalSubtitle(id=901, relative_path='old/source.ass', codec='ass')
            session.add_all([subtitle, UnresolvedExternalSubtitle(id=902,
                relative_path='old/no-match.ass', filename='historical.ass', extension='.ass',
                status='confirmed_no_match')])
            session.add(ExternalSubtitleCompatibility(external_subtitle=subtitle, video_id=100,
                status='confirmed_compatible', match_method='manual', verified_at=NOW))
            session.commit()
    finally:
        engine.dispose()
    manifest = fresh_manifest(owner, path)
    assert not manifest.payload.generation_diagnostics
    assert {p.object_kind for p in manifest.payload.locator_patches} == set(TABLES)
    return owner, manifest, path


def test_all_locator_kinds_and_filesystem_only_action_have_exact_isolated_transactions(all_locators):
    _, manifest, path = all_locators
    original = database_rows(path)
    project = getattr(database, '_locator_prefixes', None)
    assert callable(project), 'E7D sealed locator prefix projection is missing'
    states, stages, counts = project(original, manifest)
    assert counts[-1] == 7  # Three Videos, two subtitle rows, two containers.
    with sqlite3.connect(path, isolation_level=None) as connection:
        schema = database._schema(connection)
        for index, updates in enumerate(stages):
            connection.execute('BEGIN IMMEDIATE')
            database._apply_locator_updates(connection, updates)
            assert database._rows(connection) == states[index + 1]
            assert database._schema(connection) == schema
            assert connection.total_changes == counts[index + 1]
            connection.commit()
            assert database_rows(path) == states[index + 1]
            if manifest.payload.actions[index].object_kind == 'subtitle_archive':
                assert updates == ()
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


@pytest.mark.parametrize('fault', ['before', 'id', 'bool_id', 'field', 'duplicate', 'collision', 'keep', 'omitted'])
def test_invalid_or_unapproved_patch_is_rejected_before_dml(all_locators, fault):
    _, manifest, path = all_locators
    original = database_rows(path)
    patches = list(manifest.payload.locator_patches)
    patch = next(p for p in patches if p.object_kind == 'video' and p.field == 'relative_path')
    if fault == 'before': replacement = replace(patch, before='wrong/source.mkv')
    elif fault == 'id': replacement = replace(patch, object_id=999999)
    elif fault == 'bool_id': replacement = replace(patch, object_id=True)
    elif fault == 'field': replacement = replace(patch, field='filename')
    elif fault == 'duplicate': patches.append(patch)
    elif fault == 'collision':
        with sqlite3.connect(path) as connection:
            occupied = connection.execute('SELECT relative_path FROM videos WHERE id=200').fetchone()[0]
        replacement = replace(patch, after=occupied)
    elif fault == 'keep':
        kept = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.action == 'KEEP')
        patches.append(LocatorPatch('video', kept.object_id, 'relative_path', kept.source, 'unauthorized.mkv'))
    else:
        patches.remove(patch)
    if fault in {'before', 'id', 'bool_id', 'field', 'collision'}:
        patches[patches.index(patch)] = replacement
    forged = seal(manifest, locator_patches=tuple(patches))
    project = getattr(database, '_locator_prefixes', None)
    assert callable(project), 'E7D sealed locator prefix projection is missing'
    with pytest.raises(ValueError):
        project(original, forged)
    assert database_rows(path) == original


@pytest.mark.parametrize('collision', ['original_occupied', 'projected'])
def test_video_locator_collision_with_matching_sealed_actions_is_rejected(container_case, collision):
    owner, manifest, path = container_case
    from app.target_execution_manifest import _digest
    original = database_rows(path)
    moving = [a for a in manifest.payload.actions if a.action == 'MOVE']
    assert [a.object_id for a in moving[:2]] == [100, 301]
    # In the first case R0's occupant leaves before the second file action;
    # every projected prefix would be unique without the R0 occupancy guard.
    targets = ({301: moving[0].source} if collision == 'original_occupied'
        else {100: 'New/collision.mkv', 301: 'New/collision.mkv'})
    actions = []
    for action in manifest.payload.actions:
        target = targets.get(action.object_id, action.target) if action.object_kind == 'video' else action.target
        actions.append(replace(action, target=target, depends_on=(),
            action_id=_digest((action.object_kind, action.object_id, action.action, action.source, target))))
    patches = []
    for patch in manifest.payload.locator_patches:
        if patch.object_kind == 'video' and patch.object_id in targets:
            after = targets[patch.object_id] if patch.field == 'relative_path' else targets[patch.object_id].split('/')[0]
            if patch.before == after:
                continue
            patch = replace(patch, after=after)
        patches.append(patch)
    forged = seal(manifest, tuple(actions), locator_patches=tuple(patches))
    manifest_to_json(forged)  # Prove rejection is not a malformed seal/action identity.
    message = ('target locator belongs to another original DB row' if collision == 'original_occupied'
        else 'projected DB locator collision')
    with pytest.raises(ValueError, match='^' + message + '$'):
        database._locator_prefixes(original, forged)
    assert database_rows(path) == original
    assert not owner.journal_path.exists()


@pytest.mark.parametrize('kind', ['collection', 'title'])
@pytest.mark.parametrize('fault', ['id', 'field', 'before'])
def test_invalid_container_patch_is_rejected_without_db_changes_or_complete(container_case, kind, fault):
    owner, manifest, path = container_case
    from app.target_execution_coordination import ScratchCoordinationStopped
    original = database_rows(path)
    patch = next(p for p in manifest.payload.locator_patches if p.object_kind == kind)
    if fault == 'id':
        invalid = replace(patch, object_id=999999)
    elif fault == 'field':
        invalid = replace(patch, field='local_title')
    else:
        invalid = replace(patch, before='incorrect/container')
    forged = seal(manifest, locator_patches=tuple(invalid if p == patch else p for p in manifest.payload.locator_patches))
    if fault != 'field':
        manifest_to_json(forged)
    message = 'unapproved locator field' if fault == 'field' else 'locator patch ID/before differs from original R0'
    with pytest.raises(ValueError, match='^' + message + '$'):
        database._locator_prefixes(original, forged)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchCoordinationStopped):
            owner.execute(forged, maintenance_owner=lease)
    assert database_rows(path) == original
    assert not owner.journal_path.exists() and not owner.evidence_paths
    assert all((owner.root / action.source).is_file() for action in manifest.payload.actions)


@pytest.mark.parametrize('collision', ['original_occupied', 'projected'])
def test_collection_locator_collision_is_rejected_without_db_changes_or_complete(container_case, collision):
    owner, manifest, path = container_case
    from app.target_execution_coordination import ScratchCoordinationStopped
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            session.add(CatalogCollection(id=999, local_title='Other', normalized_local_title='other',
                relative_root_path='other-original'))
            session.commit()
    finally:
        engine.dispose()
    original = database_rows(path)
    patch = next(p for p in manifest.payload.locator_patches if p.object_kind == 'collection')
    # Containers share one final projection boundary. In the R0 case the
    # occupant is relocated too, so the projected state itself has no collision.
    first_target, second_target = ('other-original', 'Freed/Other') if collision == 'original_occupied' else ('Merged', 'Merged')
    patches = tuple(replace(p, after=first_target) if p == patch else p for p in manifest.payload.locator_patches)
    patches += (LocatorPatch('collection', 999, 'relative_root_path', 'other-original', second_target),)
    forged = seal(manifest, locator_patches=patches,
        db_fingerprint=type(manifest.payload.db_fingerprint)(**database_fingerprint(path)))
    manifest_to_json(forged)
    message = ('target locator belongs to another original DB row' if collision == 'original_occupied'
        else 'projected DB locator collision')
    with pytest.raises(ValueError, match='^' + message + '$'):
        database._locator_prefixes(original, forged)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchCoordinationStopped):
            owner.execute(forged, maintenance_owner=lease)
    assert database_rows(path) == original
    assert not owner.journal_path.exists() and not owner.evidence_paths


@pytest.mark.parametrize('sharing', ['existing', 'projected'])
def test_title_locators_may_share_an_existing_or_projected_container(container_case, sharing):
    owner, manifest, path = container_case
    patch = next(p for p in manifest.payload.locator_patches if p.object_kind == 'title')
    source = patch.after if sharing == 'existing' else 'other/part'
    engine = make_engine(f'sqlite:///{path}')
    try:
        with Session(engine) as session:
            first = session.get(CatalogTitle, patch.object_id)
            session.add(CatalogTitle(id=999, local_title='Part 2', normalized_local_title='part 2',
                catalog_collection_id=first.catalog_collection_id, relative_root_path=source,
                part_type='part', season_number=1, part_number=2))
            session.commit()
    finally:
        engine.dispose()
    patches = manifest.payload.locator_patches
    if sharing == 'projected':
        patches += (LocatorPatch('title', 999, 'relative_root_path', source, patch.after),)
    approved = seal(manifest, locator_patches=patches,
        db_fingerprint=type(manifest.payload.db_fingerprint)(**database_fingerprint(path)))
    manifest_to_json(approved)
    original = database_rows(path)
    states, _, _ = database._locator_prefixes(original, approved)
    assert states[-1] == expected_rows(original, patches)
    names = list(CatalogTitle.__table__.columns.keys())
    assert [row[names.index('relative_root_path')] for row in states[-1]['catalog_titles']] == [patch.after, patch.after]
    assert database_rows(path) == original and not owner.journal_path.exists()


@pytest.mark.parametrize('applied', [False, True])
def test_uncertain_final_commit_stops_without_retry_or_losing_container_evidence(container_case, monkeypatch, applied):
    owner, manifest, path = container_case
    from app.target_execution_coordination import ScratchDatabaseReconciliationRequired, ScratchCoordinationStopped
    original = database_rows(path)
    final = len(manifest.payload.actions) - 1
    commit = database._ScratchSequenceDatabase._commit
    attempts = []
    def unacknowledged(db, index):
        if index == final:
            attempts.append(index)
            if applied:
                commit(db, index)
            raise OSError('uncertain final commit')
        return commit(db, index)
    monkeypatch.setattr(database._ScratchSequenceDatabase, '_commit', unacknowledged)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchDatabaseReconciliationRequired, match='uncertain final commit'):
            owner.execute(manifest, maintenance_owner=lease)
        with pytest.raises(ScratchCoordinationStopped):
            owner.execute(manifest, maintenance_owner=lease)
    assert attempts == [final]
    from app.target_execution_journal import load_journal
    from test_v6_execution_pair import STATES
    entries = load_journal(owner.journal_path, manifest).entries
    assert [(entry.action_index, entry.state) for entry in entries] == [
        (index, state) for index in range(final) for state in STATES] + [
        (final, state) for state in ('PENDING', 'FS_APPLIED', 'FS_VERIFIED')]
    assert not any(entry.action_index == final and entry.state in {'DB_RECONCILED', 'COMPLETE'} for entry in entries)
    if applied:
        patches = manifest.payload.locator_patches
    else:
        keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:-1]}
        patches = tuple(p for p in manifest.payload.locator_patches if (p.object_kind, p.object_id) in keys)
    assert database_rows(path) == expected_rows(original, patches)
    assert all((owner.root / action.target).is_file() for action in manifest.payload.actions)


@pytest.mark.parametrize('effect', ['abort', 'filename', 'same_value'])
def test_container_trigger_failure_or_extra_write_aborts_the_whole_last_transaction(container_case, effect):
    owner, _, path = container_case
    from app.target_execution_coordination import ScratchDatabaseReconciliationRequired
    bodies = {'abort': "SELECT RAISE(ABORT,'container rejected');",
        'filename': "UPDATE videos SET filename='forbidden' WHERE id=200;",
        'same_value': 'UPDATE videos SET filename=filename WHERE id=200;'}
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TRIGGER container_effect AFTER UPDATE OF relative_root_path ON catalog_collections '
            'BEGIN ' + bodies[effect] + ' END')
    manifest = fresh_manifest(owner, path)
    original = database_rows(path)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchDatabaseReconciliationRequired):
            owner.execute(manifest, maintenance_owner=lease)
    keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:-1]}
    patches = tuple(p for p in manifest.payload.locator_patches if (p.object_kind, p.object_id) in keys)
    assert database_rows(path) == expected_rows(original, patches)


def test_windows_root_is_not_sealed_and_checked_manifest_stops_before_execution(container_case):
    owner, unchecked, path = container_case
    from app.target_execution_coordination import ScratchDatabaseReconciliationRequired
    left = fresh_manifest(owner, path, windows_root='C:\\Anime')
    right = fresh_manifest(owner, path, windows_root='D:\\Anime')
    assert left.payload == right.payload  # Same budgets do not bind the root identity.
    assert left.payload.plan_hash != unchecked.payload.plan_hash
    assert left.payload.expected_post_hash != unchecked.payload.expected_post_hash
    original = database_rows(path)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchDatabaseReconciliationRequired, match='baseline changed'):
            owner.execute(left, maintenance_owner=lease)
    assert database_rows(path) == original and not owner.journal_path.exists()
    assert not owner.evidence_paths


@pytest.mark.parametrize('effect', ['schema', 'user_version'])
def test_unapproved_schema_or_user_version_change_aborts_final_transaction(container_case, monkeypatch, effect):
    owner, manifest, path = container_case
    from app.target_execution_coordination import ScratchDatabaseReconciliationRequired
    apply = database._apply_locator_updates
    original = database_rows(path)
    with sqlite3.connect(path) as connection:
        schema = database._schema(connection)
    def tampered(connection, updates):
        apply(connection, updates)
        if any(p.object_kind == 'collection' for group in updates for p in group):
            connection.execute('CREATE TABLE forbidden(id INTEGER)' if effect == 'schema' else 'PRAGMA user_version=999')
    monkeypatch.setattr(database, '_apply_locator_updates', tampered)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchDatabaseReconciliationRequired, match='unapproved side effect'):
            owner.execute(manifest, maintenance_owner=lease)
    keys = {(a.object_kind, a.object_id) for a in manifest.payload.actions[:-1]}
    patches = tuple(p for p in manifest.payload.locator_patches if (p.object_kind, p.object_id) in keys)
    assert database_rows(path) == expected_rows(original, patches)
    with sqlite3.connect(path) as connection:
        assert database._schema(connection) == schema


def test_external_data_version_change_after_final_commit_is_a_stop(container_case, monkeypatch):
    owner, manifest, path = container_case
    from contextlib import closing
    from app.target_execution_coordination import ScratchDatabaseReconciliationRequired
    connect = sqlite3.connect
    changed = []
    class InterleavedConnection(sqlite3.Connection):
        def commit(self):
            super().commit()
            if owner._active_index == len(manifest.payload.actions) - 1 and not changed:
                # Restore domain rows within the foreign transaction, while
                # still dirtying pages and advancing the observer's data_version.
                with closing(connect(path, timeout=0)) as foreign:
                    filename = foreign.execute('SELECT filename FROM videos WHERE id=200').fetchone()[0]
                    foreign.execute("UPDATE videos SET filename='foreign transient value' WHERE id=200")
                    foreign.execute('UPDATE videos SET filename=? WHERE id=200', (filename,))
                    foreign.commit()
                changed.append(True)
    def connected(*args, **kwargs):
        if '?mode=rw' in str(args[0]):
            kwargs['factory'] = InterleavedConnection
        return connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connected)
    original = database_rows(path)
    gc.collect()
    with ScratchMaintenance(path, owner.root).maintenance() as lease:
        with pytest.raises(ScratchDatabaseReconciliationRequired, match='acknowledged prefix commit'):
            owner.execute(manifest, maintenance_owner=lease)
    assert changed == [True]
    assert database_rows(path) == expected_rows(original, manifest.payload.locator_patches)


def test_future_video_quarantine_projects_its_exact_sealed_fields_without_physical_authority(all_locators):
    _, manifest, path = all_locators
    from app.target_execution_manifest import _digest
    action = next(a for a in manifest.payload.actions if a.object_kind == 'video' and a.action == 'MOVE')
    target = 'Duplicates/secondary.mkv'
    moved = replace(action, action='QUARANTINE', target=target,
        action_id=_digest((action.object_kind, action.object_id, 'QUARANTINE', action.source, target)))
    patches = tuple(replace(p, after=target if p.field == 'relative_path' else 'Duplicates')
        if p.object_kind == 'video' and p.object_id == action.object_id else p
        for p in manifest.payload.locator_patches)
    manifest = seal(manifest, tuple(moved if a == action else a for a in manifest.payload.actions), locator_patches=patches)
    project = getattr(database, '_locator_prefixes', None)
    assert callable(project), 'E7D sealed locator prefix projection is missing'
    original = database_rows(path)
    states, _, _ = project(original, manifest)
    assert states[-1] == expected_rows(original, patches)
    assert database_rows(path) == original
