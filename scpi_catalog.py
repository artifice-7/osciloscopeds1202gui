"""Catálogo de comandos SCPI extraído de la referencia local del DS1202Z-E.

El HTML de ``Documentation`` contiene un artículo por comando con su
descripción, tabla de parámetros, valor devuelto y ejemplos. Este módulo lo
convierte en objetos utilizables por la interfaz para generar formularios y
construir los comandos.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DOC = ROOT / "Documentation" / "Rigol DS1202Z-E SCPI Commands Reference _ bootloop.html"

_TAG = re.compile(r"<[^>]+>")
_ARTICLE = re.compile(r"<article\b.*?</article>", re.S)
_CMD_LINK = re.compile(r'class="scpicmd-cmdlink"[^>]*>(.*?)</a>', re.S)
_BADGE = re.compile(r"</div>\s*<p\b", re.S)
_PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S)
_TR = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)

NUMERIC_TYPES = {"integer", "real", "float", "double"}
BOOL_TYPES = {"bool", "boolean"}
ENUM_TYPES = {"enum", "discrete"}


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _line_text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"</(p|div|pre|tr|li|h\d)>", "\n", fragment)
    text = html.unescape(_TAG.sub("", fragment))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


@dataclass
class ScpiParam:
    key: str
    name: str
    kind: str
    values: str
    description: str
    optional: bool = False
    choices: list[str] = field(default_factory=list)
    default: str = ""

    @property
    def is_index(self) -> bool:
        return self.key == "n"

    @property
    def label(self) -> str:
        return "n (índice)" if self.is_index else self.name


@dataclass
class ScpiCommand:
    name: str
    mode: str
    description: str
    params: list[ScpiParam]
    returns: str = ""
    examples: list[str] = field(default_factory=list)
    details: str = ""

    @property
    def group(self) -> str:
        if self.name.startswith("*"):
            return "Común"
        parts = self.name.lstrip(":").split(":")
        head = parts[0] if parts else self.name
        # ":CHANnel<n>:SCALe" -> "CHANnel"; ":TIMebase[MAIN]:SCALe" -> "TIMebase"
        head = re.sub(r"<[^>]*>", "", head)
        head = head.split("[")[0]
        return head.strip(":[] ") or "Otros"

    @property
    def supports_query(self) -> bool:
        return self.name.rstrip().endswith("?") or "query" in self.mode.lower()

    @property
    def supports_set(self) -> bool:
        return not self.name.rstrip().endswith("?") and "set" in self.mode.lower()

    @property
    def is_query_only(self) -> bool:
        return not self.supports_set

    @property
    def readable_mode(self) -> str:
        if self.is_query_only:
            return "consulta"
        if self.supports_query:
            return "ajuste + consulta"
        return "ajuste"

    @property
    def set_separator(self) -> str:
        """Separador observado en el ejemplo de ajuste (coma o espacio)."""
        base = re.sub(r"<n>", "", self.name, flags=re.I).rstrip("?").upper()
        for example in self.examples:
            for line in example.splitlines():
                line = re.sub(r"/\*.*?\*/", "", line).strip()
                if not line or "?" in line:
                    continue
                if line.lstrip(":").upper().startswith(base.lstrip(":")):
                    rest = line[len(self.name.rstrip("?").replace("<n>", "")) :]
                    if "," in rest:
                        return ","
                    return " "
        if len([p for p in self.params if not p.is_index]) > 1:
            return ","
        return " "

    def index_choices(self) -> list[str]:
        upper = self.name.upper()
        if "REFERENCE" in upper:
            return [str(i) for i in range(1, 11)]
        return ["1", "2"]

    @property
    def query_takes_args(self) -> bool:
        """True si el ejemplo de consulta pasa parámetros tras el «?»."""
        for example in self.examples:
            for line in example.splitlines():
                line = re.sub(r"/\*.*?\*/", "", line).strip()
                if "?" not in line:
                    continue
                after = line.split("?", 1)[1].strip()
                if after and not after.startswith(("[", "/*")):
                    return True
        # Sin ejemplos: es más seguro enviar los parámetros que omitirlos.
        return not self.examples and bool(self.params)

    def build(self, values: dict[str, str], query: bool) -> str:
        name = self.name
        for param in self.params:
            if param.is_index:
                name = re.sub(r"<n>", str(values.get(param.key, "1") or "1"), name, flags=re.I)
        base = name.rstrip("?").strip()
        parts = []
        for param in self.params:
            if param.is_index:
                continue
            value = (values.get(param.key) or "").strip()
            if value:
                parts.append(value)
        if query:
            command = base + "?"
            # Las consultas de ajuste (:CHANnel1:SCALe?) no llevan argumentos;
            # las de medida (:MEASure:ITEM? VPP,CHANnel1) sí.
            if parts and (self.is_query_only or self.query_takes_args):
                command += " " + ",".join(parts)
            return command
        if not parts:
            return base
        return base + " " + self.set_separator.join(parts)


def _parse_choices(param_type: str, values: str) -> list[str]:
    lowered = param_type.lower()
    if lowered in BOOL_TYPES:
        return ["ON", "OFF"]
    if lowered not in ENUM_TYPES:
        return []
    choices = []
    for token in values.split("|"):
        token = token.strip()
        if not token:
            continue
        # "ON (color)" -> "ON"; "CHANnel1" se conserva tal cual.
        token = re.sub(r"\s*\(.*?\)\s*", "", token).strip()
        if token and len(token) <= 24 and not token.startswith("<"):
            choices.append(token)
    # Elimina duplicados conservando el orden.
    seen = set()
    out = []
    for choice in choices:
        if choice.upper() not in seen:
            seen.add(choice.upper())
            out.append(choice)
    return out


def _parse_default(param_type: str, values: str, choices: list[str], optional: bool) -> str:
    if optional:
        return ""
    if choices:
        return choices[0]
    # Los rangos («2 - 1024», «-5 x VerticalScale») no son valores por defecto
    # razonables: se deja el campo vacío y se muestra el rango como ayuda.
    return ""


def _example_set_value(command_name: str, examples: list[str]) -> str:
    """Primer valor de ajuste visible en los ejemplos (si la tabla no lo trae)."""
    base = command_name.rstrip("?").upper()
    for example in examples:
        for line in example.splitlines():
            line = re.sub(r"/\*.*?\*/", "", line).strip()
            if not line or "?" in line:
                continue
            head, _, tail = line.partition(" ")
            if not head:
                continue
            head = head.rstrip("?").upper()
            if head == base or head.startswith(base[: max(6, len(base) - 2)]):
                tail = tail.strip()
                if tail and not tail.startswith(("<", "[")):
                    return tail.split(",")[0].strip()
    return ""


def _parse_article(article: str) -> ScpiCommand | None:
    link = _CMD_LINK.search(article)
    if not link:
        return None
    name = _plain(link.group(1))
    if not name.startswith(":") and not name.startswith("*"):
        return None

    header_end = article.find("</div>", link.end())
    header = article[link.end() : header_end if header_end > 0 else len(article)]
    mode_match = re.search(r">([^<>]*?(?:set|query)[^<>]*?)</span>", header, re.I)
    mode = _plain(mode_match.group(1)) if mode_match else ("query" if name.endswith("?") else "set")

    description = ""
    p_match = re.search(r"<p\b[^>]*>(.*?)</p>", article, re.S)
    if p_match:
        description = _plain(p_match.group(1))

    params: list[ScpiParam] = []
    for row in _TR.findall(article):
        cells = _TD.findall(row)
        if len(cells) < 4:
            continue
        raw_name, kind, values, desc = (_plain(c) for c in cells[:4])
        cleaned = raw_name.replace("<", "").replace(">", "").replace("*", "").strip()
        optional = bool(re.search(r"\bopt\b", raw_name, re.I))
        cleaned = re.sub(r"\bopt\b", "", cleaned, flags=re.I).strip()
        if not cleaned:
            continue
        is_index = cleaned.lower() in ("n", "idx", "index")
        key = "n" if is_index else f"p{len(params)}"
        choices = _parse_choices(kind, values)
        params.append(
            ScpiParam(
                key=key,
                name=cleaned,
                kind=kind.strip().lower(),
                values=values,
                description=desc,
                optional=optional,
                choices=choices,
                default=_parse_default(kind, values, choices, optional),
            )
        )

    returns = ""
    for block in re.findall(r'<div style="margin:[^"]*"[^>]*>(.*?)</div>', article, re.S):
        if "<table" in block:
            continue
        text = _plain(block)
        if text and ("query returns" in text.lower() or "the query" in text.lower()):
            returns = text
            break

    examples = [_line_text(pre) for pre in _PRE.findall(article)]

    # Algunos artículos no incluyen la tabla de parámetros. Si el ejemplo de
    # ajuste sí muestra un valor, se crea un campo de texto para poder enviarlo.
    if not params:
        value = _example_set_value(name, examples)
        if value:
            params.append(
                ScpiParam(
                    key="p0",
                    name="valor",
                    kind="text",
                    values="",
                    description="Valor deducido del ejemplo de la referencia.",
                    optional=False,
                    choices=[],
                    default=value,
                )
            )

    return ScpiCommand(
        name=name,
        mode=mode,
        description=description,
        params=params,
        returns=returns,
        examples=examples,
        details=_line_text(article),
    )


def _natural_key(name: str):
    return re.sub(r"\d+", lambda m: f"{int(m.group()):04d}", name.upper())


_cache: list[ScpiCommand] | None = None


def _example_head(command: ScpiCommand) -> str:
    for example in command.examples:
        for line in example.splitlines():
            line = re.sub(r"/\*.*?\*/", "", line).strip()
            if line:
                return line.split()[0].rstrip("?").upper()
    return ""


def load_catalog(path: str | Path | None = None, force: bool = False) -> list[ScpiCommand]:
    """Devuelve la lista completa de comandos documentados."""
    global _cache
    if _cache is not None and not force and path is None:
        return _cache
    doc = Path(path) if path else DEFAULT_DOC
    if not doc.is_file():
        raise FileNotFoundError(f"No se encuentra la referencia SCPI: {doc}")
    text = doc.read_text(encoding="utf-8", errors="replace")
    commands: list[ScpiCommand] = []
    seen = set()
    for article in _ARTICLE.findall(text):
        spec = _parse_article(article)
        if spec is None or spec.name.upper() in seen:
            continue
        seen.add(spec.name.upper())
        commands.append(spec)

    # La referencia incluye un artículo duplicado de :TRIGger:PATTern cuyo
    # ejemplo usa el nombre completo :TRIGger:PATTern:PATTern. Se descarta el
    # artículo corto en favor del completo.
    names = {command.name.upper() for command in commands}

    def duplicate_parent(command: ScpiCommand) -> bool:
        name = command.name.rstrip("?").upper()
        head = _example_head(command)
        return (head == f"{name}:{name.rsplit(':', 1)[-1]}" and head in names)

    commands = [command for command in commands if not duplicate_parent(command)]

    commands.sort(key=lambda c: (_natural_key(c.group), _natural_key(c.name)))
    if path is None:
        _cache = commands
    return commands


def groups(catalog: list[ScpiCommand] | None = None) -> list[str]:
    catalog = catalog if catalog is not None else load_catalog()
    seen = []
    for command in catalog:
        if command.group not in seen:
            seen.append(command.group)
    return seen


def find(name: str, catalog: list[ScpiCommand] | None = None) -> ScpiCommand | None:
    catalog = catalog if catalog is not None else load_catalog()
    target = name.strip().upper()
    for command in catalog:
        if command.name.upper() == target:
            return command
    return None
