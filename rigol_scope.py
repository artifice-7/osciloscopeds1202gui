"""
Driver SCPI para el osciloscopio Rigol DS1202Z-E (serie DS1000Z-E).

Comandos utilizados (ver carpeta Documentation):
  :RUN / :STOP / :SINGle / :TFORce / :AUToscale
  :ACQuire:TYPE, :ACQuire:AVERages, :ACQuire:MDEPth?, :ACQuire:SRATe?
  :CHANnel<n>:DISPlay|SCALe|OFFSet|COUPling|PROBe|BWLimit|INVert
  :TIMebase:MODE|MAIN:SCALe|MAIN:OFFSet
  :TRIGger:MODE|SWEep|EDGe:SOURce|EDGe:SLOPe|EDGe:LEVel|COUPling|STATus?
  :WAVeform:SOURce|MODE|FORMat|STARt|STOP|PREamble?|DATA?
  :MEASure:ITEM? <param>,<source>
  :SYSTem:ERRor?

Escalado de los datos (manual de programación Rigol):
    voltaje = (dato_crudo - YORigin - YREFerence) * YINCrement
    tiempo  = XORigin + indice * XINCrement
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import numpy as np
import pyvisa

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CHANNELS = ("CHAN1", "CHAN2")
CHANNEL_NUM = {"CHAN1": 1, "CHAN2": 2, "CH1": 1, "CH2": 2, 1: 1, 2: 2}
BINARY_CHUNK = 250_000  # puntos máximos por lectura TMC (RAW)
DEFAULT_TIMEOUT_MS = 20_000

# Formato -> tipo numpy para :WAVeform:FORMat
DTYPES = {
    "BYTE": np.dtype("u1"),
    "WORD": np.dtype("<u2"),
}

# Errores SCPI que indican un valor de medida no válido
UNDEFINED = 9.9e37


@dataclass
class Waveform:
    """Una captura completa con sus parámetros de escalado."""

    channel: str
    time_s: np.ndarray
    volts: np.ndarray
    raw: np.ndarray
    x_increment: float
    x_origin: float
    y_increment: float
    y_origin: float
    y_reference: float
    points: int
    sample_rate: float
    mode: str
    data_format: str
    captured_at: float = field(default_factory=time.time)

    @property
    def duration(self) -> float:
        if self.points < 2:
            return 0.0
        return float(self.time_s[-1] - self.time_s[0])

    @property
    def timestamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.captured_at))


# ---------------------------------------------------------------------------
# Utilidades de medida
# ---------------------------------------------------------------------------


def _median_or_none(values) -> Optional[float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    return float(np.median(values))


def _robust_median(values) -> Optional[float]:
    """Mediana descartando valores muy alejados del valor central.

    Evita que cruces espurios (ruido de cuantización) arruinen el período.
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if values.size == 0:
        return None
    med = float(np.median(values))
    if med <= 0:
        return None
    keep = values[(values >= 0.5 * med) & (values <= 2.0 * med)]
    if keep.size == 0:
        return med
    return float(np.mean(keep))


def _edge_points(t: np.ndarray, v: np.ndarray, low: float, high: float):
    """Flancos detectados con disparador de Schmitt entre el 10 % y el 90 %.

    El estado solo cambia al cruzar los umbrales alto/bajo, así que el ruido
    de cuantización alrededor del nivel medio no genera flancos falsos.
    Devuelve (subidas, bajadas, tiempos_subida, tiempos_bajada); las subidas
    y bajadas son el instante interpolado del cruce del nivel medio, y los
    tiempos son la duración 10 %-90 % de cada flanco.
    """
    n = v.size
    if n < 3:
        return np.empty(0), np.empty(0), [], []

    events = np.where(v >= high, 1, np.where(v <= low, 0, -1))
    definite = np.flatnonzero(events >= 0)
    if definite.size == 0:
        return np.empty(0), np.empty(0), [], []

    start = int(definite[0])
    events = events[start:]
    t = t[start:]
    v = v[start:]

    last = np.where(events >= 0, np.arange(events.size), 0)
    np.maximum.accumulate(last, out=last)
    state = events[last]

    mid = 0.5 * (low + high)
    rises: list[float] = []
    falls: list[float] = []
    rise_times: list[float] = []
    fall_times: list[float] = []

    for c in np.flatnonzero(state[1:] != state[:-1]) + 1:
        p = int(last[c - 1])
        if p >= c:
            continue
        v0, v1 = float(v[p]), float(v[c])
        frac = 0.5 if v1 == v0 else (mid - v0) / (v1 - v0)
        frac = min(max(frac, 0.0), 1.0)
        t_mid = float(t[p] + frac * (t[c] - t[p]))
        duration = float(t[c] - t[p])
        if state[c] == 1:
            rises.append(t_mid)
            rise_times.append(duration)
        else:
            falls.append(t_mid)
            fall_times.append(duration)

    return np.asarray(rises), np.asarray(falls), rise_times, fall_times


