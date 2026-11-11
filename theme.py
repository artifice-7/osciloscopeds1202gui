"""Tema oscuro compartido por la interfaz Tkinter y las gráficas Matplotlib."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# Paleta ---------------------------------------------------------------------

COLORS = {
    "bg": "#0f1218",
    "panel": "#171c24",
    "panel2": "#1e2430",
    "field": "#12161d",
    "border": "#2b3340",
    "border2": "#3a4554",
    "text": "#e7ebf1",
    "muted": "#94a1b2",
    "disabled": "#5b6675",
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "accent_text": "#ffffff",
    "ok": "#22c55e",
    "warn": "#f59e0b",
    "error": "#ef4444",
    "ch1": "#fbbf24",
    "ch2": "#22d3ee",
    "math": "#c084fc",
    "grid": "#29313d",
    "grid_minor": "#20262f",
    "plot_bg": "#12161d",
    "selection": "#27415f",
}

FONT_UI = ("Segoe UI", 9)
FONT_UI_BOLD = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
FONT_TITLE = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 9)


def apply(root: tk.Misc) -> ttk.Style:
    """Aplica el tema oscuro a todas las clases ttk usadas por la aplicación."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    c = COLORS
    root.configure(bg=c["bg"])

    # Ventanas y contenedores ------------------------------------------------
    style.configure(".", background=c["bg"], foreground=c["text"], font=FONT_UI,
                    bordercolor=c["border"], focuscolor=c["accent"])
    style.configure("TFrame", background=c["bg"])
    style.configure("Panel.TFrame", background=c["panel"])
    style.configure("Card.TFrame", background=c["panel2"])
    style.configure("Toolbar.TFrame", background=c["panel"])

    # Etiquetas --------------------------------------------------------------
    style.configure("TLabel", background=c["panel"], foreground=c["text"])
    style.configure("Bg.TLabel", background=c["bg"], foreground=c["text"])
    style.configure("Muted.TLabel", background=c["panel"], foreground=c["muted"])
    style.configure("BgMuted.TLabel", background=c["bg"], foreground=c["muted"])
    style.configure("Title.TLabel", background=c["bg"], foreground=c["text"], font=FONT_TITLE)
    style.configure("Heading.TLabel", background=c["panel"], foreground=c["text"], font=FONT_UI_BOLD)
    style.configure("Hint.TLabel", background=c["panel"], foreground=c["muted"], font=FONT_SMALL)
    style.configure("Value.TLabel", background=c["panel"], foreground=c["text"], font=FONT_MONO)
    style.configure("Status.TLabel", background=c["panel2"], foreground=c["muted"],
                    font=FONT_UI, padding=(8, 4))
    style.configure("StatusOk.TLabel", background=c["panel2"], foreground=c["ok"],
                    font=FONT_UI_BOLD, padding=(8, 4))
    style.configure("StatusErr.TLabel", background=c["panel2"], foreground=c["error"],
                    font=FONT_UI_BOLD, padding=(8, 4))

    # Botones ----------------------------------------------------------------
    style.configure("TButton", background=c["panel2"], foreground=c["text"],
                    bordercolor=c["border2"], lightcolor=c["panel2"], darkcolor=c["panel2"],
                    padding=(9, 5), font=FONT_UI)
    style.map("TButton",
              background=[("disabled", c["panel"]), ("pressed", c["border2"]), ("active", c["border"])],
              foreground=[("disabled", c["disabled"])],
              bordercolor=[("focus", c["accent"])])
    style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_text"],
                    bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                    padding=(11, 6), font=FONT_UI_BOLD)
    style.map("Accent.TButton",
              background=[("disabled", c["border"]), ("pressed", c["accent_hover"]),
                          ("active", c["accent_hover"])],
              foreground=[("disabled", c["disabled"])])
    style.configure("Small.TButton", padding=(6, 3), font=FONT_SMALL)
    style.configure("Tool.TButton", padding=(6, 3), font=FONT_UI)

    # Entradas y listas ------------------------------------------------------
    for name in ("TEntry", "TSpinbox"):
        style.configure(name, fieldbackground=c["field"], foreground=c["text"],
                        insertcolor=c["text"], bordercolor=c["border2"],
                        lightcolor=c["border2"], darkcolor=c["border2"],
                        arrowcolor=c["muted"], padding=3)
        style.map(name,
                  fieldbackground=[("disabled", c["panel"]), ("readonly", c["field"])],
                  foreground=[("disabled", c["disabled"])],
                  bordercolor=[("focus", c["accent"])])
    style.configure("TCombobox", fieldbackground=c["field"], background=c["panel2"],
                    foreground=c["text"], arrowcolor=c["text"], bordercolor=c["border2"],
                    lightcolor=c["border2"], darkcolor=c["border2"], padding=3)
    style.map("TCombobox",
              fieldbackground=[("readonly", c["field"]), ("disabled", c["panel"])],
              foreground=[("disabled", c["disabled"])],
              bordercolor=[("focus", c["accent"])],
              arrowcolor=[("disabled", c["disabled"])])
    style.configure("TSpinbox", arrowsize=11)

    # Comprobación / radio ----------------------------------------------------
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=c["panel"], foreground=c["text"],
                        indicatorbackground=c["field"], indicatormargin=(0, 0, 6, 0))
        style.map(name,
                  background=[("active", c["panel"])],
                  foreground=[("disabled", c["disabled"])],
                  indicatorbackground=[("selected", c["accent"]), ("disabled", c["panel"])],
                  indicatorforeground=[("selected", c["accent_text"])])

    # Pestañas ---------------------------------------------------------------
    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"], tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", background=c["panel"], foreground=c["muted"],
                    bordercolor=c["border"], padding=(13, 6), font=FONT_UI)
    style.map("TNotebook.Tab",
              background=[("selected", c["panel2"]), ("active", c["border"])],
              foreground=[("selected", c["text"]), ("active", c["text"])],
              expand=[("selected", (0, 0, 0, 0))])

    # Paneles con título -----------------------------------------------------
    style.configure("TLabelframe", background=c["panel"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"], padding=8)
    style.configure("TLabelframe.Label", background=c["panel"], foreground=c["accent"],
                    font=FONT_UI_BOLD)
    style.configure("Bg.TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("Bg.TLabelframe.Label", background=c["bg"], foreground=c["accent"])

    # Tablas -----------------------------------------------------------------
    style.configure("Treeview", background=c["field"], fieldbackground=c["field"],
                    foreground=c["text"], bordercolor=c["border"],
                    rowheight=23, font=FONT_UI)
    style.map("Treeview",
              background=[("selected", c["selection"])],
              foreground=[("selected", c["text"])])
    style.configure("Treeview.Heading", background=c["panel2"], foreground=c["muted"],
                    bordercolor=c["border"], font=FONT_UI_BOLD, padding=(4, 4))
    style.map("Treeview.Heading", background=[("active", c["border"])])

    # Barras de desplazamiento y progreso ------------------------------------
    style.configure("TScrollbar", background=c["panel2"], troughcolor=c["bg"],
                    bordercolor=c["bg"], arrowcolor=c["muted"])
    style.map("TScrollbar", background=[("active", c["border2"])])
    style.configure("Vertical.TScrollbar", background=c["panel2"], troughcolor=c["bg"])
    style.configure("Horizontal.TScrollbar", background=c["panel2"], troughcolor=c["bg"])
    style.configure("TProgressbar", background=c["accent"], troughcolor=c["field"],
                    bordercolor=c["border"], lightcolor=c["accent"], darkcolor=c["accent"])
    style.configure("TSeparator", background=c["border"])

    # Listas desplegables de los combobox (son widgets Tk clásicos) ----------
    root.option_add("*TCombobox*Listbox.background", c["panel2"])
    root.option_add("*TCombobox*Listbox.foreground", c["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["accent_text"])
    root.option_add("*TCombobox*Listbox.font", "Segoe UI 9")
    root.option_add("*Menu.background", c["panel2"])
    root.option_add("*Menu.foreground", c["text"])
    root.option_add("*Menu.activeBackground", c["accent"])
    root.option_add("*Menu.activeForeground", c["accent_text"])
    return style


def style_text(widget: tk.Text) -> None:
    """Colores para un tk.Text (registro / consola)."""
    c = COLORS
    widget.configure(bg=c["field"], fg=c["text"], insertbackground=c["text"],
                     selectbackground=c["selection"], selectforeground=c["text"],
                     highlightthickness=1, highlightbackground=c["border"],
                     highlightcolor=c["accent"], borderwidth=0, relief="flat",
                     padx=6, pady=4, font=FONT_MONO, wrap="word")


def style_listbox(widget: tk.Listbox) -> None:
    c = COLORS
    widget.configure(bg=c["field"], fg=c["text"], selectbackground=c["selection"],
                     selectforeground=c["text"], highlightthickness=1,
                     highlightbackground=c["border"], highlightcolor=c["accent"],
                     borderwidth=0, relief="flat", font=FONT_MONO,
                     activestyle="none")


def style_axes(ax, *, xlabel="Tiempo", ylabel="Tensión (V)") -> None:
    """Estilo oscuro para los ejes de Matplotlib."""
    c = COLORS
    ax.set_facecolor(c["plot_bg"])
    ax.grid(True, which="major", color=c["grid"], linewidth=0.7)
    ax.grid(True, which="minor", color=c["grid_minor"], linewidth=0.5)
    ax.minorticks_on()
    ax.tick_params(axis="both", labelsize=8, colors=c["muted"],
                   which="both", direction="out")
    for spine in ax.spines.values():
        spine.set_color(c["border2"])
    ax.set_xlabel(xlabel, fontsize=9, color=c["muted"])
    ax.set_ylabel(ylabel, fontsize=9, color=c["muted"])
    ax.title.set_color(c["muted"])


def style_figure(fig) -> None:
    fig.set_facecolor(COLORS["panel"])
    fig.patch.set_facecolor(COLORS["panel"])
