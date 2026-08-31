"""Tests for the web layer.

Most of these defend chart-spec invariants that are easy to break silently later:
no dual axes, colour keyed to entity rather than rank, the categorical slot cap,
and the table-view twin that makes the low-contrast palette slots legal.

They also pin two bugs found by actually rendering the app rather than by
reasoning about it — a numpy bool reaching Plotly, and a figure that collapses to
zero height because its container has none.
"""

from __future__ import annotations

import pandas as pd
import pytest
from dash import Dash

from portfolio.app import figures
from portfolio.app.components import graph, money, pct, signed_money
from portfolio.app.data import findings_summary, get_snapshot
from portfolio.app.main import create_app
from portfolio.app.pages import allocation, overview, quality, security
from portfolio.app.theme import CATEGORICAL_DARK, CATEGORICAL_LIGHT, colour_map
from portfolio.db import create_schema, make_engine
from portfolio.load.loader import run_load


@pytest.fixture
def loaded_engine(tmp_path, master, holdings, marks, transactions):
    """A database with the synthetic extract loaded, so pages have real shapes."""
    d = tmp_path / "extracts"
    d.mkdir()
    master.to_csv(d / "security_master.csv", index=False)
    holdings.to_csv(d / "holdings_monthly.csv", index=False)
    marks.to_csv(d / "marks_monthly.csv", index=False)
    transactions.to_csv(d / "transactions.csv", index=False)

    eng = make_engine(url=f"sqlite:///{tmp_path / 'app.db'}")
    create_schema(eng)
    run_load(eng, d)
    return eng


@pytest.fixture
def snap(loaded_engine):
    from portfolio.app.data import clear_cache

    clear_cache()
    return get_snapshot(loaded_engine)


# ---------------------------------------------------------------------------
# Palette invariants
# ---------------------------------------------------------------------------


def test_categorical_slots_are_capped_rather_than_cycled():
    """A ninth hue would be indistinguishable from an existing slot under CVD, so
    exceeding the validated set must fail loudly rather than wrap around."""
    keys = [f"s{i}" for i in range(9)]
    with pytest.raises(ValueError, match="exceeds"):
        colour_map(keys)


def test_colour_follows_the_entity_not_its_position():
    """Filtering a series out must not repaint the survivors. A reader who learned
    'Energy is orange' has to stay right."""
    full = colour_map(["Financials", "Energy", "Utilities"])
    # Same entities, same relative order, one dropped from the end.
    fewer = colour_map(["Financials", "Energy"])
    assert fewer["Financials"] == full["Financials"]
    assert fewer["Energy"] == full["Energy"]


def test_both_modes_expose_the_same_number_of_slots():
    assert len(CATEGORICAL_LIGHT) == len(CATEGORICAL_DARK) == 8


def test_palette_has_no_duplicate_hues():
    assert len(set(CATEGORICAL_LIGHT)) == 8
    assert len(set(CATEGORICAL_DARK)) == 8


# ---------------------------------------------------------------------------
# Sector folding
# ---------------------------------------------------------------------------


def _mix(dimension: str, keys: list[str]) -> pd.DataFrame:
    rows = []
    for i, k in enumerate(keys):
        rows.append(
            {
                "as_of_date": pd.Timestamp("2031-01-31"),
                dimension: k,
                "market_value": 1_000_000.0 * (len(keys) - i),
                "weight_pct": 100.0 / len(keys),
            }
        )
    return pd.DataFrame(rows)


def test_series_within_the_slot_limit_are_left_alone():
    mix = _mix("sector", [f"S{i}" for i in range(8)])
    order = figures.sector_order(mix)
    assert figures.OTHER not in order
    assert len(order) == 8


def test_series_past_the_slot_limit_fold_into_other():
    mix = _mix("sector", [f"S{i}" for i in range(12)])
    order = figures.sector_order(mix)
    assert order[-1] == figures.OTHER
    assert len(order) == figures.MAX_SECTOR_SLICES + 1

    folded = figures.fold_to_other(mix, order)
    assert set(folded["sector"]) == set(order)
    # Folding must not lose value.
    assert folded["market_value"].sum() == pytest.approx(mix["market_value"].sum())
    assert folded["weight_pct"].sum() == pytest.approx(mix["weight_pct"].sum())