def analyze_waveform(time_s: np.ndarray, volts: np.ndarray) -> dict:
    """Medidas básicas calculadas localmente sobre la traza capturada.

    Devuelve un diccionario con vmax, vmin, vpp, vavg, vrms, freq, period,
    rise, fall y duty. Los valores no determinables son None.
    """
    result = {
        "vmax": None,
        "vmin": None,
        "vpp": None,
        "vavg": None,
        "vrms": None,
        "freq": None,
        "period": None,
        "rise": None,
        "fall": None,
        "duty": None,
    }
    if volts is None or volts.size < 4:
        return result

    v = np.asarray(volts, dtype=float)
    t = np.asarray(time_s, dtype=float)
    finite = np.isfinite(v)
    if finite.sum() < 4:
        return result

    vmax = float(np.max(v[finite]))
    vmin = float(np.min(v[finite]))
    vpp = vmax - vmin
    result["vmax"] = vmax
    result["vmin"] = vmin
    result["vpp"] = vpp
    result["vavg"] = float(np.mean(v[finite]))
    result["vrms"] = float(np.sqrt(np.mean(v[finite] ** 2)))
    if vpp <= 0:
        return result

    low = vmin + 0.1 * vpp
    high = vmin + 0.9 * vpp
    rises, falls, rise_times, fall_times = _edge_points(t, v, low, high)
    result["rise"] = _median_or_none(rise_times)
    result["fall"] = _median_or_none(fall_times)

    period = _robust_median(np.diff(rises)) if rises.size >= 2 else None
    if period and period > 0:
        result["period"] = period
        result["freq"] = 1.0 / period

        # Ciclo de trabajo: cada subida con la siguiente bajada.
        if falls.size:
            j = np.searchsorted(falls, rises, side="right")
            valid = j < falls.size
            if valid.any():
                widths = falls[j[valid]] - rises[valid]
                widths = widths[widths > 0]
                duty = _median_or_none(widths / period)
                if duty is not None and 0.0 <= duty <= 1.0:
                    result["duty"] = duty

    return result


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


_VISA_DLL_PATHS = (
    r"C:\Program Files\IVI Foundation\VISA\Win64\Bin",
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\Bin",
)

_dll_paths_ready = False


def _prepare_visa_dll_path() -> None:
    """Añade las carpetas de NI-VISA a la búsqueda de DLL de Windows.

    El instalador de NI-VISA actualiza PATH, pero los procesos ya abiertos
    (o lanzados justo después de instalar, antes de reiniciar) pueden no verlo.
    Registrar los directorios explícitamente evita el error
    VI_ERROR_LIBRARY_NFOUND (-1073807202).
    """
    global _dll_paths_ready
    if _dll_paths_ready or not sys.platform.startswith("win"):
        return
    _dll_paths_ready = True
    extra = os.pathsep.join(p for p in _VISA_DLL_PATHS if os.path.isdir(p))
    if not extra:
        return
    os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")
    add_dir = getattr(os, "add_dll_directory", None)
    if add_dir is not None:
        for path in _VISA_DLL_PATHS:
            if os.path.isdir(path):
                try:
                    add_dir(path)
                except OSError:
                    pass


def _looks_like_scope(resource: str) -> bool:
    """Filtra puertos serie locales y prioriza el USB del DS1202Z-E."""
    upper = resource.upper()
    if upper.startswith("ASRL"):
        return False
    return True


def _scope_priority(resource: str) -> int:
    upper = resource.upper()
    if "0X1AB1::0X0517" in upper:  # VID/PID del DS1000Z-E
        return 0
    if upper.startswith("USB"):
        return 1
    if upper.startswith("TCPIP"):
        return 2
    return 3


