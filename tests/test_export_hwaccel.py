"""Hardware-encoder command construction for exports.

VAAPI encoding needs the frames on the GPU: a global ``-vaapi_device`` plus
a ``format=nv12,hwupload`` tail on the filter chain. Without them ffmpeg
fails with ``Invalid argument`` the moment any filter (scale/setsar/PiP)
is in the chain — which is every export. videotoolbox/nvenc accept software
frames directly, so they get neither.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from web.db import Database
from web.routers.exports import _resolve_default_encoder
from web.services import exporter
from web.services.exporter import ExportWorker

# --- helpers ---

def _state(prefs_encoder, available):
    snap = SimpleNamespace(export_encoder_pref=prefs_encoder)
    return SimpleNamespace(
        settings_provider=SimpleNamespace(get=lambda: snap),
        export_encoders=available,
    )


def test_auto_prefers_qsv_over_vaapi():
    st = _state("auto", {"qsv": True, "vaapi": True, "software": True})
    assert _resolve_default_encoder(st) == "qsv"


def test_auto_falls_back_to_vaapi_when_no_qsv():
    st = _state("auto", {"qsv": False, "vaapi": True, "software": True})
    assert _resolve_default_encoder(st) == "vaapi"


def test_hw_init_args_only_for_vaapi():
    assert exporter._hw_init_args("vaapi") == ["-vaapi_device", "/dev/dri/renderD128"]
    assert exporter._hw_init_args("software") == []
    assert exporter._hw_init_args("videotoolbox") == []
    assert exporter._hw_init_args("nvenc") == []


def test_hw_init_args_qsv_creates_device():
    assert exporter._hw_init_args("qsv") == [
        "-init_hw_device", "qsv=hw", "-filter_hw_device", "hw",
    ]


def test_hw_init_args_vaapi_unchanged():
    assert exporter._hw_init_args("vaapi") == [
        "-vaapi_device", "/dev/dri/renderD128",
    ]


def test_hw_decode_args_only_for_qsv():
    assert exporter._hw_decode_args("qsv") == [
        "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
    ]
    assert exporter._hw_decode_args("vaapi") == []
    assert exporter._hw_decode_args("software") == []
    assert exporter._hw_decode_args("videotoolbox") == []


def test_hw_upload_filter_only_for_vaapi():
    assert exporter._hw_upload_filter("vaapi") == "format=nv12,hwupload"
    assert exporter._hw_upload_filter("software") == ""
    assert exporter._hw_upload_filter("nvenc") == ""


# --- timeline export wiring (the reported failure) ---

@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "t.db"))


async def _noop(_event):  # broadcast stub
    pass


def _insert_clip(db: Database, path: str, ts: int, dur: float = 60.0) -> None:
    with db.write() as c:
        c.execute(
            "INSERT INTO clip_index "
            "(path, basename, group_name, timestamp, camera, sequence, "
            " event_type, has_gpx, gps_examined, scanned_at, duration_s) "
            "VALUES (?, ?, '2026-01-01', ?, 'F', 1, 'normal', 0, 0, 0, ?)",
            (path, path.split("/")[-1], ts, dur),
        )


async def _capture_timeline(db, tmp_path, monkeypatch, encoder):
    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_noop)
    base = 1_000_000
    _insert_clip(db, str(tmp_path / "f.mp4"), base, 60.0)

    captured: list[list[str]] = []

    async def fake_ffmpeg(job_id, args, total, **kw):
        captured.append(list(args))
        Path(args[-1]).write_bytes(b"\0")
        return 0, ""

    async def fake_res(_path):
        return (1920, 1080)

    monkeypatch.setattr(worker, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(worker, "_probe_resolution", fake_res)

    segments = [{"channel": "front", "start_ts": base + 10, "end_ts": base + 30}]
    await worker._run_timeline({"id": 1}, segments, encoder, str(tmp_path / "out.mp4"))

    # the per-segment encode is the call carrying the scale filter
    return next(a for a in captured
                if "-vf" in a and "scale" in a[a.index("-vf") + 1])


async def test_timeline_vaapi_adds_device_and_hwupload(db, tmp_path, monkeypatch):
    seg = await _capture_timeline(db, tmp_path, monkeypatch, "vaapi")
    assert seg[seg.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert seg.index("-vaapi_device") < seg.index("-i")          # global, before input
    assert seg[seg.index("-vf") + 1] == "scale=1920:1080,setsar=1,format=nv12,hwupload"
    assert "h264_vaapi" in seg


async def test_timeline_software_has_no_hw_args(db, tmp_path, monkeypatch):
    seg = await _capture_timeline(db, tmp_path, monkeypatch, "software")
    assert "-vaapi_device" not in seg
    assert seg[seg.index("-vf") + 1] == "scale=1920:1080,setsar=1"
    assert "libx264" in seg


def test_video_codec_args_qsv_uses_icq():
    args = exporter.video_codec_args("qsv")
    assert args == [
        "-c:v", "h264_qsv", "-global_quality", "23", "-look_ahead", "0",
    ]


def test_video_codec_args_vaapi_unchanged():
    # Regression guard: VAAPI path must not drift.
    assert exporter.video_codec_args("vaapi") == [
        "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", "24",
    ]


def test_scale_filter_dialects():
    # software/vaapi keep the exact legacy string (regression guard)
    assert exporter._scale_filter(1920, 1080, "software") == "scale=1920:1080,setsar=1"
    assert exporter._scale_filter(1920, 1080, "vaapi") == "scale=1920:1080,setsar=1"
    # qsv uses the VPP scaler and drops setsar (set by the encoder)
    assert exporter._scale_filter(1920, 1080, "qsv") == "scale_qsv=w=1920:h=1080"


async def test_timeline_qsv_uses_gpu_chain(db, tmp_path, monkeypatch):
    seg = await _capture_timeline(db, tmp_path, monkeypatch, "qsv")
    # device init present, before input
    assert seg[seg.index("-init_hw_device") + 1] == "qsv=hw"
    # per-input decode flags present, before -i
    assert "-hwaccel" in seg and seg[seg.index("-hwaccel") + 1] == "qsv"
    assert seg.index("-hwaccel") < seg.index("-i")
    # qsv scaler, NO setsar, NO hwupload
    vf = seg[seg.index("-vf") + 1]
    assert vf == "scale_qsv=w=1920:h=1080"
    assert "setsar" not in vf and "hwupload" not in vf
    assert "h264_qsv" in seg


def test_pip_filter_complex_software_unchanged():
    fc = exporter._pip_filter_complex("top_right", main="front")
    assert fc == (
        "[1:v]scale=iw/4:ih/4[pip];"
        "[0:v][pip]overlay=W-w-20:20"
    )


def test_pip_filter_complex_qsv_uses_vpp():
    fc = exporter._pip_filter_complex("top_right", main="front", encoder="qsv")
    assert fc == (
        "[1:v]scale_qsv=w=iw/4:h=ih/4[pip];"
        "[0:v][pip]overlay_qsv=x=W-w-20:y=20"
    )


def test_qsv_probe_command_exercises_mfx(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        class R:  # noqa: D401 - tiny stub
            returncode = 0
        return R()

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)
    monkeypatch.setattr(exporter.shutil, "which", lambda _x: "/usr/local/bin/ffmpeg")

    assert exporter._test_encoder_sync("qsv") is True
    cmd = captured["cmd"]
    # device init + qsv filter + qsv encoder all present
    assert "-init_hw_device" in cmd and "qsv=hw" in cmd
    assert any("scale_qsv" in c for c in cmd)
    assert "h264_qsv" in cmd


async def test_finish_ok_schedules_export_preview(db, monkeypatch):
    import asyncio

    from web.services import export_preview

    calls = []

    async def fake_ensure(recordings, job_id, output_path, duration_s):
        calls.append((job_id, output_path))
        return None

    monkeypatch.setattr(export_preview, "ensure_export_preview", fake_ensure)

    # A job row to update.
    with db.write() as c:
        c.execute(
            "INSERT INTO export_jobs (id, type, clip_ids, state, created_at) "
            "VALUES (5, 'join_front', '[1]', 'running', 0)"
        )

    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_noop)
    worker._finish(5, True, None, "/recordings/.exports/5.mp4")
    await asyncio.sleep(0)  # let the scheduled task run
    assert calls == [(5, "/recordings/.exports/5.mp4")]


async def test_finish_failure_does_not_schedule_preview(db, monkeypatch):
    import asyncio

    from web.services import export_preview

    calls = []

    async def fake_ensure(recordings, job_id, output_path, duration_s):
        calls.append(job_id)
        return None

    monkeypatch.setattr(export_preview, "ensure_export_preview", fake_ensure)
    with db.write() as c:
        c.execute(
            "INSERT INTO export_jobs (id, type, clip_ids, state, created_at) "
            "VALUES (6, 'join_front', '[1]', 'running', 0)"
        )
    worker = ExportWorker(db=db, provider=MagicMock(), broadcast=_noop)
    worker._finish(6, False, "boom", None)
    await asyncio.sleep(0)
    assert calls == []
