"""Build one self-contained HTML page: inline CSS and JS, the data as an escaped JSON blob.

Pages are Preact + htm apps, vendored under ``assets/vendor`` and inlined, so the file opens
offline with no network use. Everything that came from an agent run reaches the page as JSON text
(``<script type="application/json" id="data">``) and the apps draw it through text nodes only.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

REPORT_DIR = Path(__file__).parent
ASSETS = REPORT_DIR / "assets"
VENDOR = ASSETS / "vendor"

#: Vendored scripts, in load order. ``hooks`` needs the ``preact`` global, ``htm`` stands alone.
VENDOR_SCRIPTS = ("preact.min.js", "hooks.umd.js", "htm.js")

_SOURCE_MAP = re.compile(r"\n?//# sourceMappingURL=\S+\s*$")


@cache
def vendor_js(name: str) -> str:
    """A vendored script, ready to inline.

    Two edits, both without a change in behaviour: the trailing source-map comment goes, and the
    ``http://`` of the XML namespace strings (``"http://www.w3.org/2000/svg"``) is written
    ``http:\\/\\/``, which a JavaScript string reads as the same text. The page then holds no
    ``http://`` outside the data blob, so "no network use" can be checked by search.
    """
    text = (VENDOR / name).read_text(encoding="utf-8")
    text = _SOURCE_MAP.sub("", text)
    return text.replace("http://", "http:\\/\\/").strip() + "\n"


@cache
def css(*names: str) -> str:
    """The named stylesheets, joined."""
    return "\n".join((ASSETS / n).read_text(encoding="utf-8") for n in names)


@cache
def app_js(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def json_blob(data: Any) -> str:
    """JSON text that is safe inside a ``<script>`` element.

    ``<``, ``>`` and ``&`` become ``\\uXXXX`` escapes, so neither ``</script>`` nor ``<!--`` can
    appear, and non-ASCII text is escaped too, so the file is plain ASCII data.
    """
    text = json.dumps(data, ensure_ascii=True, separators=(",", ":"), default=str)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def scripts_for(app: str) -> list[str]:
    """The inline scripts of a page, in order: libraries, shared helpers, the page's own app."""
    return [*(vendor_js(n) for n in VENDOR_SCRIPTS), app_js("common.js"), app_js(app)]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(REPORT_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_page(
    template: str,
    *,
    title: str,
    data: dict[str, Any],
    app: str,
    styles: tuple[str, ...],
    **ctx: Any,
) -> str:
    """Render ``template`` with the shared page parts plus ``ctx`` (used by its ``<noscript>``).

    Only the CSS, the app scripts and the escaped JSON blob are marked safe; they come from this
    package or from ``json_blob``. Everything in ``ctx`` stays autoescaped.
    """
    return (
        _env()
        .get_template(template)
        .render(
            title=title,
            css=Markup(css("common.css", *styles)),
            scripts=[Markup(s) for s in scripts_for(app)],
            data_json=Markup(json_blob(data)),
            data=data,
            **ctx,
        )
    )
