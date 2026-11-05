"""Self-contained Plotly report; no web server or frontend build step."""
from html import escape
from pathlib import Path
from string import Template

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .config import FACTORS, INDICES, QVIX, PANIC_METHOD, QVIX_METHOD, RANK_WINDOWS
from .metrics import qvix_context, rolling_percentile, score_history
from .options import method_id
from .damage import DAMAGE_METHOD, DAMAGE_SCALES, score_damage, combine_pressure
from .damage_validation import validate_damage
from .heat import index_heat
from .rps import load_counts
from .report_theme import session_rangebreaks, add_display_bridges
from .review import review_data
from .service import calendar_of, index_history
from .sources import now_shanghai
from .store import Store, dumps


def report_data(store, settings):
    indices = index_history(store)
    calendar = calendar_of(indices)
    panic = store.read("panic", PANIC_METHOD).reindex(calendar)
    daily = pd.DataFrame(index=calendar)
    if set(FACTORS).issubset(panic.columns):
        panic["data_valid"] = panic.data_valid.fillna(False).astype(bool)
        daily = score_history(panic, settings.window, settings.min_history)
    damage = store.read("damage", DAMAGE_METHOD).reindex(calendar)
    if set(DAMAGE_SCALES).issubset(damage.columns):
        daily = daily.join(score_damage(damage))
    if "damage_score" not in daily:
        daily["damage_score"] = float("nan")
    daily["pressure_score"] = combine_pressure(
        daily.get("score", pd.Series(index=calendar, dtype=float)), daily.damage_score)
    for name in QVIX:
        history = store.read("qvix:" + name, QVIX_METHOD)
        values = history.close.reindex(calendar) if "close" in history else pd.Series(index=calendar, dtype=float)
        daily[name] = values
        daily[name + "_percentile"] = rolling_percentile(values, settings.window, settings.min_history)
        daily[name + "_sample_count"] = values.shift().rolling(settings.window, min_periods=1).count()
        for window, minimum in RANK_WINDOWS.items():
            daily[f"{name}_percentile_{window}"] = rolling_percentile(values, window, minimum)
            daily[f"{name}_sample_count_{window}"] = values.shift().rolling(window, min_periods=1).count()
        daily[name + "_change_5d"] = values - values.shift(5)
    daily.index.name = "date"
    return indices, daily