def test_sector_order_is_stable_across_months():
    """Ranked once against the final month, so a sector's colour cannot change as
    the reader moves through time."""
    rows = []
    for month, flip in ((pd.Timestamp("2031-01-31"), False), (pd.Timestamp("2031-02-28"), True)):
        for i, k in enumerate(["A", "B", "C"]):
            mv = (3 - i) if not flip else (i + 1)
            rows.append(
                {"as_of_date": month, "sector": k, "market_value": mv * 1e6, "weight_pct": 33.3}
            )
    mix = pd.DataFrame(rows)
    order = figures.sector_order(mix)
    # February ranks C highest, so the frozen order leads with C.
    assert order[0] == "C"


# ---------------------------------------------------------------------------
# No dual axes, anywhere
# ---------------------------------------------------------------------------


def test_no_figure_uses_a_secondary_y_axis(snap):
    """The single most misleading chart form. Two y-scales on one plot make their
    alignment arbitrary and invent a correlation that is not in the data."""
    hist = snap.positions[snap.positions["security_id"] == "AAA111"].sort_values("as_of_date")
    hist = hist.assign(price_change_pct=hist["price"].pct_change() * 100)

    built = {
        "market_value_over_time": figures.market_value_over_time(snap.overview),
        "mv_change_bars": figures.mv_change_bars(snap.overview),
        "attribution_bars": figures.attribution_bars(snap.monthly_attribution),
        "sector_area": figures.allocation_area(snap.sector_mix, "sector"),
        "rating_area": figures.allocation_area(snap.rating_mix, "rating"),
        "shift_bars": figures.shift_bars(snap.sector_shift, "sector"),
        "security_position": figures.security_position(hist),
        "security_price": figures.security_price(hist),
        "security_oas": figures.security_oas(hist),
        "findings_by_rule": figures.findings_by_rule(findings_summary(snap.findings)),
    }
    for name, fig in built.items():
        layout = fig.to_dict()["layout"]
        extra = [k for k in layout if k.startswith("yaxis") and k != "yaxis"]
        assert not extra, f"{name} has a secondary y-axis: {extra}"


def test_price_and_oas_are_separate_figures(snap):
    """Concretely: the drill-down must not merge them into one plot."""
    hist = snap.positions[snap.positions["security_id"] == "AAA111"].sort_values("as_of_date")
    price = figures.security_price(hist)
    oas = figures.security_oas(hist)
    assert price is not oas
    # Neither figure carries the other's measure.
    assert all("OAS" not in (tr.name or "") for tr in price.data)
    assert all("price" not in (tr.name or "").lower() for tr in oas.data)


# ---------------------------------------------------------------------------
# Figure construction: the bugs that only showed up on render
# ---------------------------------------------------------------------------


def test_every_figure_builds_for_every_security(snap):
    """A numpy bool leaked into showlegend from a pandas .any() and Plotly rejects
    it outright, but only on the branch where an implied price exists. Exercising
    every security is what caught it."""
    from portfolio.app.data import security_history

    failures = []
    for sid in snap.securities["security_id"]:
        hist = security_history(snap, sid)
        for fn in (figures.security_position, figures.security_price, figures.security_oas):
            try:
                fn(hist)
            except Exception as exc:  # noqa: BLE001 — collect, then report all
                failures.append(f"{sid}/{fn.__name__}: {type(exc).__name__}: {exc}")
    assert not failures, failures


def test_figures_autosize_and_the_container_carries_the_height(snap):
    """Plotly's responsive mode sizes to the container, so a figure that also sets
    layout.height invites the two to disagree — and a container with no height
    collapses the plot to nothing."""
    fig = figures.market_value_over_time(snap.overview)
    layout = fig.to_dict()["layout"]
    assert layout.get("height") is None
    assert layout.get("autosize") is True

    g = graph(fig, figures.H_STANDARD)
    assert g.style["height"] == f"{figures.H_STANDARD}px"
    assert g.style["width"] == "100%"
    assert g.config["responsive"] is True


