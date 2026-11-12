"""Aplicación de escritorio para el Rigol DS1202Z-E.

Incluye captura de formas de onda, panel de instrumento, navegador de todos
los comandos SCPI de la referencia, consola libre y captura de pantalla.

Uso:
    python osc_app.py
"""

from __future__ import annotations

import base64
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from bootstrap import ensure_runtime

if __name__ == "__main__":
    # Si se lanza con un Python sin dependencias, se reintenta con .venv.
    ensure_runtime()

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

import theme
from fft_analysis import FFTAnalyzer, FFTResult, WINDOWS
from lc_measurement import LCMeasurement, measure_resonance, estimate_q_factor
from rigol_scope import (
    DEFAULT_TIMEOUT_MS,
    DemoScope,
    RigolDS1202Z,
    analyze_waveform,
    discover_resources,
)
from scpi_catalog import ScpiCommand, groups, load_catalog

APP_TITLE = "Rigol DS1202Z-E · Control y captura"
CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capturas")
C = theme.COLORS

DEPTH_MAP = {
    "Auto": "AUTO",
    "12k": "12000",
    "120k": "120000",
    "1.2M": "1200000",
    "12M": "12000000",
    "24M": "24000000",
}
DEPTH_LABEL = {v: k for k, v in DEPTH_MAP.items()}

ACQ_TYPES = {
    "Normal": "NORMal",
    "Promedio": "AVERages",
    "Pico": "PEAK",
    "Alta resolución": "HRESolution",
}
SWEEP_MAP = {"Auto": "AUTO", "Normal": "NORMal", "Single": "SINGle"}
SCREEN_FORMATS = ("PNG", "BMP24", "BMP8", "JPEG", "TIFF")


# ---------------------------------------------------------------------------
# Utilidades de formato
# ---------------------------------------------------------------------------


def fmt_si(value, unit="", digits=4) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(v):
        return "—"
    if v == 0:
        return f"0 {unit}".strip()
    if unit in ("Hz", "Sa/s", "S/s"):
        for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if abs(v) >= factor:
                return f"{v / factor:.{digits}g} {prefix}{unit}"
        return f"{v:.{digits}g} {unit}"
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= factor * 0.999:
            return f"{v / factor:.{digits}g} {prefix}{unit}".strip()
    return f"{v:.{digits}g} {unit}".strip()


def fmt_time_span(seconds: float):
    if seconds <= 0:
        return 1.0, "s"
    if seconds < 1e-3:
        return 1e6, "µs"
    if seconds < 1.0:
        return 1e3, "ms"
    return 1.0, "s"


def decimate_for_plot(t: np.ndarray, v: np.ndarray, max_bins: int = 120_000):
    """Reduce la traza manteniendo mínimo y máximo de cada intervalo."""
    if v.size <= max_bins * 2:
        return t, v
    step = max(1, v.size // max_bins)
    n = (v.size // step) * step
    vr = v[:n].reshape(-1, step)
    tr = t[:n].reshape(-1, step)
    vmin = vr.min(axis=1)
    vmax = vr.max(axis=1)
    tm = tr.mean(axis=1)
    out_t = np.repeat(tm, 2)
    out_v = np.empty(vmin.size * 2, dtype=float)
    out_v[0::2] = vmin
    out_v[1::2] = vmax
    return out_t, out_v


def _truncate(text: str, limit: int = 2000) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… ({len(text) - limit} caracteres más)"


# ---------------------------------------------------------------------------
# Hilo de trabajo (todo el I/O VISA pasa por aquí)
# ---------------------------------------------------------------------------


class Worker(threading.Thread):
    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True, name="scpi-worker")
        self.events = events
        self.jobs: queue.Queue = queue.Queue()
        self.start()

    def submit(self, fn) -> None:
        self.jobs.put(fn)

    def run(self) -> None:
        while True:
            fn = self.jobs.get()
            if fn is None:
                return
            try:
                fn()
            except Exception as exc:  # seguridad extra
                self.events.put(("error", f"Error inesperado: {exc}"))


# ---------------------------------------------------------------------------
# Contenedores reutilizables
# ---------------------------------------------------------------------------


class ScrollFrame(ttk.Frame):
    """Marco con desplazamiento vertical."""

    def __init__(self, master, *, bg=None, **kw):
        super().__init__(master, **kw)
        color = bg or C["bg"]
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=color, borderwidth=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.inner = ttk.Frame(self.canvas, style="Bg.TFrame")
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event):
        if self.inner.winfo_height() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ScpiForm(ttk.Frame):
    """Formulario autogenerado para un comando SCPI de la referencia."""

    def __init__(self, master, run_command):
        super().__init__(master, style="Panel.TFrame")
        self.run_command = run_command
        self.command: ScpiCommand | None = None
        self.vars: dict[str, tk.StringVar] = {}
        self._building = False
        self._result_key = 0
        self.columnconfigure(0, weight=1)

        self.heading = ttk.Label(self, text="Selecciona un comando", style="Heading.TLabel")
        self.heading.grid(row=0, column=0, sticky="w")
        self.mode_label = ttk.Label(self, text="", style="Hint.TLabel")
        self.mode_label.grid(row=1, column=0, sticky="w")
        self.desc_label = ttk.Label(self, text="", style="Muted.TLabel", wraplength=620, justify="left")
        self.desc_label.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        self.examples_label = ttk.Label(self, text="", style="Hint.TLabel", wraplength=620, justify="left")
        self.examples_label.grid(row=3, column=0, sticky="ew")

        self.params_frame = ttk.Frame(self, style="Panel.TFrame")
        self.params_frame.grid(row=4, column=0, sticky="ew", pady=(8, 4))
        self.params_frame.columnconfigure(1, weight=1)

        preview = ttk.Frame(self, style="Panel.TFrame")
        preview.grid(row=5, column=0, sticky="ew", pady=(6, 2))
        preview.columnconfigure(1, weight=1)
        ttk.Label(preview, text="Comando:", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 6), sticky="w")
        self.preview = tk.StringVar()
        self.preview_entry = ttk.Entry(preview, textvariable=self.preview, font=theme.FONT_MONO)
        self.preview_entry.grid(row=0, column=1, sticky="ew")

        buttons = ttk.Frame(self, style="Panel.TFrame")
        buttons.grid(row=6, column=0, sticky="ew", pady=(6, 2))
        self.btn_apply = ttk.Button(buttons, text="Aplicar ajuste", command=lambda: self._send(False))
        self.btn_apply.pack(side="left", padx=(0, 4))
        self.btn_query = ttk.Button(buttons, text="Consultar", command=lambda: self._send(True))
        self.btn_query.pack(side="left", padx=4)
        self.btn_text = ttk.Button(buttons, text="Enviar texto", command=self._send_preview)
        self.btn_text.pack(side="left", padx=4)
        ttk.Label(buttons, text="El comando es editable antes de enviarlo.",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))

        self.result = tk.Text(self, height=3, wrap="word", borderwidth=0, font=theme.FONT_MONO)
        theme.style_text(self.result)
        self.result.grid(row=7, column=0, sticky="ew", pady=(6, 0))
        self.result.configure(state="disabled")

    # -- construcción -------------------------------------------------------

    def show_command(self, command: ScpiCommand | None):
        self.command = command
        for child in self.params_frame.winfo_children():
            child.destroy()
        self.vars.clear()
        self._result_key += 1
        self._set_result("")

        if command is None:
            self.heading.configure(text="Selecciona un comando")
            self.mode_label.configure(text="")
            self.desc_label.configure(text="")
            self.examples_label.configure(text="")
            self.preview.set("")
            for button in (self.btn_apply, self.btn_query, self.btn_text):
                button.configure(state="disabled")
            return

        self.heading.configure(text=command.name)
        self.mode_label.configure(text=f"{command.readable_mode} · {len(command.params)} parámetro(s)")
        self.desc_label.configure(text=command.description or "Sin descripción en la referencia.")
        example = ""
        if command.examples:
            example = "Ejemplo: " + command.examples[0].splitlines()[0]
        self.examples_label.configure(text=_truncate(example, 260))

        seen: dict[str, int] = {}
        for row, param in enumerate(command.params):
            base = param.label
            seen[base] = seen.get(base, 0) + 1
            label = base if seen[base] == 1 else f"{base} ({seen[base]})"
            if param.optional:
                label += " · opcional"
            ttk.Label(self.params_frame, text=label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=2)

            var = tk.StringVar(value=param.default if not param.optional else "")
            self.vars[param.key] = var
            if param.is_index:
                widget = ttk.Combobox(self.params_frame, textvariable=var, state="readonly",
                                      values=command.index_choices(), width=12)
            elif param.choices:
                widget = ttk.Combobox(self.params_frame, textvariable=var, values=param.choices,
                                      width=24)
            else:
                widget = ttk.Entry(self.params_frame, textvariable=var, width=26,
                                   font=theme.FONT_MONO)
            widget.grid(row=row, column=1, sticky="ew", pady=2)
            values_hint = param.values.replace("|", " / ")
            ttk.Label(self.params_frame, text=_truncate(values_hint, 70),
                      style="Hint.TLabel").grid(row=row, column=2, sticky="w", padx=(8, 0))
            var.trace_add("write", lambda *_: self._refresh())

        self.btn_apply.configure(state="normal" if command.supports_set else "disabled")
        self.btn_query.configure(state="normal" if command.supports_query else "disabled")
        self.btn_text.configure(state="normal")
        self._refresh()

    def _collect(self) -> dict[str, str]:
        return {key: var.get().strip() for key, var in self.vars.items()}

    def _refresh(self):
        if self.command is None or self._building:
            return
        self._building = True
        try:
            query = self.command.is_query_only
            self.preview.set(self.command.build(self._collect(), query=query))
        finally:
            self._building = False

    # -- envío --------------------------------------------------------------

    def _send(self, query: bool):
        if self.command is None:
            return
        text = self.command.build(self._collect(), query=query)
        self.preview.set(text)
        self.run_command(text, query, self._show_result)

    def _send_preview(self):
        text = self.preview.get().strip()
        if not text:
            return
        self.run_command(text, "?" in text, self._show_result)

    def _show_result(self, ok: bool, text: str):
        prefix = "→ " if ok else "ERROR: "
        self._set_result(prefix + text)

    def _set_result(self, text: str):
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)
        self.result.configure(state="disabled")


