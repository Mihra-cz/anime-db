"""One fresh scratch action: journal -> rename -> independent observation.

No production entry point, existing-root/writer adoption, retry or DB lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading

from .target_execution_filesystem import (
    ScratchRenameSession, ScratchVerification, RenameResult,
    RenameRejected, RenameReconciliationRequired,
)
from .target_execution_journal import DurableJournal, ReconciliationRequired
from .target_execution_manifest import ExecutionManifest, ManifestAction, JournalEntry


class ScratchCoordinationStopped(ValueError):
    """Known safe rejection/STOP; never permission to continue or retry."""
    reconciliation_required = False

    def __init__(self, message: str, phase: str):
        super().__init__(message)
        self.phase = phase


class ScratchFilesystemReconciliationRequired(ScratchCoordinationStopped):
    """Physical outcome or independent evidence is ambiguous; STOP."""
    reconciliation_required = True


class ScratchJournalReconciliationRequired(ScratchCoordinationStopped):
    """Journal acknowledgement/integrity failed; preserve bytes and files; STOP."""
    reconciliation_required = True


@dataclass(frozen=True)
class VerifiedScratchOutcome:
    manifest_id: str
    action_id: str
    action_index: int
    journal_path: Path
    verification: ScratchVerification
    outcome: str = 'FS_VERIFIED'


class ScratchActionSession:
    """Factory-only new root and adjacent journal; execute once, then close.

    Caller prepares isolated assets and a synthetic immutable single-action
    manifest. Exposes no existing root/writer argument or restart API.
    """

    def __init__(self):
        raise TypeError('use ScratchActionSession.create for new scratch allocation')

    @classmethod
    def create(cls, parent: Path) -> ScratchActionSession:
        filesystem = ScratchRenameSession.create(parent)
        session = object.__new__(cls)
        session._filesystem = filesystem
        session._journal_path = filesystem.root.parent / (filesystem.root.name + '.journal.jsonl')
        session._mutex = threading.Lock()
        session._pid = os.getpid()
        session._spent = session._closed = False
        session._stop_class = ScratchCoordinationStopped
        session._stop_phase = 'VALIDATION'
        return session

    @property
    def root(self) -> Path:
        return self._filesystem.root

    @property
    def mount_identity(self) -> str:
        return self._filesystem.mount_identity

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    def execute(self, manifest: ExecutionManifest, action: ManifestAction, *,
                action_index: int) -> VerifiedScratchOutcome:
        if os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: inherited scratch coordination session', 'VALIDATION')
        with self._mutex:
            if self._spent or self._closed:
                raise self._stop_class('STOP: session spent/closed; no retry or resume', self._stop_phase)
            self._spent = True
            writer = None
            primary = None
            phase = 'VALIDATION'
            try:
                if (type(manifest) is not ExecutionManifest or type(action) is not ManifestAction
                        or type(action_index) is not int or action_index != 0
                        or len(manifest.payload.actions) != 1 or action != manifest.payload.actions[0]
                        or action.action not in {'MOVE', 'QUARANTINE'}):
                    raise ScratchCoordinationStopped('STOP: exactly one sealed MOVE/QUARANTINE at index 0 required', phase)
                with self._filesystem.observe(manifest, action) as observation:
                    phase = 'JOURNAL_CREATE'
                    try:
                        writer = DurableJournal.create(self.journal_path, manifest)
                        if writer.inspect_live(manifest) != ():
                            raise ReconciliationRequired('STOP: fresh journal required')
                    except Exception as exc:
                        raise ScratchJournalReconciliationRequired('STOP: journal creation/binding failed', phase) from exc
                    history: list[JournalEntry] = []

                    def append(state):
                        entry = JournalEntry(manifest.manifest_id, action.action_id, action_index, state)
                        try:
                            writer.append(entry)
                            history.append(entry)
                            if writer.inspect_live(manifest) != tuple(history):
                                raise ReconciliationRequired('STOP: unexpected live journal history')
                        except Exception as exc:
                            raise ScratchJournalReconciliationRequired('STOP: journal durability/integrity failed', state) from exc

                    phase = 'PENDING'
                    append('PENDING')
                    phase = 'RENAME'
                    result = self._filesystem.rename(manifest, action, observation=observation)
                    if (type(result) is not RenameResult or result.manifest_id != manifest.manifest_id
                            or result.action_id != action.action_id or result.outcome != 'DURABLE'
                            or result.syscall_succeeded is not True or result.durability_confirmed is not True):
                        raise ScratchFilesystemReconciliationRequired('STOP: rename durability unconfirmed', phase)
                    phase = 'FS_APPLIED'
                    append('FS_APPLIED')
                    phase = 'VERIFICATION'
                    verification = observation.verify()
                    if (type(verification) is not ScratchVerification
                            or verification.manifest_id != manifest.manifest_id
                            or verification.action_id != action.action_id):
                        raise ScratchFilesystemReconciliationRequired('STOP: independent evidence binding mismatch', phase)
                    phase = 'FS_VERIFIED'
                    append('FS_VERIFIED')
                    return VerifiedScratchOutcome(manifest.manifest_id, action.action_id, action_index,
                        self.journal_path, verification)
            except (KeyboardInterrupt, SystemExit) as exc:
                primary = exc
                if phase in {'JOURNAL_CREATE', 'PENDING', 'FS_APPLIED', 'FS_VERIFIED'}:
                    self._stop_class = ScratchJournalReconciliationRequired
                elif phase in {'RENAME', 'VERIFICATION'}:
                    self._stop_class = ScratchFilesystemReconciliationRequired
                self._stop_phase = phase
                exc.add_note(f'E2B STOP: phase={phase}; session spent; no retry/resume; journal={self.journal_path}')
                raise
            except Exception as exc:
                if isinstance(exc, ScratchCoordinationStopped):
                    error = exc
                elif (isinstance(exc, RenameReconciliationRequired)
                        or phase in {'VERIFICATION', 'FS_APPLIED', 'FS_VERIFIED'}
                        or (phase == 'RENAME' and not isinstance(exc, RenameRejected))):
                    error = ScratchFilesystemReconciliationRequired('STOP: filesystem evidence unconfirmed', phase)
                else:
                    error = ScratchCoordinationStopped('STOP: scratch validation/rename rejected', phase)
                self._stop_class, self._stop_phase = type(error), error.phase
                primary = error
                if error is exc:
                    raise
                raise error from exc
            except BaseException as exc:
                primary = exc
                self._stop_class, self._stop_phase = ScratchFilesystemReconciliationRequired, phase
                raise
            finally:
                self._finish(writer, primary)

    def _finish(self, writer, primary: BaseException | None) -> None:
        self._closed = True
        failures = []
        for resource, category in ((writer, ScratchJournalReconciliationRequired),
                (self._filesystem, ScratchFilesystemReconciliationRequired)):
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as exc:
                failures.append((exc, category))
        if not failures:
            return
        # Closing is never retried: a failed close may already have released fd.
        interruption = next((failure for failure in failures
            if isinstance(failure[0], (KeyboardInterrupt, SystemExit))), None)
        if primary is not None and (isinstance(primary, (KeyboardInterrupt, SystemExit)) or interruption is None):
            for exc, _ in failures:
                notes = tuple(getattr(exc, '__notes__', ()))
                primary.add_note(f'E2B STOP: cleanup failed: {type(exc).__name__}: {exc}')
                for note in notes:
                    primary.add_note(note)
            return
        exc, category = interruption or failures[0]
        if primary is None:
            # A primary STOP keeps its category/phase; cleanup adds no new authority.
            self._stop_class, self._stop_phase = category, 'CLOSE'
        for other, _ in failures:
            if other is not exc:
                exc.add_note(f'E2B cleanup failed: {type(other).__name__}: {other}')
                for note in tuple(getattr(other, '__notes__', ())):
                    exc.add_note(note)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            exc.add_note('E2B STOP: interrupted cleanup; session spent; no retry/resume')
            if primary is not None:
                raise exc from primary
            raise exc
        raise self._stop_class('STOP: scratch resource cleanup failed', 'CLOSE') from exc

    def close(self) -> None:
        if os.getpid() != self._pid:
            self._closed = True
            self._filesystem.close()
            return
        with self._mutex:
            if not self._closed:
                self._closed = True
                self._filesystem.close()

    def __enter__(self) -> ScratchActionSession:
        if self._closed or os.getpid() != self._pid:
            raise ScratchCoordinationStopped('STOP: scratch coordination session unavailable', 'VALIDATION')
        return self

    def __exit__(self, _type, primary, _traceback) -> None:
        try:
            self.close()
        except BaseException as exc:
            if primary is None:
                raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not isinstance(primary, (KeyboardInterrupt, SystemExit)):
                raise exc from primary
            notes = tuple(getattr(exc, '__notes__', ()))
            primary.add_note(f'E2B STOP: cleanup failed: {type(exc).__name__}: {exc}')
            for note in notes:
                primary.add_note(note)
