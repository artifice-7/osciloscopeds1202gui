# Captura de formas de onda · Rigol DS1202Z-E

Aplicación de escritorio (Python + Tkinter + Matplotlib) y herramienta de línea de
comandos para capturar, visualizar, medir y guardar las formas de onda del
osciloscopio **Rigol DS1202Z-E** mediante comandos **SCPI** por USB.

Además incluye:
- **Pestaña Comandos**: navegador de los **393 comandos SCPI** de la referencia
  local con formularios autogenerados (enum→combobox, numérico→entry, `<n>`→índice)
  y consola libre para enviar cualquier comando.
- **Pestaña Análisis FFT**: espectro de magnitud de la última captura con
  ventanas (Hann, Hamming, Blackman, Bartlett, Flat-top, Ninguna), detección de
  picos, frecuencia fundamental, SNR estimado y guardado a CSV.

La referencia de comandos utilizada está en
`Documentation/Rigol DS1202Z-E SCPI Commands Reference _ bootloop.html`.

---

## 1. Requisitos

- Windows 10/11 (probado en este equipo), Python 3.10 o superior.
- Osciloscopio DS1202Z-E encendido y conectado por USB.
- **Runtime VISA**: NI-VISA (recomendado) o cualquier VISA de proveedor.
  El osciloscopio usa el driver de Windows «USB Test and Measurement Device (IVI)»,
  que solo es accesible a través de VISA. Sin VISA, PyVISA no puede abrir el equipo.

### Instalar NI-VISA (una sola vez)

```powershell
winget install --id NI.ni-visa --exact --accept-package-agreements --accept-source-agreements
```

También se puede descargar de <https://www.ni.com/es/support/downloads/drivers/download.ni-visa.html>.
Tras instalarlo, cierra y vuelve a abrir la aplicación (o reinicia si el instalador lo pide).

### Instalar las dependencias de Python

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> No hace falta activar el entorno: si lanzas `python osc_app.py` o `python capture.py`
> con el Python global, el programa detecta que faltan dependencias y **se relanza solo**
> con `.venv\Scripts\python.exe` (ver `bootstrap.py`). Si el entorno virtual no existe,
> muestra las instrucciones en lugar de un traceback.

> `pyvisa-py`, `pyusb` y `libusb-package` se incluyen como alternativa sin NI-VISA
> (solo funcionan si el dispositivo está accesible por libusb, cosa que normalmente
> requiere cambiar el driver con Zadig). La vía recomendada es NI-VISA.

---

## 2. Uso

### Aplicación gráfica

```powershell
.venv\Scripts\python.exe osc_app.py
```

También vale `python osc_app.py` (se relanza automáticamente con el entorno virtual),
o haz doble clic en **`iniciar.bat`** (crea el entorno virtual la primera vez).

1. Pulsa **Actualizar** y elige el recurso USB, p. ej.
   `USB0::0x1AB1::0x0517::DS1ZE243610884::INSTR`.
   También puedes escribir un recurso a mano o elegir **DEMO (simulado)** para
   probar la interfaz sin osciloscopio.
2. Pulsa **Conectar**. La barra de estado muestra la identificación `*IDN?`.
3. Ajusta **Canal**, **Base de tiempo**, **Disparo** y **Adquisición** y pulsa el
   botón «Aplicar» de cada sección (los valores se leen del propio instrumento al
   conectar).
4. Elige **Pantalla** (1200 puntos) o **Memoria** (memoria completa, leída por
   bloques de 250 000 puntos) y **BYTE**/`WORD`. Leer los 6 Mpts completos por USB
   tarda alrededor de 1–2 minutos e indica el progreso; para pruebas rápidas usa
   `--points` en la CLI o la profundidad de memoria en **Adquisición**.
5. Pulsa **Capturar (F5)**. El programa arma una adquisición **SINGle**, espera el
   disparo y lee la traza. Si no llega ningún disparo en 10 s, captura el estado
   actual (revisa el nivel de disparo).
   - **Leer pantalla (sin disparar)**: detiene el osciloscopio y lee lo que hay en
     pantalla, sin esperar un trigger nuevo.
   - **Run / Stop / Auto**: control remoto equivalente a las teclas del panel.
6. La pestaña **Medidas** muestra los valores calculados sobre la traza y los que
   devuelve el propio osciloscopio vía `:MEASure:ITEM?`.
7. **Pantalla**: lee la imagen del osciloscopio con `:DISPlay:DATA?` (BMP24 por
   defecto, legible y rápido; también PNG/BMP8/JPEG/TIFF).
