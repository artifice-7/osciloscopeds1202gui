"""Arranque robusto: usa siempre el Python del entorno virtual del proyecto.

Si el script se lanza con un Python que no tiene las dependencias instaladas,
se relanza automáticamente con ``.venv\\Scripts\\python.exe``. Si el entorno
virtual no existe, se muestra una instrucción clara en lugar de un traceback.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
MARKER = "OSC_VENV_REEXEC"
REQUIRED = ("pyvisa", "numpy", "matplotlib")


def _missing_dependencies() -> list[str]:
    missing = []
    for name in REQUIRED:
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    return missing


def ensure_runtime(script: str | None = None) -> None:
    """Relanza el script actual con el Python del entorno virtual si hace falta."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.environ.get(MARKER) == "1":
        return
    if not _missing_dependencies():
        return

    script = script or os.path.abspath(sys.argv[0])

    if not os.path.isfile(VENV_PYTHON):
        print(
            "Faltan dependencias de Python (pyvisa, numpy, matplotlib) y no existe\n"
            "el entorno virtual del proyecto.\n\n"
            "Créalo una sola vez con:\n"
            "    python -m venv .venv\n"
            '    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n\n'
            "O ejecuta directamente iniciar.bat (lo hace todo automáticamente).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(VENV_PYTHON):
        print(
            "El entorno virtual no tiene las dependencias instaladas.\n"
            'Ejecuta: .venv\\Scripts\\python.exe -m pip install -r requirements.txt',
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"Usando el entorno virtual del proyecto: {VENV_PYTHON}")
    print("(se reinicia el programa con ese intérprete)\n", flush=True)

    os.environ[MARKER] = "1"
    args = [VENV_PYTHON, script, *sys.argv[1:]]
    try:
        os.execv(VENV_PYTHON, args)
    except OSError as exc:
        print(f"No se pudo relanzar con el entorno virtual: {exc}", file=sys.stderr)
        raise SystemExit(1)
