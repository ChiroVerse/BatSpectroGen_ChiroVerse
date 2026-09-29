"""BatSpectroGen desktop application and audio-processing workflow."""

from __future__ import annotations

import math
import multiprocessing
import queue
import re
import threading
from concurrent.futures import CancelledError, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import soundfile as sf


__version__ = "1.1.2"


@dataclass(frozen=True)
class ProcessingSettings:
    """Settings that affect generated images and processing cost."""

    profile: str = "balanced"
    segment_duration: float = 5.0
    n_fft: int = 2048
    hop_length: int = 512
    image_width: int = 1920
    image_height: int = 1080
    dpi: int = 120
    stft_chunk_frames: int = 128
    colormap: str = "magma"
    dynamic_range_db: float = 80.0
    fmin_khz: float | None = None
    fmax_khz: float | None = None
    generate_power_spectrum: bool = True
    apply_display_range_to_power: bool = False
    workers: int = 1
    recursive: bool = False

    def validate(self) -> None:
        if not 0.05 <= self.segment_duration <= 600:
            raise ValueError("Segment duration must be between 0.05 and 600 seconds.")
        if self.n_fft < 256 or self.n_fft & (self.n_fft - 1):
            raise ValueError("FFT size must be a power of two and at least 256.")
        if not 1 <= self.hop_length <= self.n_fft:
            raise ValueError("Hop length must be between 1 and the FFT size.")
        if self.image_width < 800 or self.image_height < 450:
            raise ValueError("Image dimensions must be at least 800 x 450 pixels.")
        if self.dpi < 72:
            raise ValueError("Image DPI must be at least 72.")
        if self.stft_chunk_frames < 8:
            raise ValueError("STFT chunk size must be at least 8 frames.")
        if not 20 <= self.dynamic_range_db <= 160:
            raise ValueError("Dynamic range must be between 20 and 160 dB.")
        if self.fmin_khz is not None and self.fmin_khz < 0:
            raise ValueError("Minimum display frequency cannot be negative.")
        if self.fmax_khz is not None and self.fmax_khz <= 0:
            raise ValueError("Maximum display frequency must be positive.")
        if (
            self.fmin_khz is not None
            and self.fmax_khz is not None
            and self.fmin_khz >= self.fmax_khz
        ):
            raise ValueError("Minimum display frequency must be lower than maximum.")
        if not 1 <= self.workers <= 64:
            raise ValueError("Number of threads must be between 1 and 64.")


PROFILES: dict[str, ProcessingSettings] = {
    "low-memory": ProcessingSettings(
        profile="low-memory",
        n_fft=1024,
        hop_length=512,
        image_width=1600,
        image_height=900,
        dpi=100,
        stft_chunk_frames=64,
        generate_power_spectrum=False,
        workers=1,
    ),
    "balanced": ProcessingSettings(),
    "high-detail": ProcessingSettings(
        profile="high-detail",
        n_fft=4096,
        hop_length=512,
        image_width=3840,
        image_height=2160,
        dpi=200,
        stft_chunk_frames=64,
        workers=1,
    ),
}


def settings_for_profile(profile: str = "balanced", **overrides: Any) -> ProcessingSettings:
    """Return a validated copy of a named profile with selected overrides."""

    if profile not in PROFILES:
        choices = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown profile '{profile}'. Choose one of: {choices}.")
    settings = replace(PROFILES[profile], **overrides)
    settings.validate()
    return settings


ProgressCallback = Callable[[int, int, str], None]
StopRequested = Callable[[], bool]


@dataclass
class FileResult:
    source: str
    status: str
    segments: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class BatchResult:
    total_files: int
    completed_files: int
    failed_files: int
    stopped: bool
    output_folder: Path
    files: list[FileResult]


