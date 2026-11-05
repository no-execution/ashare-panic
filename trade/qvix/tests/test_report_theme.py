import pandas as pd
import plotly.graph_objects as go

from trade.qvix.report_theme import (session_rangebreaks, night_theme, add_display_bridges,
                                    COLORS, TEXT, MUTED, PLOT, SURFACE, CANDLE_UP, CANDLE_DOWN)


def test_compressed_calendar_skips_weekend_and_long_holiday_not_known_missing_session():
    # The middle trading session may have NaN in any individual indicator.
    dates = pd.to_datetime(["2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08"])
    breaks = session_rangebreaks(dates)
    removed = breaks[0]["values"]
    assert breaks[0]["dvalue"] == 86400000
    assert all(d in removed for d in ["2026-09-26", "2026-09-27", "2026-10-01", "2026-10-07"])
    assert not set(dates.strftime("%Y-%m-%d")) & set(removed)
    assert "2026-09-30" not in removed
    assert session_rangebreaks([]) == []
    assert session_rangebreaks(["2026-09-30"]) == []


def test_calendar_preserves_explicit_observation_even_if_it_is_a_weekend():
    # No blanket weekend filter that could silently remove an observed bar.
    assert session_rangebreaks(["2026-09-25", "2026-09-26", "2026-09-28"])[0]["values"] == ["2026-09-27"]


def luminance(color):
    values = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return sum(v * weight for v, weight in zip(values, (.2126, .7152, .0722)))


def contrast(first, second):
    a, b = sorted([luminance(first), luminance(second)])
    return (b + .05) / (a + .05)


def test_night_palette_readable_and_does_not_fill_or_change_data():
    figure = go.Figure(go.Scatter(x=["2026-09-29", "2026-09-30", "2026-10-08"], y=[1, None, 3],
                                 connectgaps=False, line_color="#d62728"))
    night_theme(figure)
    assert figure.data[0].y == (1, None, 3)
    assert figure.data[0].connectgaps is False
    assert figure.layout.paper_bgcolor == SURFACE
    assert figure.layout.plot_bgcolor == PLOT
    assert figure.data[0].line.color == COLORS["#d62728"]
    assert contrast(TEXT, SURFACE) >= 7
    assert contrast(MUTED, SURFACE) >= 7
    assert all(contrast(color, PLOT) >= 3 for color in COLORS.values())


def test_candles_have_opaque_contrasting_bodies_and_wicks():
    figure = go.Figure(go.Candlestick(x=["2026-09-30"], open=[2], high=[4], low=[1], close=[3]))
    night_theme(figure)
    for side, color in [(figure.data[0].increasing, CANDLE_UP), (figure.data[0].decreasing, CANDLE_DOWN)]:
        assert side.fillcolor == side.line.color == color
        assert side.line.width == 1.8
        assert contrast(color, PLOT) >= 7
    assert figure.data[0].close == (3,)


def test_display_bridges_preserve_missing_readings_and_unpublished_rps_tail():
    dates = pd.to_datetime(["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"])
    figure = go.Figure(go.Scatter(x=dates, y=[1, None, None, 4, None], xaxis="x4", yaxis="y4",
                                 name="RPS", meta={"cursor_key": "selected_pct", "rps": True},
                                 connectgaps=False, line_color="#ff7f8a"))
    add_display_bridges(figure, dates)
    original, guide = figure.data
    assert original.y == (1, None, None, 4, None)
    assert original.connectgaps is False
    assert guide.y == (1, 4, None)
    assert list(guide.x) == [dates[0], dates[3], None]
    assert guide.meta["cursor_skip"] and guide.meta["rps"]
    assert guide.meta["display_bridge"] == "missing"
    assert guide.line.dash == "dot"
    assert guide.showlegend is False
    assert guide.yaxis == "y4" and guide.legendgroup == original.legendgroup


def test_snapshot_bridge_connects_across_holiday_not_across_methods_or_stale_dates():
    dates = pd.to_datetime(["2026-09-30", "2026-10-08"])
    for join, live_date, count in [(True, dates[1], 3), (False, dates[1], 2), (True, dates[0], 2)]:
        figure = go.Figure([
            go.Scatter(x=[dates[0]], y=[70], name="历史", xaxis="x2", yaxis="y2",
                       meta={"cursor_key": "panic", "pressure": "combined"}, visible=False, line_color="#ffd166"),
            go.Scatter(x=[live_date], y=[83], name="采样", mode="markers",
                       meta={"cursor_key": "panic", "snapshot": True, "join_history": join})])
        add_display_bridges(figure, dates)
        assert len(figure.data) == count
        assert figure.data[1].y == (83,)
        if count == 3:
            guide = figure.data[-1]
            assert guide.y == (70, 83, None)
            assert guide.visible is False and guide.meta["pressure"] == "combined"
            assert guide.meta["display_bridge"] == "snapshot" and guide.meta["cursor_skip"]
            assert guide.line.dash == "dash"


def test_no_bridge_for_long_gaps_warmup_or_absent_series():
    dates = pd.bdate_range("2026-09-01", periods=10)
    for values in [[1] + [None] * 8 + [3], [None] * 9 + [3], [None] * 10]:
        figure = go.Figure(go.Scatter(x=dates, y=values, meta={"cursor_key": "heat:code", "heat_index": "code"}))
        add_display_bridges(figure, dates)
        assert len(figure.data) == 1