def build_report(settings, *, start=None, end=None, normalize=False):
    with Store(settings.data_dir) as store:
        indices, full = report_data(store, settings)
        snapshot = store.latest_run("snapshot")
        sync = store.latest_run("sync")
        # Recompute display metrics for older snapshots without rewriting their journal.
        # Slice at each quote's date, so a later sync cannot leak future observations in.
        if snapshot:
            for name, item in snapshot.get("qvix", {}).items():
                if item.get("value") is None or not item.get("quote_time"):
                    continue
                date = pd.Timestamp(item["quote_time"]).tz_localize(None).normalize()
                past = full.loc[full.index < date, name]
                if item["status"] == "estimated":
                    history = store.read("iv30:" + name, item.get("method", method_id(settings.risk_free_rate)))
                    own = history.close.reindex(past.index) if "close" in history else pd.Series(index=past.index, dtype=float)
                    item.update(qvix_context(item["value"], own, reference=past))
                else:
                    item.update(qvix_context(item["value"], past))
    if full.empty:
        raise ValueError("没有历史数据，请先执行 sync")
    if snapshot:
        snapshot_date = pd.Timestamp(snapshot.get("quote_time_max") or snapshot["generated_at"]).tz_localize(None).normalize()
        # Keep the sampling journal, but let a valid close replace its pressure
        # readings everywhere, including chart markers and browser readouts.
        snapshot["pressure_superseded"] = (
            "score" in full and full.loc[snapshot_date:, "score"].notna().any())
    default_start = start or "2020-01-01"
    if start is None:
        # Carry all available history so older bear-market cases remain clickable.
        # The initial viewport still starts in 2020, unless the browser restores it.
        start = str(full.index[0].date())
    daily = full.loc[start:end]
    if daily.empty:
        raise ValueError("所选日期范围无数据")
    rps, rps_payload = load_counts(settings)
    figure = make_subplots(rows=6, cols=1, shared_xaxes=True, vertical_spacing=.075,
                           row_heights=[.25, .18, .16, .16, .14, .11],
                           subplot_titles=["主要指数" + (" · 区间起点=100" if normalize else " · 日K"),
                                           "", "", "", "QVIX分位 · 1年实线 / 3年虚线", "QVIX原值 / 年化%"])
    if not normalize:
        figure.layout.annotations[0].text += " · 红色阳线 / 绿色阴线"
    plotted = []
    heat_frames, heat_live = {}, {}
    for code, frame in indices.items():
        quote = snapshot.get("indices", {}).get(code) if snapshot else None
        if quote and len(frame):
            date = pd.Timestamp(quote["timestamp"]).tz_localize(None).normalize()
            if date > frame.index[-1]:
                frame = pd.concat([frame, pd.DataFrame([quote], index=[date])])
        if frame.empty:
            continue
        # Compute on complete history before viewport slicing. Snapshot is a
        # separate causal append, never a historical ranking observation.
        history = indices[code].close.reindex(full.index) if "close" in indices[code] else pd.Series(index=full.index, dtype=float)
        heat_frames[code] = index_heat(history)
        if quote and len(indices[code]) and date > indices[code].index[-1] and date >= full.index[-1]:
            past = history.loc[history.index < date]
            live_heat = index_heat(pd.concat([past, pd.Series([quote["close"]], index=[date])])).iloc[-1]
            heat_live[code] = (date, live_heat)
        frame = frame.loc[start:end]
        if frame.empty:
            continue
        plotted.append(code)
        if normalize:
            figure.add_trace(go.Scatter(x=frame.index, y=frame.close / frame.close.iloc[0] * 100,
                                        name=INDICES[code], connectgaps=False), row=1, col=1)
        else:
            figure.add_trace(go.Candlestick(x=frame.index, open=frame.open, high=frame.high,
                                            low=frame.low, close=frame.close, name=INDICES[code],
                                            increasing_line_color="#d84a48", decreasing_line_color="#219674",
                                            visible=code == "000300.SH"), row=1, col=1)
    if "score" in daily:
        figure.add_trace(go.Scatter(x=daily.index, y=daily.score, name="短期恐慌",
                                    meta={"cursor_key": "panic", "pressure": "short"}, showlegend=False,
                                    line={"color": "#d47535", "width": 2}, connectgaps=False), row=2, col=1)
        events = daily[daily.panic.fillna(False)]
        figure.add_trace(go.Scatter(x=events.index, y=events.score, name="极端压力触发",
                                    meta={"cursor_skip": True, "pressure": "short"}, showlegend=False,
                                    mode="markers", marker={"color": "#d47535", "symbol": "triangle-up", "size": 7}), row=2, col=1)
    for field, name, color, dash, kind in [("damage_score", "长期损伤", "#8362bd", "dash", "long"),
                                           ("pressure_score", "综合压力（取高值）", "#2873bb", "solid", "combined")]:
        figure.add_trace(go.Scatter(x=daily.index, y=daily[field], name=name,
                                    meta={"cursor_key": field, "pressure": kind}, showlegend=False, visible=kind != "combined",
                                    line={"color": color, "dash": dash, "width": 2.5},
                                    connectgaps=False), row=2, col=1)
    colors = ["#3269ad", "#208978", "#9961b2", "#d2902a"]
    for (name, (label, _, _)), color in zip(QVIX.items(), colors):
        for window in RANK_WINDOWS:
            figure.add_trace(go.Scatter(x=daily.index, y=daily[f"{name}_percentile_{window}"],
                                        name=f"{label} {window // 252}年分位",
                                        meta={"cursor_key": f"{name}:{window}"},
                                        line={"color": color, "dash": "solid" if window == 252 else "dash"},
                                        connectgaps=False), row=5, col=1)
        figure.add_trace(go.Scatter(x=daily.index, y=daily[name], name=f"{label} 原值",
                                    meta={"cursor_key": name}, customdata=daily[name + "_change_5d"],
                                    line={"color": color}, connectgaps=False), row=6, col=1)
    if snapshot:
        stamp = pd.Timestamp(snapshot.get("quote_time_max") or snapshot["generated_at"]).tz_localize(None)
        in_range = ((start is None or stamp >= pd.Timestamp(start)) and
                    (end is None or stamp.normalize() <= pd.Timestamp(end)))
        if in_range:
            panic = snapshot.get("panic", {})
            if panic.get("score") is not None and not snapshot.get("pressure_superseded"):
                figure.add_trace(go.Scatter(x=[stamp.normalize()], y=[panic["score"]], mode="markers",
                                            meta={"cursor_key": "panic", "snapshot": True, "pressure": "short"}, showlegend=False,
                                            name="短期恐慌采样", marker={"symbol": "diamond", "color": "#d47535", "size": 10}), row=2, col=1)
            for field, value, label, color in [
                ("damage_score", snapshot.get("damage", {}).get("damage_score"), "长期损伤采样", "#8362bd"),
                ("pressure_score", snapshot.get("pressure_score"), "综合压力采样", "#2873bb"),
            ]:
                if value is not None and not snapshot.get("pressure_superseded"):
                    figure.add_trace(go.Scatter(x=[stamp.normalize()], y=[value], mode="markers", name=label,
                                                meta={"cursor_key": field, "snapshot": True, "pressure": "long" if field == "damage_score" else "combined"},
                                                showlegend=False, visible=field != "pressure_score",
                                                marker={"symbol": "diamond", "color": color, "size": 9}), row=2, col=1)
            for name, item in snapshot.get("qvix", {}).items():
                for window in RANK_WINDOWS:
                    rank = item.get("windows", {}).get(str(window), {}).get("percentile")
                    cross_method = rank is None or pd.isna(rank)
                    if cross_method:
                        rank = item.get("reference_windows", {}).get(str(window), {}).get("percentile")
                    if rank is not None and pd.notna(rank):
                        quote_stamp = pd.Timestamp(item["quote_time"]).tz_localize(None).normalize()
                        label = "跨口径参考分位" if cross_method else "采样分位"
                        figure.add_trace(go.Scatter(x=[quote_stamp], y=[rank], mode="markers",
                                                    name=f"{QVIX[name][0]} {window // 252}年{label}",
                                                    meta={"cursor_key": f"{name}:{window}", "snapshot": True,
                                                          "join_history": not cross_method and item.get("status") != "estimated"},
                                                    showlegend=False, marker={"symbol": "diamond"}), row=5, col=1)
                if item.get("value") is not None:
                    label = "自算IV30" if item["status"] == "estimated" else "盘中QVIX"
                    figure.add_trace(go.Scatter(x=[pd.Timestamp(item["quote_time"]).tz_localize(None).normalize()], y=[item["value"]],
                                                mode="markers", name=QVIX[name][0] + label,
                                                meta={"cursor_key": name, "snapshot": True,
                                                      "join_history": item.get("status") != "estimated"},
                                                customdata=[item.get("change_5d")],
                                                marker={"symbol": "diamond", "size": 9}), row=6, col=1)
    heat_active = "000300.SH" if "000300.SH" in plotted else plotted[0]
    for code in plotted:
        values = heat_frames[code].loc[start:end]
        figure.add_trace(go.Scatter(x=values.index, y=values.heat_score, name=INDICES[code] + " · 过热度",
                                    visible=code == heat_active, showlegend=False, connectgaps=False,
                                    meta={"cursor_key": "heat:" + code, "heat_index": code},
                                    line={"color": "#cf4683", "width": 2.5}), row=3, col=1)
        if code in heat_live:
            date, row = heat_live[code]
            if pd.Timestamp(start) <= date and (end is None or date <= pd.Timestamp(end)):
                figure.add_trace(go.Scatter(x=[date], y=[row.heat_score], name=INDICES[code] + " · 过热度采样",
                                            visible=code == heat_active, showlegend=False, mode="markers",
                                            meta={"cursor_key": "heat:" + code, "heat_index": code, "snapshot": True},
                                            marker={"symbol": "diamond", "color": "#cf4683", "size": 9}), row=3, col=1)
    # Reindex against the dashboard calendar, including snapshots, so gaps and
    # unpublished intraday data break the line instead of joining across them.
    rps_dates = daily.index.union(rps.loc[start:end].index)
    if snapshot:
        live_date = pd.Timestamp(snapshot.get("quote_time_max") or snapshot["generated_at"]).tz_localize(None).normalize()
        if pd.Timestamp(start) <= live_date and (end is None or live_date <= pd.Timestamp(end)):
            rps_dates = rps_dates.union(pd.DatetimeIndex([live_date]))
    rps_values = rps.reindex(rps_dates)
    for field, label, color, dash in [
        ("selected_pct", "符合条件占比", "#d62728", "solid"),
        ("candidate_pct", "RPS候选占比", "#64748b", "dash"),
        ("ma5", "RPS 5日均线", "#f59e0b", "dashdot"),
        ("ma20", "RPS 20日均线", "#0891b2", "dashdot"),
    ]:
        figure.add_trace(go.Scatter(x=rps_dates, y=rps_values[field], name=label,
                                    meta={"cursor_key": field, "rps": True, "rps_unit": "%"}, showlegend=False,
                                    connectgaps=False, line={"color": color, "dash": dash,
                                                          "width": 2.4 if field == "selected_pct" else 1.5}), row=4, col=1)
    if pd.notna(rps_payload["mean_pct"]):
        figure.add_hline(y=rps_payload["mean_pct"], row=4, col=1, line_dash="dot", line_color="#94a3b8",
                         annotation_text=f'全样本均值 {rps_payload["mean_pct"]:.2f}%（事后参考）',
                         annotation_position="top right", annotation_font_size=10)
    figure.add_hline(y=rps_payload["risk"]["threshold_pct"], row=4, col=1,
                     line_dash="dash", line_color="#c43c4b", line_width=1.8,
                     annotation_text=f'高位风险提示 {rps_payload["risk"]["threshold_pct"]:.2f}% · 非卖出信号',
                     annotation_position="bottom left", annotation_font_size=11,
                     annotation_font_color="#b52d3d", annotation_bgcolor="rgba(255,255,255,0.9)")
    if not normalize and plotted:
        active = plotted.index("000300.SH") if "000300.SH" in plotted else 0
        figure.data[active].visible = True
    figure.add_hline(y=85, row=2, col=1, line_dash="dot", line_color="#a1a9b5",
                     annotation_text="高压参考 85", annotation_position="top right",
                     annotation_font_size=10, annotation_font_color="#738297")
    figure.add_hline(y=15, row=2, col=1, line_dash="dot", line_color="#409b91",
                     annotation_text="低压参考 15 · 非卖出信号", annotation_position="bottom right",
                     annotation_font_size=10, annotation_font_color="#30867d")
    for level in (80, 90):
        figure.add_hline(y=level, row=3, col=1, line_dash="dot", line_color="#c99ab0",
                         annotation_text=f"参考 {level}", annotation_position="top right", annotation_font_size=10)
    figure.add_hline(y=95, row=5, col=1, line_dash="dot", line_color="#bd7761")
    figure.update_yaxes(range=[0, 101], row=2, col=1)
    figure.update_yaxes(range=[0, 101], row=3, col=1)
    figure.update_yaxes(rangemode="tozero", title_text="占股票池 %", row=4, col=1)
    figure.update_yaxes(range=[0, 101], row=5, col=1)
    figure.update_xaxes(rangeslider_visible=False, showspikes=False, showticklabels=True,
                        tickformat="%Y-%m-%d", nticks=7, tickfont={"size": 11, "color": "#718096"},
                        gridcolor="#f0f3f7", zeroline=False)
    figure.update_xaxes(rangeslider={"visible": True, "thickness": .055,
                                    "bgcolor": "#eef2f7", "borderwidth": 1, "bordercolor": "#dbe3ee"}, row=6, col=1)
    figure.update_yaxes(fixedrange=False, gridcolor="#eef2f6", zeroline=False, nticks=5)
    figure.update_layout(height=1970, template="plotly_white", paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
                         hovermode=False, dragmode="pan",
                         meta={"cursor_titles": ["指数", "市场压力 / 分", "指数过热度 / 分", "RPS / 占比 · 盘后", "QVIX分位 / %", "波动率 / 年化%"]},
                         margin={"l": 66, "r": 28, "t": 45, "b": 90},
                         legend={"orientation": "h", "y": -.13, "font": {"size": 11}},
                         font={"family": "Inter, PingFang SC, system-ui, sans-serif", "color": "#334155"})
    # All plotted session dates, including independent EOD RPS and snapshots.
    # Missing values within a known trading session are deliberately NOT filled.
    session_dates = sorted({pd.Timestamp(date).normalize() for trace in figure.data for date in trace.x})
    figure.update_xaxes(rangebreaks=session_rangebreaks(session_dates))
    add_display_bridges(figure, session_dates)

    def number(value):
        return f"{value:.2f}" if value is not None and pd.notna(value) else "缺失"

    def change(value):
        return f"{value:+.2f} 个百分点" if value is not None and pd.notna(value) else "缺失（同口径基准不足）"

    def rank_lines(windows):
        return "<br>".join(f'{window // 252}年分位 {number(windows.get(str(window), {}).get("percentile"))}'
                           f' · n={windows.get(str(window), {}).get("sample_count", 0)}'
                           for window in RANK_WINDOWS)

    live = ""
    if snapshot:
        panic = snapshot.get("panic", {})
        live_cards = [f'<div><b>盘中恐慌评分</b><strong>{number(panic.get("score"))}</strong>'
                      f'<small>报价 {escape(str(snapshot.get("quote_time_max", "缺失")))}<br>当前价估算，非收盘定稿</small></div>']
        for name, item in snapshot.get("qvix", {}).items():
            estimated = item["status"] == "estimated"
            title = "自算IV30" if estimated else "QVIX"
            rank_text = rank_lines(item.get("windows", {}))
            if estimated:
                rank_text = ('同口径：<br>' + rank_text + '<br>跨口径参考（未校准）：<br>'
                             + rank_lines(item.get("reference_windows", {})))
            live_cards.append(f'<div><b>{QVIX[name][0]} · {title}</b><strong>{number(item.get("value"))}%</strong>'
                              f'<small>{rank_text}<br>较5交易日前 {change(item.get("change_5d"))}'
                              f'<br>{escape(item.get("quote_time", item.get("reason", "缺失")))}</small></div>')
        live = (f'<h2>盘中采样 · {escape(snapshot["generated_at"])}</h2><div class="cards">{"".join(live_cards)}</div>'
                '<p>运行 snapshot 获取新快照。自算IV30与供应商QVIX分别保存；跨口径参考分位不是同口径历史分位。指数图最后一根可包含盘中未收盘K线。</p>')
    cards = [f'<div><b>恐慌评分（日级）</b><strong>{number(daily.iloc[-1].get("score"))}</strong>'
             f'<small>{daily.index[-1]:%Y-%m-%d} · 复权完整性未验证</small></div>']
    for name, (label, _, _) in QVIX.items():
        row = daily.iloc[-1]
        valid = full.loc[:daily.index[-1], name].dropna()
        last = valid.index[-1].strftime("%Y-%m-%d") if len(valid) else "无有效记录"
        windows = {str(w): {"percentile": row[f"{name}_percentile_{w}"],
                            "sample_count": int(row[f"{name}_sample_count_{w}"]) if pd.notna(row[f"{name}_sample_count_{w}"]) else 0}
                   for w in RANK_WINDOWS}
        cards.append(f'<div><b>{label} · QVIX</b><strong>{number(row[name])}%</strong>'
                     f'<small>{rank_lines(windows)}<br>较5交易日前 {change(row[name + "_change_5d"])}'
                     f'<br>最后有效原值日期 {last}</small></div>')
    details = []
    for label, run in [("最近同步", sync), ("最近采样（不会自动刷新）", snapshot)]:
        if not run:
            continue
        details.append(f"<h3>{label} · {escape(run['generated_at'])}</h3>")
        if label.startswith("最近采样"):
            details.append(f"<p>恐慌评分：{number(run.get('panic', {}).get('score'))} · 非日终定稿</p>")
            details.append(f"<p>报价时间范围：{escape(str(run.get('quote_time_min', '缺失')))} — "
                           f"{escape(str(run.get('quote_time_max', '缺失')))}</p>")
            for name, item in run.get("qvix", {}).items():
                details.append(f"<p>{QVIX[name][0]}：{number(item.get('value'))} · {escape(item['status'])} · "
                               f"{escape(item.get('quote_time', '行情日期未确认'))}</p>")
                if item.get("warnings"):
                    details.append("<p>" + escape("；".join(item["warnings"])) + "</p>")
        messages = [f"{key}: {value}" for key, value in run.get("errors", {}).items()] + run.get("warnings", [])
        details.append("<ul>" + "".join(f"<li>{escape(str(message)[:600])}</li>" for message in messages) + "</ul>")
    payload = review_data(daily)
    validation = validate_damage(full, indices) if "score" in full and full.damage_score.notna().any() else None
    if validation:
        (settings.data_dir / "damage-validation.json").write_text(dumps(validation), encoding="utf-8")
        pd.DataFrame(validation["cases"]).to_csv(settings.data_dir / "damage-cases.csv", index=False)
    payload["damage_validation"] = validation
    payload["default_start"] = default_start
    payload["snapshot"] = snapshot
    payload["plotted"] = plotted
    payload["normalize"] = normalize
    payload["labels"] = INDICES
    payload["rps"] = rps_payload
    rps.to_csv(settings.data_dir / "rps-breadth.csv")
    (settings.data_dir / "rps-risk.json").write_text(dumps(rps_payload["risk"]), encoding="utf-8")
    payload["heat"] = {}
    for code in plotted:
        payload["heat"][code] = {str(date.date()): row.to_dict() for date, row in heat_frames[code].loc[start:end].iterrows()}
        if code in heat_live:
            date, row = heat_live[code]
            payload["heat"][code][str(date.date())] = {**row.to_dict(), "snapshot": True}
    pd.concat([frame.assign(code=code) for code, frame in heat_frames.items()]).to_csv(settings.data_dir / "index-heat.csv")
    latest = snapshot.get("panic", {}) if snapshot else {}
    score = latest.get("score")
    live_score = score is not None and not snapshot.get("pressure_superseded")
    if not live_score:
        score = daily.iloc[-1].get("score")
    score_time = (snapshot.get("quote_time_max") or snapshot["generated_at"]) if live_score else str(daily.index[-1].date())
    features = latest.get("features", {}) if live_score else daily.iloc[-1].to_dict()
    long_row = snapshot.get("damage", {}) if live_score else daily.iloc[-1].to_dict()
    long_score = long_row.get("damage_score")
    combined = max(score, long_score) if pd.notna(score) and pd.notna(long_score) else None
    long_kind = ("持续高压" if long_score >= 85 else "未达长期高压线") if pd.notna(long_score) else "尚无同日长期数据"
    plot_html = figure.to_html(full_html=False, include_plotlyjs=True,
                              config={"responsive": True, "displaylogo": False, "scrollZoom": False,
                                      "doubleClick": "reset", "displayModeBar": False},
                              post_script=Path(__file__).with_name("report_cursor.js").read_text()
                              + "\n" + Path(__file__).with_name("report_controls.js").read_text())
    html = Template(Path(__file__).with_name("report_shell.html").read_text()).substitute(
        css=Path(__file__).with_name("report.css").read_text(),
        generated=now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        first=str(daily.index[0].date()), last=str(daily.index[-1].date()),
        score=number(score), score_time=escape(str(score_time).replace("T", " ").replace("+08:00", "")),
        long_score=number(long_score), combined=number(combined), long_kind=long_kind,
        score_kind="盘中估算" if live_score else "日级评分",
        status="极端压力触发" if (latest.get("panic") if live_score else daily.iloc[-1].get("panic", False)) else "未触发极端压力",
        count=int(features["count"]) if pd.notna(features.get("count")) else "—",
        below=number(features.get("below_ma20") * 100 if features.get("below_ma20") is not None else None),
        drop=number(features.get("drop5_share") * 100 if features.get("drop5_share") is not None else None),
        payload=dumps(payload).replace("<", "\\u003c"), plot=plot_html,
        live=live, cards="".join(cards), details="".join(details),
        window=settings.window, minimum=settings.min_history)
    path = settings.data_dir / "report.html"
    path.write_text(html, encoding="utf-8")
    full.to_csv(settings.data_dir / "daily.csv")
    return path