def test_month_axis_ticks_only_at_dates_present_in_the_data(snap):
    """Automatic ticking put a phantom 'Jan 2026' beyond the last observation."""
    fig = figures.market_value_over_time(snap.overview)
    xaxis = fig.to_dict()["layout"]["xaxis"]
    assert xaxis["tickmode"] == "array"
    assert set(pd.to_datetime(list(xaxis["tickvals"]))) == set(snap.overview["as_of_date"])


def test_single_series_charts_carry_no_legend(snap):
    """The title names the series; a legend box for one entry is noise."""
    assert figures.market_value_over_time(snap.overview).layout.showlegend is False
    assert figures.mv_change_bars(snap.overview).layout.showlegend is False


def test_multi_series_charts_carry_a_legend(snap):
    """With two or more series, identity must never be colour-alone."""
    fig = figures.attribution_bars(snap.monthly_attribution)
    assert fig.layout.showlegend is not False
    assert all(tr.name for tr in fig.data)


def test_stacked_bands_are_separated_by_a_surface_gap_not_a_border(snap):
    """The band outline is the surface colour, which reads as a gap."""
    from portfolio.app.theme import LIGHT

    fig = figures.allocation_area(snap.sector_mix, "sector")
    for tr in fig.data:
        assert tr.line.color == LIGHT["surface"]
        assert tr.line.width == figures.FILL_GAP


def test_title_truncation_breaks_on_a_word():
    assert figures._truncate("Short title") == "Short title"
    out = figures._truncate("Duplicate holdings snapshot for security and month-end", 46)
    assert out.endswith("…")
    assert not out.rstrip("…").endswith(" ")
    assert " m…" not in out  # the mid-word fragment this replaced


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "page", [overview, allocation, security, quality], ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_every_page_renders(snap, page):
    assert page.layout(snap) is not None


def test_pages_render_in_dark_mode_too(snap):
    """Dark mode is a selected set of steps, not an inversion, so it needs its own
    render path exercised."""
    for page in (overview, allocation, quality):
        assert page.layout(snap, "dark") is not None


def test_security_detail_renders_for_a_real_security(snap):
    sid = snap.securities["security_id"].iloc[0]
    assert security.detail(snap, sid) is not None


def test_security_detail_handles_no_selection(snap):
    assert security.detail(snap, None) is not None


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def test_create_app_returns_a_dash_app(loaded_engine):
    app = create_app(loaded_engine)
    assert isinstance(app, Dash)


def test_healthz_reports_ok_against_a_live_database(loaded_engine):
    app = create_app(loaded_engine)
    client = app.server.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert b"ok" in resp.data


def test_index_serves(loaded_engine):
    app = create_app(loaded_engine)
    resp = app.server.test_client().get("/")
    assert resp.status_code == 200


def test_snapshot_is_cached_per_load(loaded_engine):
    """Every callback would otherwise re-query and re-derive the whole panel."""
    from portfolio.app.data import clear_cache

    clear_cache()
    a = get_snapshot(loaded_engine)
    b = get_snapshot(loaded_engine)
    assert a is b


def test_snapshot_rebuilds_after_a_new_load(loaded_engine, tmp_path, master, holdings, marks, transactions):
    """The cache key is the load_id, so a fresh load invalidates it without a flush."""
    first = get_snapshot(loaded_engine)
    d = tmp_path / "extracts"
    run_load(loaded_engine, d)
    second = get_snapshot(loaded_engine)
    assert second.load_id != first.load_id
    assert second is not first


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_money_formatting_handles_missing_values():
    assert money(None) == "—"
    assert money(float("nan")) == "—"
    assert money(1_234_567) == "$1.2M"


def test_signed_money_shows_direction():
    assert signed_money(1_000_000).startswith("+")
    assert signed_money(-1_000_000).startswith("−")
    assert signed_money(None) == "—"


def test_pct_formatting():
    assert pct(12.345, 2) == "12.35%"
    assert pct(None) == "—"
