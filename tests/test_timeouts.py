import ast
import json
from pathlib import Path
import subprocess
import time

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Video
from app.probe import MediaInfoError, ProbeError, probe_mediainfo, probe_video
from app.scanner import LibraryUnavailableError, check_library_access, scan_library


PROBE_PAYLOAD = json.dumps({"format": {"duration": "60"}, "streams": [{
    "index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
}]})


def completed(command):
    return subprocess.CompletedProcess(command, 0, stdout=PROBE_PAYLOAD, stderr="")


def test_ffprobe_timeout_is_caught_and_timeout_is_explicit(monkeypatch, tmp_path):
    captured = {}
    def timeout(command, **kwargs):
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr("app.probe.subprocess.run", timeout)
    with pytest.raises(ProbeError, match="překročil timeout"):
        probe_video(tmp_path / "video.mkv", timeout_seconds=7)
    assert captured["timeout"] == 7
    assert captured["check"] is False


def test_mediainfo_timeout_is_caught(monkeypatch, tmp_path):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr("app.probe.subprocess.run", timeout)
    with pytest.raises(MediaInfoError, match="MediaInfo překročil timeout"):
        probe_mediainfo(tmp_path / "video.mkv", timeout_seconds=9)


def test_ffprobe_timeout_does_not_stop_scan_and_old_record_survives(monkeypatch, tmp_path):
    show = tmp_path / "Show"; show.mkdir()
    first = show / "01.mkv"; second = show / "02.mkv"
    first.write_bytes(b"old-one"); second.write_bytes(b"old-two")
    monkeypatch.setattr("app.probe.subprocess.run", lambda command, **kwargs: completed(command))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine); sessions = sessionmaker(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
        old_size = session.scalar(select(Video.size).where(Video.filename == "01.mkv"))
        first.write_bytes(b"changed-one"); second.write_bytes(b"changed-two")
        def one_timeout(command, **kwargs):
            if command[-1].endswith("01.mkv"):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return completed(command)
        monkeypatch.setattr("app.probe.subprocess.run", one_timeout)
        result = scan_library(session, tmp_path, ffprobe_timeout_seconds=3)
        assert result.errors == 1
        assert session.scalar(select(Video.size).where(Video.filename == "01.mkv")) == old_size
        assert session.scalar(select(Video.size).where(Video.filename == "02.mkv")) == len(b"changed-two")
    engine.dispose()


def test_library_loss_mid_scan_rolls_back_and_removed_stays_zero(monkeypatch, tmp_path):
    show = tmp_path / "Show"; show.mkdir()
    first = show / "01.mkv"; second = show / "02.mkv"
    first.write_bytes(b"one"); second.write_bytes(b"two")
    monkeypatch.setattr("app.scanner.service.probe_video", lambda path, **kwargs: json.loads(PROBE_PAYLOAD) and {
        "duration": 60.0, "video_codec": "h264", "width": 1920, "height": 1080,
        "audio": [], "subtitles": [],
    })
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine); sessions = sessionmaker(engine)
    with sessions() as session:
        scan_library(session, tmp_path)
        before = [(video.relative_path, video.size) for video in session.scalars(select(Video).order_by(Video.id))]
        first.write_bytes(b"changed")
        calls = 0
        def disappears(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise LibraryUnavailableError("Knihovna přestala odpovídat")
        monkeypatch.setattr("app.scanner.service.check_library_access", disappears)
        with pytest.raises(LibraryUnavailableError) as error:
            scan_library(session, tmp_path, library_healthcheck_interval_files=1)
        assert error.value.result.removed == 0
        after = [(video.relative_path, video.size) for video in session.scalars(select(Video).order_by(Video.id))]
        assert after == before
    engine.dispose()


def test_healthcheck_timeout_interrupts_check(monkeypatch, tmp_path):
    def blocked(root, require_mount, sender):
        time.sleep(5)
    monkeypatch.setattr("app.scanner.service._library_healthcheck_worker", blocked)
    started = time.monotonic()
    with pytest.raises(LibraryUnavailableError, match="timeout"):
        check_library_access(tmp_path, timeout_seconds=0.05)
    assert time.monotonic() - started < 2


def test_every_subprocess_run_has_explicit_timeout():
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run" and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                assert any(keyword.arg == "timeout" and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None) for keyword in node.keywords), f"subprocess bez timeoutu: {path}:{node.lineno}"


def test_every_http_request_and_client_has_non_null_timeout():
    paths = [Path("app/metadata/providers/anilist.py"), Path("app/metadata/artwork.py")]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            is_client = name == "Client"
            is_request = name in {"post", "stream"}
            if is_client or is_request:
                timeout = next((keyword.value for keyword in node.keywords if keyword.arg == "timeout"), None)
                assert timeout is not None, f"HTTP volání bez timeoutu: {path}:{node.lineno}"
                assert not (isinstance(timeout, ast.Constant) and timeout.value is None), f"HTTP timeout=None: {path}:{node.lineno}"
