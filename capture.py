"""Captura de forma de onda del Rigol DS1202Z-E por línea de comandos.

Ejemplos:
    python capture.py --list
    python capture.py --demo --png
    python capture.py --channel CH1 --mode screen --csv --png
    python capture.py --channel CH1 --mode memory --points 1200000 --npz

Los archivos se guardan en ./capturas por defecto.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from bootstrap import ensure_runtime

if __name__ == "__main__":
    # Si se lanza con un Python sin dependencias, se reintenta con .venv.
    ensure_runtime()

import numpy as np

from rigol_scope import DemoScope, RigolDS1202Z, analyze_waveform, discover_resources


def _setup_console() -> None:
    """Uso de UTF-8 para que µ, Ω y · se impriman bien en Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def fmt_si(value, unit="", digits=4):
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v == 0:
        return f"0 {unit}"
    if unit in ("Hz", "Sa/s"):
        for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if abs(v) >= factor:
                return f"{v / factor:.{digits}g} {prefix}{unit}"
        return f"{v:.{digits}g} {unit}"
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= factor * 0.999:
            return f"{v / factor:.{digits}g} {prefix}{unit}".strip()
    return f"{v:.{digits}g} {unit}".strip()


def print_measurements(meas: dict) -> None:
    print()
    print(f"  Vpp        : {fmt_si(meas.get('vpp'), 'V')}")
    print(f"  Vmax/Vmin  : {fmt_si(meas.get('vmax'), 'V')} / {fmt_si(meas.get('vmin'), 'V')}")
    print(f"  Vmedia     : {fmt_si(meas.get('vavg'), 'V')}")
    print(f"  Vrms       : {fmt_si(meas.get('vrms'), 'V')}")
    print(f"  Frecuencia : {fmt_si(meas.get('freq'), 'Hz')}")
    print(f"  Período    : {fmt_si(meas.get('period'), 's')}")
    print(f"  Subida     : {fmt_si(meas.get('rise'), 's')}")
    print(f"  Bajada     : {fmt_si(meas.get('fall'), 's')}")
    duty = meas.get("duty")
    print(f"  Ciclo      : {'—' if duty is None else f'{duty * 100:.1f} %'}")


def save_csv(path: str, wf, meas: dict) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write("# Rigol DS1202Z-E waveform capture\n")
        fh.write(f"# channel={wf.channel}, points={wf.points}, mode={wf.mode}, format={wf.data_format}\n")
        fh.write(f"# sample_rate={wf.sample_rate:.6e} Sa/s, x_increment={wf.x_increment:.6e} s\n")
        fh.write(f"# captured_at={wf.timestamp}\n")
        for key, val in meas.items():
            fh.write(f"# {key}={val}\n")
        np.savetxt(fh, np.column_stack((wf.time_s, wf.volts)), fmt=("%.12e", "%.9e"), delimiter=",")


def save_npz(path: str, wf, meas: dict) -> None:
    np.savez_compressed(
        path,
        time_s=wf.time_s,
        volts=wf.volts,
        raw=wf.raw,
        channel=wf.channel,
        points=wf.points,
        sample_rate=wf.sample_rate,
        x_increment=wf.x_increment,
        x_origin=wf.x_origin,
        y_increment=wf.y_increment,
        y_origin=wf.y_origin,
        y_reference=wf.y_reference,
        mode=wf.mode,
        data_format=wf.data_format,
        captured_at=wf.timestamp,
        **{f"m_{k}": (np.nan if v is None else v) for k, v in meas.items()},
    )


