from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

import BatSpectroGen
from BatSpectroGen import (
    discover_wav_files,
    process_batch,
    recording_start,
    settings_for_profile,
)


def _write_chirp(path: Path, duration: float = 0.35, sample_rate: int = 96000) -> None:
    times = np.arange(int(duration * sample_rate), dtype=np.float64) / sample_rate
    phase = 2 * np.pi * (18000 * times + (26000 - 18000) / (2 * duration) * times**2)
    samples = (0.4 * np.sin(phase)).astype(np.float32)
    sf.write(path, samples, sample_rate, subtype="PCM_16")


def test_recording_start_from_filename(tmp_path: Path) -> None:
    path = tmp_path / "20260331_190400.WAV"
    path.touch()
    timestamp, source = recording_start(path)
    assert timestamp.isoformat() == "2026-03-31T19:04:00"
    assert source == "filename"


def test_discovery_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "a.WAV").touch()
    (tmp_path / "b.wav").touch()
    (tmp_path / "ignore.mp3").touch()
    assert [path.name for path in discover_wav_files(tmp_path)] == ["a.WAV", "b.wav"]


def test_batch_keeps_final_partial_segment_without_metadata_files(tmp_path: Path) -> None:
    input_folder = tmp_path / "audio"
    output_folder = tmp_path / "output"
    input_folder.mkdir()
    source = input_folder / "20260331_190400.WAV"
    _write_chirp(source)
    settings = settings_for_profile(
        "low-memory",
        segment_duration=0.2,
        image_width=800,
        image_height=450,
        generate_power_spectrum=True,
    )

    result = process_batch(input_folder, output_folder, settings)

    assert result.completed_files == 1
    assert result.failed_files == 0
    images = sorted((output_folder / source.stem).glob("*.jpg"))
    assert len(images) == 3
    spectrograms = [path for path in images if not path.name.startswith("average_power")]
    assert len(spectrograms) == 2
    with Image.open(spectrograms[0]) as image:
        assert image.size == (800, 450)
    assert result.files[0].segments == 2
    assert not (output_folder / "spectrogram_manifest.csv").exists()
    assert not (output_folder / "run_summary.json").exists()


def test_nonempty_output_is_rejected(tmp_path: Path) -> None:
    input_folder = tmp_path / "audio"
    output_folder = tmp_path / "output"
    input_folder.mkdir()
    output_folder.mkdir()
    _write_chirp(input_folder / "sample.wav", duration=0.05)
    (output_folder / "existing.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(ValueError, match="not empty"):
        process_batch(input_folder, output_folder, settings_for_profile("low-memory"))
    assert (output_folder / "existing.txt").read_text(encoding="utf-8") == "keep me"


def test_two_worker_batch(tmp_path: Path) -> None:
    input_folder = tmp_path / "audio"
    output_folder = tmp_path / "output"
    input_folder.mkdir()
    _write_chirp(input_folder / "first.wav", duration=0.05)
    _write_chirp(input_folder / "second.WAV", duration=0.05)
    settings = settings_for_profile(
        "low-memory",
        workers=2,
        segment_duration=0.1,
        image_width=800,
        image_height=450,
    )

    result = process_batch(input_folder, output_folder, settings)

    assert result.completed_files == 2
    assert result.failed_files == 0
    assert len(list(output_folder.rglob("*.jpg"))) == 2


@pytest.mark.parametrize(
    ("apply_range", "expected_limits"),
    [(False, (0.0, 48.0)), (True, (10.0, 30.0))],
)
def test_power_spectrum_display_range_is_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    apply_range: bool,
    expected_limits: tuple[float, float],
) -> None:
    input_folder = tmp_path / "audio"
    output_folder = tmp_path / "output"
    input_folder.mkdir()
    _write_chirp(input_folder / "sample.wav", duration=0.05)
    captured: list[tuple[float, float]] = []

    def capture_plot(
        _power,
        _sample_rate,
        _source_name,
        lower_khz,
        upper_khz,
        destination,
        _settings,
    ) -> None:
        captured.append((lower_khz, upper_khz))
        destination.touch()

    monkeypatch.setattr(BatSpectroGen, "_plot_average_power", capture_plot)
    settings = settings_for_profile(
        "low-memory",
        segment_duration=0.1,
        image_width=800,
        image_height=450,
        generate_power_spectrum=True,
        fmin_khz=10,
        fmax_khz=30,
        apply_display_range_to_power=apply_range,
    )

    result = process_batch(input_folder, output_folder, settings)

    assert result.completed_files == 1
    assert captured == [expected_limits]
