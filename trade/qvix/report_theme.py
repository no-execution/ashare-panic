"""Display-only calendar compression and a high-contrast night palette."""
import pandas as pd
import plotly.graph_objects as go
from math import isfinite


SURFACE = "#101925"
PLOT = "#0b1420"
TEXT = "#edf4ff"
MUTED = "#b4c4d8"
GRID = "#27364a"
CANDLE_UP = "#ffd166"
CANDLE_DOWN = "#61c6ff"
COLORS = {
    "#d84a48": "#ff7f8a", "#219674": "#5ee0b7",
    "#d47535": "#ffb454", "#8362bd": "#c5a0ff", "#2873bb": "#79b8ff",
    "#cf4683": "#ff83bb", "#3269ad": "#79b8ff", "#208978": "#5ee0b7",
    "#9961b2": "#c5a0ff", "#d2902a": "#ffd166", "#d62728": "#ff7f8a",
    "#64748b": MUTED, "#f59e0b": "#ffd166", "#0891b2": "#55dbea",
    "#94a3b8": "#99acc5", "#c43c4b": "#ff8b98", "#b52d3d": "#ffabb4",
    "#a1a9b5": MUTED, "#738297": MUTED, "#409b91": "#5ee0b7",
    "#30867d": "#5ee0b7", "#c99ab0": "#e8a6c4", "#bd7761": "#ffb89b",
}


def session_rangebreaks(dates):
    """Remove only dates outside the union of the chart's known sessions.

    Never derive this from a single indicator's finite values: a missing RPS
    or QVIX observation on an index trading date must remain a visible gap.
    This is a display calendar, not a new input to any indicator calculation.
    """
    sessions = pd.DatetimeIndex(pd.to_datetime(list(dates))).normalize().unique().sort_values()
    if len(sessions) < 2:
        return []
    absent = pd.date_range(sessions[0], sessions[-1], freq="D").difference(sessions)
    return [{"values": absent.strftime("%Y-%m-%d").tolist(), "dvalue": 86400000}] if len(absent) else []


def night_theme(figure):
    figure.update_layout(template="plotly_dark", paper_bgcolor=SURFACE, plot_bgcolor=PLOT,
                         font={"color": TEXT}, colorway=["#79b8ff", "#5ee0b7", "#c5a0ff", "#ffd166", "#ff83bb", "#55dbea"])
    figure.update_xaxes(gridcolor=GRID, linecolor=GRID, tickfont={"color": MUTED},
                        rangeslider={"bgcolor": PLOT, "bordercolor": GRID})
    figure.update_yaxes(gridcolor=GRID, linecolor=GRID, tickfont={"color": MUTED}, title_font_color=MUTED)
    for trace in figure.data:
        if trace.type == "candlestick":
            # Set the bodies as well as the outlines: Plotly's translucent
            # default bodies are hard to distinguish on a dark background.
            trace.increasing.update(fillcolor=CANDLE_UP, line={"color": CANDLE_UP, "width": 1.8})
            trace.decreasing.update(fillcolor=CANDLE_DOWN, line={"color": CANDLE_DOWN, "width": 1.8})
        for attr in ("line", "marker"):
            component = getattr(trace, attr, None)
            color = getattr(component, "color", None)
            if isinstance(color, str) and color in COLORS:
                component.color = COLORS[color]
    for shape in figure.layout.shapes:
        shape.line.color = COLORS.get(shape.line.color, shape.line.color)
    for annotation in figure.layout.annotations:
        annotation.font.color = COLORS.get(annotation.font.color, TEXT)
        if annotation.bgcolor:
            annotation.bgcolor = SURFACE


def add_display_bridges(figure, sessions, max_missing=5):
    """Join short display gaps without creating any missing observations.

    Dotted guides span at most five missing sessions. Leading/trailing missing
    values (notably unpublished RPS) are never extrapolated. Snapshot joins are
    separate dashed guides and only allowed for compatible measurement methods.
    Guides are ignored by exact-date cursor values and dots, and inherit the
    source visibility/filter metadata. Original traces and numeric data stay put.
    """
    positions = {pd.Timestamp(day).normalize(): i for i, day in enumerate(sessions)}
    traces = list(figure.data)
    snapshots = {(t.meta or {}).get("cursor_key"): t for t in traces
                 if (t.meta or {}).get("snapshot") and not (t.meta or {}).get("cursor_skip")}

    def valid(value):
        return value is not None and isfinite(float(value))

    for trace in traces:
        meta = trace.meta or {}
        key = meta.get("cursor_key")
        if trace.type != "scatter" or not key or meta.get("snapshot") or meta.get("cursor_skip"):
            continue
        points = [(pd.Timestamp(x).normalize(), y) for x, y in zip(trace.x, trace.y) if valid(y)]
        if not points:
            continue
        segments = {"missing": [], "snapshot": []}
        for left, right in zip(points, points[1:]):
            if 0 < positions[right[0]] - positions[left[0]] - 1 <= max_missing:
                segments["missing"].append((left, right))
        live = snapshots.get(key)
        if live is not None and (live.meta or {}).get("join_history", True):
            date, value = pd.Timestamp(live.x[0]).normalize(), live.y[0]
            # A stale/same-day snapshot must never double back over history.
            if valid(value) and 0 < positions[date] - positions[points[-1][0]] <= max_missing + 1:
                kind = "snapshot" if positions[date] - positions[points[-1][0]] == 1 else "missing"
                segments[kind].append((points[-1], (date, value)))
                if not live.marker.color:
                    live.marker.color = trace.line.color
        group = trace.legendgroup or "series:" + key
        trace.legendgroup = group
        if live is not None and (live.meta or {}).get("join_history", True):
            live.legendgroup = group
        for kind, pairs in segments.items():
            if not pairs:
                continue
            xs, ys = [], []
            for left, right in pairs:
                xs.extend([left[0], right[0], None])
                ys.extend([left[1], right[1], None])
            figure.add_trace(go.Scatter(
                x=xs, y=ys, xaxis=trace.xaxis, yaxis=trace.yaxis, mode="lines",
                name=trace.name + (" · 缺数连接（非补值）" if kind == "missing" else " · 采样连接"),
                meta={**meta, "cursor_skip": True, "display_bridge": kind},
                visible=trace.visible, showlegend=False, legendgroup=group,
                hoverinfo="skip", connectgaps=False,
                line={"color": trace.line.color, "dash": "dot" if kind == "missing" else "dash",
                      "width": 1.5 if kind == "missing" else 2},
            ))