def save_png(path: str, wf, meas: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = wf.time_s
    scale, unit = 1.0, "s"
    if wf.points > 1:
        span = max(abs(t[-1] - t[0]), 1e-15)
        if span < 1e-3:
            scale, unit = 1e6, "µs"
        elif span < 1.0:
            scale, unit = 1e3, "ms"

    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=120)
    ax.plot(t * scale, wf.volts, color="#c9a227", linewidth=0.9)
    ax.set_xlabel(f"Tiempo ({unit})")
    ax.set_ylabel("Tensión (V)")
    ax.set_title(
        f"Rigol DS1202Z-E · {wf.channel} · {wf.points} puntos · "
        f"{fmt_si(wf.sample_rate, 'Sa/s')} · {wf.timestamp}"
    )
    ax.grid(True, which="both", alpha=0.3)
    ax.margins(x=0.01)

    lines = [
        f"Vpp {fmt_si(meas.get('vpp'), 'V')}",
        f"Vrms {fmt_si(meas.get('vrms'), 'V')}",
        f"f {fmt_si(meas.get('freq'), 'Hz')}",
        f"T {fmt_si(meas.get('period'), 's')}",
    ]
    ax.text(
        0.01,
        0.98,
        "\n".join(lines),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        family="monospace",
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.8, edgecolor="#cccccc"),
    )
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Captura de formas de onda del Rigol DS1202Z-E")
    parser.add_argument("--resource", help="Recurso VISA (USB0::...::INSTR). Si se omite, se autodetecta.")
    parser.add_argument("--list", action="store_true", help="Listar recursos VISA y salir.")
    parser.add_argument("--demo", action="store_true", help="Captura simulada, sin osciloscopio.")
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2"], help="Canal a capturar.")
    parser.add_argument("--mode", default="screen", choices=["screen", "memory"],
                        help="screen=pantalla (1200 pts), memory=memoria completa.")
    parser.add_argument("--points", type=int, default=0, help="Nº de puntos a leer en modo memory (0=todos).")
    parser.add_argument("--format", dest="data_format", default="BYTE", choices=["BYTE", "WORD"],
                        help="Formato de datos SCPI.")
    parser.add_argument("--no-single", action="store_true", help="No disparar en SINGle; leer lo que haya en pantalla.")
    parser.add_argument("--outdir", default="capturas", help="Carpeta de salida.")
    parser.add_argument("--csv", action="store_true", help="Guardar CSV (todos los puntos).")
    parser.add_argument("--npz", action="store_true", help="Guardar NPZ con arrays y metadatos.")
    parser.add_argument("--png", action="store_true", help="Guardar imagen PNG de la traza.")
    parser.add_argument("--scpi-meas", action="store_true", help="Incluir también las medidas del osciloscopio.")
    args = parser.parse_args(argv)

    if args.list:
        print("Recursos VISA encontrados:")
        res = discover_resources()
        if not res:
            print("  (ninguno)")
            print("  ¿Está encendido el osciloscopio y conectado por USB? ¿NI-VISA instalado?")
        for r in res:
            print(f"  {r}")
        return 0

    if args.demo:
        scope = DemoScope().open()
    else:
        scope = RigolDS1202Z(resource=args.resource).open()

    try:
        print(f"Instrumento: {scope.idn}")
        if not args.demo and not args.no_single:
            print("Disparando en SINGle...")
            scope.single()
            if not scope.wait_for_stop(timeout=8.0):
                print("  Aviso: no se detectó disparo; se captura el estado actual.")

        print(f"Capturando {args.channel} ({args.mode}, {args.data_format})...")

        def progress(frac, text):
            print(f"\r  {frac * 100:5.1f}%  {text}", end="", flush=True)

        wf = scope.capture(
            channel=args.channel,
            mode=args.mode,
            data_format=args.data_format,
            points=args.points or None,
            progress=progress,
        )
        print()

        meas = analyze_waveform(wf.time_s, wf.volts)
        actual_fs = 1.0 / wf.x_increment if wf.x_increment else 0.0
        print(f"  Puntos     : {wf.points:,}".replace(",", "."))
        print(f"  Fs         : {fmt_si(actual_fs, 'Sa/s')}")
        print(f"  Ventana    : {fmt_si(wf.duration, 's')}")
        print_measurements(meas)

        if args.scpi_meas:
            scpi = scope.scpi_measurements(args.channel)
            print("\n  Medidas del osciloscopio (SCPI):")
            for key in ("VPP", "VMAX", "VMIN", "FREQUENCY", "PERIOD"):
                if key in scpi:
                    unit = "V" if key.startswith("V") else ("Hz" if key == "FREQUENCY" else "s")
                    print(f"    {key:<10}: {fmt_si(scpi[key], unit)}")
            # Contraste con las medidas locales: valida el escalado YINCrement/
            # YORigin/YREFerence del bloque de preámbulo.
            print("\n  Contraste local vs osciloscopio:")
            pairs = (("VPP", "vpp"), ("VMAX", "vmax"), ("VMIN", "vmin"), ("FREQUENCY", "freq"), ("PERIOD", "period"))
            for scpi_key, local_key in pairs:
                ref = scpi.get(scpi_key)
                got = meas.get(local_key)
                if ref is None or got is None:
                    continue
                unit = "V" if scpi_key.startswith("V") else ("Hz" if scpi_key == "FREQUENCY" else "s")
                diff = got - ref
                rel = diff / ref if ref else float("nan")
                print(
                    f"    {scpi_key:<10}: local {fmt_si(got, unit):>12}  "
                    f"error {rel * 100:+.2f} %"
                )


        os.makedirs(args.outdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.join(args.outdir, f"{args.channel}_{stamp}")
        if args.csv:
            save_csv(base + ".csv", wf, meas)
            print(f"\n  CSV  -> {base}.csv")
        if args.npz:
            save_npz(base + ".npz", wf, meas)
            print(f"  NPZ  -> {base}.npz")
        if args.png:
            save_png(base + ".png", wf, meas)
            print(f"  PNG  -> {base}.png")

        if not (args.csv or args.npz or args.png):
            print("\n  (sin salida a disco; usa --csv, --npz o --png)")
        return 0
    finally:
        scope.close()


if __name__ == "__main__":
    _setup_console()
    sys.exit(main())