def discover_wav_files(
    input_folder: Path | str,
    *,
    recursive: bool = False,
    exclude_folder: Path | str | None = None,
) -> list[Path]:
    """Find WAV files in a stable, case-insensitive order."""

    root = Path(input_folder).expanduser().resolve()
    pattern = "**/*" if recursive else "*"
    excluded = Path(exclude_folder).expanduser().resolve() if exclude_folder else None
    files: list[Path] = []
    for candidate in root.glob(pattern):
        if not candidate.is_file() or candidate.suffix.lower() != ".wav":
            continue
        resolved = candidate.resolve()
        if excluded is not None and (resolved == excluded or excluded in resolved.parents):
            continue
        files.append(resolved)
    return sorted(files, key=lambda path: str(path).casefold())


def recording_start(path: Path) -> tuple[datetime, str]:
    """Infer a recording start from an AudioMoth-like name, else file mtime."""

    match = re.search(r"(?<!\d)(\d{8})[_-]?(\d{6})(?!\d)", path.stem)
    if match:
        try:
            return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S"), "filename"
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime), "file_modified_time"


def _frequency_limits(
    settings: ProcessingSettings, sample_rate: int
) -> tuple[float, float, list[str]]:
    nyquist_khz = sample_rate / 2000.0
    lower = settings.fmin_khz if settings.fmin_khz is not None else 0.0
    upper = settings.fmax_khz if settings.fmax_khz is not None else nyquist_khz
    warnings: list[str] = []
    if upper > nyquist_khz:
        warnings.append(
            f"Maximum display frequency clipped from {upper:g} to the "
            f"{nyquist_khz:g} kHz Nyquist limit."
        )
        upper = nyquist_khz
    if lower >= nyquist_khz or lower >= upper:
        raise ValueError(
            f"Display range {lower:g}-{upper:g} kHz is invalid for a "
            f"{sample_rate / 1000:g} kHz recording."
        )
    return lower, upper, warnings


