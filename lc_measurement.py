"""Medición de inductancia/capacitancia por resonancia de circuito tanque LC.

El circuito tanque LC excitado con un pulso genera oscilaciones amortiguadas
según:

    v(t) = A * exp(-alpha * t) * cos(2*pi*fr*t + phi)

donde fr = 1 / (2 * pi * sqrt(L * C)).

Este módulo detecta fr a partir de una forma de onda capturada (picos
sucesivos de la oscilación amortiguada) y calcula L o C dado el otro
componente conocido.

Fórmulas:
    L = 1 / (4 * pi^2 * fr^2 * C)   si se conoce C y se mide fr
    C = 1 / (4 * pi^2 * fr^2 * L)   si se conoce L y se mide fr
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Optional


@dataclass
class LCMeasurement:
    """Resultado de una medición de resonancia LC."""

    resonance_freq_hz: Optional[float] = None
    period_s: Optional[float] = None
    inductance_h: Optional[float] = None
    capacitance_f: Optional[float] = None
    known_component: str = ""  # "C" o "L"
    known_value: Optional[float] = None
    peaks: list[float] = None  # tiempos de los picos detectados
    peak_voltages: list[float] = None  # voltajes en los picos
    periods: list[float] = None  # periodos entre picos consecutivos
    peak_count: int = 0
    quality: Optional[float] = None  # coeficiente de variación de los periodos (0..1, menor es mejor)
    error: Optional[str] = None

    def __post_init__(self):
        if self.peaks is None:
            self.peaks = []
        if self.peak_voltages is None:
            self.peak_voltages = []
        if self.periods is None:
            self.periods = []

    @property
    def result_value(self) -> Optional[float]:
        """Valor calculado (L en H o C en F)."""
        return self.inductance_h if self.known_component == "C" else self.capacitance_f

    @property
    def result_unit(self) -> str:
        return "H" if self.known_component == "C" else "F"

    def formatted_result(self, prefix: bool = True) -> str:
        if self.result_value is None:
            return "—"
        v = self.result_value
        if prefix:
            for factor, symbol in ((1e9, "n"), (1e6, "µ"), (1e3, "m"), (1.0, ""), (1e-3, "k")):
                if abs(v) >= factor:
                    return f"{v / factor:.4g} {symbol}{self.result_unit}"
        return f"{v:.6g} {self.result_unit}"

    def formatted_freq(self) -> str:
        if self.resonance_freq_hz is None:
            return "—"
        v = self.resonance_freq_hz
        for factor, symbol in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")):
            if abs(v) >= factor:
                return f"{v / factor:.4g} {symbol}"
        return f"{v:.6g} Hz"


def _moving_average(x: np.ndarray, window: int) -> np.ndarray:
    """Filtro de promedio móvil simple para suprimir ruido de alta frecuencia."""
    if window < 2 or x.size < window:
        return x.copy()
    kernel = np.ones(window) / float(window)
    return np.convolve(x, kernel, mode="same")


def _find_peaks(
    time_s: np.ndarray,
    volts: np.ndarray,
    *,
    threshold_frac: float = 0.10,
    min_distance_s: Optional[float] = None,
    smooth_window: int = 5,
) -> tuple[list[float], list[float]]:
    """Detecta máximos locales (picos positivos) de una señal amortiguada.

    Devuelve (tiempos_de_picos, voltajes_de_picos).
    """
    v = np.asarray(volts, dtype=float)
    t = np.asarray(time_s, dtype=float)
    if v.size < 3:
        return [], []

    finite = np.isfinite(v) & np.isfinite(t)
    v = v[finite]
    t = t[finite]
    if v.size < 3:
        return [], []

    # Suavizar ligeramente para suprimir ruido de cuantización / alta frecuencia.
    vs = _moving_average(v, smooth_window)

    # Umbral relativo al pico absoluto de la señal (antes del suavizado es irrelevante;
    # usamos el pico del señal suavizada como referencia).
    abs_max = float(np.max(np.abs(vs)))
    if abs_max <= 0:
        return [], []
    threshold = abs_max * threshold_frac

    # Distancia mínima entre picos: si no se da, se infiere de la duración total
    # (al menos 10 µs, como máximo el 2% de la captura).
    if min_distance_s is None:
        duration = float(t[-1] - t[0]) if t.size > 1 else 0.0
        min_distance_s = max(10e-6, duration * 0.02) if duration > 0 else 10e-6

    # Máximos locales estrictos sobre la señal suavizada.
    candidates = np.flatnonzero((vs[1:-1] > vs[:-2]) & (vs[1:-1] >= vs[2:])) + 1
    # Solo picos positivos por encima del umbral.
    candidates = [int(i) for i in candidates if vs[i] > 0 and vs[i] >= threshold]

    peaks_t: list[float] = []
    peaks_v: list[float] = []
    last_t = -np.inf

    for idx in candidates:
        ti = float(t[idx])
        if ti - last_t < min_distance_s:
            continue
        peaks_t.append(ti)
        peaks_v.append(float(v[idx]))  # voltaje original, no suavizado
        last_t = ti

    return peaks_t, peaks_v


def measure_resonance(
    time_s: np.ndarray,
    volts: np.ndarray,
    *,
    known_component: str = "C",
    known_value: Optional[float] = None,
    threshold_frac: float = 0.10,
    min_cycles: int = 2,
) -> LCMeasurement:
    """Estima la frecuencia de resonancia y calcula L o C.

    Args:
        time_s: vector de tiempo (s).
        volts: vector de tensión (V).
        known_component: "C" si se conoce la capacitancia (calcula L), o "L"
            si se conoce la inductancia (calcula C).
        known_value: valor del componente conocido (Faradios o Henrios).
        threshold_frac: fracción del pico absoluto para considerar un pico válido.
        min_cycles: número mínimo de ciclos necesarios para aceptar la medición.
    """
    result = LCMeasurement(known_component=known_component, known_value=known_value)

    if time_s is None or volts is None:
        result.error = "No hay datos de forma de onda."
        return result

    t = np.asarray(time_s, dtype=float)
    v = np.asarray(volts, dtype=float)
    if t.size < 4 or v.size < 4:
        result.error = "Forma de onda demasiado corta."
        return result

    finite = np.isfinite(t) & np.isfinite(v)
    t = t[finite]
    v = v[finite]
    if t.size < 4:
        result.error = "Forma de onda sin datos finitos."
        return result

    # Detectar picos de la oscilación amortiguada.
    peaks_t, peaks_v = _find_peaks(t, v, threshold_frac=threshold_frac)
    result.peaks = peaks_t
    result.peak_voltages = peaks_v
    result.peak_count = len(peaks_t)

    if len(peaks_t) < min_cycles + 1:
        result.error = (
            f"Se detectaron {len(peaks_t)} pico(s); se necesitan al menos "
            f"{min_cycles + 1} para estimar la frecuencia. "
            "Verifica la conexión, el nivel de disparo y que el circuito esté excitado."
        )
        return result

    # Periodos entre picos consecutivos.
    periods = np.diff(peaks_t)
    result.periods = [float(p) for p in periods]

    # Filtrar periodos anómalos (desviación > 50% de la mediana).
    med_period = float(np.median(periods))
    if med_period <= 0:
        result.error = "Periodo estimado no válido."
        return result
    valid = periods[(periods > 0.25 * med_period) & (periods < 4.0 * med_period)]
    if valid.size == 0:
        result.error = "Los periodos detectados son inconsistentes (ruido excesivo)."
        return result

    period_s = float(np.mean(valid))
    result.period_s = period_s
    result.resonance_freq_hz = 1.0 / period_s

    # Calidad: coeficiente de variación de los periodos válidos (menor = mejor).
    if valid.size > 1:
        result.quality = float(np.std(valid) / np.mean(valid))
    else:
        result.quality = 0.0

    # Calcular L o C.
    fr = result.resonance_freq_hz
    if known_value is not None and known_value > 0 and fr > 0:
        factor = 4.0 * np.pi ** 2 * fr ** 2
        if known_component.upper() == "C":
            result.inductance_h = 1.0 / (factor * known_value)
        elif known_component.upper() == "L":
            result.capacitance_f = 1.0 / (factor * known_value)

    return result


def estimate_q_factor(peaks_voltages: list[float], peaks_times: list[float]) -> Optional[float]:
    """Estima el factor de calidad Q a partir del decaimiento de picos sucesivos.

    Q ≈ pi / ln(Vn / Vn+1) para decaimiento logarithmicamente decreciente,
    mediado sobre pares de picos consecutivos.
    """
    if len(peaks_voltages) < 2 or len(peaks_times) < 2:
        return None
    qs = []
    for i in range(len(peaks_voltages) - 1):
        v0 = abs(peaks_voltages[i])
        v1 = abs(peaks_voltages[i + 1])
        if v0 <= 0 or v1 <= 0:
            continue
        delta = np.log(v0 / v1)
        if delta <= 0:
            continue
        qs.append(np.pi / delta)
    if not qs:
        return None
    return float(np.mean(qs))