8. **Comandos**: selecciona cualquiera de los 393 comandos de la referencia para
   generar su formulario, edita los parámetros y envía ajuste/consulta.
9. **Análisis FFT**: calcula el espectro de la última captura, detecta picos y
   guarda el espectro a CSV.
10. **CSV / NPZ / PNG** guardan la captura en `capturas/`.

Atajos: `F5` capturar, `Ctrl+S` guardar CSV, `Ctrl+L` limpiar consola,
`Ctrl+Return` enviar consola.

### Línea de comandos

```powershell
# Listar instrumentos VISA detectados
.venv\Scripts\python.exe capture.py --list

# Captura de pantalla (1200 puntos) a CSV + PNG
.venv\Scripts\python.exe capture.py --channel CH1 --mode screen --csv --png

# Memoria completa (hasta la profundidad máxima del instrumento) a NPZ
.venv\Scripts\python.exe capture.py --channel CH2 --mode memory --npz

# Recortar a 1 200 000 puntos, formato WORD y medidas del osciloscopio
.venv\Scripts\python.exe capture.py --mode memory --points 1200000 --format WORD --scpi-meas

# Sin hardware (traza simulada)
.venv\Scripts\python.exe capture.py --demo --png
```

Opciones principales: `--resource`, `--channel CH1|CH2`, `--mode screen|memory`,
`--points N`, `--format BYTE|WORD`, `--no-single`, `--csv`, `--npz`, `--png`,
`--scpi-meas`, `--outdir`.

---

## 3. Archivos del proyecto

| Archivo | Descripción |
|---|---|
| `rigol_scope.py` | Driver SCPI: conexión, configuración, captura, medidas y screenshot. Incluye `DemoScope`. |
| `osc_app.py` | Aplicación gráfica (Tkinter + Matplotlib) con pestañas, tema oscuro y análisis FFT. |
| `capture.py` | Capturador por línea de comandos. |
| `fft_analysis.py` | Motor de análisis FFT (ventanas, detección de picos, fundamental, SNR). |
| `theme.py` | Paleta oscura y estilos ttk/Tk/Matplotlib. |
| `scpi_catalog.py` | Parser automático de la referencia HTML → 393 comandos SCPI con parámetros. |
| `bootstrap.py` | Relanza el programa con el Python del entorno virtual si faltan dependencias. |
| `iniciar.bat` | Lanzador de la GUI que prepara el entorno virtual. |
| `requirements.txt` | Dependencias de Python. |
| `capturas/` | Carpeta de salida por defecto (no se sube al repositorio). |
| `Documentation/` | Referencia SCPI del DS1202Z-E (no se sube al repositorio). |

Formatos de salida:

- **CSV**: metadatos en comentarios `#` y columnas `time_s,voltage_V` (todos los puntos).
- **NPZ**: arrays `time_s`, `volts`, `raw`, metadatos del preámbulo y medidas.
- **PNG**: gráfica de la traza con las medidas principales (150 ppp).

---

## 4. Comandos SCPI utilizados

Todos están documentados en la referencia local. El driver usa las formas cortas
y largas indistintamente (SCPI no distingue mayúsculas/minúsculas).

| Función | Comandos |
|---|---|
| Identificación / errores | `*IDN?`, `*OPC?`, `:SYSTem:ERRor?` |
| Control | `:RUN`, `:STOP`, `:SINGle`, `:TFORce`, `:AUToscale` |
| Adquisición | `:ACQuire:TYPE`, `:ACQuire:AVERages`, `:ACQuire:MDEPth?`, `:ACQuire:SRATe?` |
| Canal | `:CHANnel<n>:DISPlay`, `:SCALe`, `:OFFSet`, `:COUPling`, `:PROBe`, `:BWLimit`, `:INVert` |
| Base de tiempo | `:TIMebase:MODE`, `:TIMebase:MAIN:SCALe`, `:TIMebase:MAIN:OFFSet` |
| Disparo | `:TRIGger:MODE`, `:TRIGger:SWEep`, `:TRIGger:EDGe:SOURce`, `:SLOPe`, `:LEVel`, `:TRIGger:COUPling`, `:TRIGger:STATus?` |
| Datos | `:WAVeform:SOURce`, `:MODE`, `:FORMat`, `:STARt`, `:STOP`, `:PREamble?`, `:DATA?` |
| Medidas | `:MEASure:ITEM? VPP|VMAX|VMIN|FREQuency|PERiod,<canal>` |
| Pantalla | `:DISPlay:DATA? [color,invert,format]` |

