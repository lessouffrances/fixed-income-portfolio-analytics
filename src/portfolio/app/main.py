"""Dash application: factory, routing, and callbacks.

Reads exclusively from the database (assignment requirement 1). The CSVs are the
loader's input, never the app's.

Run locally:
    python -m portfolio.app.main

Served in production by gunicorn against the module-level `server`:
    gunicorn portfolio.app.main:server --bind 0.0.0.0:8050
"""

from __future__ import annotations

import logging

from dash import Dash, Input, Output, dcc, html

from ..config import ConfigError, Settings
from ..db import healthcheck, make_engine
from .components import empty_state
from .data import get_snapshot
from .pages import allocation, overview, quality, security
from .theme import LIGHT

log = logging.getLogger(__name__)

PAGES = [
    ("/", "Overview"),
    ("/allocation", "Allocation"),
    ("/security", "Security"),
    ("/quality", "Data quality"),
]


def _nav(pathname: str):
    return html.Nav(
        [
            html.Div("Fixed-income portfolio analytics", className="brand"),
            html.Div(
                [
                    dcc.Link(
                        label,
                        href=href,
                        className="nav-link nav-link-active"
                        if pathname == href
                        else "nav-link",
                    )
                    for href, label in PAGES
                ],
                className="nav-links",
            ),
        ],
        className="nav",
    )


def create_app(engine=None, *, mode: str = "light") -> Dash:
    """Build the Dash app.

    `engine` is injectable so tests can point the app at SQLite without touching
    real configuration.
    """
    if engine is None:
        engine = make_engine(Settings.from_env())

    app = Dash(
        __name__,
        title="Fixed-income portfolio analytics",
        update_title=None,
        suppress_callback_exceptions=True,
    )

    app.layout = html.Div(
        [
            dcc.Location(id="url", refresh=False),
            html.Div(id="nav-container"),
            html.Main(id="page-content", className="page"),
            html.Footer(
                html.Div(id="footer-content", className="footer-inner"),
                className="footer",
            ),
        ],
        className="app",
        style={"backgroundColor": LIGHT["plane"]},
    )

    @app.callback(Output("nav-container", "children"), Input("url", "pathname"))
    def _render_nav(pathname):
        return _nav(pathname or "/")

    @app.callback(Output("page-content", "children"), Input("url", "pathname"))
    def _render_page(pathname):
        snap = get_snapshot(engine)
        if snap.is_empty:
            return empty_state(
                "The database is reachable but holds no positions. Run the loader "
                "to populate it, then reload this page."
            )
        if pathname == "/allocation":
            return allocation.layout(snap, mode)
        if pathname == "/security":
            return security.layout(snap, mode)
        if pathname == "/quality":
            return quality.layout(snap, mode)
        return overview.layout(snap, mode)

    @app.callback(
        Output("security-detail", "children"),
        Input("security-picker", "value"),
    )
    def _render_security(security_id):
        snap = get_snapshot(engine)
        return security.detail(snap, security_id, mode)

    @app.callback(Output("footer-content", "children"), Input("url", "pathname"))
    def _render_footer(_pathname):
        snap = get_snapshot(engine)
        healthy = healthcheck(engine)
        bits = [
            html.Span("database: ", className="footer-label"),
            html.Span(
                "connected" if healthy else "unreachable",
                className="footer-ok" if healthy else "footer-bad",
            ),
        ]
        if snap.load_id is not None:
            bits += [
                html.Span(" · ", className="footer-sep"),
                html.Span(f"load {snap.load_id}", className="footer-label"),
            ]
        if not snap.runs.empty:
            run = snap.runs.iloc[0]
            if run["finished_at"] is not None:
                bits += [
                    html.Span(" · ", className="footer-sep"),
                    html.Span(
                        f"loaded {run['finished_at']:%Y-%m-%d %H:%M} UTC",
                        className="footer-label",
                    ),
                ]
        return bits

    # Plain liveness endpoint, for a load balancer or a smoke test.
    @app.server.route("/healthz")
    def _healthz():
        return ("ok", 200) if healthcheck(engine) else ("unhealthy", 503)

    return app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s"
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    app = create_app(make_engine(settings))
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
    return 0


def _misconfigured_server(message: str):
    """A WSGI app that reports why the real one could not be built.

    gunicorn resolves `portfolio.app.main:server` at import. If that were None,
    gunicorn would fail with "application object must be callable" — which says
    nothing about the cause and sends you reading gunicorn's source instead of
    your own configuration. Returning a WSGI callable instead means the process
    starts, /healthz fails honestly with the real reason, and `ec2.sh logs` shows
    the configuration error rather than a crash loop.
    """

    def app(environ, start_response):
        body = f"configuration error: {message}\n".encode()
        start_response(
            "503 Service Unavailable",
            [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))],
        )
        return [body]

    return app


# For gunicorn. Built at import so a misconfiguration surfaces at startup rather
# than on the first request, but without raising — see above.
def _make_server():
    try:
        return create_app().server
    except ConfigError as exc:
        log.error("cannot build the app: %s", exc)
        return _misconfigured_server(str(exc))


server = _make_server()


if __name__ == "__main__":
    raise SystemExit(main())
