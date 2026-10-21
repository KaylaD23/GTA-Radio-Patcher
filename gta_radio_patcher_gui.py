#!/usr/bin/env python3
"""Multi-game GTA Radio Patcher GUI with an isolated LCS ISO writer."""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from gta_radio_profiles import PROFILES, PROFILE_BY_DISPLAY, get_profile
from gta_radio_backends import get_backend, UnverifiedGameWriterError

try:
    import lcs_duration_aware_exe_patcher as patcher
    import lcs_duration_aware_iso_patcher as iso_patcher
    from lcs_stream_bounds import installed_size, READ_CHUNK_BYTES
    from lcs_patch_calculator import calculate_plan
except Exception as exc:
    patcher = None
    iso_patcher = None
    IMPORT_ERROR = str(exc)
else:
    IMPORT_ERROR = ""

APP_TITLE = "GTA Radio Patcher"
OFFSET_MODE_LABELS = {
    "Combined: IOP safe seeks + patched ELF (stock offsets)": "iop-elf-stock",
    "Combined: IOP safe seeks + patched ELF (shared adaptive offsets)": "iop-elf-shared",
    "Combined: IOP safe seeks + patched ELF (station-specific offsets)": "iop-elf-station-specific",
    "IOP safe seeks only (original ELF comparison)": "iop-safe-seek",
    "Native IOP duration (original streaming; control comparison)": "native",
    "Original stock offsets (safer baseline)": "stock",
    "Shared adaptive offsets (all radios)": "shared",
    "Station-specific adaptive offsets": "station-specific",
}
OFFSET_MODE_DESCRIPTIONS = {
    "iop-elf-stock": "Combined: calculate the original ELF's ten durations plus IOP seek, position and duration overflow changes from the installed audio sizes. Extremely short streams are padded to 256 KiB. Stock offsets preserved. Actual gameplay unverified.",
    "iop-elf-shared": "Combined: modified ELF with shared adaptive offsets + conditional IOP safe seeks; streams below 256 KiB padded. Injected ELF segment may conflict with game heap at runtime.",
    "iop-elf-station-specific": "Combined: modified ELF with station-specific offsets + conditional IOP safe seeks; streams below 256 KiB padded. RWX segment and helper remain experimental.",
    "native": "Control baseline: pristine SLUS_214.23 and CDSTREAM.IRX unchanged. Longer streams may have seek, position and duration overflows; >4h39m cannot be safely built in this mode.",
    "iop-safe-seek": "IOP-only comparison: calculate seek scaling, position conversion and (where required) duration multiplication safety directly from installed .VB lengths. ELF unchanged; PCSX2 playback unverified.",
    "shared": "One duration-scaled shared sequence, using an extra ELF PT_LOAD near original game memory; heap-clobber risk is unverified. Experimental; compare against stock mode.",
    "station-specific": "Each station gets its own duration-scaled golden-stride sequence and independent cursor. Experimental MIPS helper/RWX ELF segment; test in PCSX2.",
    "stock": "Experimental ELF duration override with the stock random-offset table preserved. The original IOP already supplies native duration: prefer Native IOP mode unless comparing behavior.",
}


