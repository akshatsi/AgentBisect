"""The page ``agentbisect serve`` shows: one self-contained HTML file with no network use.

The page holds no run data. It asks the local server for it with ``fetch`` calls to same-origin
``/api/...`` routes and sends the access token from the address (``#token=...``) as the header
``X-AgentBisect-Token``. Everything the server returns is drawn through text nodes.
"""

from __future__ import annotations

from agentbisect.report.page import render_page

TEMPLATE_NAME = "serve.html.j2"
STYLES = ("study.css", "serve.css")
APP = "serve_app.js"
EXTRA_SCRIPTS = ("builder.js", "runform.js")


def render_serve_page() -> str:
    """The local app page, as one HTML string."""
    return render_page(
        TEMPLATE_NAME,
        title="AgentBisect local app",
        data={"page": "serve"},
        app=APP,
        styles=STYLES,
        extra_scripts=EXTRA_SCRIPTS,
    )