def discover_resources() -> list[str]:
    """Recursos VISA USB/TCPIP vistos por cualquier backend disponible."""
    _prepare_visa_dll_path()
    found: list[str] = []
    seen: set[str] = set()
    for backend in (None, "@py"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rm = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
                resources = list(rm.list_resources())
                rm.close()
        except Exception:
            continue
        for res in resources:
            if res not in seen:
                seen.add(res)
                found.append(res)
        if found:
            # Si el VISA del sistema ya encontró instrumentos, no hace falta
            # seguir probando backends alternativos (ni sus avisos).
            break
    found.sort(key=_scope_priority)
    return found


class RigolDS1202Z:
    """Cliente SCPI mínimo y robusto para el DS1202Z-E."""

    def __init__(self, resource: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT_MS):
        self.resource = resource
        self.backend: Optional[str] = None
        self.timeout = int(timeout)
        self.inst = None
        self._rm = None
        self._idn = ""

    # -- ciclo de vida ------------------------------------------------------

    def open(self) -> "RigolDS1202Z":
        _prepare_visa_dll_path()
        attempts: list[tuple[Optional[str], str]] = []
        if self.resource:
            attempts = [(None, self.resource), ("@py", self.resource)]
        else:
            for res in discover_resources():
                if not _looks_like_scope(res):
                    continue
                attempts.append((None, res))
                attempts.append(("@py", res))
        if not attempts:
            raise RuntimeError(
                "No se encontró ningún instrumento VISA. Comprueba el cable USB, "
                "que el osciloscopio esté encendido y que NI-VISA esté instalado."
            )

        errors: list[str] = []
        for backend, res in attempts:
            try:
                rm = pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
                inst = rm.open_resource(res)
                inst.timeout = self.timeout
                inst.write_termination = "\n"
                inst.read_termination = "\n"
                try:
                    inst.chunk_size = 512 * 1024
                except Exception:
                    pass
                idn = inst.query("*IDN?").strip()
                self._rm = rm
                self.inst = inst
                self.backend = backend or "VISA del sistema"
                self.resource = res
                self._idn = idn
                self._clear_buffer()
                return self
            except Exception as exc:
                errors.append(f"{res} [{backend or 'VISA'}]: {exc}")
                try:
                    inst.close()
                except Exception:
                    pass

        raise RuntimeError(
            "No se pudo abrir el osciloscopio. Intentos:\n  " + "\n  ".join(errors)
        )

    def close(self) -> None:
        try:
            if self.inst is not None:
                self.inst.close()
        except Exception:
            pass
        try:
            if self._rm is not None:
                self._rm.close()
        except Exception:
            pass
        self.inst = None
        self._rm = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- E/S básica ---------------------------------------------------------

    @property
    def idn(self) -> str:
        return self._idn

    def write(self, cmd: str) -> None:
        if self.inst is None:
            raise RuntimeError("Osciloscopio no conectado")
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        """Escritura + lectura única de una respuesta de texto."""
        if self.inst is None:
            raise RuntimeError("Osciloscopio no conectado")
        self.inst.write(cmd)
        resp = self.inst.read()
        return resp.strip()

    def query_block(self, cmd: str, timeout_ms: Optional[int] = None) -> bytes:
        """Ejecuta una consulta que devuelve un bloque binario TMC."""
        if self.inst is None:
            raise RuntimeError("Osciloscopio no conectado")
        self.write(cmd)
        old = self.inst.timeout
        self.inst.timeout = max(old, int(timeout_ms or 30_000))
        try:
            return self._read_binary_block()
        finally:
            self.inst.timeout = old

    def query_float(self, cmd: str) -> Optional[float]:
        txt = self.query(cmd)
        try:
            val = float(txt.split(",")[0])
        except (TypeError, ValueError):
            return None
        if abs(val) >= UNDEFINED:
            return None
        return val

    def _clear_buffer(self) -> None:
        try:
            n = int(self.inst.bytes_in_buffer)
        except Exception:
            n = 0
        if n > 0:
            try:
                self.inst.read_bytes(n)
            except Exception:
                pass

    def check_errors(self) -> list[str]:
        """Lee y vacía la cola de errores del instrumento."""
        errors = []
        for _ in range(12):
            try:
                msg = self.query(":SYSTem:ERRor?")
            except Exception:
                break
            if not msg:
                break
            if msg.startswith("0,") or msg.startswith('0,"'):
                break
            errors.append(msg)
        return errors

    # -- lectura de ajustes -------------------------------------------------

    @staticmethod
    def _norm_ch(channel) -> str:
        key = channel.upper() if isinstance(channel, str) else channel
        if key in ("CH1", "CHAN1", 1):
            return "CHAN1"
        if key in ("CH2", "CHAN2", 2):
            return "CHAN2"
        raise ValueError(f"Canal no válido: {channel!r}")

    def get_channel_settings(self, channel) -> dict:
        ch = self._norm_ch(channel)
        coupling = self.query(f":{ch}:COUPling?")
        coupling = coupling.split(",")[0] if coupling else "DC"
        probe = self.query_float(f":{ch}:PROBe?") or 1.0
        bw = self.query(f":{ch}:BWLimit?")
        invert = self.query(f":{ch}:INVert?")
        display = self.query(f":{ch}:DISPlay?")
        return {
            "display": display.strip().upper() in ("1", "ON"),
            "scale": self.query_float(f":{ch}:SCALe?") or 1.0,
            "offset": self.query_float(f":{ch}:OFFSet?") or 0.0,
            "coupling": coupling.strip().upper(),
            "probe": probe,
            "bwlimit": bw.strip().upper().startswith("20"),
            "invert": invert.strip().upper() in ("1", "ON"),
        }

    def get_timebase_settings(self) -> dict:
        return {
            "scale": self.query_float(":TIMebase:MAIN:SCALe?") or 1e-3,
            "offset": self.query_float(":TIMebase:MAIN:OFFSet?") or 0.0,
            "mode": self.query(":TIMebase:MODE?").strip().upper() or "MAIN",
        }

    def get_trigger_settings(self) -> dict:
        source = self.query(":TRIGger:EDGe:SOURce?").strip().upper()
        slope = self.query(":TRIGger:EDGe:SLOPe?").strip().upper()
        return {
            "mode": self.query(":TRIGger:MODE?").strip().upper(),
            "sweep": self.query(":TRIGger:SWEep?").strip().upper(),
            "source": source or "CHAN1",
            "slope": slope or "POSITIVE",
            "level": self.query_float(":TRIGger:EDGe:LEVel?") or 0.0,
            "coupling": self.query(":TRIGger:COUPling?").strip().upper() or "DC",
        }

    def get_acquisition_settings(self) -> dict:
        return {
            "type": self.query(":ACQuire:TYPE?").strip().upper() or "NORMAL",
            "averages": self.query_float(":ACQuire:AVERages?") or 16,
            "depth": self.query(":ACQuire:MDEPth?").strip().upper() or "AUTO",
            "sample_rate": self.query_float(":ACQuire:SRATe?") or 0.0,
        }

    # -- configuración ------------------------------------------------------

    def apply_channel(
        self,
        channel,
        display: Optional[bool] = None,
        scale: Optional[float] = None,
        offset: Optional[float] = None,
        coupling: Optional[str] = None,
        probe: Optional[float] = None,
        bwlimit: Optional[bool] = None,
        invert: Optional[bool] = None,
    ) -> None:
        ch = self._norm_ch(channel)
        # El orden importa: la sonda condiciona el rango de la escala.
        if probe is not None:
            self.write(f":{ch}:PROBe {probe:g}")
        if coupling:
            self.write(f":{ch}:COUPling {coupling}")
        if scale is not None:
            self.write(f":{ch}:SCALe {scale:.6g}")
        if offset is not None:
            self.write(f":{ch}:OFFSet {offset:.6g}")
        if display is not None:
            self.write(f":{ch}:DISPlay {'ON' if display else 'OFF'}")
        if bwlimit is not None:
            self.write(f":{ch}:BWLimit {'20M' if bwlimit else 'OFF'}")
        if invert is not None:
            self.write(f":{ch}:INVert {'ON' if invert else 'OFF'}")

    def apply_timebase(
        self,
        scale: Optional[float] = None,
        offset: Optional[float] = None,
        mode: Optional[str] = None,
    ) -> None:
        if mode:
            self.write(f":TIMebase:MODE {mode}")
        if offset is not None:
            self.write(f":TIMebase:MAIN:OFFSet {offset:.6g}")
        if scale is not None:
            self.write(f":TIMebase:MAIN:SCALe {scale:.6g}")

    def apply_trigger(
        self,
        sweep: Optional[str] = None,
        source: Optional[str] = None,
        slope: Optional[str] = None,
        level: Optional[float] = None,
        coupling: Optional[str] = None,
        mode: str = "EDGE",
    ) -> None:
        self.write(f":TRIGger:MODE {mode}")
        if coupling:
            self.write(f":TRIGger:COUPling {coupling}")
        if source:
            self.write(f":TRIGger:EDGe:SOURce {self._norm_ch(source)}")
        if slope:
            sl = slope.upper()
            if sl.startswith("NEG") or sl in ("BAJADA", "FALLING", "NEGATIVE"):
                sl = "NEGative"
            else:
                sl = "POSitive"
            self.write(f":TRIGger:EDGe:SLOPe {sl}")
        if level is not None:
            self.write(f":TRIGger:EDGe:LEVel {level:.6g}")
        if sweep:
            sw = sweep.upper()
            if sw.startswith("NORM"):
                sw = "NORMal"
            elif sw.startswith("SING"):
                sw = "SINGle"
            else:
                sw = "AUTO"
            self.write(f":TRIGger:SWEep {sw}")

    def apply_acquisition(
        self,
        type: Optional[str] = None,
        averages: Optional[int] = None,
        depth: Optional[str] = None,
    ) -> None:
        if type:
            t = type.upper()
            if t.startswith("AVER"):
                t = "AVERages"
            elif t.startswith("PEAK"):
                t = "PEAK"
            elif t.startswith("HRES"):
                t = "HRESolution"
            else:
                t = "NORMal"
            self.write(f":ACQuire:TYPE {t}")
        if averages is not None:
            self.write(f":ACQuire:AVERages {int(averages)}")
        if depth is not None:
            d = str(depth).upper()
            if d in ("AUTO", ""):
                self.write(":ACQuire:MDEPth AUTO")
            else:
                self.write(f":ACQuire:MDEPth {int(float(d))}")

    # -- control de adquisición --------------------------------------------

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def single(self) -> None:
        self.write(":SINGle")

    def force(self) -> None:
        self.write(":TFORce")

    def autoscale(self) -> None:
        self.write(":AUToscale")
        try:
            self.inst.timeout = max(self.timeout, 15_000)
            self.query("*OPC?")
        finally:
            self.inst.timeout = self.timeout

    def trigger_status(self) -> str:
        return self.query(":TRIGger:STATus?").strip().upper()

    def wait_for_stop(self, timeout: float = 10.0, poll: float = 0.1) -> bool:
        """Espera a que una adquisición SINGle termine (estado STOP)."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                st = self.trigger_status()
            except Exception:
                return False
            if st.startswith("STOP"):
                return True
            time.sleep(poll)
        return False

    # -- lectura de la forma de onda ---------------------------------------

    def _read_preamble(self) -> dict:
        vals = self.query(":WAVeform:PREamble?").split(",")
        if len(vals) < 10:
            raise RuntimeError(f"PREamble inesperado: {vals!r}")
        num = [float(x) for x in vals[:10]]
        return {
            "format": int(num[0]),
            "type": int(num[1]),
            "points": int(num[2]),
            "count": int(num[3]),
            "x_increment": num[4],
            "x_origin": num[5],
            "x_reference": num[6],
            "y_increment": num[7],
            "y_origin": num[8],
            "y_reference": num[9],
        }

    def _read_binary_block(self) -> bytes:
        """Lee un bloque TMC (#<n><longitud><datos>) de una consulta binaria.

        Lee el bloque en fragmentos para no bloquear el hilo durante mucho
        tiempo y devuelve los datos recibidos aunque el instrumento cierre
        antes de completar todos los bytes declarados (comportamiento
        observado en :DISPlay:DATA? con algunos formatos).
        """
        inst = self.inst
        lead = inst.read_bytes(1)
        guard = 0
        while lead != b"#" and guard < 4:
            lead = inst.read_bytes(1)
            guard += 1
        if lead != b"#":
            raise RuntimeError("No se recibió un bloque binario TMC válido")
        ndigits = int(inst.read_bytes(1).decode("ascii"))
        if not 1 <= ndigits <= 9:
            raise RuntimeError(f"Cabecera TMC no válida (n={ndigits})")
        nbytes = int(inst.read_bytes(ndigits).decode("ascii"))
        data = bytearray()
        timeout = inst.timeout
        chunk_timeout = max(1000, timeout // 8) if timeout else 2000
        try:
            inst.timeout = chunk_timeout
            while len(data) < nbytes:
                chunk = inst.read_bytes(min(65536, nbytes - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        except Exception:
            # Si ya recibimos parte del bloque, lo devolvemos; el llamador
            # decidirá si la imagen es válida.
            if not data:
                raise
        finally:
            try:
                inst.timeout = timeout
            except Exception:
                pass
        self._clear_buffer()
        return bytes(data)

    def capture(
        self,
        channel="CHAN1",
        mode: str = "screen",
        data_format: str = "BYTE",
        points: Optional[int] = None,
        progress: Optional[Callable[[float, str], None]] = None,
    ) -> Waveform:
        """Captura una traza.

        mode: 'screen' (1200 puntos de pantalla) o 'memory' (memoria completa,
              leída por bloques de 250 kpuntos).
        data_format: 'BYTE' (8 bits) o 'WORD' (16 bits).
        points: recorta la captura de memoria a ese número de puntos.
        """
        if self.inst is None:
            raise RuntimeError("Osciloscopio no conectado")
        ch = self._norm_ch(channel)
        fmt = data_format.upper()
        if fmt not in DTYPES:
            raise ValueError("data_format debe ser 'BYTE' o 'WORD'")
        mode = mode.lower()
        if mode not in ("screen", "memory"):
            raise ValueError("mode debe ser 'screen' o 'memory'")

        self.write(f":WAVeform:SOURce {ch}")
        self.write(f":WAVeform:FORMat {fmt}")

        if mode == "screen":
            self.write(":WAVeform:MODE NORMal")
            self.write(":WAVeform:STARt 1")
            self.write(":WAVeform:STOP 1200")
        else:
            # En RAW la memoria no se puede leer mientras el osciloscopio barre.
            if not self.trigger_status().startswith("STOP"):
                self.stop()
                time.sleep(0.3)
            self.write(":WAVeform:MODE RAW")
            time.sleep(0.3)

        pre = self._read_preamble()
        if mode == "screen":
            total = int(pre["points"])
        else:
            depth = self.query(":ACQuire:MDEPth?").strip()
            try:
                total = int(float(depth))
            except ValueError:
                total = int(pre["points"])
            if points:
                total = min(total, int(points))
            self.write(":WAVeform:STARt 1")
            self.write(f":WAVeform:STOP {total}")
            # El instrumento puede recortar STOP al tamaño real de memoria.
            total = int(float(self.query(":WAVeform:STOP?")))
            pre = self._read_preamble()

        if total <= 0:
            raise RuntimeError("La captura no contiene puntos")

        dtype = DTYPES[fmt]
        raw = np.zeros(total, dtype=np.uint16)
        self.inst.timeout = max(self.timeout, 30_000)
        try:
            if mode == "screen":
                self.write(":WAVeform:DATA?")
                block = self._read_binary_block()
                arr = np.frombuffer(block, dtype=dtype)
                n = min(total, arr.size)
                raw[:n] = arr[:n]
                if progress:
                    progress(1.0, f"{n} puntos")
            else:
                chunk = min(BINARY_CHUNK, max(1000, total))
                for start in range(1, total + 1, chunk):
                    stop = min(start + chunk - 1, total)
                    self.write(f":WAVeform:STARt {start}")
                    self.write(f":WAVeform:STOP {stop}")
                    self.write(":WAVeform:DATA?")
                    block = self._read_binary_block()
                    arr = np.frombuffer(block, dtype=dtype)
                    expected = stop - start + 1
                    if arr.size != expected:
                        if arr.size < expected:
                            arr = np.pad(arr, (0, expected - arr.size), mode="edge")
                        else:
                            arr = arr[:expected]
                    raw[start - 1 : stop] = arr
                    if progress:
                        progress(stop / total, f"{stop:,}/{total:,} puntos".replace(",", "."))
        finally:
            self.inst.timeout = self.timeout

        x_inc = float(pre["x_increment"])
        x_org = float(pre["x_origin"])
        y_inc = float(pre["y_increment"])
        y_org = float(pre["y_origin"])
        y_ref = float(pre["y_reference"])

        times = x_org + x_inc * np.arange(total, dtype=np.float64)
        volts = (raw.astype(np.float64) - y_org - y_ref) * y_inc
        sample_rate = 1.0 / x_inc if x_inc else 0.0

        return Waveform(
            channel=ch,
            time_s=times,
            volts=volts,
            raw=raw,
            x_increment=x_inc,
            x_origin=x_org,
            y_increment=y_inc,
            y_origin=y_org,
            y_reference=y_ref,
            points=total,
            sample_rate=sample_rate,
            mode="Pantalla" if mode == "screen" else "Memoria",
            data_format=fmt,
        )

    # -- captura de la pantalla del instrumento -----------------------------

    def screenshot(self, color: str = "ON", invert: str = "OFF", fmt: str = "PNG") -> bytes:
        """Lee la imagen de la pantalla del osciloscopio (:DISPlay:DATA?).

        Devuelve los bytes de la imagen en el formato solicitado (PNG, BMP24,
        BMP8, JPEG o TIFF).
        """
        color = (color or "ON").upper()
        invert = (invert or "OFF").upper()
        fmt = (fmt or "PNG").upper()
        cmd = f":DISPlay:DATA? {color},{invert},{fmt}"
        return self.query_block(cmd, timeout_ms=45_000)

    # -- medidas del propio osciloscopio -----------------------------------

    MEASURE_ITEMS = ("VPP", "VMAX", "VMIN", "FREQuency", "PERiod")

    def scpi_measurements(self, channel="CHAN1", items: Iterable[str] = MEASURE_ITEMS) -> dict:
        ch = self._norm_ch(channel)
        try:
            # Deja libre la lista de medidas en pantalla (máx. 5 elementos).
            self.write(":MEASure:CLEar ALL")
        except Exception:
            pass
        out = {}
        for item in items:
            try:
                out[item.upper()] = self.query_float(f":MEASure:ITEM? {item},{ch}")
            except Exception:
                out[item.upper()] = None
        return out


# ---------------------------------------------------------------------------
# Modo demostración (sin hardware)
# ---------------------------------------------------------------------------


def _demo_screen_png(stopped: bool = False, width: int = 480, height: int = 280) -> bytes:
    """Genera una imagen PNG simulada de la pantalla del osciloscopio."""
    import struct
    import zlib

    img = np.empty((height, width, 3), dtype=np.uint8)
    img[:, :] = (18, 22, 29)
    img[:, ::48] = (31, 37, 46)
    img[::40, :] = (31, 37, 46)
    img[height // 2, :] = (58, 68, 82)
    img[:, width // 2] = (58, 68, 82)

    x = np.arange(width)
    phase = (time.time() * 2.0) % (2 * np.pi)
    y = (height / 2 - 78 * np.sin(x / width * 4 * np.pi + phase)).astype(int)
    for offset in (-1, 0, 1):
        img[np.clip(y + offset, 0, height - 1), x] = (251, 191, 36)

    # Indicador RUN/STOP en la esquina.
    img[10:34, 10:70] = (239, 68, 68) if stopped else (34, 197, 94)

    raw = b"".join(b"\x00" + img[row].tobytes() for row in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class DemoScope:
    """Simula la interfaz pública de RigolDS1202Z para probar la aplicación."""

    def __init__(self, *args, **kwargs):
        self.resource = "DEMO"
        self.backend = "simulación"
        self._idn = "RIGOL TECHNOLOGIES,DS1202Z-E,DEMO000000,00.04.04 (modo demo)"
        self._ch = {
            1: {
                "display": True,
                "scale": 0.5,
                "offset": 0.0,
                "coupling": "DC",
                "probe": 1.0,
                "bwlimit": False,
                "invert": False,
            },
            2: {
                "display": True,
                "scale": 1.0,
                "offset": 0.0,
                "coupling": "DC",
                "probe": 1.0,
                "bwlimit": False,
                "invert": False,
            },
        }
        self._tb = {"scale": 0.0005, "offset": 0.0, "mode": "MAIN"}
        self._trig = {
            "mode": "EDGE",
            "sweep": "AUTO",
            "source": "CHAN1",
            "slope": "POSITIVE",
            "level": 0.0,
            "coupling": "DC",
        }
        self._acq = {"type": "NORMAL", "averages": 16, "depth": "AUTO", "sample_rate": 0.0}
        self._stopped = False

    def open(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    @property
    def idn(self):
        return self._idn

    def write(self, cmd):  # pragma: no cover - compatibilidad
        pass

    def query(self, cmd):
        upper = cmd.strip().upper()
        if upper.startswith("*IDN?"):
            return self._idn
        if "SYSTEM:ERROR" in upper:
            return '0,"No error"'
        if "TRIGGER:STATUS" in upper:
            return self.trigger_status()
        if upper.startswith(":ACQUIRE:MDEPTH?"):
            return self._acq.get("depth", "AUTO")
        if upper.startswith(":ACQUIRE:SRATE?"):
            return f"{self._acq.get('sample_rate', 0.0):.6e}"
        return ""

    def query_block(self, cmd, timeout_ms=None):
        return self.screenshot()

    def screenshot(self, color="ON", invert="OFF", fmt="PNG"):
        return _demo_screen_png(self._stopped)

    def check_errors(self):
        return []

    def get_channel_settings(self, channel):
        return dict(self._ch[1 if str(channel).upper() in ("CH1", "CHAN1", "1") else 2])

    def get_timebase_settings(self):
        return dict(self._tb)

    def get_trigger_settings(self):
        return dict(self._trig)

    def get_acquisition_settings(self):
        return dict(self._acq)

    def apply_channel(self, channel, **kw):
        n = 1 if str(channel).upper() in ("CH1", "CHAN1", "1") else 2
        for k in ("display", "scale", "offset", "coupling", "probe", "bwlimit", "invert"):
            if kw.get(k) is not None:
                self._ch[n][k] = kw[k]

    def apply_timebase(self, scale=None, offset=None, mode=None):
        if scale is not None:
            self._tb["scale"] = scale
        if offset is not None:
            self._tb["offset"] = offset
        if mode is not None:
            self._tb["mode"] = mode

    def apply_trigger(self, sweep=None, source=None, slope=None, level=None, coupling=None, mode="EDGE"):
        if sweep:
            s = sweep.upper()
            self._trig["sweep"] = "NORMAL" if s.startswith("NORM") else ("SINGLE" if s.startswith("SING") else "AUTO")
        if source:
            self._trig["source"] = "CHAN1" if str(source).upper() in ("CH1", "CHAN1", "1") else "CHAN2"
        if slope:
            self._trig["slope"] = "NEGATIVE" if str(slope).upper().startswith(("NEG", "BAJ", "FALL")) else "POSITIVE"
        if level is not None:
            self._trig["level"] = level
        if coupling:
            self._trig["coupling"] = coupling.upper()
        self._trig["mode"] = mode

    def apply_acquisition(self, type=None, averages=None, depth=None):
        if type:
            t = type.upper()
            self._acq["type"] = (
                "AVERAGES" if t.startswith("AVER") else "PEAK" if t.startswith("PEAK") else "HRES" if t.startswith("HRES") else "NORMAL"
            )
        if averages is not None:
            self._acq["averages"] = averages
        if depth is not None:
            self._acq["depth"] = str(depth).upper()

    def run(self):
        self._stopped = False

    def stop(self):
        self._stopped = True

    def single(self):
        self._stopped = True

    def force(self):
        pass

    def autoscale(self):
        pass

    def trigger_status(self):
        return "STOP" if self._stopped else "AUTO"

    def wait_for_stop(self, timeout=10.0, poll=0.1):
        return True

    def capture(self, channel="CHAN1", mode="screen", data_format="BYTE", points=None, progress=None):
        n = 1 if str(channel).upper() in ("CH1", "CHAN1", "1") else 2
        settings = self._ch[n]
        ch_name = f"CHAN{n}"
        scale = float(settings["scale"]) or 0.5
        tb = float(self._tb["scale"]) or 0.0005

        if mode.lower() == "screen":
            total = 1200
        else:
            total = int(points) if points else 120_000

        fs = max(1.0 / (tb * 12.0 / total), 1000.0)
        t = np.arange(total) / fs - total / fs / 2.0
        freq = 1000.0
        amp = scale * 2.0
        phase = (time.time() * 3.0) % (2 * np.pi)
        base = amp * np.sin(2 * np.pi * freq * t + phase)
        if n == 2:
            base = 0.65 * amp * np.sign(np.sin(2 * np.pi * 250 * t + phase))
        noise = np.random.normal(0, max(scale * 0.01, 1e-4), total)
        volts = base + noise + float(settings["offset"])
        if settings["invert"]:
            volts = -volts
        volts = np.clip(volts, -scale * 4.2, scale * 4.2)

        raw = np.zeros(total, dtype=np.uint32)
        y_inc = scale * 8.0 / 256.0
        y_ref = 127.0
        raw[:] = np.clip(np.round(volts / y_inc + y_ref), 0, 255).astype(np.uint32)

        if progress:
            for p in (0.25, 0.5, 0.75, 1.0):
                progress(p, f"demo {int(p * total)}/{total} puntos")
                time.sleep(0.05)

        self._acq["sample_rate"] = fs
        return Waveform(
            channel=ch_name,
            time_s=t,
            volts=volts,
            raw=raw,
            x_increment=1.0 / fs,
            x_origin=float(t[0]),
            y_increment=y_inc,
            y_origin=0.0,
            y_reference=y_ref,
            points=total,
            sample_rate=fs,
            mode="Pantalla" if mode.lower() == "screen" else "Memoria",
            data_format=data_format.upper(),
        )

    def scpi_measurements(self, channel="CHAN1", items=("VPP", "VMAX", "VMIN", "FREQuency", "PERiod")):
        # Valores "del osciloscopio" simulados a partir del ajuste del canal,
        # coherentes con la traza que genera capture().
        n = 1 if str(channel).upper() in ("CH1", "CHAN1", "1") else 2
        scale = float(self._ch[n]["scale"]) or 0.5
        if n == 2:
            amp = 0.65 * scale * 2.0
            return {"VPP": 2 * amp, "VMAX": amp, "VMIN": -amp, "FREQUENCY": 250.0, "PERIOD": 0.004}
        amp = scale * 2.0
        return {"VPP": 2 * amp, "VMAX": amp, "VMIN": -amp, "FREQUENCY": 1000.0, "PERIOD": 0.001}