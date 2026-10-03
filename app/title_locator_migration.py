"""Atomic SQLite title-locator and grouping-authority schema upgrade."""
from __future__ import annotations

import json
import re

from sqlalchemy import inspect, text

from .models import GroupingDecisionTitle


def _locator_unique_indexes(connection) -> set[str]:
    inspector = inspect(connection)
    return {
        index['name'] for index in inspector.get_indexes('catalog_titles')
        if index['unique'] and index['column_names'] == ['relative_root_path']
    }


def _reconstruct_titles(connection) -> None:
    """Copy every old column verbatim; change only locator uniqueness.

    Retain the actual old CREATE SQL, rather than projecting it onto today's ORM:
    extra historical columns, defaults, FKs and unrelated constraints survive.
    SQLite's safe create/copy/drop/rename order keeps incoming FK names intact.
    Explicit indexes and triggers are replayed from their original SQL.
    """
    inspector = inspect(connection)
    locator_constraints = [c for c in inspector.get_unique_constraints('catalog_titles')
                           if c['column_names'] == ['relative_root_path']]
    locator_indexes = _locator_unique_indexes(connection)
    if not locator_constraints and not locator_indexes:
        if not any(i['column_names'] == ['relative_root_path'] and not i['unique']
                   for i in inspector.get_indexes('catalog_titles')):
            connection.exec_driver_sql(
                'CREATE INDEX ix_catalog_titles_relative_root_path '
                'ON catalog_titles(relative_root_path)'
            )
        return
    objects = connection.execute(text(
        "SELECT name, sql FROM sqlite_schema WHERE tbl_name='catalog_titles' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
    )).all()
    source = connection.scalar(text("SELECT sql FROM sqlite_schema WHERE name='catalog_titles'"))
    replacement = _without_locator_unique(source, len(locator_constraints))
    sequence = None
    if connection.scalar(text("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'")):
        sequence = connection.scalar(text("SELECT seq FROM sqlite_sequence WHERE name='catalog_titles'"))
    connection.exec_driver_sql(replacement)
    quote = connection.dialect.identifier_preparer.quote
    columns = ', '.join(quote(row[1]) for row in connection.exec_driver_sql('PRAGMA table_info(catalog_titles)'))
    connection.exec_driver_sql(
        f'INSERT INTO catalog_titles_locator_migration ({columns}) '
        f'SELECT {columns} FROM catalog_titles'
    )
    connection.exec_driver_sql('DROP TABLE catalog_titles')
    connection.exec_driver_sql('ALTER TABLE catalog_titles_locator_migration RENAME TO catalog_titles')
    if sequence is not None:
        connection.exec_driver_sql("UPDATE sqlite_sequence SET seq=? WHERE name='catalog_titles'", (sequence,))
    for name, sql in objects:
        if name not in locator_indexes:
            connection.exec_driver_sql(sql)
    connection.exec_driver_sql(
        'CREATE INDEX IF NOT EXISTS ix_catalog_titles_relative_root_path '
        'ON catalog_titles(relative_root_path)'
    )


def _without_locator_unique(source: str, expected: int) -> str:
    """Remove only a project-conventional table-level locator UNIQUE clause.

    Split top-level definitions, respecting quoted strings/identifiers and
    nested CHECK expressions. Unsupported inline uniqueness fails closed.
    All remaining SQL (including AUTOINCREMENT and table options) stays intact.
    """
    start = source.index('(')
    depth, quote, begin, definitions = 1, None, start + 1, []
    i = begin
    while i < len(source):
        char = source[i]
        if quote:
            if char == quote:
                if i + 1 < len(source) and source[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in "'\"`[":
            quote = ']' if char == '[' else char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                definitions.append(source[begin:i])
                break
        elif char == ',' and depth == 1:
            definitions.append(source[begin:i])
            begin = i + 1
        i += 1
    if depth != 0 or quote:
        raise RuntimeError('Unsupported catalog_titles CREATE statement.')
    identifier = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)'
    locator = r'(?:"relative_root_path"|`relative_root_path`|\[relative_root_path\]|relative_root_path)'
    unique = re.compile(
        rf'\s*(?:CONSTRAINT\s+{identifier}\s+)?UNIQUE\s*\(\s*{locator}\s*\)'
        r'(?:\s+ON\s+CONFLICT\s+\w+)?\s*', re.IGNORECASE,
    )
    retained = [definition for definition in definitions if not unique.fullmatch(definition)]
    if len(definitions) - len(retained) != expected:
        raise RuntimeError('Unsupported inline title locator UNIQUE constraint; migration aborted.')
    return 'CREATE TABLE catalog_titles_locator_migration (' + ','.join(retained) + source[i:]


