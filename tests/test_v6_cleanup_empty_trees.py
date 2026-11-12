"""Post-E7 empty subtrees use original evidence, with no recursive file deletion."""
import pytest

from app import target_execution_cleanup as cleanup
from app.target_execution_sequence import ScratchManifestExecution
from test_v6_execution_cleanup import prepare, run, records
from test_v6_execution_file_actions import generate


@pytest.mark.parametrize('with_import', [False, True])
def test_all_unneeded_empty_trees_removed_and_retained_evidence_reopens(tmp_path, with_import):
    with ScratchManifestExecution.create(tmp_path) as owner:
        _, database = prepare(owner)
        for path in ('simple', 'temp/beta s1/deep/leaf', 'Duplicates/empty', 'Show/Season 01/unused'):
            (owner.root/path).mkdir(parents=True, exist_ok=True, mode=0o700)
        if with_import:
            (owner.root/'IMPORT/empty').mkdir(parents=True, mode=0o700)
        for directory in owner.root.rglob('*'):
            if directory.is_dir(): directory.chmod(0o700)
        manifest = generate(owner, database)
        def decide(proposal):
            paths = {item.path for item in proposal.candidates}
            assert {'simple', 'temp', 'temp/beta s1', 'temp/beta s1/deep',
                'temp/beta s1/deep/leaf', 'empty', 'empty/unapproved', 'Subs', 'Subs/empty'} <= paths
            assert not paths & {'.', 'Duplicates', 'Duplicates/empty', 'IMPORT', 'IMPORT/empty'}
            assert not paths.intersection(manifest.payload.directories)
            return cleanup.approve_cleanup(proposal, tuple(sorted(paths)), actor='operator', reason='complete proposal')
        run(owner, manifest, database, decide)
        assert not any((owner.root/path).exists() for path in ('simple', 'temp', 'empty', 'Subs'))
        assert (owner.root/'Duplicates/empty').is_dir()
        assert (owner.root/'IMPORT').exists() is with_import
        assert all((owner.root/path).is_dir() for path in manifest.payload.directories)
        assert cleanup.read_completed_cleanup(database, manifest, owner.root) == owner.cleanup_outcome
        removed = owner.cleanup_outcome.removed
        assert removed.index('temp/beta s1/deep/leaf') < removed.index('temp/beta s1/deep') < removed.index('temp/beta s1') < removed.index('temp')
        assert records(owner)[-1]['state'] == 'COMPLETE'


def test_post_state_candidates_preserve_files_required_empty_paths_and_unknown_subtrees(tmp_path):
    # A regular file is never cleanup authority, regardless of whether it has a DB row.
    root = tmp_path
    for path in ('empty/a/b', 'required/spare', 'unknown/empty', 'partial/empty', 'Subs/unused', 'Duplicates/empty', 'IMPORT/empty'):
        (root/path).mkdir(parents=True)
    (root/'unknown/unrecognized.bin').write_bytes(b'unknown contents')
    snapshot = [cleanup._entry('.', 'directory', root.stat())]
    for path in sorted(root.rglob('*')):
        snapshot.append(cleanup._entry(path.relative_to(root).as_posix(), 'directory' if path.is_dir() else 'regular', path.stat()))
    from app.target_planner_coverage import DirectoryCoverage
    review = tuple(DirectoryCoverage(e.path, 'REVIEW', True, False, e.path=='required',
        e.path!='partial', ()) for e in snapshot if e.kind=='directory')
    candidates = cleanup._cleanup_candidates(tuple(snapshot), review, ('required', 'Subs'))
    paths = {row.path for row in candidates}
    assert paths == {'empty', 'empty/a', 'empty/a/b', 'required/spare', 'unknown/empty', 'partial/empty', 'Subs/unused'}
    assert (root/'unknown/unrecognized.bin').read_bytes() == b'unknown contents'


def test_retaining_child_rejects_parent_before_any_rmdir(tmp_path):
    with ScratchManifestExecution.create(tmp_path) as owner:
        manifest, database = prepare(owner)
        def decide(proposal):
            with pytest.raises(ValueError, match='retained/unknown'):
                cleanup.approve_cleanup(proposal, ('empty',), actor='operator', reason='retained leaf')
            return cleanup.approve_cleanup(proposal, ('old/copy',), actor='operator', reason='independent leaf')
        run(owner, manifest, database, decide)
        assert (owner.root/'empty/unapproved').is_dir() and (owner.root/'empty').is_dir()
