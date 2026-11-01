"""E7 checks exact locator writes without retaining or scanning every prefix."""
from collections import Counter
import gc
import json
import os
from pathlib import Path
import sqlite3
import time
import tracemalloc

import pytest

from app import target_execution_database as database
from app.maintenance import ScratchMaintenance
from app.target_execution_coordination import ScratchDatabaseReconciliationRequired
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_projection import expected_rows, fresh_manifest
from test_v6_execution_sequence import prepare_sequence
from v6_database_case import database_rows


def test_sequence_full_database_work_is_bounded_across_actions(tmp_path, monkeypatch):
    """A full row/schema/hash scan at each commit makes this regression fail."""
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner, count=12)
        before = database_rows(path)
        counts = Counter()
        rows, schema, hashed = database._rows, database._schema, database.identity._hash_file

        def all_rows(connection):
            counts['rows'] += 1
            return rows(connection)

        def all_schema(connection):
            counts['schema'] += 1
            return schema(connection)

        def full_hash(fd):
            import os
            if os.path.samefile(f'/proc/self/fd/{fd}', path):
                counts['hash'] += 1
            return hashed(fd)

        monkeypatch.setattr(database, '_rows', all_rows)
        monkeypatch.setattr(database, '_schema', all_schema)
        monkeypatch.setattr(database.identity, '_hash_file', full_hash)
        gc.collect()
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            outcomes = owner.execute(manifest, maintenance_owner=lease)
        assert all(outcome.outcome == 'COMPLETE' for outcome in outcomes)
        assert database_rows(path) == expected_rows(before, manifest.payload.locator_patches)
        assert counts['rows'] <= 16, counts
        assert counts['schema'] <= 16, counts
        assert counts['hash'] <= 4, counts


@pytest.mark.parametrize('count', [int(value) for value in os.environ.get('V6_DB_PROJECTION_COUNTS', '96').split(',')])
def test_sequence_projection_memory_grows_with_rows_and_patches(tmp_path, monkeypatch, count):
    """Retaining a complete database projection per action exceeds this budget."""
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner, count=count)
        validate = database._ScratchSequenceDatabase._validate_complete_plan
        opened = database._ScratchSequenceDatabase.__init__
        measured = []
        duration = []

        def validated(db):
            validate(db)
            duration.append(time.perf_counter())
            tracemalloc.start()

        def constructed(db, *args, **kwargs):
            try:
                opened(db, *args, **kwargs)
                measured.append(tracemalloc.get_traced_memory()[1])
                duration.append(time.perf_counter())
            finally:
                tracemalloc.stop()
                db.close()
            raise RuntimeError('projection measurement completed before filesystem execution')

        monkeypatch.setattr(database._ScratchSequenceDatabase, '_validate_complete_plan', validated)
        monkeypatch.setattr(database._ScratchSequenceDatabase, '__init__', constructed)
        gc.collect()
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            from app.target_execution_coordination import ScratchCoordinationStopped
            with pytest.raises(ScratchCoordinationStopped, match='projection measurement completed'):
                owner.execute(manifest, maintenance_owner=lease)
        assert len(measured) == 1
        result = dict(actions=len(manifest.payload.actions), projection_peak_bytes=measured[0],
            projection_seconds=duration[-1]-duration[0])
        destination = os.environ.get('V6_DB_PROJECTION_METRICS')
        if destination:
            Path(destination).write_text(json.dumps(result, indent=2)+'\n')
        assert measured[0] < 400_000 + 12_000 * len(manifest.payload.actions), measured


@pytest.mark.parametrize('sql', [
    "UPDATE videos SET filename='forbidden' WHERE id=200",
    'UPDATE videos SET filename=filename WHERE id=200',
    'CREATE TABLE forbidden(id INTEGER)',
    'PRAGMA user_version=999',
])
def test_sequence_rejects_non_locator_sql_before_it_can_commit(tmp_path, monkeypatch, sql):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, path = prepare_sequence(owner)
        before = database_rows(path)
        apply = database._apply_locator_updates

        def tampered(connection, updates):
            apply(connection, updates)
            connection.execute(sql)

        monkeypatch.setattr(database, '_apply_locator_updates', tampered)
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            with pytest.raises(ScratchDatabaseReconciliationRequired):
                owner.execute(manifest, maintenance_owner=lease)
        assert database_rows(path) == before
        with sqlite3.connect(path) as connection:
            assert not connection.execute("SELECT name FROM sqlite_master WHERE name='forbidden'").fetchall()
            assert connection.execute('PRAGMA user_version').fetchone()[0] != 999


def test_sequence_rejects_trigger_write_to_another_approved_locator(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, path = prepare_sequence(owner)
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TRIGGER locator_effect AFTER UPDATE OF relative_path ON videos '
                "BEGIN UPDATE videos SET relative_path=relative_path WHERE id=200; END")
        manifest = fresh_manifest(owner, path)
        before = database_rows(path)
        with ScratchMaintenance(path, owner.root).maintenance() as lease:
            with pytest.raises(ScratchDatabaseReconciliationRequired):
                owner.execute(manifest, maintenance_owner=lease)
        assert database_rows(path) == before