def _mono(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 1:
        return samples.astype(np.float32, copy=False)
    return samples.mean(axis=1, dtype=np.float32)


def _stft(
    samples: np.ndarray,
    sample_rate: int,
    settings: ProcessingSettings,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """Compute a bounded-memory STFT and mean power per frequency bin."""

    signal = _mono(samples)
    original_length = len(signal)
    if original_length == 0:
        raise ValueError("Audio segment is empty.")

    n_fft = settings.n_fft
    hop = settings.hop_length
    total_frames = max(1, 1 + math.ceil(max(0, original_length - n_fft) / hop))
    frequency_bins = n_fft // 2 + 1
    log_amplitude = np.empty((frequency_bins, total_frames), dtype=np.float32)
    power_sum = np.zeros(frequency_bins, dtype=np.float64)
    window = np.hanning(n_fft).astype(np.float32)
    offsets = np.arange(n_fft, dtype=np.int64)
    peak_db = -np.inf

    for first in range(0, total_frames, settings.stft_chunk_frames):
        last = min(total_frames, first + settings.stft_chunk_frames)
        starts = np.arange(first, last, dtype=np.int64) * hop
        indices = starts[:, None] + offsets[None, :]
        valid = indices < original_length
        safe_indices = np.minimum(indices, original_length - 1)
        frames = signal[safe_indices].astype(np.float32, copy=True)
        frames[~valid] = 0.0
        frames *= window
        spectrum = np.fft.rfft(frames, n=n_fft, axis=1)
        magnitude = np.abs(spectrum).astype(np.float32)
        power_sum += np.square(magnitude, dtype=np.float64).sum(axis=0)
        chunk_db = 20.0 * np.log10(np.maximum(magnitude, np.finfo(np.float32).tiny))
        peak_db = max(peak_db, float(chunk_db.max()))
        log_amplitude[:, first:last] = chunk_db.T

    log_amplitude -= peak_db
    np.maximum(log_amplitude, -settings.dynamic_range_db, out=log_amplitude)
    frequencies_khz = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate) / 1000.0
    times = np.arange(total_frames, dtype=np.float64) * hop / sample_rate
    mean_power = power_sum / total_frames
    return log_amplitude, frequencies_khz, times, mean_power, total_frames


def _plot_spectrogram(
    db: np.ndarray,
    frequencies_khz: np.ndarray,
    duration: float,
    source_name: str,
    segment_start: datetime,
    lower_khz: float,
    upper_khz: float,
    destination: Path,
    settings: ProcessingSettings,
) -> None:
    mask = (frequencies_khz >= lower_khz) & (frequencies_khz <= upper_khz)
    displayed = db[mask, :]
    displayed_frequencies = frequencies_khz[mask]
    if displayed.size == 0:
        raise ValueError("The selected frequency range contains no FFT bins.")

    fig = plt.figure(
        figsize=(settings.image_width / settings.dpi, settings.image_height / settings.dpi),
        dpi=settings.dpi,
        facecolor="#ffffff",
    )
    ax = fig.add_subplot(111)
    image = ax.imshow(
        displayed,
        origin="lower",
        aspect="auto",
        interpolation="nearest",
        extent=(0, duration, displayed_frequencies[0], displayed_frequencies[-1]),
        cmap=settings.colormap,
        vmin=-settings.dynamic_range_db,
        vmax=0,
    )
    ax.set_xlim(0, duration)
    ax.set_ylim(lower_khz, upper_khz)
    ax.set_xlabel("Time within segment (s)")
    ax.set_ylabel("Frequency (kHz)")
    ax.set_title(f"{source_name}  |  {segment_start:%Y-%m-%d %H:%M:%S}", fontsize=13)
    colorbar = fig.colorbar(image, ax=ax, pad=0.02)
    colorbar.set_label("Relative amplitude (dB; segment peak = 0 dB)")
    ax.text(
        0.995,
        1.015,
        "ChiroVerse · BatSpectroGen",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        color="#333333",
    )
    ax.grid(False)
    fig.subplots_adjust(left=0.08, right=0.9, bottom=0.1, top=0.9)
    fig.savefig(
        destination,
        dpi=settings.dpi,
        format="jpg",
        facecolor=fig.get_facecolor(),
        pil_kwargs={"quality": 94, "optimize": True},
    )
    plt.close(fig)


def _plot_average_power(
    mean_power: np.ndarray,
    sample_rate: int,
    source_name: str,
    lower_khz: float,
    upper_khz: float,
    destination: Path,
    settings: ProcessingSettings,
) -> None:
    frequencies_khz = np.fft.rfftfreq(settings.n_fft, d=1.0 / sample_rate) / 1000.0
    reference = float(np.max(mean_power))
    if reference <= 0:
        relative_db = np.full_like(mean_power, -settings.dynamic_range_db, dtype=np.float64)
    else:
        relative_db = 10.0 * np.log10(
            np.maximum(mean_power, np.finfo(np.float64).tiny) / reference
        )
        relative_db = np.maximum(relative_db, -settings.dynamic_range_db)
    mask = (frequencies_khz >= lower_khz) & (frequencies_khz <= upper_khz)

    fig = plt.figure(
        figsize=(settings.image_width / settings.dpi, settings.image_height / settings.dpi),
        dpi=settings.dpi,
        facecolor="#ffffff",
    )
    ax = fig.add_subplot(111)
    ax.plot(frequencies_khz[mask], relative_db[mask], color="#6a1b9a", linewidth=1.0)
    ax.set_xlim(lower_khz, upper_khz)
    ax.set_ylim(-settings.dynamic_range_db, 3)
    ax.set_xlabel("Frequency (kHz)")
    ax.set_ylabel("Mean relative power (dB; file peak = 0 dB)")
    ax.set_title(f"Average power spectrum  |  {source_name}", fontsize=13)
    ax.grid(True, color="#d9d9d9", linewidth=0.6)
    ax.text(
        0.995,
        1.015,
        "ChiroVerse · BatSpectroGen",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        color="#333333",
    )
    fig.subplots_adjust(left=0.09, right=0.97, bottom=0.1, top=0.9)
    fig.savefig(
        destination,
        dpi=settings.dpi,
        format="jpg",
        facecolor=fig.get_facecolor(),
        pil_kwargs={"quality": 94, "optimize": True},
    )
    plt.close(fig)


def process_file(
    source: str,
    input_folder: str,
    output_folder: str,
    settings: ProcessingSettings,
) -> FileResult:
    """Process one audio file. This function is safe to run in a worker process."""

    source_path = Path(source)
    input_root = Path(input_folder)
    output_root = Path(output_folder)
    result = FileResult(source=str(source_path), status="failed")

    try:
        relative = source_path.relative_to(input_root)
        destination_folder = output_root / relative.parent / source_path.stem
        destination_folder.mkdir(parents=True, exist_ok=True)
        start_time, _timestamp_source = recording_start(source_path)

        with sf.SoundFile(source_path) as audio:
            sample_rate = int(audio.samplerate)
            lower_khz, upper_khz, warnings = _frequency_limits(settings, sample_rate)
            result.warnings.extend(warnings)
            frames_per_segment = max(1, int(round(settings.segment_duration * sample_rate)))
            segment_number = 0
            accumulated_power: np.ndarray | None = None
            accumulated_stft_frames = 0

            while True:
                block = audio.read(
                    frames=frames_per_segment,
                    dtype="float32",
                    always_2d=True,
                )
                if len(block) == 0:
                    break
                segment_number += 1
                actual_duration = len(block) / sample_rate
                segment_start = start_time + timedelta(
                    seconds=(segment_number - 1) * settings.segment_duration
                )
                db, frequencies, _times, mean_power, stft_frames = _stft(
                    block, sample_rate, settings
                )
                timestamp = segment_start.strftime("%Y%m%d_%H%M%S")
                image_name = f"{timestamp}_part{segment_number:04d}.jpg"
                image_path = destination_folder / image_name
                _plot_spectrogram(
                    db,
                    frequencies,
                    actual_duration,
                    source_path.name,
                    segment_start,
                    lower_khz,
                    upper_khz,
                    image_path,
                    settings,
                )
                if accumulated_power is None:
                    accumulated_power = np.zeros_like(mean_power, dtype=np.float64)
                accumulated_power += mean_power * stft_frames
                accumulated_stft_frames += stft_frames
                result.segments = segment_number

            if segment_number == 0:
                raise ValueError("Audio file contains no samples.")

            if (
                settings.generate_power_spectrum
                and accumulated_power is not None
                and accumulated_stft_frames > 0
            ):
                power_path = destination_folder / f"average_power_{source_path.stem}.jpg"
                power_lower_khz = lower_khz if settings.apply_display_range_to_power else 0.0
                power_upper_khz = (
                    upper_khz
                    if settings.apply_display_range_to_power
                    else sample_rate / 2000.0
                )
                _plot_average_power(
                    accumulated_power / accumulated_stft_frames,
                    sample_rate,
                    source_path.name,
                    power_lower_khz,
                    power_upper_khz,
                    power_path,
                    settings,
                )

        result.status = "completed"
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def process_batch(
    input_folder: Path | str,
    output_folder: Path | str,
    settings: ProcessingSettings,
    *,
    progress_callback: ProgressCallback | None = None,
    stop_requested: StopRequested | None = None,
) -> BatchResult:
    """Process all WAV files into spectrogram and optional power images."""

    settings.validate()
    input_root = Path(input_folder).expanduser().resolve()
    output_root = Path(output_folder).expanduser().resolve()
    if not input_root.is_dir():
        raise ValueError(f"Input folder does not exist: {input_root}")
    if settings.colormap not in matplotlib.colormaps:
        raise ValueError(f"Unknown Matplotlib colormap: {settings.colormap}")
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(
            "The output folder is not empty. Choose a new or empty folder so results "
            "from different settings cannot be mixed."
        )
    output_root.mkdir(parents=True, exist_ok=True)
    files = discover_wav_files(
        input_root,
        recursive=settings.recursive,
        exclude_folder=output_root,
    )
    if not files:
        raise ValueError("No WAV files were found in the selected input folder.")

    should_stop = stop_requested or (lambda: False)
    results: list[FileResult] = []
    total = len(files)

    if settings.workers == 1:
        for source in files:
            if should_stop():
                break
            result = process_file(str(source), str(input_root), str(output_root), settings)
            results.append(result)
            if progress_callback:
                progress_callback(len(results), total, source.name)
    else:
        with ProcessPoolExecutor(max_workers=settings.workers) as executor:
            futures = {
                executor.submit(
                    process_file, str(source), str(input_root), str(output_root), settings
                ): source
                for source in files
            }
            cancellation_requested = False
            for future in as_completed(futures):
                source = futures[future]
                try:
                    result = future.result()
                except CancelledError:
                    continue
                except Exception as exc:
                    result = FileResult(
                        source=str(source),
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                results.append(result)
                if progress_callback:
                    progress_callback(len(results), total, source.name)
                if should_stop() and not cancellation_requested:
                    cancellation_requested = True
                    for pending in futures:
                        pending.cancel()

    stopped = should_stop() and len(results) < total
    results.sort(key=lambda item: item.source.casefold())
    failed = sum(item.status != "completed" for item in results)
    return BatchResult(
        total_files=total,
        completed_files=sum(item.status == "completed" for item in results),
        failed_files=failed,
        stopped=stopped,
        output_folder=output_root,
        files=results,
    )


class BatSpectroGenApp:
    """The BatSpectroGen graphical interface."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("BatSpectroGen: By ChiroVerse")
        root.geometry("850x650")
        root.minsize(850, 650)

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.duration = tk.StringVar(value="5")
        self.workers = tk.StringVar(value="1")
        self.profile = tk.StringVar(value="balanced")
        self.colormap = tk.StringVar(value="magma")
        self.power = tk.BooleanVar(value=True)
        self.multiprocessing = tk.BooleanVar(value=False)
        self.fmin = tk.StringVar()
        self.fmax = tk.StringVar()
        self.filter_power = tk.BooleanVar(value=False)
        self.progress = tk.IntVar(value=0)
        self.progress_text = tk.StringVar(value="0% | 0/0 files")
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build()

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(1, weight=1)

        tk.Label(root, text="Input Folder:").grid(
            row=0, column=0, sticky="w", padx=6, pady=6
        )
        tk.Entry(root, textvariable=self.input_path, width=70).grid(
            row=0, column=1, columnspan=2, sticky="ew"
        )
        tk.Button(root, text="Browse", command=self._browse_input).grid(
            row=0, column=3, padx=(4, 6)
        )

        tk.Label(root, text="Output Folder (optional):").grid(
            row=1, column=0, sticky="w", padx=6, pady=6
        )
        tk.Entry(root, textvariable=self.output_path, width=70).grid(
            row=1, column=1, columnspan=2, sticky="ew"
        )
        tk.Button(root, text="Browse", command=self._browse_output).grid(
            row=1, column=3, padx=(4, 6)
        )

        tk.Label(root, text="Segment Duration (s):").grid(
            row=2, column=0, sticky="w", padx=6, pady=6
        )
        tk.Entry(root, textvariable=self.duration, width=10).grid(
            row=2, column=1, sticky="w"
        )
        tk.Label(root, text="Number of Threads:").grid(
            row=2, column=2, sticky="w", padx=6
        )
        tk.Entry(root, textvariable=self.workers, width=8).grid(
            row=2, column=3, sticky="w"
        )

        tk.Label(root, text="Processing Profile:").grid(
            row=3, column=0, sticky="w", padx=6
        )
        profile_box = ttk.Combobox(
            root,
            textvariable=self.profile,
            values=("low-memory", "balanced", "high-detail"),
            state="readonly",
            width=15,
        )
        profile_box.grid(row=3, column=1, sticky="w")
        profile_box.bind("<<ComboboxSelected>>", self._profile_changed)

        tk.Label(root, text="Colormap:").grid(row=3, column=2, sticky="w", padx=6)
        ttk.Combobox(
            root,
            textvariable=self.colormap,
            values=("viridis", "bone", "YlGnBu", "magma", "Greys"),
            state="readonly",
            width=15,
        ).grid(row=3, column=3, sticky="w")

        tk.Checkbutton(root, text="Generate Power Spectrum", variable=self.power).grid(
            row=4, column=0, sticky="w"
        )
        tk.Checkbutton(
            root,
            text="Use Multiprocessing",
            variable=self.multiprocessing,
        ).grid(row=4, column=1, sticky="w")

        tk.Label(root, text="Filter (Select frequencies to display):").grid(
            row=5,
            column=0,
            columnspan=4,
            sticky="w",
            padx=6,
            pady=(10, 2),
        )
        tk.Label(root, text="Min (kHz):").grid(row=6, column=0, sticky="w", padx=6)
        tk.Entry(root, textvariable=self.fmin, width=12).grid(
            row=6, column=1, sticky="w"
        )
        tk.Label(root, text="Max (kHz):").grid(row=6, column=2, sticky="w", padx=6)
        tk.Entry(root, textvariable=self.fmax, width=12).grid(
            row=6, column=3, sticky="w"
        )

        tk.Checkbutton(
            root,
            text="Apply filter to Power Spectrum",
            variable=self.filter_power,
        ).grid(row=7, column=0, columnspan=4, sticky="w", padx=6)

        self.start_button = tk.Button(
            root,
            text="Generate Spectrograms",
            bg="#28a745",
            fg="white",
            command=self._start,
        )
        self.start_button.grid(
            row=8, column=0, columnspan=2, sticky="we", padx=6, pady=10
        )
        self.stop_button = tk.Button(
            root,
            text="Stop",
            bg="#dc3545",
            fg="white",
            state="disabled",
            command=self._stop,
        )
        self.stop_button.grid(
            row=8, column=2, columnspan=2, sticky="we", padx=6, pady=10
        )

        ttk.Progressbar(root, maximum=100, variable=self.progress, length=500).grid(
            row=9, column=0, columnspan=3, padx=6, pady=6, sticky="ew"
        )
        tk.Label(root, textvariable=self.progress_text).grid(
            row=9, column=3, sticky="w", padx=(4, 6)
        )

        tk.Label(root, text="Log:").grid(row=10, column=0, sticky="w", padx=6)
        self.log = tk.Text(root, height=14, width=88, wrap="word", state="disabled")
        self.log.grid(
            row=11, column=0, columnspan=4, padx=6, pady=(0, 6), sticky="nsew"
        )
        root.rowconfigure(11, weight=1)

        tk.Button(
            root,
            text="About",
            command=self._show_about,
            bg="#007bff",
            fg="white",
            width=8,
        ).grid(row=12, column=3, sticky="e", padx=6, pady=(0, 6))

    def _browse_input(self) -> None:
        selected = filedialog.askdirectory(title="Select the folder containing WAV files")
        if selected:
            self.input_path.set(selected)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(title="Select an empty output folder")
        if selected:
            self.output_path.set(selected)

    def _profile_changed(self, _event: object | None = None) -> None:
        if self.profile.get() == "low-memory":
            self.workers.set("1")
            self.multiprocessing.set(False)
            self.power.set(False)
        else:
            self.power.set(True)

    @staticmethod
    def _optional_float(value: str) -> float | None:
        stripped = value.strip()
        return float(stripped) if stripped else None

    def _settings(self) -> ProcessingSettings:
        workers = int(self.workers.get()) if self.multiprocessing.get() else 1
        return settings_for_profile(
            self.profile.get(),
            segment_duration=float(self.duration.get()),
            workers=workers,
            fmin_khz=self._optional_float(self.fmin.get()),
            fmax_khz=self._optional_float(self.fmax.get()),
            colormap=self.colormap.get(),
            generate_power_spectrum=self.power.get(),
            apply_display_range_to_power=self.filter_power.get(),
        )

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self) -> None:
        try:
            input_folder = Path(self.input_path.get().strip()).expanduser()
            if not input_folder.is_dir():
                raise ValueError("Select a valid input folder.")
            if not self.output_path.get().strip():
                self.output_path.set(
                    str(input_folder.parent / f"BatSpectroGen_Output_{input_folder.name}")
                )
            output_folder = Path(self.output_path.get().strip()).expanduser()
            settings = self._settings()
        except Exception as exc:
            messagebox.showerror("Error", str(exc))
            return

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress.set(0)
        self.progress_text.set("0% | 0/0 files")
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append_log(f"Input: {input_folder}")
        self._append_log(f"Output: {output_folder}")
        self._append_log(
            f"Profile: {settings.profile}; concurrent files: {settings.workers}"
        )

        def progress(done: int, total: int, name: str) -> None:
            self.events.put(("progress", (done, total, name)))

        def run() -> None:
            try:
                result = process_batch(
                    input_folder,
                    output_folder,
                    settings,
                    progress_callback=progress,
                    stop_requested=self.stop_event.is_set,
                )
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=run, daemon=True).start()
        self.root.after(100, self._poll)

    def _stop(self) -> None:
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self._append_log("Stop requested by user. Running work will finish safely.")

    def _poll(self) -> None:
        finished = False
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                done, total, name = payload
                percent = int(done / total * 100)
                self.progress.set(percent)
                self.progress_text.set(f"{percent}% | {done}/{total} files")
                self._append_log(f"OK: {name}")
            elif event == "done":
                self._finish(payload)
                finished = True
            elif event == "error":
                messagebox.showerror("Processing failed", str(payload))
                self._append_log(f"ERR: {payload}")
                finished = True
        if finished:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
        else:
            self.root.after(100, self._poll)

    def _finish(self, result: BatchResult) -> None:
        for item in result.files:
            for warning in item.warnings:
                self._append_log(f"Warning ({Path(item.source).name}): {warning}")
            if item.error:
                self._append_log(f"ERR: {Path(item.source).name} -> {item.error}")
        if result.stopped:
            self._append_log("Stopped safely. Completed outputs were kept.")
            return
        self.progress.set(100)
        self.progress_text.set(
            f"100% | {result.completed_files}/{result.total_files} files"
        )
        messagebox.showinfo(
            "BatSpectroGen finished",
            f"Processed {result.completed_files} file(s).\n\nOutput:\n{result.output_folder}",
        )

    def _show_about(self) -> None:
        about = tk.Toplevel(self.root)
        about.title("About BatSpectroGen")
        about.geometry("420x235")
        about.resizable(False, False)
        tk.Label(
            about,
            text="BatSpectroGen by ChiroVerse",
            wraplength=400,
            font=("Aptos", 10, "bold"),
        ).pack(anchor="w", padx=20, pady=(8, 0))
        tk.Label(
            about,
            text="Created by Vedant Barje and Kadambari Deshpande",
            wraplength=400,
        ).pack(anchor="w", padx=20, pady=(6, 0))
        tk.Label(
            about,
            text=(
                "Supported by the Indian Institute for Human Settlements, Bengaluru, "
                "and Wildlife Conservation Trust, Mumbai"
            ),
            wraplength=400,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(6, 0))
        tk.Label(about, text=f"Version {__version__}").pack(
            anchor="w", padx=20, pady=(6, 0)
        )
        tk.Label(about, text="Contact:").pack(
            anchor="w", padx=20, pady=(6, 0)
        )
        tk.Label(about, text="connect.chiroverse@gmail.com").pack(
            anchor="w", padx=20, pady=(3, 8)
        )
        tk.Button(about, text="Close", command=about.destroy).pack(pady=5)


def main() -> None:
    multiprocessing.freeze_support()
    root = tk.Tk()
    BatSpectroGenApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