### Detalles de la lectura de datos

- **Preamble** (`:WAVeform:PREamble?`) devuelve 10 valores:
  `formato, tipo, puntos, count, xincrement, xorigin, xreference, yincrement, yorigin, yreference`.
- Conversión aplicada:

  ```text
  voltaje = (dato_crudo - YORigin - YREFerence) * YINCrement
  tiempo  = XORigin + índice * XINCrement
  ```

- **`BYTE`** = 1 byte por muestra, **`WORD`** = 2 bytes little-endian.
- El bloque binario llega con cabecera TMC `#<n><longitud><datos>` (Rigol suele usar
  `#9`). El driver la parsea y consume el terminador para no corromper la siguiente query.
- En modo **RAW** (memoria) el osciloscopio debe estar **STOP**; la memoria se lee en
  bloques de 250 000 puntos cambiando `:WAVeform:STARt/:STOP` y pidiendo `:DATA?`
  en cada bloque. Así se puede leer la memoria completa sin agotar el tiempo de espera USB.
- El modo **NORMal** devuelve los 1200 puntos de pantalla.
- En este instrumento `:ACQuire:MDEPth?` con `AUTO` devuelve `AUTO`, pero
  `:WAVeform:PREamble?` en modo RAW informa de la profundidad real
  (6 000 000 puntos en la unidad probada, a 500 MSa/s = 12 ms de registro).
- Las medidas locales se calculan con un disparador de Schmitt entre el 10 % y el
  90 % de la excursión, de modo que el ruido de cuantización del ADC (8 bits, pasos
  de ~50 mV en memoria) no genera flancos falsos. La validez del escalado se
  comprueba con `--scpi-meas`: los valores locales deben coincidir con los del
  osciloscopio (en la unidad probada, error < 0,1 % en Vpp, Vmax, Vmin, frecuencia
  y período; en memoria la diferencia es como mucho 1 LSB ≈ 50 mV).
- `:MEASure:CLEar ALL` se envía antes de pedir las medidas SCPI: ocupa la lista de
  cinco medidas en pantalla y sustituye a las que tuviera el usuario.

---

## 5. Solución de problemas

| Síntoma | Causa / solución |
|---|---|
| `VI_ERROR_LIBRARY_NFOUND` | Falta un proveedor VISA. Instala NI-VISA; la aplicación añade automáticamente `C:\Program Files\IVI Foundation\VISA\Win64\Bin` al *path* de DLL, así que normalmente no hace falta reiniciar. |
| `No se encontró ningún instrumento VISA` | Cable USB, osciloscopio apagado, o NI-VISA no instalado. Prueba `capture.py --list`. |
| `VI_ERROR_RSRC_BUSY` / recurso ocupado | Otro programa está usando el osciloscopio (Ultra Sigma, LabVIEW, otro script). Ciérralo. |
| La captura se queda en «esperando disparo» | El nivel de disparo está fuera de la señal o el flanco/fuente no coincide. Ajusta el nivel (o usa **Auto**). |
| La traza aparece invertida o con offset | Revisa `:CHANnel<n>:OFFSet`, la sonda (`PROBe`) y `:INVert`. También el acoplamiento (AC elimina la continua). |
| Timeout en capturas de memoria | Baja la profundidad de memoria (`Adquisición → Memoria`) o usa `--points`. El USB del DS1202Z-E es lento (~1 MB/s). |
| `Permission denied` con pyvisa-py | El driver de Windows es USBTMC/IVI; usa NI-VISA. libusb solo funciona con driver WinUSB/libusbK. |

Consejo: si el osciloscopio está en **RUN**, para leer la memoria el programa lo
detiene automáticamente. Después puedes pulsar **Run** para reanudar.

---

## 6. Notas

- El DS1202Z-E tiene 2 canales, 200 MHz de ancho de banda y 1 GSa/s. Las
  profundidades de memoria que admite la referencia SCPI son `AUTO`, `12000`,
  `120000`, `1200000`, `12000000` y `24000000`; la máxima disponible depende del
  modelo y de los canales activos.
- Solo se admite un cliente USB a la vez. Cierra Rigol Ultra Sigma u otras
  aplicaciones que puedan tener el instrumento abierto.
- El modo DEMO genera una senoide de 1 kHz en CH1 y una onda cuadrada de 250 Hz en
  CH2 para probar toda la aplicación sin hardware.