def _backfill_grouping_owners(connection) -> None:
    """Translate exact unique legacy references once; missing stays history."""
    columns = {c['name'] for c in inspect(connection).get_columns('collection_grouping_decisions')}
    if 'target_collection_id' in columns:
        return
    connection.exec_driver_sql(
        'ALTER TABLE collection_grouping_decisions ADD COLUMN target_collection_id '
        'INTEGER NULL REFERENCES catalog_collections(id) ON DELETE SET NULL'
    )
    connection.exec_driver_sql(
        'CREATE INDEX ix_collection_grouping_decisions_target_collection_id '
        'ON collection_grouping_decisions(target_collection_id)'
    )
    GroupingDecisionTitle.__table__.create(connection, checkfirst=True)
    titles = {}
    for owner_id, path in connection.exec_driver_sql('SELECT id, relative_root_path FROM catalog_titles'):
        titles.setdefault(path, []).append(owner_id)
    collections = {}
    for owner_id, path in connection.exec_driver_sql('SELECT id, relative_root_path FROM catalog_collections'):
        collections.setdefault(path, []).append(owner_id)
    decisions = connection.exec_driver_sql(
        'SELECT id, decision, target_collection_path, selected_title_paths_json '
        'FROM collection_grouping_decisions ORDER BY id'
    ).all()
    for decision_id, kind, target_path, paths_json in decisions:
        if kind != 'merged':
            continue
        targets = collections.get(target_path, [])
        if len(targets) > 1:
            raise RuntimeError(f'Ambiguous grouping target in decision {decision_id}.')
        if targets:
            connection.exec_driver_sql(
                'UPDATE collection_grouping_decisions SET target_collection_id=? WHERE id=?',
                (targets[0], decision_id),
            )
        try:
            paths = json.loads(paths_json or '[]')
        except (TypeError, ValueError):
            paths = []
        # Match the v8 resolver's complete-list validation. Invalid snapshots
        # remain verbatim evidence and never generate partial authority.
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            continue
        for path in dict.fromkeys(paths):
            owners = titles.get(path, [])
            if len(owners) > 1:
                raise RuntimeError(f'Ambiguous grouping title locator in decision {decision_id}: {path}')
            connection.exec_driver_sql(
                'INSERT INTO grouping_decision_titles '
                '(decision_id,catalog_title_id,title_path_snapshot) VALUES (?,?,?)',
                (decision_id, owners[0] if owners else None, path),
            )


def _check_sqlite_integrity(connection) -> None:
    violations = connection.exec_driver_sql('PRAGMA foreign_key_check').all()
    integrity = connection.exec_driver_sql('PRAGMA integrity_check').all()
    if violations or integrity != [('ok',)]:
        raise RuntimeError(f'Title locator migration integrity failure: FK={violations[:3]}, integrity={integrity[:3]}')


def migrate_title_locator_persistence(engine, *, mark_version: bool = False) -> None:
    """One transaction includes reconstruction, backfill, checks and v9 marker.

    FK enforcement is disabled only on this connection, outside the transaction
    (SQLite ignores toggles within BEGIN). Checks run before committing. Finally
    restores enforcement even on failure; ordinary fresh connections use the
    project's foreign_keys=ON hook.
    """
    if engine.dialect.name != 'sqlite':
        raise RuntimeError('Title locator persistence migration requires SQLite.')
    with engine.connect() as connection:
        # Complete any SQLAlchemy transaction before toggling the DBAPI PRAGMA.
        dbapi = connection.connection.dbapi_connection
        dbapi.execute('PRAGMA foreign_keys=OFF')
        try:
            dbapi.execute('BEGIN IMMEDIATE')
            _check_sqlite_integrity(connection)
            _reconstruct_titles(connection)
            _backfill_grouping_owners(connection)
            _check_sqlite_integrity(connection)
            if mark_version:
                connection.exec_driver_sql('PRAGMA user_version=9')
            connection.commit()
        except BaseException:
            connection.rollback()
            dbapi.rollback()
            raise
        finally:
            dbapi.execute('PRAGMA foreign_keys=ON')