class CommandBrowser(ttk.Frame):
    """Lista filtrable de comandos + formulario autogenerado."""

    def __init__(self, master, commands: list[ScpiCommand], run_command, **kw):
        super().__init__(master, **kw)
        self.commands = commands
        self.run_command = run_command
        self.filtered: list[ScpiCommand] = []
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, style="Panel.TFrame", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(3, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="Buscar", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.search = tk.StringVar()
        search_entry = ttk.Entry(left, textvariable=self.search)
        search_entry.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.search.trace_add("write", lambda *_: self.refresh())
        search_entry.bind("<Escape>", lambda e: self.search.set(""))

        self.group_var = tk.StringVar(value="Todos")
        self.group_combo = ttk.Combobox(left, textvariable=self.group_var, state="readonly")
        self.group_combo.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.group_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        list_frame = ttk.Frame(left, style="Panel.TFrame")
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_frame, height=18, exportselection=False)
        theme.style_listbox(self.listbox)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        self.count = ttk.Label(left, text="", style="Hint.TLabel")
        self.count.grid(row=4, column=0, sticky="w", pady=(4, 0))

        right = ttk.Frame(self, style="Panel.TFrame", padding=10)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.form = ScpiForm(right, run_command)
        self.form.grid(row=0, column=0, sticky="nsew")

        groups_list = ["Todos"] + groups(commands)
        self.group_combo.configure(values=groups_list)
        self.refresh()

    def refresh(self):
        needle = self.search.get().strip().lower()
        group = self.group_var.get()
        self.filtered = []
        for command in self.commands:
            if group and group != "Todos" and command.group != group:
                continue
            if needle and needle not in command.name.lower() and needle not in command.description.lower():
                continue
            self.filtered.append(command)
        self.listbox.delete(0, "end")
        for command in self.filtered:
            self.listbox.insert("end", command.name)
        self.count.configure(text=f"{len(self.filtered)} de {len(self.commands)} comandos")
        if self.filtered:
            self.listbox.selection_set(0)
            self.form.show_command(self.filtered[0])
        else:
            self.form.show_command(None)

    def _on_select(self, _event=None):
        selection = self.listbox.curselection()
        if not selection:
            return
        index = selection[0]
        if 0 <= index < len(self.filtered):
            self.form.show_command(self.filtered[index])


# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------


class OScopeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(900, 560)
        self._fit_screen()
        theme.apply(self)

        self.events: queue.Queue = queue.Queue()
        self.worker = Worker(self.events)
        self.scope = None
        self.settings = None
        self.busy = False
        self.last_waveform = None
        self.last_measurements = {}
        self.last_scpi = {}
        self.last_screen: bytes | None = None
        self.screen_photo = None
        self._action_widgets: list[tk.Widget] = []
        self._loading = False
        self._result_handlers: dict[int, callable] = {}
        self._result_seq = 0
        self._history: list[str] = []
        self._history_pos = 0

        self.catalog = self._load_catalog()
        self._build_vars()
        self._build_ui()
        self.after(80, self._poll_events)
        self.after(200, self.refresh_ports)

    # -- tamaño y catálogo ---------------------------------------------------

    def _fit_screen(self):
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        width = max(900, int(sw * 0.94))
        height = max(560, int(sh * 0.90))
        x = max(0, int((sw - width) / 2))
        y = max(0, int((sh - height) / 2))
        self.geometry(f"{width}x{height}+{x}+{y}")
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

    def _load_catalog(self) -> list[ScpiCommand]:
        try:
            return load_catalog()
        except Exception as exc:
            self.after(300, lambda: messagebox.showwarning(
                "Catálogo SCPI",
                f"No se pudo leer la referencia de comandos:\n\n{exc}"))
            return []

    # -- variables -----------------------------------------------------------

    def _build_vars(self):
        self.port_var = tk.StringVar()
        self.conn_status = tk.StringVar(value="Desconectado")
        self.status_var = tk.StringVar(value="Listo. Conecta el osciloscopio y pulsa «Conectar».")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text = tk.StringVar(value="")

        self.ch_var = tk.StringVar(value="CH1")
        self.ch_display = tk.BooleanVar(value=True)
        self.ch_scale = tk.StringVar(value="0.5")
        self.ch_offset = tk.StringVar(value="0")
        self.ch_coupling = tk.StringVar(value="DC")
        self.ch_probe = tk.StringVar(value="1X")
        self.ch_bw = tk.BooleanVar(value=False)
        self.ch_inv = tk.BooleanVar(value=False)

        self.tb_scale = tk.StringVar(value="0.001")
        self.tb_offset = tk.StringVar(value="0")
        self.tb_mode = tk.StringVar(value="MAIN")

        self.trig_sweep = tk.StringVar(value="Auto")
        self.trig_source = tk.StringVar(value="CH1")
        self.trig_slope = tk.StringVar(value="Subida")
        self.trig_level = tk.StringVar(value="0")
        self.trig_coupling = tk.StringVar(value="DC")

        self.acq_type = tk.StringVar(value="Normal")
        self.acq_avg = tk.StringVar(value="16")
        self.acq_depth = tk.StringVar(value="Auto")

        self.read_mode = tk.StringVar(value="Pantalla")
        self.wf_format = tk.StringVar(value="BYTE")

        self.screen_fmt = tk.StringVar(value="BMP24")
        self.screen_color = tk.BooleanVar(value=True)
        self.screen_invert = tk.BooleanVar(value=False)

        # -- Análisis FFT ---------------------------------------------------
        self.fft_window = tk.StringVar(value="Hann")
        self.fft_top_peaks = tk.StringVar(value="10")
        self.fft_prominence = tk.StringVar(value="6")
        self.fft_min_distance = tk.StringVar(value="3")
        self.fft_use_db = tk.BooleanVar(value=True)
        self.fft_result: Optional[FFTResult] = None

        # -- Medidor L/C ----------------------------------------------------
        self.lc_mode = tk.StringVar(value="L")
        self.lc_known_value = tk.StringVar(value="100e-9")
        self.lc_channel = tk.StringVar(value="CH1")
        self.lc_threshold = tk.StringVar(value="0.10")
        self.lc_result: Optional[LCMeasurement] = None

    # -- interfaz ------------------------------------------------------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_toolbar()
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self._build_capture_tab()
        self._build_instrument_tab()
        self._build_trigger_tab()
        self._build_math_tab()
        self._build_commands_tab()
        self._build_screen_tab()
        self._build_console_tab()
        self._build_fft_tab()
        self._build_lc_tab()
        self._build_statusbar()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 8, 10, 8))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(2, weight=1)

        ttk.Label(bar, text="Recurso VISA", style="Heading.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, state="normal", width=46)
        self.port_combo.grid(row=0, column=1, sticky="w")

        self.btn_refresh = ttk.Button(bar, text="Actualizar", command=self.refresh_ports)
        self.btn_refresh.grid(row=0, column=2, sticky="w", padx=6)
        self.btn_connect = ttk.Button(bar, text="Conectar", command=self.on_connect, style="Accent.TButton")
        self.btn_connect.grid(row=0, column=3, padx=(6, 4))
        self.btn_disconnect = ttk.Button(bar, text="Desconectar", command=self.on_disconnect,
                                         state="disabled")
        self.btn_disconnect.grid(row=0, column=4, padx=(0, 12))

        self.progress = ttk.Progressbar(bar, variable=self.progress_var, maximum=100.0, length=150)
        self.progress.grid(row=0, column=5, padx=(0, 8))
        ttk.Label(bar, textvariable=self.progress_text, width=24,
                  style="Muted.TLabel").grid(row=0, column=6)

    def _build_statusbar(self):
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, textvariable=self.conn_status, style="Status.TLabel", width=34).grid(
            row=0, column=0, sticky="w")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel", anchor="w").grid(
            row=0, column=1, sticky="ew")
        ttk.Label(bar, text="F5 capturar · Ctrl+S guardar · Ctrl+L limpiar consola",
                  style="Status.TLabel").grid(row=0, column=2, sticky="e")

    # -- pestaña Captura -----------------------------------------------------

    def _build_capture_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text="  Captura  ")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab, style="Bg.TFrame", width=340)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_propagate(False)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        scroll = ScrollFrame(left)
        scroll.grid(row=0, column=0, sticky="nsew")
        inner = scroll.inner
        inner.columnconfigure(0, weight=1)

        # Canal
        sec = self._section(inner, "Canal")
        sec.pack(fill="x", pady=(0, 8))
        chrow = ttk.Frame(sec)
        chrow.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        for name in ("CH1", "CH2"):
            ttk.Radiobutton(chrow, text=name, value=name, variable=self.ch_var,
                            command=self._on_channel_change).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(chrow, text="Mostrar", variable=self.ch_display).pack(side="left", padx=(6, 0))
        self._row(sec, 1, "Escala (V/div)", self._entry(sec, self.ch_scale))
        self._row(sec, 2, "Offset (V)", self._entry(sec, self.ch_offset))
        self._row(sec, 3, "Acoplamiento", self._combo(sec, self.ch_coupling, ("DC", "AC", "GND")))
        self._row(sec, 4, "Sonda", self._combo(sec, self.ch_probe,
                                               ("0.01X", "0.1X", "1X", "10X", "100X", "1000X")))
        bwrow = ttk.Frame(sec)
        bwrow.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 2))
        ttk.Checkbutton(bwrow, text="Límite 20 MHz", variable=self.ch_bw).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(bwrow, text="Invertir", variable=self.ch_inv).pack(side="left")
        self._add_action(ttk.Button(sec, text="Aplicar canal", command=self.on_apply_channel),
                         row=6, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        # Base de tiempo
        sec = self._section(inner, "Base de tiempo")
        sec.pack(fill="x", pady=(0, 8))
        self._row(sec, 0, "Escala (s/div)", self._entry(sec, self.tb_scale))
        self._row(sec, 1, "Offset (s)", self._entry(sec, self.tb_offset))
        self._row(sec, 2, "Modo", self._combo(sec, self.tb_mode, ("MAIN", "XY", "ROLL")))
        self._add_action(ttk.Button(sec, text="Aplicar tiempo", command=self.on_apply_timebase),
                         row=3, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        # Disparo
        sec = self._section(inner, "Disparo")
        sec.pack(fill="x", pady=(0, 8))
        self._row(sec, 0, "Barrido", self._combo(sec, self.trig_sweep, ("Auto", "Normal", "Single")))
        self._row(sec, 1, "Fuente", self._combo(sec, self.trig_source, ("CH1", "CH2")))
        self._row(sec, 2, "Flanco", self._combo(sec, self.trig_slope, ("Subida", "Bajada")))
        self._row(sec, 3, "Nivel (V)", self._entry(sec, self.trig_level))
        self._row(sec, 4, "Acoplamiento", self._combo(sec, self.trig_coupling, ("DC", "AC", "LFReject", "HFReject")))
        self._add_action(ttk.Button(sec, text="Aplicar disparo", command=self.on_apply_trigger),
                         row=5, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        # Adquisición
        sec = self._section(inner, "Adquisición")
        sec.pack(fill="x", pady=(0, 8))
        self._row(sec, 0, "Modo", self._combo(sec, self.acq_type, tuple(ACQ_TYPES)))
        self._row(sec, 1, "Promedios", self._entry(sec, self.acq_avg))
        self._row(sec, 2, "Memoria", self._combo(sec, self.acq_depth, tuple(DEPTH_MAP)))
        self._add_action(ttk.Button(sec, text="Aplicar adquisición", command=self.on_apply_acquisition),
                         row=3, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        # Guardar
        sec = self._section(inner, "Guardar")
        sec.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(sec)
        row.grid(row=0, column=0, columnspan=2, sticky="ew")
        for text, kind in (("CSV", "csv"), ("NPZ", "npz"), ("PNG", "png")):
            b = ttk.Button(row, text=text, command=lambda k=kind: self.save(k))
            b.pack(side="left", expand=True, fill="x", padx=1)
            self._action_widgets.append(b)
        ttk.Label(inner, text="Se guardan todos los puntos; la gráfica se decima solo para verla.",
                  style="BgMuted.TLabel", wraplength=310, justify="left").pack(fill="x", pady=(0, 6))

        # Panel fijo de captura
        bottom = ttk.Frame(left, style="Panel.TFrame", padding=8)
        bottom.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="Puntos:", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        pframe = ttk.Frame(bottom, style="Panel.TFrame")
        pframe.grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(pframe, text="Pantalla", value="Pantalla", variable=self.read_mode).pack(side="left")
        ttk.Radiobutton(pframe, text="Memoria", value="Memoria", variable=self.read_mode).pack(side="left", padx=(8, 0))
        self._row(bottom, 1, "Formato", self._combo(bottom, self.wf_format, ("BYTE", "WORD")))
        self._add_action(ttk.Button(bottom, text="Capturar  (F5)", command=self.on_capture,
                                    style="Accent.TButton"),
                         row=2, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        self._add_action(ttk.Button(bottom, text="Leer pantalla (sin disparar)", command=self.on_read_screen),
                         row=3, column=0, columnspan=2, sticky="ew", pady=2)
        row = ttk.Frame(bottom, style="Panel.TFrame")
        row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        for text, cmd in (("Run", self.on_run), ("Stop", self.on_stop), ("Single", self.on_single),
                          ("Auto", self.on_autoscale)):
            b = ttk.Button(row, text=text, command=cmd)
            b.pack(side="left", expand=True, fill="x", padx=1)
            self._action_widgets.append(b)

        # Zona derecha: gráfica + medidas/registro
        right = ttk.Frame(tab, style="Bg.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        plotframe = ttk.Frame(right, style="Panel.TFrame")
        plotframe.grid(row=0, column=0, sticky="nsew")
        plotframe.rowconfigure(0, weight=1)
        plotframe.columnconfigure(0, weight=1)
        self.fig = Figure(figsize=(8.5, 3.3), dpi=100)
        theme.style_figure(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.085, right=0.985, top=0.93, bottom=0.14)
        theme.style_axes(self.ax)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plotframe)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        # Barra propia (los iconos de Matplotlib son oscuros sobre fondo oscuro).
        bar = ttk.Frame(plotframe, style="Panel.TFrame", padding=(4, 2))
        bar.grid(row=1, column=0, sticky="ew")
        self._mpl_toolbar = NavigationToolbar2Tk(self.canvas, bar, pack_toolbar=False)
        for text, cmd in (("⌂", self._mpl_toolbar.home), ("←", self._mpl_toolbar.back),
                          ("→", self._mpl_toolbar.forward), ("Pan", self._mpl_toolbar.pan),
                          ("Zoom", self._mpl_toolbar.zoom)):
            ttk.Button(bar, text=text, command=cmd, style="Tool.TButton", width=5).pack(
                side="left", padx=1, pady=1)
        ttk.Button(bar, text="Guardar PNG", command=lambda: self.save("png"),
                   style="Tool.TButton").pack(side="left", padx=(8, 1))
        self.plot_info = ttk.Label(bar, text="Sin captura", style="Hint.TLabel")
        self.plot_info.pack(side="right", padx=6)
        self._draw_placeholder()

        lower = ttk.Notebook(right)
        lower.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        measframe = ttk.Frame(lower, style="Panel.TFrame", padding=4)
        lower.add(measframe, text="  Medidas  ")
        self.tree = ttk.Treeview(measframe, columns=("local", "scpi"), show="tree headings", height=5)
        self.tree.heading("#0", text="Medida")
        self.tree.heading("local", text="Calculado sobre la traza")
        self.tree.heading("scpi", text="Osciloscopio (SCPI)")
        self.tree.column("#0", width=180, stretch=False)
        self.tree.column("local", width=220, anchor="w")
        self.tree.column("scpi", width=190, anchor="w")
        tree_scroll = ttk.Scrollbar(measframe, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.meas_rows = [
            ("vpp", "Vpp"), ("vmax", "Vmax"), ("vmin", "Vmin"), ("vavg", "Vmedia"),
            ("vrms", "Vrms"), ("freq", "Frecuencia"), ("period", "Período"),
            ("rise", "Tiempo de subida"), ("fall", "Tiempo de bajada"),
            ("duty", "Ciclo de trabajo"), ("points", "Puntos"), ("fs", "Frecuencia de muestreo"),
        ]
        for key, label in self.meas_rows:
            self.tree.insert("", "end", iid=key, text=label, values=("—", "—"))

        logframe = ttk.Frame(lower, style="Panel.TFrame", padding=4)
        lower.add(logframe, text="  Registro  ")
        self.log_text = tk.Text(logframe, height=5)
        theme.style_text(self.log_text)
        logsb = ttk.Scrollbar(logframe, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=logsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        logsb.pack(side="right", fill="y")
        self.log_text.configure(state="disabled")

    # -- pestaña Instrumento ------------------------------------------------

    def _build_instrument_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame")
        self.notebook.add(tab, text="  Instrumento  ")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)
        scroll = ScrollFrame(tab)
        scroll.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        inner = scroll.inner
        inner.columnconfigure(0, weight=1, uniform="col")
        inner.columnconfigure(1, weight=1, uniform="col")

        # Canal detallado
        sec = self._section(inner, "Canal")
        sec.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        chrow = ttk.Frame(sec)
        chrow.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        for name in ("CH1", "CH2"):
            ttk.Radiobutton(chrow, text=name, value=name, variable=self.ch_var,
                            command=self._on_channel_change).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(chrow, text="Mostrar", variable=self.ch_display).pack(side="left")
        self._row(sec, 1, "Escala (V/div)", self._entry(sec, self.ch_scale))
        self._row(sec, 2, "Offset (V)", self._entry(sec, self.ch_offset))
        self._row(sec, 3, "Acoplamiento", self._combo(sec, self.ch_coupling, ("DC", "AC", "GND")))
        self._row(sec, 4, "Sonda", self._combo(sec, self.ch_probe,
                                               ("0.01X", "0.1X", "1X", "10X", "100X", "1000X")))
        self._row(sec, 5, "Límite 20 MHz", ttk.Checkbutton(sec, text="Activar", variable=self.ch_bw))
        self._row(sec, 6, "Invertir", ttk.Checkbutton(sec, text="Activar", variable=self.ch_inv))
        self._add_action(ttk.Button(sec, text="Aplicar canal", command=self.on_apply_channel),
                         row=7, column=0, columnspan=2, sticky="ew", pady=(8, 2))

        # Adquisición
        sec = self._section(inner, "Adquisición")
        sec.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        self._row(sec, 0, "Modo", self._combo(sec, self.acq_type, tuple(ACQ_TYPES)))
        self._row(sec, 1, "Promedios", self._entry(sec, self.acq_avg))
        self._row(sec, 2, "Memoria", self._combo(sec, self.acq_depth, tuple(DEPTH_MAP)))
        self._add_action(ttk.Button(sec, text="Aplicar adquisición", command=self.on_apply_acquisition),
                         row=3, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        ttk.Label(sec, text="La profundidad real se consulta con :ACQuire:MDEPth? y\n"
                            "puede depender del modo de adquisición y del barrido.",
                  style="Hint.TLabel", justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Base de tiempo
        sec = self._section(inner, "Base de tiempo")
        sec.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        self._row(sec, 0, "Escala (s/div)", self._entry(sec, self.tb_scale))
        self._row(sec, 1, "Offset (s)", self._entry(sec, self.tb_offset))
        self._row(sec, 2, "Modo", self._combo(sec, self.tb_mode, ("MAIN", "XY", "ROLL")))
        self._add_action(ttk.Button(sec, text="Aplicar tiempo", command=self.on_apply_timebase),
                         row=3, column=0, columnspan=2, sticky="ew", pady=(8, 2))

        # Disparo básico
        sec = self._section(inner, "Disparo (flanco)")
        sec.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        self._row(sec, 0, "Barrido", self._combo(sec, self.trig_sweep, ("Auto", "Normal", "Single")))
        self._row(sec, 1, "Fuente", self._combo(sec, self.trig_source, ("CH1", "CH2")))
        self._row(sec, 2, "Flanco", self._combo(sec, self.trig_slope, ("Subida", "Bajada")))
        self._row(sec, 3, "Nivel (V)", self._entry(sec, self.trig_level))
        self._row(sec, 4, "Acoplamiento", self._combo(sec, self.trig_coupling,
                                                      ("DC", "AC", "LFReject", "HFReject")))
        self._add_action(ttk.Button(sec, text="Aplicar disparo", command=self.on_apply_trigger),
                         row=5, column=0, columnspan=2, sticky="ew", pady=(8, 2))

        # Acciones
        sec = self._section(inner, "Control del instrumento")
        sec.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        row = ttk.Frame(sec)
        row.grid(row=0, column=0, columnspan=2, sticky="ew")
        for text, cmd in (("RUN", self.on_run), ("STOP", self.on_stop), ("SINGLE", self.on_single),
                          ("TFORCE", self.on_force), ("AUTOSCALE", self.on_autoscale),
                          ("Leer ajustes", self.reload_settings), ("Estado de disparo", self.on_trigger_status)):
            b = ttk.Button(row, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4), pady=2)
            self._action_widgets.append(b)
        self.idn_label = ttk.Label(sec, text="Sin conexión", style="Hint.TLabel", wraplength=900,
                                   justify="left")
        self.idn_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # -- pestaña Disparo -----------------------------------------------------

    def _build_trigger_tab(self):
        tab = self._build_browser_tab("  Disparo  ", "TRIGger")
        ttk.Label(tab, text="Todos los comandos de disparo de la referencia: flanco, pulso, pendiente, "
                            "vídeo, patrón, duración, timeout, runt, ventana, retardo, setup/hold, "
                            "flanco N, RS232, I²C, SPI, CAN, LIN, USB, FlexRay y MIL-STD-1553.",
                  style="BgMuted.TLabel", wraplength=1000, justify="left").pack(
            side="bottom", fill="x", padx=8, pady=(0, 6))

    def _build_math_tab(self):
        tab = self._build_browser_tab("  MATH  ", "MATH")
        ttk.Label(tab, text="Operaciones matemáticas, filtros, FFT y funciones avanzadas del DS1202Z-E.",
                  style="BgMuted.TLabel", wraplength=1000, justify="left").pack(
            side="bottom", fill="x", padx=8, pady=(0, 6))

    def _build_browser_tab(self, title: str, group: str):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text=title)
        commands = [c for c in self.catalog if c.group == group]
        browser = CommandBrowser(tab, commands, self.execute)
        browser.pack(fill="both", expand=True)
        return tab

    # -- pestaña Comandos ----------------------------------------------------

    def _build_commands_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text="  Comandos  ")
        top = ttk.Frame(tab, style="Bg.TFrame")
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text=f"Catálogo completo: {len(self.catalog)} comandos de la referencia",
                  style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  ·  selecciona uno para generar su formulario",
                  style="BgMuted.TLabel").pack(side="left")
        browser = CommandBrowser(tab, self.catalog, self.execute)
        browser.pack(fill="both", expand=True)

    # -- pestaña Pantalla ----------------------------------------------------

    def _build_screen_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text="  Pantalla  ")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.Frame(tab, style="Panel.TFrame", padding=10, width=280)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_propagate(False)
        left.columnconfigure(1, weight=1)
        ttk.Label(left, text="Captura de pantalla", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Checkbutton(left, text="Imagen en color", variable=self.screen_color).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(left, text="Invertir colores", variable=self.screen_invert).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=2)
        self._row(left, 3, "Formato", self._combo(left, self.screen_fmt, SCREEN_FORMATS))
        ttk.Label(left, text="PNG se puede previsualizar; el resto se guarda en disco.",
                  style="Hint.TLabel", wraplength=240, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(2, 8))
        self._add_action(ttk.Button(left, text="Capturar pantalla", command=self.on_screenshot,
                                    style="Accent.TButton"),
                         row=5, column=0, columnspan=2, sticky="ew", pady=2)
        self._add_action(ttk.Button(left, text="Guardar imagen…", command=self.on_save_screen),
                         row=6, column=0, columnspan=2, sticky="ew", pady=2)
        self.screen_info = ttk.Label(left, text="Sin imagen", style="Hint.TLabel", wraplength=240,
                                     justify="left")
        self.screen_info.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

        right = ttk.Frame(tab, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.screen_label = tk.Label(right, text="Pulsa «Capturar pantalla» para leer\n"
                                                 "la imagen del osciloscopio (:DISPlay:DATA?).",
                                     bg=C["plot_bg"], fg=C["muted"], font=theme.FONT_UI,
                                     justify="center")
        self.screen_label.grid(row=0, column=0, sticky="nsew")

    # -- pestaña Consola -----------------------------------------------------

    def _build_console_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text="  Consola  ")
        tab.rowconfigure(0, weight=1)
        tab.columnconfigure(0, weight=1)

        frame = ttk.Frame(tab, style="Panel.TFrame", padding=8)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.console_text = tk.Text(frame, height=18)
        theme.style_text(self.console_text)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.console_text.yview)
        self.console_text.configure(yscrollcommand=sb.set)
        self.console_text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.console_text.configure(state="disabled")

        row = ttk.Frame(tab, style="Bg.TFrame")
        row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        row.columnconfigure(0, weight=1)
        self.console_var = tk.StringVar()
        self.console_entry = ttk.Entry(row, textvariable=self.console_var, font=theme.FONT_MONO)
        self.console_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.console_entry.bind("<Return>", lambda e: self.on_console_send())
        self.console_entry.bind("<Up>", lambda e: self._history_move(-1))
        self.console_entry.bind("<Down>", lambda e: self._history_move(1))
        ttk.Button(row, text="Enviar", command=self.on_console_send).grid(row=0, column=1, padx=2)
        ttk.Button(row, text="Consultar", command=self.on_console_query).grid(row=0, column=2, padx=2)
        ttk.Button(row, text="Leer errores", command=self.on_read_errors).grid(row=0, column=3, padx=2)
        ttk.Button(row, text="Limpiar", command=self.clear_console).grid(row=0, column=4, padx=2)
        ttk.Label(tab, text="Escribe cualquier comando SCPI. «Enviar» detecta «?» y hace la consulta; "
                            "también puedes pegar una lista separada por «;».",
                  style="BgMuted.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.console_entry.focus_set()

    # -- pestaña Análisis FFT -----------------------------------------------

    def _build_fft_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text="  Análisis FFT  ")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        # Panel de controles -------------------------------------------------
        left = ttk.Frame(tab, style="Panel.TFrame", padding=10, width=300)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_propagate(False)
        left.columnconfigure(1, weight=1)

        ttk.Label(left, text="Análisis de Fourier", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(left, text="Calcula el espectro de magnitud de la última captura.",
                  style="Hint.TLabel", wraplength=260, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self._row(left, 2, "Ventana", self._combo(left, self.fft_window, tuple(WINDOWS)))
        self._row(left, 3, "Picos máx.", self._entry(left, self.fft_top_peaks))
        self._row(left, 4, "Prominencia (dB)", self._entry(left, self.fft_prominence))
        self._row(left, 5, "Distancia mínima (bins)", self._entry(left, self.fft_min_distance))
        ttk.Checkbutton(left, text="Escala dB", variable=self.fft_use_db).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self._add_action(
            ttk.Button(left, text="Analizar captura", command=self.on_fft_analyze, style="Accent.TButton"),
            row=7, column=0, columnspan=2, sticky="ew", pady=2)
        self._add_action(
            ttk.Button(left, text="Guardar espectro CSV", command=self.on_fft_save_csv),
            row=8, column=0, columnspan=2, sticky="ew", pady=2)

        self.fft_status = ttk.Label(left, text="Sin análisis", style="Muted.TLabel", wraplength=260,
                                    justify="left")
        self.fft_status.grid(row=9, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # Panel de resultados ------------------------------------------------
        right = ttk.Frame(tab, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(right, text="Espectro de magnitud", style="Heading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))

        plotframe = ttk.Frame(right, style="Panel.TFrame")
        plotframe.grid(row=1, column=0, sticky="nsew")
        plotframe.rowconfigure(0, weight=1)
        plotframe.columnconfigure(0, weight=1)

        self.fft_fig = Figure(figsize=(7.5, 4.2), dpi=100)
        theme.style_figure(self.fft_fig)
        self.fft_ax = self.fft_fig.add_subplot(111)
        self.fft_fig.subplots_adjust(left=0.09, right=0.985, top=0.92, bottom=0.16)
        theme.style_axes(self.fft_ax, xlabel="Frecuencia (Hz)", ylabel="Magnitud (dB)")
        self.fft_canvas = FigureCanvasTkAgg(self.fft_fig, master=plotframe)
        self.fft_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._draw_fft_placeholder()

        # Tabla de picos -----------------------------------------------------
        peaks_frame = ttk.Frame(right, style="Panel.TFrame", padding=4)
        peaks_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        peaks_frame.rowconfigure(0, weight=1)
        peaks_frame.columnconfigure(0, weight=1)

        self.fft_tree = ttk.Treeview(
            peaks_frame, columns=("freq", "mag_db"), show="tree headings", height=6)
        self.fft_tree.heading("#0", text="Pico")
        self.fft_tree.heading("freq", text="Frecuencia (Hz)")
        self.fft_tree.heading("mag_db", text="Magnitud (dB)")
        self.fft_tree.column("#0", width=60, stretch=False)
        self.fft_tree.column("freq", width=150, anchor="w")
        self.fft_tree.column("mag_db", width=120, anchor="w")
        tree_sb = ttk.Scrollbar(peaks_frame, orient="vertical", command=self.fft_tree.yview)
        self.fft_tree.configure(yscrollcommand=tree_sb.set)
        self.fft_tree.pack(side="left", fill="both", expand=True)
        tree_sb.pack(side="right", fill="y")

    # -- análisis FFT -------------------------------------------------------

    def on_fft_analyze(self):
        if self.last_waveform is None:
            messagebox.showinfo("Sin datos", "Primero captura una forma de onda en la pestaña Captura.")
            return
        try:
            top_peaks = int(self.fft_top_peaks.get())
            prominence = float(self.fft_prominence.get().replace(",", "."))
            min_dist = int(self.fft_min_distance.get())
        except ValueError:
            messagebox.showerror("Valor no válido", "Revisa los campos numéricos del análisis FFT.")
            return

        analyzer = FFTAnalyzer(self.last_waveform)
        result = analyzer.analyze(
            window=self.fft_window.get(),
            top_peaks=top_peaks,
            prominence_db=prominence,
            min_distance_bins=min_dist,
            use_db=self.fft_use_db.get(),
        )
        self.fft_result = result
        self._plot_fft(result)
        self._fill_fft_peaks(result)

        lines = []
        if result.fundamental_frequency is not None:
            lines.append(f"Fundamental: {result.fundamental_frequency:,.1f} Hz")
        if result.fundamental_magnitude_db is not None:
            lines.append(f"Nivel: {result.fundamental_magnitude_db:+.1f} dB")
        if result.snr_db is not None:
            lines.append(f"SNR estimado: {result.snr_db:.1f} dB")
        if result.noise_floor_db is not None:
            lines.append(f"Ruido de piso: {result.noise_floor_db:.1f} dB")
        lines.append(f"Muestreo: {result.sample_rate:,.0f} Sa/s · Ventana: {result.window}")
        self.fft_status.configure(text=" · ".join(lines))
        self.log(f"FFT: {len(result.peaks)} pico(s) detectado(s), "
                 f"Fs={result.sample_rate:,.0f} Sa/s")

    def _draw_fft_placeholder(self):
        self.fft_ax.clear()
        theme.style_axes(self.fft_ax, xlabel="Frecuencia (Hz)", ylabel="Magnitud (dB)")
        self.fft_ax.text(0.5, 0.5, "Sin análisis\nCaptura una señal y pulsa «Analizar captura»",
                         transform=self.fft_ax.transAxes, ha="center", va="center",
                         fontsize=11, color=C["disabled"])
        self.fft_canvas.draw_idle()

    def _plot_fft(self, result: FFTResult):
        self.fft_ax.clear()
        theme.style_axes(self.fft_ax, xlabel="Frecuencia (Hz)", ylabel="Magnitud (dB)")
        if result.freq.size and result.magnitude_db.size:
            self.fft_ax.plot(result.freq, result.magnitude_db, color=C["math"], linewidth=1.0)
            for p in result.peaks:
                self.fft_ax.axvline(p.frequency, color=C["ch1"], linewidth=0.7, linestyle="--", alpha=0.6)
                self.fft_ax.annotate(
                    f"{p.frequency:,.0f} Hz\n{p.magnitude_db:+.1f} dB",
                    xy=(p.frequency, p.magnitude_db), xytext=(p.frequency, p.magnitude_db),
                    fontsize=7, color=C["ch1"],
                    arrowprops=dict(arrowstyle="->", color=C["ch1"], lw=0.6),
                    ha="left", va="bottom")
        self.fft_ax.set_title(
            f"FFT · {result.window} · Fs {fmt_si(result.sample_rate, 'Sa/s')} · "
            f"{result.freq.size:,} bins".replace(",", "."),
            fontsize=9, loc="right")
        self.fft_canvas.draw_idle()

    def _fill_fft_peaks(self, result: FFTResult):
        for item in self.fft_tree.get_children():
            self.fft_tree.delete(item)
        for i, p in enumerate(result.peaks, 1):
            self.fft_tree.insert("", "end", iid=str(i), text=str(i),
                                 values=(f"{p.frequency:,.2f}".replace(",", "."),
                                         f"{p.magnitude_db:+.2f}".replace(",", ".")))

    def on_fft_save_csv(self):
        if self.fft_result is None:
            messagebox.showinfo("Sin datos", "No hay ningún análisis FFT para guardar.")
            return
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Guardar espectro FFT como CSV", initialdir=CAPTURE_DIR,
            initialfile=f"fft_{stamp}.csv", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Todos los archivos", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write("# Rigol DS1202Z-E FFT spectrum\n")
                fh.write(f"# sample_rate={self.fft_result.sample_rate:.6e} Sa/s\n")
                fh.write(f"# window={self.fft_result.window}\n")
                fh.write("# frequency_hz,magnitude_linear,magnitude_db\n")
                for f, m, mdb in zip(self.fft_result.freq, self.fft_result.magnitude,
                                     self.fft_result.magnitude_db):
                    fh.write(f"{f:.6e},{m:.9e},{mdb:.4f}\n")
            self.log(f"Espectro FFT guardado: {path}")
        except Exception as exc:
            messagebox.showerror("Error al guardar", str(exc))

    # -- pestaña Medidor L/C -----------------------------------------------

    def _build_lc_tab(self):
        tab = ttk.Frame(self.notebook, style="Bg.TFrame", padding=8)
        self.notebook.add(tab, text="  Medidor L/C  ")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)

        # Panel de controles ------------------------------------------------
        left = ttk.Frame(tab, style="Panel.TFrame", padding=10, width=320)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_propagate(False)
        left.columnconfigure(1, weight=1)

        ttk.Label(left, text="Medidor de Inductancia / Capacitancia",
                  style="Heading.TLabel").grid(row=0, column=0, columnspan=2,
                                               sticky="w", pady=(0, 6))
        ttk.Label(left, text="Mide la frecuencia de resonancia del circuito tanque "
                             "LC y calcula L o C usando la captura del osciloscopio.",
                  style="Hint.TLabel", wraplength=280, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # Qué medir
        mode_frame = ttk.Frame(left, style="Panel.TFrame")
        mode_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(mode_frame, text="Medir:", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Inductancia L (C conocida)",
                        value="L", variable=self.lc_mode).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(mode_frame, text="Capacitancia C (L conocida)",
                        value="C", variable=self.lc_mode).pack(side="left", padx=(8, 0))

        self._row(left, 3, "Valor conocido", self._entry(left, self.lc_known_value))
        unit_row = ttk.Frame(left, style="Panel.TFrame")
        unit_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(unit_row, text="Unidad:", style="Muted.TLabel").pack(side="left")
        self.lc_unit_combo = ttk.Combobox(unit_row, textvariable=tk.StringVar(value="nF"),
                                          values=("nF", "µF", "mF", "F", "nH", "µH", "mH", "H"),
                                          width=8, state="readonly")
        self.lc_unit_combo.pack(side="left", padx=(6, 0))
        self.lc_unit_combo.bind("<<ComboboxSelected>>", self._on_lc_unit_change)
        # Sincronizar unidad inicial con el modo.
        self._on_lc_unit_change()

        self._row(left, 5, "Canal", self._combo(left, self.lc_channel, ("CH1", "CH2")))
        self._row(left, 6, "Umbral de detección", self._entry(left, self.lc_threshold))
        ttk.Label(left, text="Fracción del pico absoluto para aceptar un pico (0.05–0.5).",
                  style="Hint.TLabel", wraplength=280, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._add_action(
            ttk.Button(left, text="Capturar y medir", command=self.on_lc_measure,
                       style="Accent.TButton"),
            row=8, column=0, columnspan=2, sticky="ew", pady=2)
        self._add_action(
            ttk.Button(left, text="Guardar medición CSV", command=self.on_lc_save_csv),
            row=9, column=0, columnspan=2, sticky="ew", pady=2)

        self.lc_status = ttk.Label(left, text="Listo", style="Muted.TLabel",
                                   wraplength=280, justify="left")
        self.lc_status.grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # Panel de resultados -----------------------------------------------
        right = ttk.Frame(tab, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(right, text="Oscilación amortiguada (circuito tanque)",
                  style="Heading.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))

        plotframe = ttk.Frame(right, style="Panel.TFrame")
        plotframe.grid(row=1, column=0, sticky="nsew")
        plotframe.rowconfigure(0, weight=1)
        plotframe.columnconfigure(0, weight=1)

        self.lc_fig = Figure(figsize=(7.5, 4.2), dpi=100)
        theme.style_figure(self.lc_fig)
        self.lc_ax = self.lc_fig.add_subplot(111)
        self.lc_fig.subplots_adjust(left=0.09, right=0.985, top=0.92, bottom=0.16)
        theme.style_axes(self.lc_ax, xlabel="Tiempo (s)", ylabel="Tensión (V)")
        self.lc_canvas = FigureCanvasTkAgg(self.lc_fig, master=plotframe)
        self.lc_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._draw_lc_placeholder()

        # Tabla de resultados -----------------------------------------------
        res_frame = ttk.Frame(right, style="Panel.TFrame", padding=4)
        res_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        res_frame.columnconfigure(1, weight=1)

        rows = [
            ("fr (resonancia)", "fr_value"),
            ("Periodo", "period_value"),
            (f"{'L' if self.lc_mode.get() == 'C' else 'C'} calculado", "result_value"),
            ("Calidad (0..1)", "quality_value"),
            ("Picos detectados", "peaks_value"),
        ]
        self.lc_labels = {}
        for i, (label, key) in enumerate(rows):
            ttk.Label(res_frame, text=label, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=1)
            val_lbl = ttk.Label(res_frame, text="—", style="Value.TLabel")
            val_lbl.grid(row=i, column=1, sticky="e", pady=1)
            self.lc_labels[key] = val_lbl

    # -- lógica Medidor L/C -------------------------------------------------

    def _on_lc_unit_change(self, _event=None):
        mode = self.lc_mode.get()
        if mode == "L":
            # Se mide L, conocido es C → unidades de faradios
            units = ("pF", "nF", "µF", "mF", "F")
            default = "nF"
        else:
            # Se mide C, conocido es L → unidades de henrios
            units = ("pH", "nH", "µH", "mH", "H")
            default = "µH"
        try:
            self.lc_unit_combo.configure(values=units)
            if self.lc_unit_combo.get() not in units:
                self.lc_unit_combo.set(default)
        except tk.TclError:
            pass

    def _si_to_unit(self, value: float, unit: str) -> float:
        """Convierte un valor SI a la unidad dada."""
        factors = {
            "pF": 1e12, "nF": 1e9, "µF": 1e6, "mF": 1e3, "F": 1.0,
            "pH": 1e12, "nH": 1e9, "µH": 1e6, "mH": 1e3, "H": 1.0,
        }
        return value * factors.get(unit, 1.0)

    def _unit_to_si(self, value: float, unit: str) -> float:
        factors = {
            "pF": 1e-12, "nF": 1e-9, "µF": 1e-6, "mF": 1e-3, "F": 1.0,
            "pH": 1e-12, "nH": 1e-9, "µH": 1e-6, "mH": 1e-3, "H": 1.0,
        }
        return value * factors.get(unit, 1.0)

    def on_lc_measure(self):
        if self.last_waveform is None:
            messagebox.showinfo(
                "Sin datos",
                "Captura primero una forma de onda del circuito tanque en la pestaña "
                "Captura (usa modo Memoria y canal donde tengas el LC conectado).")
            return

        try:
            known_val = float(self.lc_known_value.get().replace(",", "."))
        except (ValueError, AttributeError, tk.TclError):
            messagebox.showerror("Valor no válido", "Revisa el valor conocido.")
            return
        try:
            threshold = float(self.lc_threshold.get().replace(",", "."))
            if not 0.01 <= threshold <= 0.9:
                raise ValueError
        except ValueError:
            messagebox.showerror("Valor no válido", "El umbral debe estar entre 0.01 y 0.9.")
            return

        if known_val <= 0:
            messagebox.showerror("Valor no válido", "El valor conocido debe ser mayor que cero.")
            return

        unit = self.lc_unit_combo.get() if hasattr(self, "lc_unit_combo") else "nF"
        known_si = self._unit_to_si(known_val, unit)
        mode = self.lc_mode.get()  # "L" o "C"

        self.set_busy(True)
        self.lc_status.configure(text="Analizando resonancia…")
        wf = self.last_waveform

        def job():
            try:
                result = measure_resonance(
                    wf.time_s, wf.volts,
                    known_component=mode,
                    known_value=known_si,
                    threshold_frac=threshold,
                    min_cycles=2,
                )
                self.events.put(("lc_result", result))
            except Exception as exc:
                self.events.put(("lc_error", str(exc)))
            finally:
                self.events.put(("busy", False))

        self.worker.submit(job)

    def _handle_lc_result(self, result: LCMeasurement):
        self.lc_result = result
        if result.error:
            self.lc_status.configure(text="Error: " + result.error)
            self.log(f"LC medidor: {result.error}")
            for key in ("fr_value", "period_value", "result_value", "quality_value", "peaks_value"):
                self.lc_labels[key].configure(text="—")
            self._draw_lc_placeholder()
            return

        self.lc_labels["fr_value"].configure(text=result.formatted_freq())
        self.lc_labels["period_value"].configure(
            text=f"{result.period_s*1e6:.4g} µs" if result.period_s else "—")
        self.lc_labels["result_value"].configure(
            text=result.formatted_result(prefix=True))
        self.lc_labels["quality_value"].configure(
            text=f"{result.quality:.3f}" if result.quality is not None else "—")
        self.lc_labels["peaks_value"].configure(text=str(result.peak_count))

        known_name = "C" if result.known_component == "C" else "L"
        known_si = result.known_value
        unit = self.lc_unit_combo.get() if hasattr(self, "lc_unit_combo") else "nF"
        known_disp = known_si / self._si_to_unit(1.0, unit)
        self.lc_status.configure(
            text=f"{known_name} = {known_disp:.4g} {unit} · fr = {result.formatted_freq()} · "
                 f"resultado = {result.formatted_result(prefix=True)}")
        self.log(
            f"LC medidor: {known_name}={known_disp:.4g} {unit}, fr={result.resonance_freq_hz:.2f} Hz, "
            f"resultado={result.formatted_result()}, picos={result.peak_count}, calidad={result.quality}")

        self._plot_lc(wf, result)

    def _handle_lc_error(self, msg: str):
        self.lc_status.configure(text="Error: " + msg)
        self.log(f"LC medidor error: {msg}")
        set_busy = getattr(self, "set_busy", None)
        if set_busy:
            set_busy(False)

    def _draw_lc_placeholder(self):
        self.lc_ax.clear()
        theme.style_axes(self.lc_ax, xlabel="Tiempo (s)", ylabel="Tensión (V)")
        self.lc_ax.text(0.5, 0.5, "Sin medición\nExcita el circuito tanque y pulsa «Capturar y medir»",
                        transform=self.lc_ax.transAxes, ha="center", va="center",
                        fontsize=11, color=C["disabled"])
        self.lc_canvas.draw_idle()

    def _plot_lc(self, wf, result: LCMeasurement):
        self.lc_ax.clear()
        theme.style_axes(self.lc_ax, xlabel="Tiempo (s)", ylabel="Tensión (V)")
        t = wf.time_s
        v = wf.volts
        self.lc_ax.plot(t, v, color=C["ch2"] if "2" in wf.channel else C["ch1"],
                        linewidth=0.8, label="Captura")
        if result.peaks:
            # Graficar picos detectados.
            peak_v = np.interp(result.peaks, t, v)
            self.lc_ax.plot(result.peaks, peak_v, "o", color=C["ch1"], markersize=4,
                            label="Picos detectados")
        self.lc_ax.set_title(
            f"LC resonancia · fr = {result.formatted_freq()} · "
            f"{'L' if result.known_component == 'C' else 'C'} = {result.formatted_result()}",
            fontsize=9, loc="right")
        self.lc_ax.legend(fontsize=7, loc="upper right")
        self.lc_canvas.draw_idle()

    def on_lc_save_csv(self):
        if self.lc_result is None or self.lc_result.error:
            messagebox.showinfo("Sin datos", "No hay ninguna medición L/C para guardar.")
            return
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Guardar medición L/C como CSV", initialdir=CAPTURE_DIR,
            initialfile=f"lc_medicion_{stamp}.csv", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Todos los archivos", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write("# Rigol DS1202Z-E medicion de resonancia LC\n")
                fh.write(f"# componente_conocido={self.lc_result.known_component}\n")
                fh.write(f"# valor_conocido={self.lc_result.known_value:.12e}\n")
                fh.write(f"# frecuencia_resonancia_hz={self.lc_result.resonance_freq_hz:.12e}\n")
                fh.write(f"# periodo_s={self.lc_result.period_s:.12e}\n")
                fh.write(f"# calidad={self.lc_result.quality}\n")
                fh.write(f"# picos_detectados={self.lc_result.peak_count}\n")
                if self.lc_result.inductance_h is not None:
                    fh.write(f"# inductancia_calculada_H={self.lc_result.inductance_h:.12e}\n")
                if self.lc_result.capacitance_f is not None:
                    fh.write(f"# capacitancia_calculada_F={self.lc_result.capacitance_f:.12e}\n")
                fh.write("# pico_t_s,pico_V,periodo_s\n")
                periods = self.lc_result.periods or []
                for i, pt in enumerate(self.lc_result.peaks):
                    pv = self.lc_result.peak_voltages[i] if i < len(self.lc_result.peak_voltages) else ""
                    per = periods[i] if i < len(periods) else ""
                    fh.write(f"{pt:.12e},{pv},{per}\n")
            self.log(f"Medición L/C guardada: {path}")
        except Exception as exc:
            messagebox.showerror("Error al guardar", str(exc))

    # -- helpers de widgets --------------------------------------------------

    def _section(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=(10, 6, 10, 8))
        frame.columnconfigure(1, weight=1)
        return frame

    @staticmethod
    def _entry(parent, var):
        return ttk.Entry(parent, textvariable=var, width=12)

    @staticmethod
    def _combo(parent, var, values):
        return ttk.Combobox(parent, textvariable=var, values=values, width=12, state="readonly")

    @staticmethod
    def _row(parent, row, label, widget):
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        widget.grid(row=row, column=1, sticky="ew", pady=2)

    def _add_action(self, widget, **grid):
        widget.grid(**grid)
        self._action_widgets.append(widget)

    def _draw_placeholder(self):
        self.ax.clear()
        theme.style_axes(self.ax)
        self.ax.text(0.5, 0.5, "Sin captura\nConecta el osciloscopio y pulsa «Capturar»",
                     transform=self.ax.transAxes, ha="center", va="center",
                     fontsize=11, color=C["disabled"])
        self.canvas.draw_idle()

    # -- registro y estado ---------------------------------------------------

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {message}\n"
        for widget in (getattr(self, "log_text", None), getattr(self, "console_text", None)):
            if widget is None:
                continue
            widget.configure(state="normal")
            widget.insert("end", line)
            widget.see("end")
            widget.configure(state="disabled")
        self.status_var.set(str(message).splitlines()[0][:200])

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in self._action_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.configure(cursor="watch" if busy else "")

    def _require_connection(self) -> bool:
        if self.scope is None:
            messagebox.showinfo("Sin conexión", "Primero conecta el osciloscopio (o el modo DEMO).")
            return False
        return True

    # -- eventos del hilo ----------------------------------------------------

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _handle_event(self, kind, payload):
        if kind == "ports":
            values = list(payload) or ["DEMO (simulado)"]
            if "DEMO (simulado)" not in values:
                values.append("DEMO (simulado)")
            self.port_combo["values"] = values
            if not self.port_var.get():
                self.port_var.set(values[0])
            self.log(f"Recursos VISA encontrados: {len(payload)}")
        elif kind == "ports_done":
            if self.scope is None:
                self.btn_refresh.configure(state="normal")
        elif kind == "connected":
            self.scope = payload["scope"]
            self.settings = payload["settings"]
            name = "DEMO" if payload["demo"] else payload["resource"]
            self.conn_status.set(f"Conectado · {name}")
            self.status_var.set(payload["idn"])
            self.idn_label.configure(text=payload["idn"])
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
            self.btn_refresh.configure(state="disabled")
            self.set_busy(False)
            self._load_settings()
            self.log(f"Conectado: {payload['idn']}")
        elif kind == "disconnected":
            self.scope = None
            self.settings = None
            self.conn_status.set("Desconectado")
            self.status_var.set("Listo.")
            self.idn_label.configure(text="Sin conexión")
            self.btn_connect.configure(state="normal")
            self.btn_disconnect.configure(state="disabled")
            self.btn_refresh.configure(state="normal")
            self.set_busy(False)
            self.log("Desconectado.")
        elif kind == "settings":
            self.settings = payload
            self._load_settings()
        elif kind == "waveform":
            wf, scpi = payload
            self.last_waveform = wf
            self.last_scpi = scpi or {}
            self._plot_waveform(wf)
            self._update_measurements(wf)
            self.status_var.set(
                f"{wf.channel} · {wf.points:,} puntos · Fs {fmt_si(wf.sample_rate, 'Sa/s')} · "
                f"{wf.mode} · {wf.timestamp}".replace(",", ".")
            )
            self.progress_var.set(100)
            self.progress_text.set("Captura completada")
        elif kind == "progress":
            frac, text = payload
            self.progress_var.set(max(0.0, min(1.0, frac)) * 100)
            self.progress_text.set(text)
        elif kind == "error":
            self.set_busy(False)
            self.progress_text.set("Error")
            self.log("ERROR: " + str(payload))
            messagebox.showerror("Error", str(payload))
        elif kind == "connect_failed":
            self.set_busy(False)
            if self.scope is None:
                self.btn_connect.configure(state="normal")
                self.btn_refresh.configure(state="normal")
        elif kind == "log":
            self.log(str(payload))
        elif kind == "busy":
            self.set_busy(bool(payload))
        elif kind == "result":
            self._handle_result(payload)
        elif kind == "lc_result":
            self._handle_lc_result(payload)
        elif kind == "lc_error":
            self._handle_lc_error(payload)
        elif kind == "screenshot":
            self._show_screenshot(payload)

    def _handle_result(self, payload):
        text = payload["text"]
        ok = payload["ok"]
        self.log(("→ " if ok else "ERROR: ") + f"{text}  {payload['request']}")
        if not ok:
            self.conn_status.set("Error SCPI")
        callback = self._result_handlers.pop(payload["key"], None)
        if callback:
            callback(ok, text)

    # -- puertos / conexión --------------------------------------------------

    def refresh_ports(self):
        if self.scope is not None:
            return
        self.btn_refresh.configure(state="disabled")
        self.log("Buscando instrumentos VISA…")

        def job():
            try:
                resources = discover_resources()
            except Exception as exc:
                resources = []
                self.events.put(("log", f"No se pudo enumerar VISA: {exc}"))
            self.events.put(("ports", resources))
            self.events.put(("ports_done", None))

        self.worker.submit(job)

    def on_connect(self):
        if self.scope is not None:
            return
        resource = self.port_var.get().strip()
        if not resource:
            messagebox.showinfo("Recurso", "Selecciona o escribe un recurso VISA.")
            return
        self.set_busy(True)
        self.btn_connect.configure(state="disabled")
        self.log(f"Conectando a {resource}…")

        def job():
            try:
                if resource.upper().startswith("DEMO"):
                    scope = DemoScope().open()
                    demo = True
                else:
                    scope = RigolDS1202Z(resource=resource, timeout=DEFAULT_TIMEOUT_MS).open()
                    demo = False
                settings = {
                    "ch1": scope.get_channel_settings(1),
                    "ch2": scope.get_channel_settings(2),
                    "tb": scope.get_timebase_settings(),
                    "trig": scope.get_trigger_settings(),
                    "acq": scope.get_acquisition_settings(),
                }
                self.events.put(("connected", {
                    "scope": scope, "resource": resource, "demo": demo,
                    "idn": scope.idn, "settings": settings,
                }))
            except Exception as exc:
                self.events.put(("error", f"No se pudo conectar con {resource}:\n\n{exc}"))
                self.events.put(("connect_failed", None))

        self.worker.submit(job)

    def on_disconnect(self):
        if self.scope is None:
            return
        scope = self.scope
        self.set_busy(True)

        def job():
            try:
                scope.close()
            finally:
                self.events.put(("disconnected", None))

        self.worker.submit(job)

    def on_close(self):
        try:
            if self.scope is not None:
                self.scope.close()
        except Exception:
            pass
        self.worker.jobs.put(None)
        self.destroy()

    # -- ajustes en los controles -------------------------------------------

    def _load_settings(self):
        if not self.settings:
            return
        self._loading = True
        try:
            n = 1 if self.ch_var.get() == "CH1" else 2
            s = self.settings.get(f"ch{n}", {})
            self.ch_display.set(bool(s.get("display", True)))
            self.ch_scale.set(f"{s.get('scale', 1.0):g}")
            self.ch_offset.set(f"{s.get('offset', 0.0):g}")
            self.ch_coupling.set(str(s.get("coupling", "DC")))
            probe = float(s.get("probe", 1.0))
            self.ch_probe.set(f"{probe:g}X")
            self.ch_bw.set(bool(s.get("bwlimit", False)))
            self.ch_inv.set(bool(s.get("invert", False)))

            tb = self.settings.get("tb", {})
            self.tb_scale.set(f"{tb.get('scale', 1e-3):g}")
            self.tb_offset.set(f"{tb.get('offset', 0.0):g}")
            self.tb_mode.set(str(tb.get("mode", "MAIN")) or "MAIN")

            tr = self.settings.get("trig", {})
            sweep = str(tr.get("sweep", "AUTO")).upper()
            self.trig_sweep.set("Normal" if sweep.startswith("NORM") else
                                "Single" if sweep.startswith("SING") else "Auto")
            src = str(tr.get("source", "CHAN1")).upper()
            self.trig_source.set("CH2" if "2" in src else "CH1")
            slope = str(tr.get("slope", "POSITIVE")).upper()
            self.trig_slope.set("Bajada" if slope.startswith("NEG") else "Subida")
            self.trig_level.set(f"{tr.get('level', 0.0):g}")
            self.trig_coupling.set(str(tr.get("coupling", "DC")) or "DC")

            acq = self.settings.get("acq", {})
            typ = str(acq.get("type", "NORMAL")).upper()
            self.acq_type.set(
                "Promedio" if typ.startswith("AVER") else
                "Pico" if typ.startswith("PEAK") else
                "Alta resolución" if typ.startswith("HRES") else "Normal")
            self.acq_avg.set(str(int(acq.get("averages", 16))))
            depth = str(acq.get("depth", "AUTO")).upper()
            self.acq_depth.set(DEPTH_LABEL.get(depth, "Auto"))
        finally:
            self._loading = False

    def _on_channel_change(self):
        if self._loading:
            return
        self._load_settings()

    def reload_settings(self):
        if not self._require_connection() or self.busy:
            return
        self.set_busy(True)

        def job():
            try:
                settings = {
                    "ch1": self.scope.get_channel_settings(1),
                    "ch2": self.scope.get_channel_settings(2),
                    "tb": self.scope.get_timebase_settings(),
                    "trig": self.scope.get_trigger_settings(),
                    "acq": self.scope.get_acquisition_settings(),
                }
                errors = self.scope.check_errors() if hasattr(self.scope, "check_errors") else []
                for err in errors:
                    self.events.put(("log", f"SCPI error: {err}"))
                self.events.put(("settings", settings))
                self.events.put(("log", "Ajustes actualizados desde el instrumento."))
            except Exception as exc:
                self.events.put(("error", f"No se pudieron leer los ajustes:\n\n{exc}"))
            finally:
                self.events.put(("busy", False))

        self.worker.submit(job)

    # -- comandos genéricos --------------------------------------------------

    def execute(self, text: str, query: bool, callback=None):
        """Ejecuta un comando desde cualquier formulario/consola."""
        text = text.strip()
        if not text:
            return
        if not self._require_connection():
            if callback:
                callback(False, "Sin conexión con el osciloscopio.")
            return
        self._result_seq += 1
        key = self._result_seq
        if callback:
            self._result_handlers[key] = callback
        is_query = query or "?" in text
        binary = self._is_binary_query(text)
        self.log((">>> " if not is_query else ">>> ") + text)

        def job():
            try:
                if binary:
                    data = self.scope.query_block(text)
                    result = f"Bloque binario recibido: {len(data)} bytes."
                elif is_query:
                    result = self.scope.query(text)
                    if not result:
                        result = "(respuesta vacía)"
                else:
                    self.scope.write(text)
                    errors = self.scope.check_errors() if hasattr(self.scope, "check_errors") else []
                    if errors:
                        result = "Ajuste enviado, pero el instrumento informa:\n" + "\n".join(errors)
                        self.events.put(("result", {
                            "key": key, "ok": False, "text": result, "request": text}))
                        return
                    result = "Ajuste enviado correctamente."
                result = _truncate(result)
                self.events.put(("result", {
                    "key": key, "ok": True, "text": result, "request": text}))
            except Exception as exc:
                self.events.put(("result", {
                    "key": key, "ok": False, "text": str(exc), "request": text}))

        self.worker.submit(job)

    @staticmethod
    def _is_binary_query(text: str) -> bool:
        upper = text.strip().upper()
        return upper.startswith((":DISP:DATA?", ":DISPLAY:DATA?",
                                 ":WAV:DATA?", ":WAVEFORM:DATA?"))

    # -- aplicar ajustes -----------------------------------------------------

    def _run_command(self, description, fn):
        if not self._require_connection():
            return
        if self.busy:
            return
        self.set_busy(True)

        def job():
            try:
                fn()
                errors = self.scope.check_errors() if hasattr(self.scope, "check_errors") else []
                for err in errors:
                    self.events.put(("log", f"SCPI error: {err}"))
                self.events.put(("log", description))
            except Exception as exc:
                self.events.put(("error", f"{description}\n\n{exc}"))
            finally:
                self.events.put(("busy", False))

        self.worker.submit(job)

    def _parse_float(self, var, name, positive=False):
        text = var.get().replace(",", ".").strip()
        try:
            value = float(text)
        except ValueError:
            messagebox.showerror("Valor no válido", f"«{name}» debe ser un número (por ejemplo 0.5).")
            raise
        if positive and value <= 0:
            messagebox.showerror("Valor no válido", f"«{name}» debe ser mayor que cero.")
            raise ValueError(name)
        return value

    def on_apply_channel(self):
        if not self._require_connection():
            return
        try:
            scale = self._parse_float(self.ch_scale, "Escala", positive=True)
            offset = self._parse_float(self.ch_offset, "Offset")
            probe = float(self.ch_probe.get().rstrip("Xx"))
            ch = self.ch_var.get()
            args = dict(
                display=self.ch_display.get(), scale=scale, offset=offset,
                coupling=self.ch_coupling.get(), probe=probe,
                bwlimit=self.ch_bw.get(),
                invert=self.ch_inv.get(),
            )
        except ValueError:
            return

        def fn():
            self.scope.apply_channel(ch, **args)
            self.settings[f"ch{1 if ch == 'CH1' else 2}"] = self.scope.get_channel_settings(ch)

        self._run_command(f"Canal {ch} configurado.", fn)

    def on_apply_timebase(self):
        if not self._require_connection():
            return
        try:
            scale = self._parse_float(self.tb_scale, "Escala de tiempo", positive=True)
            offset = self._parse_float(self.tb_offset, "Offset de tiempo")
        except ValueError:
            return

        def fn():
            self.scope.apply_timebase(scale=scale, offset=offset, mode=self.tb_mode.get())
            self.settings["tb"] = self.scope.get_timebase_settings()

        self._run_command("Base de tiempo configurada.", fn)

    def on_apply_trigger(self):
        if not self._require_connection():
            return
        try:
            level = self._parse_float(self.trig_level, "Nivel de disparo")
        except ValueError:
            return
        sweep = SWEEP_MAP[self.trig_sweep.get()]
        source = "CHAN1" if self.trig_source.get() == "CH1" else "CHAN2"
        slope = "POSitive" if self.trig_slope.get() == "Subida" else "NEGative"

        def fn():
            self.scope.apply_trigger(sweep=sweep, source=source, slope=slope, level=level,
                                     coupling=self.trig_coupling.get())
            self.settings["trig"] = self.scope.get_trigger_settings()

        self._run_command("Disparo configurado.", fn)

    def on_apply_acquisition(self):
        if not self._require_connection():
            return
        try:
            averages = int(float(self.acq_avg.get().replace(",", ".")))
        except ValueError:
            messagebox.showerror("Valor no válido", "El número de promedios debe ser entero (2–1024).")
            return
        typ = ACQ_TYPES[self.acq_type.get()]
        depth = DEPTH_MAP.get(self.acq_depth.get(), "AUTO")

        def fn():
            self.scope.apply_acquisition(type=typ, averages=averages, depth=depth)
            self.settings["acq"] = self.scope.get_acquisition_settings()

        self._run_command("Adquisición configurada.", fn)

    # -- acciones de adquisición --------------------------------------------

    def on_run(self):
        self._run_command("RUN", lambda: self.scope.run())

    def on_stop(self):
        self._run_command("STOP", lambda: self.scope.stop())

    def on_single(self):
        self._run_command("SINGLE", lambda: self.scope.single())

    def on_force(self):
        self._run_command("TFORCE", lambda: self.scope.force())

    def on_autoscale(self):
        self._run_command("AUTOSCALE", lambda: self.scope.autoscale())

    def on_trigger_status(self):
        if not self._require_connection():
            return
        self.execute(":TRIGger:STATus?", True, lambda ok, text: self.log(f"Estado de disparo: {text}"))

    def on_read_errors(self):
        if not self._require_connection():
            return
        self.execute(":SYSTem:ERRor?", True)

    def on_capture(self):
        self._capture(single=True)

    def on_read_screen(self):
        self._capture(single=False)

    def _capture(self, single: bool):
        if not self._require_connection() or self.busy:
            return
        ch = self.ch_var.get()
        mode = "screen" if self.read_mode.get() == "Pantalla" else "memory"
        data_format = self.wf_format.get()
        self.set_busy(True)
        self.progress_var.set(0)
        self.progress_text.set("Capturando…")
        self.log(f"Capturando {ch} ({'pantalla' if mode == 'screen' else 'memoria'}, {data_format})…")

        def progress(frac, text):
            self.events.put(("progress", (frac, text)))

        def job():
            try:
                if single:
                    self.scope.single()
                    if not self.scope.wait_for_stop(timeout=10.0):
                        self.events.put(("log", "Sin disparo en 10 s; se captura el estado actual "
                                                "(¿nivel de disparo?)."))
                else:
                    self.scope.stop()
                    time.sleep(0.15)
                wf = self.scope.capture(channel=ch, mode=mode, data_format=data_format,
                                        progress=progress)
                scpi = {}
                try:
                    scpi = self.scope.scpi_measurements(ch)
                except Exception as exc:
                    self.events.put(("log", f"Medidas SCPI no disponibles: {exc}"))
                self.events.put(("waveform", (wf, scpi)))
            except Exception as exc:
                self.events.put(("error", f"Fallo en la captura:\n\n{exc}"))
            finally:
                self.events.put(("busy", False))

        self.worker.submit(job)

    # -- gráfica y medidas ---------------------------------------------------

    def _plot_waveform(self, wf):
        t = wf.time_s
        v = wf.volts
        t_plot, v_plot = decimate_for_plot(t, v)
        scale, unit = fmt_time_span(wf.duration)
        color = C["ch2"] if "2" in wf.channel else C["ch1"]

        self.ax.clear()
        theme.style_axes(self.ax, xlabel=f"Tiempo ({unit})",
                         ylabel=f"Tensión (V) · {wf.channel}")
        self.ax.plot(t_plot * scale, v_plot, color=color, linewidth=0.9)
        self.ax.margins(x=0.005)
        self.ax.relim()
        self.ax.autoscale_view()
        self.ax.set_title(
            f"{wf.channel} · {wf.points:,} pts · Fs {fmt_si(wf.sample_rate, 'Sa/s')} · "
            f"{wf.mode} · {wf.data_format}".replace(",", "."),
            fontsize=9, loc="right")
        self.canvas.draw_idle()
        self.plot_info.configure(text=f"{wf.points:,} pts · {wf.data_format}".replace(",", "."))

    def _update_measurements(self, wf):
        meas = analyze_waveform(wf.time_s, wf.volts)
        self.last_measurements = meas
        scpi = self.last_scpi
        rows = {
            "vpp": fmt_si(meas.get("vpp"), "V"), "vmax": fmt_si(meas.get("vmax"), "V"),
            "vmin": fmt_si(meas.get("vmin"), "V"), "vavg": fmt_si(meas.get("vavg"), "V"),
            "vrms": fmt_si(meas.get("vrms"), "V"), "freq": fmt_si(meas.get("freq"), "Hz"),
            "period": fmt_si(meas.get("period"), "s"), "rise": fmt_si(meas.get("rise"), "s"),
            "fall": fmt_si(meas.get("fall"), "s"),
            "duty": "—" if meas.get("duty") is None else f"{meas['duty'] * 100:.1f} %",
            "points": f"{wf.points:,}".replace(",", "."),
            "fs": fmt_si(wf.sample_rate, "Sa/s"),
        }
        scpi_rows = {
            "vpp": fmt_si(scpi.get("VPP"), "V"), "vmax": fmt_si(scpi.get("VMAX"), "V"),
            "vmin": fmt_si(scpi.get("VMIN"), "V"), "freq": fmt_si(scpi.get("FREQUENCY"), "Hz"),
            "period": fmt_si(scpi.get("PERIOD"), "s"),
        }
        for key, _label in self.meas_rows:
            self.tree.item(key, values=(rows.get(key, "—"), scpi_rows.get(key, "—")))

    # -- guardar -------------------------------------------------------------

    def save(self, kind: str):
        if kind in ("csv", "npz") and self.last_waveform is None:
            messagebox.showinfo("Sin datos", "Todavía no hay ninguna captura para guardar.")
            return
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        wf = self.last_waveform
        name = f"{wf.channel}_{stamp}.{kind}" if wf is not None else f"grafica_{stamp}.{kind}"
        types = {"csv": ("CSV", "*.csv"), "npz": ("NPZ", "*.npz"), "png": ("PNG", "*.png")}
        label, pattern = types[kind]
        path = filedialog.asksaveasfilename(
            title=f"Guardar {label}", initialdir=CAPTURE_DIR, initialfile=name,
            defaultextension="." + kind,
            filetypes=[(label, pattern), ("Todos los archivos", "*.*")])
        if not path:
            return
        try:
            if kind == "csv":
                self._save_csv(path, wf)
            elif kind == "npz":
                self._save_npz(path, wf)
            else:
                self.fig.savefig(path, dpi=150, facecolor=self.fig.get_facecolor())
            self.log(f"Guardado: {path}")
        except Exception as exc:
            messagebox.showerror("Error al guardar", str(exc))

    def _save_csv(self, path, wf):
        self.progress_text.set("Guardando CSV…")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            fh.write("# Rigol DS1202Z-E waveform capture\n")
            fh.write(f"# channel={wf.channel}, points={wf.points}, mode={wf.mode}, "
                     f"format={wf.data_format}\n")
            fh.write(f"# sample_rate={wf.sample_rate:.6e} Sa/s, x_increment={wf.x_increment:.6e} s\n")
            fh.write(f"# captured_at={wf.timestamp}\n")
            fh.write("# time_s,voltage_V\n")
            np.savetxt(fh, np.column_stack((wf.time_s, wf.volts)),
                       fmt=("%.12e", "%.9e"), delimiter=",")
        self.progress_text.set("CSV guardado")

    def _save_npz(self, path, wf):
        meas = self.last_measurements
        np.savez_compressed(
            path, time_s=wf.time_s, volts=wf.volts, raw=wf.raw, channel=wf.channel,
            points=wf.points, sample_rate=wf.sample_rate, x_increment=wf.x_increment,
            x_origin=wf.x_origin, y_increment=wf.y_increment, y_origin=wf.y_origin,
            y_reference=wf.y_reference, mode=wf.mode, data_format=wf.data_format,
            captured_at=wf.timestamp,
            **{f"m_{k}": (np.nan if v is None else v) for k, v in meas.items()})
        self.progress_text.set("NPZ guardado")

    # -- pantalla del instrumento -------------------------------------------

    def on_screenshot(self):
        if not self._require_connection():
            return
        color = "ON" if self.screen_color.get() else "OFF"
        invert = "ON" if self.screen_invert.get() else "OFF"
        fmt = self.screen_fmt.get()
        self.set_busy(True)
        self.screen_info.configure(text="Leyendo pantalla…")
        self.log(f"Leyendo pantalla ({color}, {fmt})…")

        def job():
            try:
                data = self.scope.screenshot(color=color, invert=invert, fmt=fmt)
                self.events.put(("screenshot", (data, fmt)))
            except Exception as exc:
                self.events.put(("error", f"No se pudo leer la pantalla:\n\n{exc}"))
            finally:
                self.events.put(("busy", False))

        self.worker.submit(job)

    def _show_screenshot(self, payload):
        data, fmt = payload
        self.last_screen = data
        if fmt == "PNG":
            try:
                encoded = base64.b64encode(data).decode("ascii")
                self.screen_photo = tk.PhotoImage(data=encoded)
                self.screen_label.configure(image=self.screen_photo, text="")
                self.screen_info.configure(
                    text=f"{len(data):,} bytes · PNG · {self.screen_photo.width()}×"
                         f"{self.screen_photo.height()}".replace(",", "."))
                self.log(f"Pantalla capturada ({len(data)} bytes, PNG).")
                return
            except tk.TclError as exc:
                self.log(f"No se pudo previsualizar el PNG: {exc}")
        self.screen_label.configure(image="", text=f"Imagen {fmt} de {len(data)} bytes.\n"
                                                  "Usa «Guardar imagen…» para guardarla.")
        self.screen_info.configure(text=f"{len(data):,} bytes · {fmt}".replace(",", "."))
        self.log(f"Pantalla capturada ({len(data)} bytes, {fmt}).")

    def on_save_screen(self):
        if not self.last_screen:
            messagebox.showinfo("Sin imagen", "Primero captura la pantalla del osciloscopio.")
            return
        fmt = self.screen_fmt.get()
        ext = {"JPEG": "jpg"}.get(fmt, fmt.lower())
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Guardar pantalla del osciloscopio", initialdir=CAPTURE_DIR,
            initialfile=f"pantalla_{stamp}.{ext}", defaultextension="." + ext,
            filetypes=[(fmt, f"*.{ext}"), ("Todos los archivos", "*.*")])
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(self.last_screen)
            self.log(f"Pantalla guardada: {path}")
        except Exception as exc:
            messagebox.showerror("Error al guardar", str(exc))

    # -- consola -------------------------------------------------------------

    def clear_console(self):
        self.console_text.configure(state="normal")
        self.console_text.delete("1.0", "end")
        self.console_text.configure(state="disabled")

    def on_console_send(self):
        text = self.console_var.get().strip()
        if not text:
            return
        self._history.append(text)
        self._history_pos = len(self._history)
        self.console_var.set("")
        self.execute(text, "?" in text)

    def on_console_query(self):
        text = self.console_var.get().strip()
        if not text:
            return
        if "?" not in text:
            text = text.rstrip() + "?"
        self.console_var.set(text)
        self.on_console_send()

    def _history_move(self, delta: int):
        if not self._history:
            return "break"
        self._history_pos = max(0, min(len(self._history), self._history_pos + delta))
        if self._history_pos < len(self._history):
            self.console_var.set(self._history[self._history_pos])
        else:
            self.console_var.set("")
        return "break"

    # -- errores no controlados ---------------------------------------------

    def report_callback_exception(self, exc, val, tb):
        self.log(f"ERROR interno: {val}")
        messagebox.showerror("Error interno", str(val))


def main():
    app = OScopeApp()
    app.bind("<F5>", lambda e: app.on_capture())
    app.bind("<Control-s>", lambda e: app.save("csv"))
    app.bind("<Control-l>", lambda e: app.clear_console())
    app.bind("<Control-Return>", lambda e: app.on_console_send())
    app.mainloop()


if __name__ == "__main__":
    main()
