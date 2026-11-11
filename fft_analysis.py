"""Análisis de Fourier (FFT) para trazas capturadas del osciloscopio.

Proporciona una clase reutilizable ``FFTAnalyzer`` que calcula el espectro de
magnitud de una señal capturada, aplica ventanas, detecta picos y devuelve
los resultados listos para graficar en la interfaz.

Diseñado como el primer "aplicativo de análisis" de la aplicación; otros
análisis pueden añadirse siguiendo el mismo patrón.
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import Callable, Optional

# Ventanas disponibles (nombre legible -> función numpy o callable).
WINDOWS = {
    "Ninguna": None,
    "Hann": np.hanning,
    "Hamming": np.hamming,
    "Blackman": np.blackman,
    "Bartlett": np.bartlett,
    "Flat-top": None,  # se implementa manualmente abajo
}


def _flattop(n: int) -> np.ndarray:
    """Ventana flat-top (buena amplitud en el pico, peor resolución)."""
    if n < 1:
        return np.ones(0)
    k = np.arange(n)
    a0 = 1.0
    a1 = 1.93
    a2 = 1.29
    a3 = 0.388
    a4 = 0.028
    return (
        a0
        - a1 * np.cos(2 * np.pi * k / (n - 1))
        + a2 * np.cos(4 * np.pi * k / (n - 1))
        - a3 * np.cos(6 * np.pi * k / (n - 1))
        + a4 * np.cos(8 * np.pi * k / (n - 1))
    )


def _apply_window(samples: np.ndarray, name: str) -> np.ndarray:
    n = samples.size
    if n == 0:
        return samples.copy()
    func = WINDOWS.get(name)
    if func is None:
        if name == "Flat-top":
            return _flattop(n)
        return samples.copy()
    win = func(n)
    # Las ventanas de numpy devuelven 0 en los bordes para n=1; protegemos.
    if win.size != n:
        return samples.copy()
    return samples * win


@dataclass
class FFTPeak:
    """Un pico detectado en el espectro."""

    frequency: float
    magnitude: float
    magnitude_db: float
    bin_index: int

    def __str__(self) -> str:
        return f"{self.frequency:,.1f} Hz · {self.magnitude_db:+.1f} dB"


@dataclass
class FFTResult:
    """Resultado completo del análisis FFT."""

    freq: np.ndarray
    magnitude: np.ndarray
    magnitude_db: np.ndarray
    sample_rate: float
    window: str
    peaks: list[FFTPeak] = field(default_factory=list)
    fundamental_frequency: Optional[float] = None
    fundamental_magnitude_db: Optional[float] = None
    noise_floor_db: Optional[float] = None
    snr_db: Optional[float] = None

    @property
    def max_frequency(self) -> float:
        return float(self.freq[-1]) if self.freq.size else 0.0

    @property
    def peak_magnitude_db(self) -> float:
        if self.magnitude_db.size == 0:
            return float("-inf")
        return float(np.max(self.magnitude_db))


class FFTAnalyzer:
    """Calcula el espectro de una traza capturada y detecta sus picos."""

    def __init__(self, waveform, sample_rate: Optional[float] = None):
        self.waveform = waveform
        self.sample_rate = sample_rate or getattr(waveform, "sample_rate", 0.0) or 0.0
        if self.sample_rate <= 0 and waveform is not None:
            xi = getattr(waveform, "x_increment", 0.0) or 0.0
            self.sample_rate = 1.0 / xi if xi else 0.0

    def analyze(
        self,
        *,
        window: str = "Hann",
        top_peaks: int = 10,
        prominence_db: float = 6.0,
        min_distance_bins: int = 3,
        use_db: bool = True,
    ) -> FFTResult:
        """Ejecuta el análisis FFT completo.

        Args:
            window: nombre de ventana (ver WINDOWS).
            top_peaks: número máximo de picos a reportar.
            prominence_db: prominencia mínima en dB respecto al ruido local.
            min_distance_bins: distancia mínima en bins entre picos.
            use_db: si True, escala la magnitud en dB.
        """
        v = np.asarray(getattr(self.waveform, "volts", None), dtype=float)
        if v is None or v.size < 4:
            return self._empty(window)

        v = v[np.isfinite(v)]
        if v.size < 4:
            return self._empty(window)

        # Elimina la componente DC para que no saturen los picos bajos.
        v = v - np.mean(v)

        samples = _apply_window(v, window)
        n = samples.size

        # FFT de un solo lado (señal real).
        spectrum = np.fft.rfft(samples)
        magnitude = np.abs(spectrum) / n  # normalizado por número de muestras
        # Dobla excepto DC y Nyquist para recuperar energía de un solo lado.
        if n > 1:
            magnitude[1:-1] *= 2.0

        freqs = np.fft.rfftfreq(n, d=1.0 / self.sample_rate) if self.sample_rate > 0 else np.arange(magnitude.size)

        if use_db:
            magnitude_db = 20 * np.log10(np.maximum(magnitude, 1e-12))
        else:
            magnitude_db = magnitude.copy()

        # Suavizado ligero del ruido de piso para estimación.
        noise_floor_db = float(np.percentile(magnitude_db, 10)) if magnitude_db.size else None

        peaks = self._detect_peaks(
            freqs,
            magnitude_db,
            prominence_db=prominence_db,
            min_distance_bins=min_distance_bins,
            top_peaks=top_peaks,
        )

        fundamental = None
        fundamental_db = None
        if peaks:
            fundamental = peaks[0]
            fundamental_db = fundamental.magnitude_db
        snr_db = None
        if fundamental_db is not None and noise_floor_db is not None:
            snr_db = fundamental_db - noise_floor_db

        return FFTResult(
            freq=freqs,
            magnitude=magnitude,
            magnitude_db=magnitude_db,
            sample_rate=self.sample_rate,
            window=window,
            peaks=peaks,
            fundamental_frequency=fundamental.frequency if fundamental else None,
            fundamental_magnitude_db=fundamental_db,
            noise_floor_db=noise_floor_db,
            snr_db=snr_db,
        )

    @staticmethod
    def _detect_peaks(
        freqs: np.ndarray,
        mag_db: np.ndarray,
        *,
        prominence_db: float,
        min_distance_bins: int,
        top_peaks: int,
    ) -> list[FFTPeak]:
        """Detecta máximos locales con prominencia mínima y distancia mínima."""
        if mag_db.size < 3:
            return []
        peaks: list[FFTPeak] = []
        n = mag_db.size
        # Máximos locales estrictos (ignoramos DC en el bin 0 por claridad).
        candidates = np.flatnonzero(
            (mag_db[1:-1] > mag_db[:-2]) & (mag_db[1:-1] >= mag_db[2:])
        ) + 1
        for idx in candidates:
            # Prominencia: diferencia con el mínimo entre el pico y los vecinos
            # más cercanos más bajos (aproximación local simple).
            left = idx
            while left > 0 and mag_db[left - 1] <= mag_db[left]:
                left -= 1
            right = idx
            while right < n - 1 and mag_db[right + 1] <= mag_db[right]:
                right += 1
            base = min(mag_db[left], mag_db[right])
            prom = mag_db[idx] - base
            if prom < prominence_db:
                continue
            # Distancia mínima respecto a picos ya aceptados (por frecuencia).
            if any(abs(freqs[idx] - p.frequency) < (min_distance_bins * (freqs[1] - freqs[0] if freqs.size > 1 else 1)) for p in peaks):
                continue
            peaks.append(
                FFTPeak(
                    frequency=float(freqs[idx]),
                    magnitude=float(10.0 ** (mag_db[idx] / 20.0)),
                    magnitude_db=float(mag_db[idx]),
                    bin_index=int(idx),
                )
            )
        peaks.sort(key=lambda p: p.magnitude_db, reverse=True)
        return peaks[:top_peaks]

    def _empty(self, window: str) -> FFTResult:
        return FFTResult(
            freq=np.array([]),
            magnitude=np.array([]),
            magnitude_db=np.array([]),
            sample_rate=self.sample_rate,
            window=window,
            peaks=[],
        )


def freq_axis(sample_rate: float, n_points: int) -> np.ndarray:
    """Devuelve el eje de frecuencias de un solo lado para una FFT."""
    if sample_rate <= 0 or n_points < 1:
        return np.array([])
    return np.fft.rfftfreq(n_points, d=1.0 / sample_rate)
