import sys
import subprocess
import importlib

def install_and_import(package, import_name=None, version=None):
    try:
        if import_name is None:
            import_name = package
        importlib.import_module(import_name)
    except ImportError:
        print(f"{package} not found. Installing...")
        pkg = package if version is None else f"{package}>={version}"
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", pkg]
        )
        globals()[import_name] = importlib.import_module(import_name)

# Force correct packages
install_and_import("numpy")
install_and_import("matplotlib", version="3.0")
install_and_import("librosa")
install_and_import("soundfile", "soundfile")

import os
import re
import time
import numpy as np
import multiprocessing as mp
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import queue as th_queue

def create_spectrogram_for_file(file, input_folder, output_folder,
                                segment_duration=5, cmap='magma', do_power=True,
                                sampling_rate=384000, log_queue=None, stop_event=None,
                                fmin=None, fmax=None, apply_filter_to_power=False):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
        import librosa
        import librosa.display

        if stop_event is not None and stop_event.is_set():
            if log_queue:
                log_queue.put(f"STOPPED: {file}")
            return

        input_file = os.path.join(input_folder, file)
        samples, sample_rate = librosa.load(input_file, sr=sampling_rate)
        if len(samples) == 0:
            raise ValueError("Empty audio")

        samples_per_segment = int(segment_duration * sample_rate)
        num_segments = max(1, len(samples) // samples_per_segment)

        base_filename = os.path.splitext(file)[0]
        try:
            date_str, raw_time_str = base_filename.split('_')[:2]
            time_str = re.sub(r'\D', '', raw_time_str)
            start_time = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
        except Exception:
            start_time = datetime.fromtimestamp(os.path.getmtime(input_file))

        output_subfolder = os.path.join(output_folder, os.path.splitext(file)[0])

        wm = "".join([chr(c) for c in [67,114,101,97,116,101,100,32,119,105,116,104,32,67,104,105,114,111,86,101,114,115,101]])

        for i in range(num_segments):
            if stop_event is not None and stop_event.is_set():
                if log_queue:
                    log_queue.put(f"STOPPED mid-file: {file}")
                return

            if i == 0:
                os.makedirs(output_subfolder, exist_ok=True)

            start = i * samples_per_segment
            end = start + samples_per_segment
            segment = samples[start:end]
            if len(segment) < samples_per_segment:
                segment = np.pad(segment, (0, samples_per_segment - len(segment)))

            Sxx = np.abs(librosa.stft(segment, n_fft=1024, hop_length=256))
            Sxx_db = librosa.amplitude_to_db(Sxx, ref=np.max)

            segment_time = start_time + timedelta(seconds=i * segment_duration)
            segment_filename = segment_time.strftime("%Y%m%d_%H%M%S") + ".jpg"

            fig, ax = plt.subplots(figsize=(19.20, 10.80))
            img = librosa.display.specshow(Sxx_db, sr=sample_rate, x_axis='time',
                                           y_axis='hz', cmap=cmap, ax=ax)
            ax.set_xlim(0, segment_duration)

            y_min = fmin if (fmin is not None and fmax is not None and fmin < fmax) else 0
            y_max = fmax if (fmin is not None and fmax is not None and fmin < fmax) else sample_rate // 2

            ax.set_ylim(y_min, y_max)
            ax.set_ylabel('Frequency (kHz)')

            ticks_khz = np.arange(int(y_min/1000), int(y_max/1000)+1, 10)
            ticks_hz = ticks_khz * 1000
            ax.yaxis.set_major_locator(ticker.FixedLocator(ticks_hz))
            ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: f"{int(round(v/1000))}" if v % 10000 == 0 else ""))

            ax.xaxis.set_major_locator(ticker.MultipleLocator(0.5))
            ax.set_xlabel('Time (s)')

            title_text = f'Spectrogram: {segment_time.strftime("%Y-%m-%d %H:%M:%S")}'
            ax.set_title(title_text, fontsize=14)

            ax.text(0.985, 1.02, wm,
                    fontsize=9, color="black", alpha=1,
                    ha="right", va="top",
                    transform=ax.transAxes)

            if ax.collections:
                fig.colorbar(img, ax=ax, label='Intensity (dB)')

            fig.savefig(os.path.join(output_subfolder, segment_filename), dpi=100)
            plt.close(fig)

            if log_queue:
                log_queue.put(f"{file}: {i+1}/{num_segments} spectrograms generated")

        if do_power and not (stop_event is not None and stop_event.is_set()):
            create_power_spectrum(samples, sample_rate, output_subfolder, file,
                                  fmin=fmin if apply_filter_to_power else None,
                                  fmax=fmax if apply_filter_to_power else None)

        if log_queue:
            log_queue.put(f"OK: {file}")

    except Exception as e:
        if log_queue:
            log_queue.put(f"ERR: {file} -> {repr(e)}")


