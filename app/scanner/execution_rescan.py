"""Closed-world rescan of a manifest's persisted planner/execution projection.

The read-only verifier never assigns an owner or updates a locator. The locator
check below also supports the ordinary scanner after explicit guard completion.
"""
from pathlib import Path

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.target_execution_manifest import (
    ExecutionManifest, manifest_to_json, post_state_hash_from_manifest,
)
from app.target_planner_filesystem import inventory_filesystem
from app.target_planner_service import load_planner_context


def verify_scanning_locators(session: Session, manifest: ExecutionManifest) -> None:
    """Check ID-keyed committed locators before the ordinary scanner can write.

    Admission comes from the explicit completed guard, not from this comparison.
    Extra DB/filesystem objects are allowed; lost or reverted known IDs are not.
    """
    from sqlalchemy import select
    from app.models import (Video, CatalogTitle, CatalogCollection,
        ExternalSubtitle, UnresolvedExternalSubtitle)
    models = {'video': Video, 'title': CatalogTitle, 'collection': CatalogCollection,
        'subtitle': ExternalSubtitle, 'unmatched_subtitle': UnresolvedExternalSubtitle}
    # Core rows leave ORM relationship loader state untouched for the scanner.
    rows = {kind: {row.id: row._mapping for row in session.execute(select(model.__table__))}
        for kind, model in models.items()}
    for patch in manifest.payload.locator_patches:
        if rows[patch.object_kind].get(patch.object_id, {}).get(patch.field) != patch.after:
            raise ValueError('completed execution locator missing or stale')
    for action in manifest.payload.actions:
        kind = ('unmatched_subtitle' if action.object_kind in
            {'confirmed_no_match', 'unresolved_subtitle'} else action.object_kind)
        if kind in {'video', 'subtitle', 'unmatched_subtitle'}:
            if rows[kind].get(action.object_id, {}).get('relative_path') != action.target:
                raise ValueError('completed execution object ID/target differs')


def require_unstarted_execution_session(session: Session) -> None:
    """Refuse caller-owned transactions without reading or ending them.

    A flushed ORM change or Core UPDATE need not appear in Session.dirty.
    Even SELECT autobegin is refused; the caller must finish it explicitly.
    """
    if session.in_transaction():
        raise ValueError('execution rescan requires a session without an open transaction')
    bind = session.get_bind()
    if isinstance(bind, Connection) and bind.in_transaction():
        raise ValueError('execution rescan cannot adopt a connection transaction')


def verify_execution_rescan(session: Session, root: Path, manifest: ExecutionManifest) -> int:
    """Return known video count for the checked approved planner/execution projection.

    Stat parity is a freshness check after ID/locator admission, never identity
    matching. The manifest and carried evidence remain the only routing evidence.
    """
    require_unstarted_execution_session(session)
    if session.new or session.dirty or session.deleted:
        raise ValueError('execution rescan requires a clean session')
    if type(manifest) is not ExecutionManifest:
        raise ValueError('immutable execution manifest required')
    manifest_to_json(manifest)  # Validate the immutable envelope, including seal.
    payload = manifest.payload
    if payload.version != 2 or payload.generation_diagnostics or not payload.expected_post_hash:
        raise ValueError('execution manifest has no admissible complete post-state')
    if str(root.absolute()) != payload.library_root:
        raise ValueError('execution library root mismatch')
    expected_warnings = {(w.warning_id, w.warning_class, w.object_kind, w.object_id) for w in payload.warnings}
    approved_warnings = {(w.warning_id, w.warning_class, w.object_kind, w.object_id) for w in manifest.acknowledgements}
    if expected_warnings != approved_warnings:
        raise ValueError('execution warnings require exact acknowledgement')

    # A clean session can still cache pre-patch values across transactions.
    # Admission must observe persisted locators, never that historical cache.
    session.expire_all()
    context = load_planner_context(session)
    baselines = {(b.object_kind, b.object_id, b.field): b.value for b in context.locator_baselines}
    patches = {(p.object_kind, p.object_id, p.field): p for p in payload.locator_patches}
    for patch in payload.locator_patches:
        if baselines.get((patch.object_kind, patch.object_id, patch.field)) != patch.after:
            raise ValueError('execution locator patch missing or stale')

    known = {
        **{('video', row.id): row.source for row in context.videos},
        **{('subtitle', row.id): row.source for row in context.subtitles},
        **{('confirmed_no_match' if row.match_status == 'confirmed_no_match' else 'unresolved_subtitle', row.id): row.source
           for row in context.unmatched},
    }
    actions = {}
    for action in payload.actions:
        if action.object_kind not in {'video', 'subtitle', 'confirmed_no_match', 'unresolved_subtitle'}:
            continue
        key = (action.object_kind, action.object_id)
        if type(action.object_id) is not int or key in actions:
            raise ValueError('ambiguous execution object identity')
        locator_kind = 'unmatched_subtitle' if action.object_kind in {'confirmed_no_match', 'unresolved_subtitle'} else action.object_kind
        patch = patches.get((locator_kind, action.object_id, 'relative_path'))
        if action.action != 'KEEP':
            if patch is None or (patch.before, patch.after) != (action.source, action.target):
                raise ValueError('execution action lacks its exact locator patch')
        elif patch is not None:
            raise ValueError('KEEP cannot change a file locator')
        actions[key] = action.target
    if actions != known:
        raise ValueError('execution object IDs or target locators do not match the database')
    snapshot = inventory_filesystem(root)
    entries = {entry.relative_path: entry for entry in snapshot.entries}
    for action in payload.actions:
        entry = entries.get(action.target)
        if (entry is None or entry.kind != 'regular' or entry.size != action.expected_size
                or entry.mtime_ns != action.expected_mtime_ns):
            raise ValueError('execution target absent or stale')
        if action.action != 'KEEP' and action.source in entries:
            raise ValueError('execution source still present')
    if post_state_hash_from_manifest(manifest, context, snapshot) != payload.expected_post_hash:
        raise ValueError('execution post-state changed; incremental routing requires review')
    return len(context.videos)