def format_duration_ms(duration_ms: int) -> str:
    hours, remainder = divmod(int(duration_ms), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


class DurationPatcherGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1260x890")
        self.root.minsize(1120, 760)
        self.root.configure(bg="#101820")

        self.profile_key = 'lcs_ps2'
        self.profile_var = tk.StringVar(value=PROFILES[self.profile_key].display)
        self._profile_paths = {}
        self._changing_profile = False
        self.input_iso_var = tk.StringVar()
        self.input_exe_var = tk.StringVar()
        self.input_vcs_irx_var = tk.StringVar()
        self.boost_radio_3db_var = tk.BooleanVar(value=False)
        self.vcs_adaptive_offsets_var = tk.BooleanVar(value=False)
        self.vcs_boost_3db_var = self.boost_radio_3db_var  # compatibility with older GUI scripts
        self.single_station_var = tk.StringVar(value='Auto (from .VB filename)')
        self.radio_dir_var = tk.StringVar()
        self.output_iso_var = tk.StringVar()
        self.report_var = tk.StringVar()
        # Default to full combined EE+IOP and high-count adaptive offsets.
        # Comparison-only modes remain available explicitly; none is gameplay-certified.
        self.offset_mode_var = tk.StringVar(value="Combined: IOP safe seeks + patched ELF (shared adaptive offsets)")
        self.offset_mode_help_var = tk.StringVar(value=OFFSET_MODE_DESCRIPTIONS["iop-elf-shared"])
        self.report_custom = False
        self.output_custom = False
        self.status_var = tk.StringVar(value="Choose a pristine input ISO and one or more station .VB replacements. All sizes are detected automatically.")
        self.validation_var = tk.StringVar(value="Not validated")
        self.queue: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.cached_iso_info = None
        self.cached_iso_signature = None
        self.preview_metrics = {}
        self.current_calculation = None
        self._last_audio_signature = None

        self._setup_style()
        self._build_ui()
        self._auto_refresh_job = None
        for watched in (self.input_iso_var, self.input_exe_var, self.radio_dir_var):
            watched.trace_add('write', self._schedule_calculator_refresh)
        self.root.after(100, self._drain_queue)
        self.root.after(1500, self._poll_replacement_file_changes)

        self._apply_profile_controls()
        if patcher is None or iso_patcher is None:
            self._log(f"ERROR: Could not import a patcher module: {IMPORT_ERROR}")
            self._log("Keep the GUI, lcs_duration_aware_exe_patcher.py and lcs_duration_aware_iso_patcher.py in the same folder.")
            self.run_button.configure(state="disabled")
            self.validate_button.configure(state="disabled")

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#101820")
        style.configure("Card.TFrame", background="#17232d")
        style.configure("TLabel", background="#101820", foreground="#eaf0f5", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="#17232d", foreground="#eaf0f5", font=("Segoe UI", 10))
        style.configure("Card.TCheckbutton", background="#17232d", foreground="#eaf0f5", font=("Segoe UI", 10))
        style.map("Card.TCheckbutton", background=[("active", "#17232d")], foreground=[("disabled", "#81909b")])
        style.configure("Title.TLabel", background="#101820", foreground="#ffffff", font=("Segoe UI", 19, "bold"))
        style.configure("SubTitle.TLabel", background="#101820", foreground="#a9bac7", font=("Segoe UI", 10))
        style.configure("Warning.TLabel", background="#432f12", foreground="#ffe0a3", font=("Segoe UI", 10, "bold"), padding=9)
        style.configure("Section.TLabel", background="#17232d", foreground="#83c7ff", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(9, 6))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(13, 8))
        style.map("Accent.TButton", background=[("active", "#3488c8"), ("!disabled", "#1769aa")], foreground=[("!disabled", "white")])
        style.configure("Treeview", background="#0f1921", fieldbackground="#0f1921", foreground="#eaf0f5", rowheight=25, bordercolor="#344552")
        style.configure("Treeview.Heading", background="#263844", foreground="#ffffff", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#285c80")])
        style.configure("Horizontal.TProgressbar", troughcolor="#273540", background="#56b2ee")

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="GTA Radio Patcher", style="Title.TLabel").pack(anchor="w")
        self.profile_summary_var = tk.StringVar(value="Choose a game-specific radio profile. LCS ISO patching is experimental; VCS has a standalone EE overflow fix.")
        ttk.Label(header, textvariable=self.profile_summary_var, style="SubTitle.TLabel").pack(anchor="w", pady=(3, 0))
        profile_row = ttk.Frame(outer, style="Card.TFrame", padding=(10, 8))
        profile_row.pack(fill="x", pady=(0, 8))
        ttk.Label(profile_row, text="Patching profile", style="Card.TLabel").pack(side="left", padx=(0, 15))
        self.profile_combo = ttk.Combobox(profile_row, textvariable=self.profile_var,
                    values=tuple(p.display for p in PROFILES.values()), state="readonly", width=43)
        self.profile_combo.pack(side="left")
        self.profile_combo.bind("<<ComboboxSelected>>", self._profile_changed)
        self.profile_status_var = tk.StringVar()
        ttk.Label(profile_row, textvariable=self.profile_status_var, style="Card.TLabel").pack(side="left", padx=(16, 0))

        # Keep the main action visible near the top of the window. Previously the
        # only build button was below a tall station table and could be off-screen
        # on Windows displays using high DPI scaling or a shorter taskbar area.
        action_bar = ttk.Frame(outer, style="Card.TFrame", padding=(10, 8))
        action_bar.pack(fill="x", pady=(0, 10))
        self.validate_button = ttk.Button(action_bar, text="Validate inputs", command=self._validate_clicked)
        self.validate_button.pack(side="left")
        self.run_button = ttk.Button(
            action_bar,
            text="BUILD PATCHED OUTPUT ISO",
            style="Accent.TButton",
            command=self._run_clicked,
        )
        self.run_button.pack(side="left", padx=(10, 0))
        self.vcs_exe_fix_button = ttk.Button(action_bar, text="PATCH VCS EXECUTABLE (OVERFLOW ONLY)",
                    command=self._patch_vcs_executable_clicked)
        self.vcs_exe_fix_button.pack(side="left", padx=(10, 0))
        irx_action_bar = ttk.Frame(outer, style="Card.TFrame", padding=(10, 5))
        irx_action_bar.pack(fill="x", pady=(0, 8))
        self.vcs_irx_fix_button = ttk.Button(irx_action_bar,
              text="PATCH VCS CDSTREAM.IRX (SIGNED POSITION · EXPERIMENTAL)",
              command=self._patch_vcs_irx_clicked)
        self.vcs_irx_fix_button.pack(side="left")
        self.vcs_iso_install_button = ttk.Button(irx_action_bar,
            text="INSTALL VCS RADIO(S) INTO NEW ISO", command=self._vcs_install_clicked)
        self.vcs_iso_install_button.pack(side="left", padx=(10,0))
        ttk.Label(action_bar, textvariable=self.validation_var, style="Card.TLabel").pack(side="right", padx=(10, 0))

        self.game_warning_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.game_warning_var, style="Warning.TLabel",
                  wraplength=1200, justify="left").pack(fill="x", pady=(0, 10))

        inputs = ttk.Frame(outer, style="Card.TFrame", padding=12)
        inputs.pack(fill="x", pady=(0, 10))
        ttk.Label(inputs, text="1  ISO, EXECUTABLE AND AUDIO INPUTS / OUTPUT", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self._path_row(inputs, 1, "Input PS2 ISO", self.input_iso_var, self._browse_input_iso, "Browse ISO…")
        self._path_row(inputs, 2, "Input executable (optional; original ISO used by default)", self.input_exe_var, self._browse_input_exe, "Browse EXE…")
        self._path_row(inputs, 3, "Radio folder (+ optional GXT/DTZ) or single .VB", self.radio_dir_var, self._browse_radio_folder, "Choose folder…")
        ttk.Button(inputs, text="Choose one VB…", command=self._browse_single_vb).grid(row=3, column=3, padx=(6, 0), pady=3)
        self._path_row(inputs, 4, "Output patched ISO", self.output_iso_var, self._browse_output_iso, "Save ISO as…")
        self._path_row(inputs, 5, "JSON report (optional)", self.report_var, self._browse_report, "Save as…")
        self._path_row(inputs, 6, "Original VCS CDSTREAM.IRX (IOP fix only)",
                       self.input_vcs_irx_var, self._browse_vcs_irx, "Browse IRX…")
        self.game_inputs_help_var = tk.StringVar()
        ttk.Label(inputs, textvariable=self.game_inputs_help_var, style="Card.TLabel",
                  foreground="#b9c9d4", wraplength=1180, justify="left"
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(inputs, text="Offset mode", style="Card.TLabel").grid(row=8, column=0, sticky="w", pady=(9, 0))
        self.offset_mode_combo = ttk.Combobox(
            inputs,
            textvariable=self.offset_mode_var,
            values=tuple(OFFSET_MODE_LABELS),
            state="readonly",
            width=78,
        )
        self.offset_mode_combo.grid(row=8, column=1, columnspan=2, sticky="ew", pady=(9, 0))
        self.offset_mode_combo.bind("<<ComboboxSelected>>", self._offset_mode_changed)
        ttk.Label(
            inputs,
            textvariable=self.offset_mode_help_var,
            style="Card.TLabel",
            foreground="#b9c9d4",
            wraplength=1180,
            justify="left",
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(inputs, text="Single VB target (VCS)", style="Card.TLabel").grid(row=10, column=0, sticky="w", pady=(6,0))
        self.single_station_combo = ttk.Combobox(inputs, textvariable=self.single_station_var,
            values=('Auto (from .VB filename)',) + tuple(name for _, name in get_profile('vcs_ps2').stations),
            state='readonly', width=37)
        self.single_station_combo.grid(row=10, column=1, sticky="w", pady=(6,0))
        self.radio_gain_checkbox = ttk.Checkbutton(
            inputs, text="Boost ALL detected replacement radios by +3 dB (LCS & VCS)",
            variable=self.boost_radio_3db_var, style="Card.TCheckbutton")
        self.radio_gain_checkbox.grid(row=11, column=0, columnspan=3, sticky="w", pady=(9,0))
        self.vcs_gain_checkbox = self.radio_gain_checkbox  # compatibility alias
        self.vcs_offset_checkbox = ttk.Checkbutton(
            inputs, text="VCS EXPERIMENTAL: expand shared radio offsets (~5-second spacing)",
            variable=self.vcs_adaptive_offsets_var, style="Card.TCheckbutton",
            command=self._vcs_offset_checkbox_changed)
        self.vcs_offset_checkbox.grid(row=13, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(inputs, text="VCS standalone ELF only: ~5-second offset contributions, not guaranteed song/start spacing. "
                  "A second PT_LOAD at 0x00780000 is NOT verified safe from the game heap; test in a disposable ISO. "
                  "Leaves existing CDSTREAM.IRX unchanged; supports only the original or known two-DIVU VCS executable.",
                  style="Card.TLabel", foreground="#ffd38d", wraplength=1100).grid(
                      row=14, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(inputs,
            text="Optional: re-encode each replacement .VB at +3 dB before the ISO build. Unselected stations are unchanged. "
                 "Clipping is possible; this does not change the game's fade-in timing. Windows: local C++17 compiler required.",
            style="Card.TLabel", foreground="#b9c9d4", wraplength=1100).grid(
                row=12, column=0, columnspan=3, sticky="w", pady=(2,0))
        inputs.columnconfigure(1, weight=1)
        inputs.columnconfigure(2, weight=0)
        inputs.columnconfigure(3, weight=0)

        self.calculation_var = tk.StringVar(value="Automatic patch calculator: select the original ISO and replacement audio.")
        ttk.Label(outer, textvariable=self.calculation_var, wraplength=1250, justify="left",
                  style="SubTitle.TLabel").pack(fill="x", pady=(0, 8))

        station_card = ttk.Frame(outer, style="Card.TFrame", padding=10)
        station_card.pack(fill="both", expand=True, pady=(0, 10))
        headrow = ttk.Frame(station_card, style="Card.TFrame")
        headrow.pack(fill="x", pady=(0, 7))
        ttk.Label(headrow, text="2  FINAL STATION DURATION PREVIEW", style="Section.TLabel").pack(side="left")
        ttk.Label(headrow, text="Replacement overrides ISO original when present", style="Card.TLabel", foreground="#b9c9d4").pack(side="right")

        columns = ("station", "file", "source", "size", "duration", "boundary", "check")
        self.tree = ttk.Treeview(station_card, columns=columns, show="headings", height=10)
        headings = {"station": "Station", "file": "ISO file", "source": "Source", "size": "Source → Installed (bytes)", "duration": "GUI duration", "boundary": "Expected / patched boundary", "check": "Validation"}
        widths = {"station": 110, "file": 115, "source": 105, "size": 135, "duration": 120, "boundary": 145, "check": 235}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=80, anchor="w" if col in ("station", "file", "source", "check") else "e")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("ok", foreground="#a9edc2")
        self.tree.tag_configure("bad", foreground="#ffb5a8")
        self.tree.tag_configure("missing", foreground="#ffd38d")
        self._populate_empty_rows()

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(controls, mode="indeterminate", length=220)
        self.progress.pack(side="left", padx=(0, 12))
        ttk.Label(controls, textvariable=self.status_var, wraplength=760).pack(side="left", fill="x", expand=True)

        log_card = ttk.Frame(outer, style="Card.TFrame", padding=9)
        log_card.pack(fill="both", expand=True)
        log_head = ttk.Frame(log_card, style="Card.TFrame")
        log_head.pack(fill="x", pady=(0, 5))
        ttk.Label(log_head, text="PATCH / BUILD LOG", style="Section.TLabel").pack(side="left")
        ttk.Button(log_head, text="Clear", command=self._clear_log).pack(side="right")
        log_body = ttk.Frame(log_card, style="Card.TFrame")
        log_body.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_body, height=7, wrap="word", bg="#0c141a", fg="#d9e5ed", insertbackground="white", relief="flat", font=("Consolas", 9), padx=8, pady=6)
        scrollbar = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._log("Ready. Default combined mode patches SLUS_214.23, required CDSTREAM.IRX arithmetic, and shared adaptive offsets. Small files have a minimum installed read size. Original files remain untouched.")

    def _path_row(self, parent, row, label, variable, browse_command, button_text):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text=button_text, command=browse_command).grid(row=row, column=2, sticky="e", padx=(8, 0), pady=3)
        if variable is self.output_iso_var:
            variable.trace_add("write", self._output_changed)
            entry.bind("<KeyRelease>", self._output_typed)
        elif variable is self.report_var:
            entry.bind("<KeyRelease>", self._report_typed)

    def _schedule_calculator_refresh(self, *_):
        if self._changing_profile:
            return
        # Text pasted/typed into paths receives the same automatic calculation
        # as files selected from Browse. Debounce partial typing on Windows.
        if self.running:
            return
        if self._auto_refresh_job is not None:
            self.root.after_cancel(self._auto_refresh_job)
        self._auto_refresh_job = self.root.after(400, self._deferred_calculator_refresh)

    def _deferred_calculator_refresh(self):
        self._auto_refresh_job = None
        if not self.running:
            self._refresh_audio_preview()

    def _vcs_offset_checkbox_changed(self):
        enabled = bool(self.vcs_adaptive_offsets_var.get())
        self.vcs_exe_fix_button.configure(text=(
            "PATCH VCS EXECUTABLE (EE + ADAPTIVE OFFSETS · EXPERIMENTAL)" if enabled
            else "PATCH VCS EXECUTABLE (OVERFLOW ONLY)"))
        self.vcs_iso_install_button.configure(text=(
            "INSTALL VCS RADIOS + OFFSET ELF INTO NEW ISO" if enabled
            else "INSTALL VCS RADIO(S) INTO NEW ISO"))

    def _apply_profile_controls(self):
        """Keep unimplemented game writers visibly unavailable and fail-closed."""
        profile = get_profile(self.profile_key)
        if self.running:
            return
        self.profile_summary_var.set(profile.description)
        self.profile_status_var.set('ISO patch writer available (experimental)' if profile.can_build
                                    else 'VCS ISO radio installer · optional experimental shared offsets; IOP preserved')
        self.validate_button.configure(text='Validate inputs' if profile.can_build else 'Analyze VCS files', state='normal')
        self.run_button.configure(state='normal' if profile.can_build and patcher and iso_patcher else 'disabled')
        self.offset_mode_combo.configure(state='readonly' if profile.can_build else 'disabled')
        self.vcs_exe_fix_button.configure(state='normal' if self.profile_key == 'vcs_ps2' else 'disabled')
        self.vcs_irx_fix_button.configure(state='normal' if self.profile_key == 'vcs_ps2' else 'disabled')
        self.vcs_iso_install_button.configure(state='normal' if self.profile_key == 'vcs_ps2' else 'disabled')
        self.vcs_offset_checkbox.configure(state='normal' if self.profile_key == 'vcs_ps2' else 'disabled')
        self._vcs_offset_checkbox_changed()
        self.radio_gain_checkbox.configure(state='normal')
        self.single_station_combo.configure(state='readonly' if self.profile_key == 'vcs_ps2' else 'disabled')
        if profile.can_build:
            self.game_warning_var.set('LCS PS2: experimental ELF and CDSTREAM.IRX patches; original source required. PCSX2 gameplay remains unverified.')
            self.game_inputs_help_var.set('LCS profile: SLUS_214.23 and original MODULES/CDSTREAM.IRX; ten radio .VB streams. Optional ENGLISH.GXT / GAME.DTZ in the VB folder are included automatically. Originals are never overwritten.')
        else:
            self.game_warning_var.set('VCS PS2 target: 1 sec–4h39m per station. Exact-revision EE DIVU fix covers aligned streams to 4:39:37.152; IOP signed positions have an experimental MIPS fix; loader behaviour, EOF and ISO building remain unverified.')
            self.game_inputs_help_var.set('VCS ISO installer: choose a filesystem-consistent US ISO and one or more station VBs. Optional +3 dB affects every detected replacement. The NEW EXPERIMENTAL offset checkbox patches the game executable only when it is original or exactly EE-DIVU-only; any other pre-existing executable changes are refused. CDSTREAM.IRX is always preserved.')

    def _profile_changed(self, _event=None):
        if self.running:
            self.profile_var.set(get_profile(self.profile_key).display)
            return
        target = PROFILE_BY_DISPLAY.get(self.profile_var.get())
        if target is None:
            raise ValueError('Unknown GTA radio game profile')
        if target.key == self.profile_key:
            return
        self._profile_paths[self.profile_key] = (
            self.input_iso_var.get(), self.input_exe_var.get(), self.radio_dir_var.get(),
            self.output_iso_var.get(), self.report_var.get(), self.input_vcs_irx_var.get(),
            self.output_custom, self.report_custom,
        )
        self._changing_profile = True
        try:
            self.profile_key = target.key
            iso, exe, audio, output, report, irx, out_custom, rep_custom = self._profile_paths.get(
                target.key, ('', '', '', '', '', '', False, False))
            for var, value in zip((self.input_iso_var, self.input_exe_var,
                                   self.radio_dir_var, self.output_iso_var, self.report_var, self.input_vcs_irx_var),
                                  (iso, exe, audio, output, report, irx)):
                var.set(value)
            self.output_custom, self.report_custom = out_custom, rep_custom
        finally:
            self._changing_profile = False
        self._invalidate_iso_cache()
        self.preview_metrics = {}
        self.current_calculation = None
        self._populate_empty_rows()
        self._apply_profile_controls()
        self.validation_var.set('Not validated' if target.can_build else 'Analysis only')
        self.status_var.set('Selected ' + target.display)
        self._refresh_audio_preview()
        self._log('Selected profile: ' + target.display)

    def _vcs_calculation(self):
        """Calculate using VCS-only station keys, original sizes and arithmetic."""
        backend = get_backend('vcs_ps2')
        files = self._replacement_map()
        replacements = {
            key: files[filename.upper()].stat().st_size
            for key, filename in backend.profile.stations
            if filename.upper() in files
        }
        originals = {}
        iso_text = self.input_iso_var.get().strip()
        if iso_text:
            exe_text = self.input_exe_var.get().strip()
            originals = backend.inspect_original_source(
                Path(iso_text), Path(exe_text) if exe_text else None)
        return backend.calculate(originals, replacements)

    def _refresh_vcs_preview(self):
        """Dispatch to the independent VCS PS2 calculator, never the LCS plan."""
        self.preview_metrics = {}
        self.current_calculation = None
        backend = get_backend('vcs_ps2')
        try:
            plan = self._vcs_calculation()
        except (OSError, ValueError, KeyError) as exc:
            self.calculation_var.set(f'VCS calculator: {exc}')
            for key, name in backend.profile.stations:
                self.tree.item(key, values=(key.upper(), name, '—', '—', '—', '—',
                                            str(exc)), tags=('bad',))
            return
        self.current_calculation = plan
        for row in plan.stations:
            if row.installed_size is None:
                self.tree.item(row.station, values=(row.station.upper(), row.filename,
                     '—', '—', '—', '—', 'Original VCS ISO not supplied'), tags=('missing',))
                continue
            source = 'Replacement' if row.source_size is not None else 'Original VCS ISO'
            display_size = f'{row.installed_size:,}'
            duration = format_duration_ms(row.ee_boundary_ms)
            boundary = f'{row.ee_boundary_ms:,} ms (VCS model)'
            status = 'VCS EE range assessment'
            if row.classification in ('longer', 'shorter', 'same-size'):
                status += f'; {row.classification}'
            if row.original_size is None:
                status += '; stock length unknown'
            if not row.aligned_8kb:
                status += '; WARNING: not 8 KiB aligned'
            if row.position_operand_risk_if_raw_bytes:
                status += '; possible signed-position overflow'
            if row.duration_overflow_risk:
                status += '; duration instruction overflow'
            if row.ee_signed_duration_to_bytes_overflow:
                status += '; EE signed-duration overflow (fix available in range)'
            if row.ee_divu_fix_range_supported is False:
                status += '; EE DIVU-only fix cannot handle this length'
            if row.source_size is not None and not row.within_vcs_1s_to_4h39_target_allocation:
                status += '; OUTSIDE VCS 1-second–4h39m target/allocation (16KiB pairs required)'
            self.tree.item(row.station, values=(row.station.upper(), row.filename, source,
                display_size, duration, boundary, status), tags=('ok',))
            self.preview_metrics[row.station] = {
                'size_bytes': row.installed_size,
                'duration_ms': row.ee_boundary_ms,
            }
        self.calculation_var.set(
            f'VCS PS2 calculator: {plan.selected_replacement_count} replacement(s) / '
            f'{len(plan.stations)} stations | longest known VB '
            f'{plan.largest_known_size if plan.largest_known_size is not None else "unknown"} bytes | '
            f'32-bit IOP duration risk {"YES" if plan.duration_instruction_overflow_risk else "no"} | '
            f'EE duration-to-byte overflow {"YES" if plan.ee_signed_duration_to_bytes_overflow else "no"} | '
            f'DIVU-only fix covers all known {"yes" if plan.ee_divu_fix_range_supported_for_all_known_streams else "NO"} | '
            f'VCS selected VB target range {"yes" if plan.all_selected_replacements_within_target else "NO"} | '
            f'conditional signed position risk {"YES" if plan.signed_position_operand_risk_if_raw_bytes else "no"} | '
            f'original size lookup {"complete" if plan.original_sizes_complete else "incomplete"}. '
            'Read-only; VCS runtime seek operands and writer not validated.')

    def _analyze_vcs_clicked(self):
        """Run the VCS backend only; no LCS ISO/code patches."""
        self._log('--- VCS profile: game-specific calculation and validation ---')
        try:
            self._refresh_vcs_preview()
            plan = self.current_calculation
            if plan is None:
                raise ValueError('VCS inputs could not be calculated (see preview).')
            if plan.selected_replacement_count == 0:
                raise ValueError('Choose a VCS radio .VB replacement or folder first.')
            for row in plan.stations:
                if row.source_size is None:
                    continue
                self._log(f'{row.filename}: {row.installed_size:,} bytes → '
                          f'{format_duration_ms(row.ee_boundary_ms)}; {row.classification}; '
                          f'signed-position risk: {row.position_operand_risk_if_raw_bytes}')
            exe = self.input_exe_var.get().strip()
            if exe:
                details = get_backend('vcs_ps2').inspect_executable(Path(exe))
                self._log(f"VCS executable recognized: {details['recognized_reference']} ({details['sha256']})")
                if not details['recognized_reference']:
                    raise ValueError('VCS executable reference SHA-256 does not match the analyzed SLUS_215.90')
            self.validation_var.set('VCS analyzed')
            self.status_var.set('VCS-specific analysis completed; no game files modified.')
            messagebox.showinfo('GTA VCS static analysis complete',
                'VCS calculator and checks completed. See station table and log.\n\n'
                'VCS executable/ISO patching remains disabled until the VCS writer is validated.')
        except (OSError, ValueError) as exc:
            self.validation_var.set('Analysis failed')
            self._log('VCS analysis failed: ' + str(exc))
            messagebox.showerror('VCS analysis failed', str(exc))

    def _selected_offset_mode(self) -> str:
        label = self.offset_mode_var.get()
        try:
            return OFFSET_MODE_LABELS[label]
        except KeyError as exc:
            raise ValueError(f"Unknown offset mode: {label}") from exc

    def _offset_mode_changed(self, _event=None):
        if self.profile_key != 'lcs_ps2':
            return
        mode = self._selected_offset_mode()
        self.offset_mode_help_var.set(OFFSET_MODE_DESCRIPTIONS[mode])
        if not self.running:
            self._refresh_audio_preview()

    def _populate_empty_rows(self):
        self.tree.delete(*self.tree.get_children())
        for key, filename in get_profile(self.profile_key).stations:
            self.tree.insert("", "end", iid=key, values=(key.upper(), filename, "—", "—", "—", "—", "Choose input ISO"), tags=("missing",))

    def _invalidate_iso_cache(self):
        self.cached_iso_info = None
        self.cached_iso_signature = None

    def _get_iso_info(self, force=False):
        if self.profile_key != 'lcs_ps2':
            raise ValueError('VCS must never be inspected as an LCS ISO')
        text = self.input_iso_var.get().strip()
        if not text:
            raise ValueError("Choose the input PS2 ISO.")
        path = Path(text)
        if not path.is_file():
            raise ValueError(f"Input ISO not found: {path}")
        stat = path.stat()
        exe_text = self.input_exe_var.get().strip()
        exe_signature = None
        if exe_text and Path(exe_text).is_file():
            exe_path = Path(exe_text)
            exe_stat = exe_path.stat()
            exe_signature = (str(exe_path.resolve()), exe_stat.st_size, exe_stat.st_mtime_ns)
        signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, exe_signature)
        if not force and self.cached_iso_info is not None and signature == self.cached_iso_signature:
            return self.cached_iso_info
        info = get_backend(self.profile_key).inspect_iso(path, Path(exe_text) if exe_text else None)
        self.cached_iso_signature = signature
        self.cached_iso_info = info
        return info

    def _replacement_map(self):
        folder_text = self.radio_dir_var.get().strip()
        if not folder_text:
            return {}
        folder = Path(folder_text)
        if not (folder.is_dir() or (folder.is_file() and folder.suffix.upper() == '.VB')):
            raise ValueError(f"Replacement folder or .VB file not found: {folder}")
        result = {}
        recognized = {filename for _key, filename in get_profile(self.profile_key).stations}
        candidates = [folder] if folder.is_file() else folder.iterdir()
        for candidate in candidates:
            if not candidate.is_file():
                continue
            filename = candidate.name.upper()
            if filename.endswith(".VB") and filename not in recognized:
                raise ValueError(f"Unrecognized .VB file {candidate.name}; rename it to a supported station filename")
            if filename in result:
                raise ValueError(f"Duplicate station filename differing only in case: {result[filename].name}, {candidate.name}")
            result[filename] = candidate
        return result

    def _replacement_file_signature(self):
        """Cheap metadata poll, not an audio read and never a default file size.

        A user may replace a .VB in place without reselecting its GUI path.
        Include ctime and mtime to notice same-size edits as well as size edits;
        final build verification still hashes the copied bytes.
        """
        text = self.radio_dir_var.get().strip()
        if not text:
            return ('none',)
        path = Path(text)
        try:
            if path.is_file():
                st = path.stat()
                return ('file', str(path.resolve()), st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if path.is_dir():
                files = []
                for file in path.iterdir():
                    if file.is_file() and file.suffix.upper() == '.VB':
                        st = file.stat()
                        files.append((file.name, st.st_size, st.st_mtime_ns, st.st_ctime_ns))
                return ('folder', str(path.resolve()), tuple(sorted(files)))
        except OSError as exc:
            return ('error', str(path), str(exc))
        return ('missing', str(path))

    def _poll_replacement_file_changes(self):
        """Refresh displayed plan automatically after disk edits, without UI action."""
        try:
            signature = self._replacement_file_signature()
            if not self.running and signature != self._last_audio_signature:
                self._refresh_audio_preview()
        finally:
            self.root.after(1500, self._poll_replacement_file_changes)

    def _show_selected_audio_without_iso(self, iso_error):
        """Show actual selected VB measurements even before an ISO is chosen.

        A complete ten-station/IOP plan requires original sizes from the ISO;
        never fabricate those sizes or pretend this partial preview is patchable.
        """
        self.calculation_var.set('Selected VB sizes measured; choose a valid original ISO to calculate the ten-station patch plan.')
        try:
            replacements = self._replacement_map()
        except Exception as exc:
            for key, filename in patcher.STATIONS:
                self.tree.item(key, values=(key.upper(), filename, '—', '—', '—', '—', str(exc)), tags=('bad',))
            return
        from lcs_iop_safe_seek import duration_ms as duration_from_installed_size
        for key, filename in patcher.STATIONS:
            source = replacements.get(filename.upper())
            if source is None:
                self.tree.item(key, values=(key.upper(), filename, '—', '—', '—', '—',
                                            'Select original ISO'), tags=('missing',))
                continue
            try:
                source_size, _ = patcher.duration_ms_from_raw_stereo_psx_adpcm(source)
                final_size = installed_size(source_size)
                millis = duration_from_installed_size(final_size)
                status = 'Waiting for original ISO' if final_size == source_size else 'Padded; waiting for original ISO'
                display_size = f'{source_size:,} → {final_size:,}' if final_size != source_size else f'{source_size:,}'
                self.tree.item(key, values=(key.upper(), filename, 'Replacement', display_size,
                                            format_duration_ms(millis), f'{millis:,} ms (preview)', status), tags=('missing',))
            except (ValueError, OSError) as exc:
                self.tree.item(key, values=(key.upper(), filename, 'Replacement', '—', '—', '—', str(exc)), tags=('bad',))

    def _refresh_audio_preview(self):
        if self.running:
            return
        self._last_audio_signature = self._replacement_file_signature()
        self.preview_metrics = {}
        self.current_calculation = None
        if self.profile_key == 'vcs_ps2':
            self._refresh_vcs_preview()
            return
        if hasattr(self, 'calculation_var'):
            self.calculation_var.set('Calculator awaiting valid input ISO and VB sizes.')
        if patcher is None or iso_patcher is None:
            return
        try:
            info = self._get_iso_info()
            originals = {station["key"]: station for station in info["stations"]}
        except Exception as exc:
            self._show_selected_audio_without_iso(exc)
            return
        try:
            replacements = self._replacement_map()
        except Exception as exc:
            replacements = {}
            for key, filename in patcher.STATIONS:
                if self.tree.exists(key):
                    old = list(self.tree.item(key, "values"))
                    old[6] = str(exc)
                    self.tree.item(key, values=old, tags=("bad",))
            return
        try:
            original_sizes = {key: originals[key]['size_bytes'] for key, _ in patcher.STATIONS}
            replacement_sizes = {key: replacements[filename.upper()].stat().st_size
                                 for key, filename in patcher.STATIONS if filename.upper() in replacements}
            self.current_calculation = get_backend(self.profile_key).calculate(original_sizes, replacement_sizes)
            plan = self.current_calculation
            self.calculation_var.set(
                f'Auto calculator: {len(replacement_sizes)} replacement(s) | '
                f'IOP shifts {plan.iop_shift_right}/{plan.iop_shift_left} '
                f'| duration overflow fix {"YES" if plan.iop_duration_overflow_fix_required else "no"} '
                f'| position overflow fix {"YES" if plan.iop_position_overflow_fix_required else "no"} '
                f'(extra +{plan.iop_extra_shift}) | '
                f'max signed product {plan.maximum_signed_product:,} / {plan.maximum_signed_product_limit:,} | '
                f'ELF: calculated per-station millisecond boundaries'
            )
        except Exception as exc:
            self.calculation_var.set(f'Calculator error: {exc}')
            for key, filename in patcher.STATIONS:
                self.tree.item(key, values=(key.upper(), filename, '—', '—', '—', '—', str(exc)), tags=('bad',))
            return
        calculated_entries = {entry.station: entry for entry in self.current_calculation.stations}
        for key, filename in patcher.STATIONS:
            original = originals[key]
            replacement = replacements.get(filename.upper())
            if replacement is not None:
                source = "Replacement"
                try:
                    source_size, _ = patcher.duration_ms_from_raw_stereo_psx_adpcm(replacement)
                    size = calculated_entries[key].installed_size
                    display_size = f"{source_size:,} → {size:,}" if source_size != size else f"{source_size:,}"
                    duration_ms = calculated_entries[key].ee_boundary_ms
                    tag, status = ("ok", "OK + silent padding to 256 KiB") if source_size < READ_CHUNK_BYTES else ("ok", "OK")
                except Exception as exc:
                    size = replacement.stat().st_size
                    display_size = f"{size:,}"
                    duration_ms = None
                    tag, status = "bad", str(exc)
            else:
                source = "Original ISO"
                size = original["size_bytes"]
                display_size = f"{size:,}"
                try:
                    duration_ms = calculated_entries[key].ee_boundary_ms
                    tag, status = "ok", "Unchanged"
                except Exception as exc:
                    duration_ms = None
                    tag, status = "bad", str(exc)
            duration = "—" if duration_ms is None else format_duration_ms(duration_ms)
            boundary = "—" if duration_ms is None else (f"native: {duration_ms:,} ms" if self._selected_offset_mode() in ("native", "iop-safe-seek") else f"patched: {duration_ms:,} ms")
            if duration_ms is not None:
                # The preview and patcher use this same size-to-duration function.
                # The ISO builder checks these measurements again before writing output.
                self.preview_metrics[key] = {"size_bytes": int(size), "duration_ms": int(duration_ms)}
            self.tree.item(key, values=(key.upper(), filename, source, display_size, duration, boundary, status), tags=(tag,))

    def _browse_input_iso(self):
        path = filedialog.askopenfilename(
            title=f"Select original {get_profile(self.profile_key).game} PS2 ISO" + (" or loose-file ZIP" if self.profile_key == 'vcs_ps2' else ""),
            filetypes=([("VCS original ISO or reference ZIP", "*.iso *.ISO *.zip *.ZIP"), ("All files", "*.*")]
                       if self.profile_key == 'vcs_ps2' else
                       [("ISO disc images", "*.iso *.ISO"), ("All files", "*.*")]),
        )
        if not path:
            return
        self.input_iso_var.set(path)
        self._invalidate_iso_cache()
        if not self.output_custom:
            src = Path(path)
            self.output_iso_var.set(str(src.with_name(src.stem + ".duration-aware.iso")))
        self._refresh_audio_preview()
        self._log(f"Input ISO selected: {path}")

    def _browse_input_exe(self):
        path = filedialog.askopenfilename(
            title=f"Select the pristine {get_profile(self.profile_key).executable} executable",
            filetypes=[("PS2 executable", get_profile(self.profile_key).executable), ("ELF executable", "*.elf *.ELF"), ("All files", "*.*")],
        )
        if not path:
            return
        self.input_exe_var.set(path)
        self._invalidate_iso_cache()
        self._refresh_audio_preview()
        self._log(f"Standalone executable selected: {path}")

    def _browse_vcs_irx(self):
        if self.profile_key != 'vcs_ps2':
            return
        path = filedialog.askopenfilename(title="Select pristine VCS MODULES/CDSTREAM.IRX",
                   filetypes=[("IOP modules", "*.irx *.IRX"), ("All files", "*.*")])
        if path:
            self.input_vcs_irx_var.set(path)
            self._log('VCS CDSTREAM.IRX selected: '+path)

    def _browse_radio_folder(self):
        path = filedialog.askdirectory(title=("Choose LCS radio .VB folder (+ optional ENGLISH.GXT/GAME.DTZ)" if self.profile_key == 'lcs_ps2' else "Choose VCS radio .VB folder (EE overflow assessment)"))
        if path:
            self.radio_dir_var.set(path)
            self._refresh_audio_preview()
            self._log(f"Replacement folder selected: {path}")

    def _browse_single_vb(self):
        path = filedialog.askopenfilename(title=f"Select one {get_profile(self.profile_key).game} .VB radio",
                   filetypes=[("PS2 radio VB", "*.vb *.VB"), ("All files", "*.*")])
        if path:
            self.radio_dir_var.set(path)
            self._refresh_audio_preview()
            self._log(f"Single .VB selected: {path}; calculator recomputed executable and IOP parameters")

    def _browse_output_iso(self):
        initial = self.output_iso_var.get().strip()
        initial_path = Path(initial) if initial else None
        path = filedialog.asksaveasfilename(
            title="Save the patched ISO as a new image",
            defaultextension=".iso",
            initialdir=str(initial_path.parent) if initial_path and initial_path.parent.exists() else None,
            initialfile=initial_path.name if initial_path else "GTA_Radio_Patcher.iso",
            filetypes=[("ISO disc images", "*.iso"), ("All files", "*.*")],
        )
        if path:
            self.output_custom = True
            self.output_iso_var.set(path)

    def _browse_report(self):
        initial = self.report_var.get().strip()
        initial_path = Path(initial) if initial else None
        path = filedialog.asksaveasfilename(
            title="Save JSON build report",
            defaultextension=".json",
            initialdir=str(initial_path.parent) if initial_path and initial_path.parent.exists() else None,
            initialfile=initial_path.name if initial_path else "duration_aware_iso_report.json",
            filetypes=[("JSON report", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.report_custom = True
            self.report_var.set(path)

    def _output_typed(self, _event=None):
        self.output_custom = True

    def _output_changed(self, *_):
        if self.report_custom:
            return
        output = self.output_iso_var.get().strip()
        if output:
            self.report_var.set(output + ".duration_report.json")

    def _report_typed(self, _event=None):
        self.report_custom = True

    def _validate(self):
        errors = []
        if self.profile_key != 'lcs_ps2':
            return ['GTA VCS has its own diagnostic validator; its binary patch writer is not yet verified.']
        if patcher is None or iso_patcher is None:
            return [f"Patcher module could not be loaded: {IMPORT_ERROR}"]
        iso_text = self.input_iso_var.get().strip()
        exe_text = self.input_exe_var.get().strip()
        out_text = self.output_iso_var.get().strip()
        replacement_text = self.radio_dir_var.get().strip()
        report_text = self.report_var.get().strip()
        info = None
        if exe_text and not Path(exe_text).is_file():
            errors.append(f"Input executable not found: {exe_text}")
        elif exe_text:
            try:
                exe_meta = patcher.validate_original_executable(Path(exe_text).read_bytes())
                self._log(f"Standalone executable SHA-256 validated: {exe_meta['sha256']}")
            except Exception as exc:
                errors.append(f"Input executable validation failed: {exc}")

        if not iso_text:
            errors.append("Choose the input PS2 ISO.")
        elif not Path(iso_text).is_file():
            errors.append(f"Input ISO not found: {iso_text}")
        else:
            try:
                info = self._get_iso_info(force=True)
                self._log(f"ISO validated. Boot executable destination: {info['boot_executable_path']}")
                self._log(f"Selected executable SHA-256: {info['executable_sha256']}")
                self._log(f"Original embedded executable SHA-256: {info['embedded_executable_sha256']}")
                if self._selected_offset_mode() in ("native", "iop-safe-seek", "iop-elf-stock", "iop-elf-shared", "iop-elf-station-specific"):
                    if not info.get("embedded_executable_matches_supported", False):
                        errors.append("Selected mode requires an original pristine SLUS_214.23 in the input ISO; cannot build on a previously patched image.")
                    else:
                        try:
                            module = iso_patcher.Iso9660Image(Path(iso_text)).read_entry_bytes("MODULES/CDSTREAM.IRX")
                            from lcs_radio_stream_audit import audit_cdstream_module
                            stream_meta = audit_cdstream_module(module)
                            if stream_meta["sha256"] != "fcb4fff15053652ab162e046d491697fdbf08eeb0954d3d870d6a1b07a2fed43":
                                errors.append("Selected mode requires the original unmodified CDSTREAM.IRX; input ISO module differs.")
                            else:
                                self._log("Original CDSTREAM.IRX verified: native radio length is milliseconds from the ISO directory byte size.")
                        except Exception as exc:
                            errors.append(f"Selected mode needs original MODULES/CDSTREAM.IRX in the ISO: {exc}")
            except Exception as exc:
                errors.append(f"ISO validation failed: {exc}")

        if replacement_text and not (Path(replacement_text).is_dir() or (Path(replacement_text).is_file() and Path(replacement_text).suffix.upper() == '.VB')):
            errors.append(f"Replacement folder or .VB file not found: {replacement_text}")
        elif replacement_text:
            source = Path(replacement_text)
            present = {source.name.upper()} if source.is_file() else {p.name.upper() for p in source.iterdir() if p.is_file()}
            recognized = {filename.upper() for _, filename in patcher.STATIONS}
            replacements_found = present & recognized
            if not replacements_found:
                errors.append("The selected folder contains none of the ten recognized station filenames. Use stock names such as KJAH.VB.")
            else:
                self._log("Replacement files found: " + ", ".join(sorted(replacements_found)))

        replacements = {}
        if replacement_text and (Path(replacement_text).is_dir() or Path(replacement_text).is_file()):
            try:
                replacements = self._replacement_map()
            except (OSError, ValueError) as exc:
                errors.append(f"Replacement folder validation failed: {exc}")
        if info is not None:
            original_by_key = {s["key"]: s for s in info["stations"]}
            for key, filename in patcher.STATIONS:
                override = replacements.get(filename.upper())
                try:
                    if override:
                        patcher.duration_ms_from_raw_stereo_psx_adpcm(override)
                    else:
                        iso_patcher.duration_ms_from_size(original_by_key[key]["size_bytes"], filename)
                except Exception as exc:
                    errors.append(str(exc))

        if not out_text:
            errors.append("Choose an output ISO path.")
        else:
            out = Path(out_text)
            try:
                if iso_text and out.resolve() == Path(iso_text).resolve():
                    errors.append("Output ISO must differ from input ISO.")
                if exe_text and out.resolve() == Path(exe_text).resolve():
                    errors.append("Output ISO must differ from the input executable file.")
            except OSError:
                pass
            if out.suffix.lower() != ".iso":
                errors.append("Output image filename must end in .iso.")
            if report_text:
                try:
                    rp = Path(report_text).resolve()
                    blocked_report_paths = {out.resolve()}
                    if iso_text:
                        blocked_report_paths.add(Path(iso_text).resolve())
                    if exe_text:
                        blocked_report_paths.add(Path(exe_text).resolve())
                    if rp in blocked_report_paths:
                        errors.append("JSON report path must differ from the input ISO, input executable and output ISO.")
                except OSError:
                    pass
        self._refresh_audio_preview()
        if info is not None and self.current_calculation is None:
            errors.append('Automatic VB calculator could not calculate a safe patch plan.')
        if self.current_calculation is not None:
            try:
                iso_patcher.validate_mode_against_calculated_iop_requirements(
                    self.current_calculation, self._selected_offset_mode())
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    def _patch_vcs_irx_clicked(self):
        """Write a separate experimental VCS IOP patch, never the LCS module."""
        if self.profile_key != 'vcs_ps2' or self.running:
            return
        source_text=self.input_vcs_irx_var.get().strip()
        if not source_text or not Path(source_text).is_file():
            messagebox.showerror('Original VCS CDSTREAM.IRX required',
                'Choose the pristine original VCS MODULES/CDSTREAM.IRX first.')
            return
        try:
            from vcs_ee_overflow_patch import validate_radio_sizes
            selections=self._replacement_map()
            sizes={k:selections[name.upper()].stat().st_size
                   for k,name in get_profile('vcs_ps2').stations
                   if name.upper() in selections}
            if sizes:validate_radio_sizes(list(sizes.values()))
        except Exception as exc:
            messagebox.showerror('VCS IRX input rejected',str(exc))
            self._log('ERROR VCS IOP: '+str(exc))
            return
        output=filedialog.asksaveasfilename(
            title='Save NEW experimental VCS CDSTREAM.IRX (not ISO)',
            initialfile='CDSTREAM.vcs_position_fixed.IRX',
            filetypes=[('IOP module','*.IRX *.irx'),('All files','*.*')])
        if not output:return
        try:
            result=get_backend('vcs_ps2').patch_cdstream_position_overflow_only(
                Path(source_text),Path(output),sizes)
            self._log('Experimental VCS CDSTREAM.IRX written: '+output)
            self._log('  Original SHA256: '+result['original_sha256'])
            self._log('  Patched SHA256:  '+result['new_sha256'])
            self.validation_var.set('VCS IRX generated (experimental)')
            self.status_var.set('VCS IOP position fix built; no ISO modification or gameplay validation.')
            messagebox.showinfo('VCS CDSTREAM.IRX generated',
                'VCS-only experimental signed-position correction saved as a NEW IRX.\n\n'
                + output + '\n\n'
                + 'ELF helper and relocation metadata pass offline checks, but '
                + 'the PS2 IOP loader and PCSX2 gameplay are NOT yet verified. '
                + 'The game ISO was not changed.')
        except Exception as exc:
            self._log('ERROR VCS IOP patch: '+str(exc))
            messagebox.showerror('VCS IOP patch failed',str(exc))

    def _patch_vcs_executable_clicked(self):
        """Explicit, separate VCS ELF fix: cannot invoke LCS ISO builder."""
        if self.profile_key != 'vcs_ps2' or self.running:
            return
        source_text = self.input_exe_var.get().strip()
        if not source_text or not Path(source_text).is_file():
            messagebox.showerror('Original VCS executable needed',
                'Choose the original SLUS_215.90 in the Input executable field first. '
                'This fix modifies only a new standalone executable, NOT your game ISO.')
            return
        try:
            from vcs_ee_overflow_patch import validate_radio_sizes
            from vcs_vb_gain_install import _get_replacements
            source = self.radio_dir_var.get().strip()
            override = None if self.single_station_var.get().startswith('Auto (') else self.single_station_var.get()
            if not source:
                sizes = {}
            else:
                selections = _get_replacements(Path(source), override)
                names = {filename.upper(): k for k, filename in get_profile('vcs_ps2').stations}
                sizes = {names[name]: path.stat().st_size for name, path in selections.items()}
            if sizes:
                validate_radio_sizes(list(sizes.values()))
            if self.vcs_adaptive_offsets_var.get() and not sizes:
                raise ValueError('VCS adaptive offsets require at least one selected station VB file')
        except Exception as exc:
            messagebox.showerror('VCS overflow-fix input rejected', str(exc))
            self._log('ERROR VCS overflow-fix: ' + str(exc))
            return
        output = filedialog.asksaveasfilename(
            title=('Save NEW VCS executable with experimental adaptive offsets'
                   if self.vcs_adaptive_offsets_var.get() else 'Save NEW overflow-corrected VCS executable (not ISO)'),
            initialfile=('SLUS_215.90.offsets_experimental'
                         if self.vcs_adaptive_offsets_var.get() else 'SLUS_215.90.overflow_fixed'),
            filetypes=[('PS2 executable', '*.elf *.ELF *.90 *.*'), ('All files', '*.*')])
        if not output:
            return
        try:
            if self.vcs_adaptive_offsets_var.get():
                result = get_backend('vcs_ps2').patch_executable_with_adaptive_offsets(
                    Path(source_text), Path(output), sizes)
                info = result['offsets']
                self._log(f"VCS experimental adaptive offset ELF: {output}")
                self._log(f"  New entries: {info['table_count']}; stride: {info['stride']}; "
                          f"average time distribution: {info['average_distribution_spacing_ms']} ms")
                self._log(f"  New table VA {result['table_va']}; original load end: {result['original_ee_pt_load_memory_end']}")
                self._log('  HEAP/LOADER RISK UNVERIFIED: use disposable ISO and test in PCSX2.')
                self.validation_var.set('VCS adaptive ELF generated (experimental)')
                self.status_var.set('VCS standalone adaptive ELF generated; game ISO unchanged.')
                messagebox.showwarning('Experimental VCS adaptive-offset ELF created',
                    f"New VCS executable saved: {output}\n\n{info['table_count']} shared offsets, "
                    f"~{info['average_distribution_spacing_ms']} ms apart in distribution.\n\n"
                    'This patch preserves/applies the known EE DIVU corrections. The standalone file '
                    'is NOT installed into your ISO, and the separate CDSTREAM.IRX fix is unchanged. '
                    'Runtime heap/loader safety and reduced audible repeats have NOT been verified. '
                    'Use a disposable test image only.')
            else:
                result = get_backend('vcs_ps2').patch_executable_overflow_only(
                    Path(source_text), Path(output), sizes)
                self._log('VCS EE overflow fix written: ' + str(output))
                for site in result['patches']:
                    self._log(f"  {site['name']}: {site['original_opcode']} -> {site['patched_opcode']} at {site['runtime_address']}")
                self.validation_var.set('VCS ELF corrected')
                self.status_var.set('Standalone VCS ELF fixed; ISO installation and runtime tests still required.')
                messagebox.showinfo('VCS executable written',
                    'VCS 1-second–4h39m target: two signed divisions changed to unsigned for nonnegative time values.\n\n'
                    + str(output) + '\n\nThis does NOT fix your ISO filesystem, '
                    'CDSTREAM.IRX signed-position arithmetic or 5-hour overflows. '
                    'Gameplay remains unverified.')
        except Exception as exc:
            self._log('ERROR VCS overflow-fix: ' + str(exc))
            messagebox.showerror('Could not patch VCS executable', str(exc))

    def _vcs_install_clicked(self):
        """Audio-only US VCS ISO builder for any station or set of stations."""
        if self.profile_key != 'vcs_ps2' or self.running:
            return
        try:
            selected_text = self.radio_dir_var.get().strip()
            if not selected_text:
                raise ValueError('Select a supported radio VB or a folder containing station .VB files.')
            source = Path(selected_text)
            if source.is_file() and source.suffix.lower() != '.vb':
                raise ValueError('Single radio replacement must be a .VB file.')
            override = None if self.single_station_var.get().startswith('Auto (') else self.single_station_var.get()
            if source.is_dir() and override:
                raise ValueError('Station selection override is for one VB only. Use named VB files for folders.')
            from vcs_vb_gain_install import _get_replacements
            station_map = _get_replacements(source, override)
            input_iso = Path(self.input_iso_var.get().strip())
            output_iso = Path(self.output_iso_var.get().strip())
            if not input_iso.is_file():
                raise ValueError('Choose an existing VCS PS2 US input ISO.')
            if not str(output_iso) or str(output_iso) == '.':
                raise ValueError('Choose a NEW output ISO path.')
            if output_iso.exists():
                raise ValueError('Output ISO already exists. Overwriting is disabled.')
            report_value = self.report_var.get().strip()
            report = Path(report_value) if report_value else None
            if report and report.exists():
                raise ValueError('Report already exists. Overwriting is disabled.')
            boost = bool(self.boost_radio_3db_var.get())
            expand_vcs_offsets = bool(self.vcs_adaptive_offsets_var.get())
        except Exception as exc:
            self._log('ERROR VCS audio ISO: ' + str(exc))
            messagebox.showerror('VCS ISO inputs invalid', str(exc))
            return
        names = ', '.join(sorted(station_map))
        msg = (f'Create a NEW VCS ISO with {len(station_map)} radio station(s): {names}?\n\n'
               + ('+3 dB ENABLED: decode, amplify and re-encode EACH selected VB.\n'
                  if boost else 'Gain OFF: install audio unchanged.\n')
               + ('\nEXPERIMENTAL VCS OFFSET ELF ENABLED: adds new ELF segment; only recognized '
                  'original or DIVU-only executable is permitted, unknown EE patches are REJECTED. '
                  'Heap/loader safety and in-game repeat improvement UNVERIFIED.\n' if expand_vcs_offsets
                  else '\nAUDIO-ONLY: existing EE and IOP patches are preserved.\n')
               + 'CDSTREAM.IRX is preserved byte-for-byte. Older anomalous/overlapping ISO records are rejected.\n\n'
                 'Originals are never overwritten. Proceed?')
        if not messagebox.askyesno('VCS radio audio-only ISO installation', msg, icon='warning'):
            return
        self.running = True
        self.validation_var.set('VCS ISO building')
        self.status_var.set('Preparing station audio and building a NEW VCS ISO…')
        self.progress.start(10)
        self.profile_combo.configure(state='disabled')
        self.vcs_iso_install_button.configure(state='disabled')
        self._log(f'VCS installer {names}: +3dB = {boost}; experimental offsets = {expand_vcs_offsets}')
        def work():
            try:
                from vcs_vb_gain_install import build_radio_audio_iso
                result = build_radio_audio_iso(input_iso=input_iso, replacement_vb=source,
                    output_iso=output_iso, station=override, boost_radio_3db=boost,
                    expand_vcs_offsets=expand_vcs_offsets,
                    report_path=report, progress=lambda line:self.queue.put(('line',line)))
                self.queue.put(('vcs_audio_done', result))
            except Exception as exc:
                self.queue.put(('failed', str(exc)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _validate_clicked(self):
        if self.profile_key == 'vcs_ps2':
            return self._analyze_vcs_clicked()
        self._log("--- Validating input ISO, native duration provider and audio ---")
        errors = self._validate()
        if errors:
            self.validation_var.set(f"{len(errors)} issue(s)")
            self.status_var.set("Validation failed. Fix the listed issue(s) before building the output ISO.")
            for err in errors:
                self._log("ERROR: " + err)
            messagebox.showerror("Input validation failed", "\n\n".join(errors[:12]) + ("\n\nMore errors are shown in the log." if len(errors) > 12 else ""))
        else:
            self.validation_var.set("Validated")
            self.status_var.set("Input ISO, supported executable and final station durations passed static validation.")
            self._log("Validation successful. The patch button is ready; output ISO will be written separately.")
            mode = self._selected_offset_mode()
            self._log(f"Offset mode: {mode}")
            messagebox.showinfo(
                "Validation successful",
                "The ISO filesystem, original executable/IOP checks (native mode) and station durations passed validation.\n\n"
                "Only station files present in the replacement folder will be installed; missing stations remain original.\n\n"
                + OFFSET_MODE_DESCRIPTIONS[mode] + "\n\n"
                + "The original CDSTREAM.IRX function 0x1FDC confirms the millisecond unit from ISO file-size bytes. Native mode does not patch SLUS_214.23. Runtime playback with large files still requires gameplay verification.",
            )

    def _run_clicked(self):
        if self.profile_key != 'lcs_ps2':
            # Fail closed even if invoked directly despite its disabled GUI button.
            get_backend(self.profile_key).build()
        if self.running:
            return
        self._log("--- Pre-build validation ---")
        errors = self._validate()
        if errors:
            self.validation_var.set(f"{len(errors)} issue(s)")
            self.status_var.set("Cannot patch/build until validation errors are corrected.")
            for err in errors:
                self._log("ERROR: " + err)
            messagebox.showerror("Cannot build ISO", "\n\n".join(errors[:12]))
            return

        preview_metrics = {key: dict(value) for key, value in self.preview_metrics.items()}
        if set(preview_metrics) != {key for key, _ in patcher.STATIONS}:
            messagebox.showerror("Cannot build ISO", "The GUI could not calculate duration values for all ten stations. Refresh the preview and correct validation errors first.")
            self._log("ERROR: GUI preview metrics are incomplete; refusing to patch the executable.")
            return

        input_iso = Path(self.input_iso_var.get().strip())
        input_executable = Path(self.input_exe_var.get().strip()) if self.input_exe_var.get().strip() else None
        output_iso = Path(self.output_iso_var.get().strip())
        replacement_text = self.radio_dir_var.get().strip()
        replacement_dir = Path(replacement_text) if replacement_text else None
        report_text = self.report_var.get().strip()
        report = Path(report_text) if report_text else Path(str(output_iso) + ".duration_report.json")

        existing = [str(p) for p in (output_iso, report) if p.exists()]
        if existing and not messagebox.askyesno("Overwrite existing output?", "These output files already exist:\n\n" + "\n".join(existing) + "\n\nOverwrite them? The input ISO will remain unchanged."):
            return
        offset_mode = self._selected_offset_mode()
        gain_enabled = bool(self.boost_radio_3db_var.get())
        # Show exactly which radio replacements will receive gain; the full
        # ten-station duration preview also contains untouched stock stations.
        detected_station_names = []
        if replacement_dir is not None:
            sources = ([replacement_dir] if replacement_dir.is_file() else
                       sorted((p for p in replacement_dir.iterdir() if p.is_file()), key=lambda p: p.name.upper()))
            file_names = {p.name.upper() for p in sources}
            detected_station_names = [name for _, name in patcher.STATIONS if name.upper() in file_names]
        detected_summary = (', '.join(detected_station_names) if detected_station_names else '(none)')
        warning = (
            ("Combined mode: patches BOTH the EE executable and CDSTREAM.IRX in one ISO.\n\n" if offset_mode.startswith("iop-elf-") else ("IOP-only control: preserves the original ELF.\n\n" if offset_mode in ("native", "iop-safe-seek") else "Legacy experimental ELF-only patch.\n\n"))
            + "Any .VB station file and optional ENGLISH.GXT or GAME.DTZ companion in the replacement folder will be installed. Unlisted stations and companions stay as in the input ISO. "
            "Larger files may be appended to the ISO, with their ISO directory records and volume size updated.\n\n"
            "The game uses milliseconds for station duration (proved in original CDSTREAM.IRX); compatibility with extended streams still requires normal gameplay testing.\n\n"
            f"Selected offset mode: {offset_mode}.\n{OFFSET_MODE_DESCRIPTIONS[offset_mode]}\n\n"
            + ("CAUTION: Adaptive mode adds an ELF segment just 52,888 bytes above the original program memory end. Unknown heap/buffer activity could overwrite the injected segment.\n\n" if offset_mode in ("shared", "station-specific", "iop-elf-shared", "iop-elf-station-specific") else "")
            + ("Station-specific mode also injects executable code and requires a fresh PCSX2 cold-boot test.\n\n" if offset_mode in ("station-specific", "iop-elf-station-specific") else "")
            + (f"Detected {len(detected_station_names)} radio replacement(s): {detected_summary}.\n"
               + ("+3 dB ENABLED for every detected replacement; untouched stations excluded. May clip.\n\n"
                  if gain_enabled else "Gain disabled.\n\n"))
            + "Proceed and test only with a disposable ISO?"
        )
        if not messagebox.askyesno("Patch executable and build experimental ISO?", warning, icon="warning"):
            return

        self._log(f"Input ISO: {input_iso}")
        self._log(f"Input executable (optional; original ISO used by default): {input_executable}")
        self._log(f"Output ISO: {output_iso}")
        self._log(f"Offset mode: {offset_mode}")
        self._log(f"Detected radio replacements ({len(detected_station_names)}): {detected_summary}; "
                  f"+3 dB to each = {gain_enabled}")
        self._log("Exact GUI preview values used to verify the selected installation mode:")
        for key, filename in patcher.STATIONS:
            metric = preview_metrics[key]
            self._log(f"  {filename}: {metric['size_bytes']:,} bytes -> {metric['duration_ms']:,} ms ({format_duration_ms(metric['duration_ms'])})")
        self._log("Starting executable patch and ISO build; large ISO files may take some time to copy.")
        self.status_var.set("Patching executable and building output ISO…")
        self.validation_var.set("Running")
        self.profile_combo.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.validate_button.configure(state="disabled")
        self.progress.start(10)
        self.running = True

        def worker_fn():
            try:
                result = get_backend("lcs_ps2").build(
                    input_iso=input_iso,
                    replacement_dir=replacement_dir,
                    output_iso=output_iso,
                    report_path=report,
                    progress=lambda message: self.queue.put(("line", message)),
                    executable_path=input_executable,
                    offset_mode=offset_mode,
                    expected_preview_metrics=preview_metrics,
                    boost_radio_3db=gain_enabled,
                )
                self.queue.put(("done", result))
            except Exception as exc:
                self.queue.put(("failed", str(exc)))

        self.worker = threading.Thread(target=worker_fn, daemon=True)
        self.worker.start()

    def _drain_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "line":
                    self._log(item[1])
                elif kind == "done":
                    result = item[1]
                    self.running = False
                    self.progress.stop()
                    self.profile_combo.configure(state="readonly")
                    self._apply_profile_controls()
                    output = Path(result["output_iso"])
                    report = Path(result["report_path"]) if result.get("report_path") else None
                    if output.is_file():
                        self.validation_var.set("ISO created")
                        self.status_var.set(f"Patched ISO written to {output}")
                        self._log(f"SUCCESS: Output ISO verified ({output.stat().st_size:,} bytes).")
                        self._log(f"ISO files updated: {len(result.get('iso_files_replaced', []))}")
                        for name, gain in result.get('gain_by_station', {}).items():
                            self._log(f'{name}: +3 dB requested, measured decoded RMS {gain["measured_decoded_rms_gain_db"]:.3f} dB; '
                                      f'clipped target samples {gain["target_clipped_samples"]:,}/{gain["samples"]:,}')
                        offset_info = result.get("executable_patch_report", {}).get("offset_sequence_strategy", {})
                        chosen_mode = result.get("offset_mode", "unknown")
                        self._log(f"Offset mode applied: {chosen_mode}")
                        exe_report = result.get("executable_patch_report", {})
                        self._log("Native computed durations (no ELF modifications):" if chosen_mode in ("native", "iop-safe-seek") else "+0x18 values written into the executable:")
                        for station in exe_report.get("stations", []):
                            self._log(
                                f"  {station.get('station')}: {station.get('size_bytes', 0):,} bytes -> "
                                f"{(station.get('boundary_value_written') or station.get('duration_ms', 0)):,} ms"
                            )
                        if chosen_mode == "station-specific":
                            self._log("Repeat-optimized station-specific sequences installed with independent runtime cursors; terminal zero is skipped at wrap.")
                            self._log(f"Helper VA: {offset_info.get('helper_va')}; call-site hook applied: {offset_info.get('station_specific_hook_applied')}.")
                            for station_name, entry_count in (offset_info.get("per_station_entry_counts") or {}).items():
                                self._log(f"{station_name}: {entry_count} adaptive offsets for its own duration.")
                            self._log("WARNING: station-specific mode uses an injected MIPS helper and RWX PT_LOAD; confirm boot/playback in PCSX2.")
                        elif chosen_mode == "shared":
                            self._log(f"Repeat-optimized shared offset count: {offset_info.get('entry_count')} (stock runtime-selectable count: {offset_info.get('stock_entry_count_available_to_runtime')}).")
                            self._log(f"Shared cursor cycle length: {offset_info.get('shared_event_cycle_length')}; reads per rebase: {offset_info.get('shared_cursor_reads_per_rebase')} (one selected offset is applied to all ten stations).")
                            self._log(f"Exact modulo-residue collisions across radios: {sum(v.get('duplicate_residues', 0) for v in offset_info.get('modulo_residue_analysis', {}).values())}.")
                            self._log(f"Shared offsets span longest station duration {offset_info.get('longest_duration_ms'):,} ms with target average spacing ~{offset_info.get('target_spacing_ms', 0):,.1f} ms.")
                            self._log(f"Offset table relocated to VA {offset_info.get('table_va')} in second PT_LOAD; confirm this loads in PCSX2.")
                            stock_residues = offset_info.get("stock_modulo_residue_analysis", {})
                            adaptive_residues = offset_info.get("modulo_residue_analysis", {})
                            longest_name = offset_info.get("longest_station")
                            if longest_name in adaptive_residues and longest_name in stock_residues:
                                before = stock_residues[longest_name]
                                after = adaptive_residues[longest_name]
                                self._log(
                                    f"{longest_name} circular offset-gap range: stock {before['minimum_cyclic_gap_ms']:,}–{before['maximum_cyclic_gap_ms']:,} ms; "
                                    f"optimized shared {after['minimum_cyclic_gap_ms']:,}–{after['maximum_cyclic_gap_ms']:,} ms."
                                )
                                transitions = offset_info.get("sequence_transition_analysis", {}).get(longest_name, {})
                                if transitions:
                                    self._log(f"{longest_name} smallest next-offset step in read order: {transitions.get('minimum_next_offset_gap_ms'):,} ms.")
                        elif chosen_mode in ("native", "iop-safe-seek"):
                            self._log("Original ELF preserved. CDSTREAM.IRX obtains native duration from ISO9660 file sizes.")
                            if chosen_mode == "iop-safe-seek":
                                fix = result.get("iop_seek_patch_report") or {}
                                self._log(f"IOP safe-seek applied: {fix.get('extra_shift')} extra bits, {len(fix.get('changed_instruction_offsets', []))} instructions changed.")
                                self._log("WARNING: static tests passed; booting and uninterrupted playback still need to be established in the game.")
                        elif chosen_mode.startswith("iop-elf-"):
                            self._log("COMBINED: both executable and IOP streaming module patched in the output ISO.")
                            seek = result.get("iop_seek_patch_report") or {}
                            self._log(f"IOP: {len(seek.get('changed_instruction_offsets', []))} instruction changes (extra shift {seek.get('extra_shift')}).")
                            self._log(f"EE: {exe_report.get('length_getter_modified')} duration getter patch; offset policy {result.get('ee_offset_mode')}.")
                            self._log("WARNING: Full PCSX2 playback has not been verified.")
                        else:
                            self._log("Original stock offset sequence retained as an A/B fallback.")
                        for stream_warning in result.get("stream_compatibility_warnings", []):
                            self._log("STREAM WARNING: " + stream_warning)
                        if chosen_mode in ("shared", "station-specific", "iop-elf-shared", "iop-elf-station-specific"):
                            self._log("MEMORY WARNING: additional PT_LOAD is only 52,888 bytes above original executable memory end; runtime heap collision has not been excluded.")
                        report_warning = result.get("report_write_error")
                        if report_warning:
                            self._log("WARNING: " + report_warning)
                        if report and report.is_file() and not report_warning:
                            self._log(f"JSON report: {report}")
                        messagebox.showinfo(
                            "ISO build complete",
                            f"Patched ISO created and its replaced payloads were verified:\n\n{output}\n\n"
                            + (f"JSON report:\n{report}\n\n" if report and not report_warning else "")
                            + (f"WARNING: {report_warning}\n\n" if report_warning else "")
                            + "This confirms static ISO construction, not game/emulator compatibility.",
                        )
                    else:
                        self.validation_var.set("Output not found")
                        self.status_var.set("Build returned success, but the output ISO was not found.")
                        self._log("ERROR: Build returned success but output ISO does not exist.")
                elif kind == "vcs_audio_done":
                    self.running = False
                    self.progress.stop()
                    self.profile_combo.configure(state='readonly')
                    self._apply_profile_controls()
                    result=item[1]
                    self.validation_var.set('VCS radio ISO created')
                    self.status_var.set(('Experimental adaptive-offset ISO created' if result.get('experimental_vcs_adaptive_offsets') else 'Audio-only ISO created') + '; PCSX2 playback not verified')
                    self._log('VCS ISO created: ' + result['output_iso'])
                    for name, gain in result.get('gain_by_station', {}).items():
                        self._log(f'{name}: +3 dB requested; measured RMS gain {gain["measured_decoded_rms_gain_db"]:.3f} dB')
                        self._log(f'Clipped target samples: {gain["target_clipped_samples"]:,} / {gain["samples"]:,}')
                    self._log('Original VCS CDSTREAM.IRX preserved and verified.')
                    if result.get('experimental_vcs_adaptive_offsets'):
                        self._log(f"Expanded VCS EE random-offset table: {result['offset_patch_report']['offsets']['table_count']} values (loader/heap unverified).")
                    else:
                        self._log('VCS executable also preserved and verified.')
                    messagebox.showinfo('VCS ISO built',
                        f'New ISO created with {len(result["detected_replacement_stations"])} radio replacement(s).\n\n'
                        + result['output_iso'] + '\n\n'
                        + ('EXPERIMENTAL offset ELF included: runtime heap/loader safety and repeat reduction NOT verified. '
                           if result.get('experimental_vcs_adaptive_offsets') else 'Existing EE and IRX preserved. ')
                        + 'CDSTREAM.IRX preserved. +3 dB does not change fade-in timing. '
                          'Actual playback is not yet verified in PCSX2.')
                elif kind == "failed":
                    self.running = False
                    self.progress.stop()
                    self.profile_combo.configure(state="readonly")
                    self._apply_profile_controls()
                    self.validation_var.set("Failed")
                    self.status_var.set("ISO build failed. See the log for details.")
                    self._log("ERROR: " + item[1])
                    messagebox.showerror("ISO build failed", item[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


def main():
    root = tk.Tk()
    DurationPatcherGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