def create_power_spectrum(samples, sample_rate, output_subfolder, file,
                          fmin=None, fmax=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fft_spectrum = np.fft.fft(samples)
    freq = np.fft.fftfreq(len(fft_spectrum), d=1 / sample_rate)
    power = np.abs(fft_spectrum) ** 2

    freq = freq[:len(freq) // 2]
    power = power[:len(power) // 2]
    freq_khz = freq / 1000

    fig, ax = plt.subplots(figsize=(19.20, 10.80))
    ax.plot(freq_khz, power, linewidth=0.5)
    ax.set_title(f"Power Spectrum - {file}")
    ax.set_xlabel("Frequency (kHz)")
    ax.set_ylabel("Amplitude")
    ax.grid(True)

    if fmin is not None and fmax is not None and fmin < fmax:
        ax.set_xlim(fmin / 1000, fmax / 1000)

    power_spectrum_filename = f"power_spectrum_{os.path.splitext(file)[0]}.jpg"
    fig.savefig(os.path.join(output_subfolder, power_spectrum_filename), dpi=100)
    plt.close(fig)


def worker_controller(input_folder, output_folder, files, segment_duration,
                      num_workers, cmap, do_power, sampling_rate, progress_queue, log_queue,
                      stop_event, use_multiproc=True, fmin=None, fmax=None, apply_filter_to_power=False):
    total = len(files)
    if total == 0:
        progress_queue.put((0, 0))
        return

    manager = mp.Manager()
    mp_log_q = manager.Queue()
    mp_stop_event = manager.Event()
    if stop_event.is_set():
        mp_stop_event.set()

    def update_stop_event():
        if stop_event.is_set():
            mp_stop_event.set()
        return mp_stop_event

    if not use_multiproc:
        for i, f in enumerate(files, 1):
            if stop_event.is_set():
                break
            create_spectrogram_for_file(f, input_folder, output_folder,
                                        segment_duration, cmap, do_power,
                                        sampling_rate, log_queue=log_queue,
                                        stop_event=update_stop_event(),
                                        fmin=fmin, fmax=fmax,
                                        apply_filter_to_power=apply_filter_to_power)
            progress_queue.put((i, total))
        return

    pool = mp.Pool(processes=num_workers)
    results = []
    for f in files:
        args = (f, input_folder, output_folder, segment_duration,
                cmap, do_power, sampling_rate, mp_log_q, mp_stop_event,
                fmin, fmax, apply_filter_to_power)
        res = pool.apply_async(create_spectrogram_for_file, args=args)
        results.append(res)

    done = 0
    while True:
        try:
            while True:
                msg = mp_log_q.get_nowait()
                log_queue.put(msg)
        except Exception:
            pass

        done = sum(1 for r in results if r.ready())
        progress_queue.put((done, total))
        if done >= total or stop_event.is_set():
            mp_stop_event.set()
            break
        time.sleep(0.3)

    pool.close()
    pool.join()


def build_gui_and_run():
    root = tk.Tk()
    root.title("Automated Batch Spectrogram Generator: By ChiroVerse")
    root.geometry("850x650")

    input_path = tk.StringVar()
    output_path = tk.StringVar()
    duration_var = tk.StringVar(value="5")
    cores_var = tk.StringVar(value=str(max(1, mp.cpu_count() - 1)))
    colormap_var = tk.StringVar(value="magma")
    power_var = tk.BooleanVar(value=True)
    multiproc_var = tk.BooleanVar(value=True)
    sampling_var = tk.StringVar(value="384")
    filter_min_var = tk.StringVar(value="")
    filter_max_var = tk.StringVar(value="")
    filter_power_var = tk.BooleanVar(value=False)
    progress_var = tk.IntVar(value=0)

    # --- GUI Widgets ---
    tk.Label(root, text="Input Folder:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
    tk.Entry(root, textvariable=input_path, width=70).grid(row=0, column=1, columnspan=2, sticky="w")
    tk.Button(root, text="Browse", command=lambda: input_path.set(filedialog.askdirectory())).grid(row=0, column=3)

    tk.Label(root, text="Output Folder (optional):").grid(row=1, column=0, sticky="w", padx=6, pady=6)
    tk.Entry(root, textvariable=output_path, width=70).grid(row=1, column=1, columnspan=2, sticky="w")
    tk.Button(root, text="Browse", command=lambda: output_path.set(filedialog.askdirectory())).grid(row=1, column=3)

    tk.Label(root, text="Segment Duration (s):").grid(row=2, column=0, sticky="w", padx=6, pady=6)
    tk.Entry(root, textvariable=duration_var, width=10).grid(row=2, column=1, sticky="w")

    tk.Label(root, text="Number of Threads:").grid(row=2, column=2, sticky="w", padx=6)
    tk.Entry(root, textvariable=cores_var, width=8).grid(row=2, column=3, sticky="w")

    tk.Label(root, text="Sampling Frequency (kHz):").grid(row=3, column=0, sticky="w", padx=6)
    tk.Entry(root, textvariable=sampling_var, width=12).grid(row=3, column=1, sticky="w")

    tk.Label(root, text="Colormap:").grid(row=3, column=2, sticky="w", padx=6)
    ttk.Combobox(root, textvariable=colormap_var,
                 values=['viridis', 'bone', 'YlGnBu', 'magma', 'Greys'], width=15).grid(row=3, column=3, sticky="w")

    tk.Checkbutton(root, text="Generate Power Spectrum", variable=power_var).grid(row=4, column=0, sticky="w")
    tk.Checkbutton(root, text="Use Multiprocessing", variable=multiproc_var).grid(row=4, column=1, sticky="w")

    # Filter options
    tk.Label(root, text="Filter (Select frequencies to display):").grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(10, 2))
    tk.Label(root, text="Min (kHz):").grid(row=6, column=0, sticky="w", padx=6)
    tk.Entry(root, textvariable=filter_min_var, width=12).grid(row=6, column=1, sticky="w")
    tk.Label(root, text="Max (kHz):").grid(row=6, column=2, sticky="w", padx=6)
    tk.Entry(root, textvariable=filter_max_var, width=12).grid(row=6, column=3, sticky="w")

    tk.Checkbutton(root, text="Apply filter to Power Spectrum", variable=filter_power_var).grid(row=7, column=0, columnspan=4, sticky="w", padx=6)

    start_btn = tk.Button(root, text="Generate Spectrograms", bg="#28a745", fg="white")
    start_btn.grid(row=8, column=0, columnspan=2, sticky="we", padx=6, pady=10)
    stop_btn = tk.Button(root, text="Stop", bg="#dc3545", fg="white")
    stop_btn.grid(row=8, column=2, columnspan=2, sticky="we", padx=6, pady=10)

    progress_bar = ttk.Progressbar(root, maximum=100, variable=progress_var, length=500)
    progress_bar.grid(row=9, column=0, columnspan=3, padx=6, pady=6, sticky="w")
    progress_label = tk.Label(root, text="0% | 0/0 files")
    progress_label.grid(row=9, column=3, sticky="w")

    tk.Label(root, text="Log:").grid(row=10, column=0, sticky="w", padx=6)
    log_text = tk.Text(root, height=14, width=88)
    log_text.grid(row=11, column=0, columnspan=4, padx=6, pady=(0,6))
    log_text.configure(state="disabled")

    # About button
    def show_about():
        about_win = tk.Toplevel(root)
        about_win.title("About")
        about_win.geometry("420x220")
        about_win.resizable(False, False)
        tk.Label(about_win, text="Automated Batch Spectrogram Generator: by ChiroVerse", wraplength=400, font=("Aptos", 10, "bold")).pack(anchor="w", padx=20, pady=(6,0))
        tk.Label(about_win, text="Created by - Kadambari Deshpande and Vedant Barje", wraplength=400).pack(anchor="w", padx=20, pady=(6,0))
        tk.Label(about_win, text="Supported by - Indian Institute for Human Settlements, Bengaluru and Wildlife Conservation Trust, Mumbai", wraplength=400, justify="left").pack(anchor="w", padx=20, pady=(6,0))
        tk.Label(about_win, text="Build - v1.0").pack(anchor="w", padx=20, pady=(6,0))
        tk.Label(about_win, text="For more info get in touch at:", wraplength=400, justify="left").pack(anchor="w", padx=20, pady=(6,0))
        tk.Label(about_win, text="Kadambari: kvd.novel@gmail.com and Vedant: vab4698@gmail.com").pack(anchor="w", padx=20, pady=(6,10))

        close_btn = tk.Button(about_win, text="Close", command=about_win.destroy)
        close_btn.pack(pady=5)

    about_btn = tk.Button(root, text="About", command=show_about, bg="#007bff", fg="white")
    about_btn.place(x=770, y=10, width=60)

    stop_event = threading.Event()
    gui_progress_queue = th_queue.Queue()
    gui_log_queue = th_queue.Queue()

    def append_log(msg):
        log_text.configure(state="normal")
        log_text.insert("end", msg + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def start_processing():
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_label.config(text="0% | 0/0 files")

        in_folder = input_path.get().strip()
        out_folder = output_path.get().strip()
        try:
            segment_duration = int(duration_var.get())
            if segment_duration <= 0:
                raise ValueError
        except:
            messagebox.showerror("Error", "Segment duration must be a positive integer")
            return
        try:
            num_cores = int(cores_var.get())
            if num_cores <= 0:
                raise ValueError
        except:
            messagebox.showerror("Error", "Number of cores must be a positive integer")
            return
        try:
            sampling_rate = int(float(sampling_var.get()) * 1000)
            if sampling_rate <= 0:
                raise ValueError
        except:
            messagebox.showerror("Error", "Sampling frequency must be a positive number")
            return

        try:
            fmin = float(filter_min_var.get()) * 1000 if filter_min_var.get() else None
            fmax = float(filter_max_var.get()) * 1000 if filter_max_var.get() else None
        except:
            messagebox.showerror("Error", "Filter values must be numeric")
            return

        if fmin is not None and fmax is not None and fmin >= fmax:
            messagebox.showerror("Error", "Min frequency must be less than Max frequency")
            return

        cmap = colormap_var.get()
        do_power = bool(power_var.get())
        use_mp = bool(multiproc_var.get())
        apply_filter_to_power = bool(filter_power_var.get())

        if not in_folder:
            messagebox.showerror("Error", "Select input folder")
            return

        if not out_folder:
            name = os.path.basename(os.path.normpath(in_folder))
            out_folder = os.path.join(in_folder, f"Automated_Spectrogram_{name}")
            output_path.set(out_folder)

        files = [f for f in os.listdir(in_folder) if f.lower().endswith(".wav")]
        total = len(files)
        if total == 0:
            messagebox.showinfo("No files", "No .wav files found in input folder")
            return

        start_btn.config(state="disabled")
        stop_event.clear()

        def background_runner():
            worker_controller(in_folder, out_folder, files, segment_duration,
                              num_cores, cmap, do_power, sampling_rate,
                              gui_progress_queue, gui_log_queue, stop_event,
                              use_multiproc=use_mp,
                              fmin=fmin, fmax=fmax,
                              apply_filter_to_power=apply_filter_to_power)
            gui_progress_queue.put("DONE")

        threading.Thread(target=background_runner, daemon=True).start()
        root.after(100, gui_update_loop)

    def gui_update_loop():
        stop = False
        while not gui_progress_queue.empty():
            msg = gui_progress_queue.get_nowait()
            if msg == "DONE":
                stop = True
            elif isinstance(msg, tuple):
                done, total = msg
                percent = int((done/total)*100)
                progress_var.set(percent)
                progress_label.config(text=f"{percent}% | {done}/{total} files")
        while not gui_log_queue.empty():
            append_log(gui_log_queue.get_nowait())
        if not stop:
            root.after(100, gui_update_loop)
        else:
            start_btn.config(state="normal")

    def stop_processing():
        stop_event.set()
        append_log("Stop requested by user...")

    start_btn.config(command=start_processing)
    stop_btn.config(command=stop_processing)

    root.mainloop()


if __name__ == "__main__":
    build_gui_and_run()
