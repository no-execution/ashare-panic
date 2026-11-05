/* Offline review controls. Dates are UTC day keys, independent of browser zone. */
(() => {
    const DAY = 86400000;
    const iso = value => new Date(value).toISOString().slice(0, 10);
    const stamp = value => Date.parse(String(value).slice(0, 10) + 'T00:00:00Z');
    const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '缺失';
    function clampRange(left, right, min, max) {
        const width = Math.min(Math.max(right - left, DAY), max - min);
        left = Math.max(min, Math.min(left, max - width));
        return [left, left + width];
    }
    function shiftRange(range, scale, shift, bounds) {
        const width = range[1] - range[0];
        const center = (range[0] + range[1]) / 2 + shift * width;
        return clampRange(center - width * scale / 2, center + width * scale / 2, ...bounds);
    }
    function pressureVisible(kind, mode) {
        return mode === 'compare' ? kind === 'short' || kind === 'long' : kind === mode;
    }
    function sessionIndices(range, dates) {
        const times = dates.map(stamp);
        let left = times.findIndex(t => t >= range[0]);
        if (left < 0) left = times.length - 1;
        let right = times.length - 1;
        while (right > 0 && times[right] > range[1]) right--;
        if (right < left) {right = left; left = Math.max(0, left - 1);}
        if (right === left) {if (right < times.length - 1) right++; else left = Math.max(0, left - 1);}
        return [left, right];
    }
    function snapSessionRange(range, dates) {
        if (dates.length < 2) return range;
        return sessionIndices(range, dates).map(i => stamp(dates[i]));
    }
    function shiftSessionRange(range, scale, shift, dates) {
        if (dates.length < 2) return range;
        const [left, right] = sessionIndices(range, dates), width = right - left;
        const span = Math.min(dates.length - 1, Math.max(1, Math.round(width * scale)));
        const center = (left + right) / 2 + shift * width;
        const start = Math.max(0, Math.min(dates.length - 1 - span, Math.round(center - span / 2)));
        return [stamp(dates[start]), stamp(dates[start + span])];
    }
    function sessionTicks(range, dates, count = 7) {
        const inside = dates.filter(date => stamp(date) >= range[0] && stamp(date) <= range[1]);
        const size = Math.min(count, inside.length);
        if (size < 2) return inside;
        return Array.from({length: size}, (_, i) => inside[Math.round(i * (inside.length - 1) / (size - 1))]);
    }
    if (typeof module !== 'undefined') module.exports = {clampRange, shiftRange, pressureVisible, snapSessionRange, shiftSessionRange, sessionTicks};
    if (typeof document === 'undefined') return;
    async function init() {
        const graph = document.getElementById('{plot_id}');
        const data = JSON.parse(document.getElementById('review-data').textContent);
        const $ = id => document.getElementById(id);
        const dates = [...new Set(graph.data.filter(t => !t.meta?.display_bridge)
            .flatMap(t => Array.from(t.x || []).filter(x => x != null).map(x => String(x).slice(0, 10))))].sort();
        const bounds = [stamp(dates[0]), stamp(dates.at(-1))];
        let range = [...bounds];
        const storageKey = 'qvix-review-view-v1:' + location.pathname;
        let saved;
        try {saved = JSON.parse(localStorage.getItem(storageKey));} catch (_) { /* Storage may be disabled. */ }
        function remember() {
            try {localStorage.setItem(storageKey, JSON.stringify({range, index: $('index-select').value}));} catch (_) {}
        }
        const axes = ['xaxis', 'xaxis2', 'xaxis3', 'xaxis4', 'xaxis5', 'xaxis6'];
        function placePressureTools() {
            const top = graph._fullLayout.yaxis2._offset;
            const panel = $('pressure-tools');
            panel.style.top = (top - panel.offsetHeight - 8) + 'px';
            const heatPanel = $('heat-tools');
            heatPanel.style.top = (graph._fullLayout.yaxis3._offset - heatPanel.offsetHeight - 8) + 'px';
            const rpsPanel = $('rps-tools');
            rpsPanel.style.top = (graph._fullLayout.yaxis4._offset - rpsPanel.offsetHeight - 8) + 'px';
        }
        graph.on('plotly_afterplot', placePressureTools);
        const pressureTraces = graph.data.map((trace, i) => trace.meta?.pressure ? i : null).filter(i => i != null);
        function pressureLabels(mode) {
            document.querySelectorAll('#pressure-modes button').forEach(button => {
                button.classList.toggle('active', button.dataset.mode === mode);
                button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
            });
            for (const kind of ['short', 'long', 'combined']) {
                const drawn = pressureVisible(kind, mode);
                const chip = document.querySelector('.pressure-chip.' + kind);
                chip.classList.toggle('not-plotted', !drawn);
                chip.title = drawn ? '当前已绘制此曲线' : '当前仅显示数值，未绘制曲线';
            }
            $('pressure-hint').textContent = mode === 'compare' ? '综合默认不叠画 · 高压85 / 低压15 · ▲短期触发' : '高压85 / 低压15 · 菱形为盘中采样' + (mode === 'short' ? ' · ▲短期触发' : '');
        }
        $('pressure-modes').onclick = async event => {
            const mode = event.target.dataset.mode;
            if (!['compare', 'short', 'long', 'combined'].includes(mode)) return;
            await Plotly.restyle(graph, {visible: pressureTraces.map(i => pressureVisible(graph.data[i].meta.pressure, mode))}, pressureTraces);
            pressureLabels(mode);
            graph._reviewSelect(graph._reviewDate || dates.at(-1));
            placePressureTools();
        };
        pressureLabels('compare'); placePressureTools();
        const dateFields = [$('date-start'), $('date-end')];
        for (const input of dateFields) {input.min = dates[0]; input.max = dates.at(-1);}

        function syncFields() {
            dateFields.forEach((input, i) => {input.value = iso(range[i]);});
            $('range-status').textContent = `${iso(range[0])} — ${iso(range[1])} · 交易日压缩轴（跳过休市空档）· 缺数不补值 · 拖动平移 / 框选放大`;
        }
        function fitY(next) {
            const values = graph.data.filter(t => (t.yaxis || 'y') === 'y' && t.visible !== false && t.visible !== 'legendonly')
                .flatMap(t => Array.from(t.x, (x, i) => {
                    const time = stamp(x);
                    return time >= next[0] && time <= next[1] ? [t.low?.[i] ?? t.y?.[i], t.high?.[i] ?? t.y?.[i]] : [];
                }).flat()).filter(v => typeof v === 'number' && Number.isFinite(v));
            if (!values.length) return {};
            const lo = Math.min(...values), hi = Math.max(...values), pad = (hi - lo || hi * .02 || 1) * .08;
            return {'yaxis.range': [lo - pad, hi + pad], 'yaxis.autorange': false};
        }
        function dateTicks(next) {
            // Plotly's auto date ticks may label a holiday at a compressed
            // boundary. Use actual session dates for both tick position and text.
            const ticks = sessionTicks(next, dates), update = {};
            for (const axis of axes) {
                update[axis + '.tickmode'] = 'array';
                update[axis + '.tickvals'] = ticks;
                update[axis + '.ticktext'] = ticks;
            }
            return update;
        }
        async function setRange(next, preset) {
            range = snapSessionRange(clampRange(Math.max(next[0], bounds[0]), Math.min(next[1], bounds[1]), ...bounds), dates);
            const update = {...fitY(range), ...dateTicks(range)};
            for (const axis of axes) {update[axis + '.range'] = range.map(iso); update[axis + '.autorange'] = false;}
            syncFields();
            await Plotly.relayout(graph, update);
            document.querySelectorAll('#presets button').forEach(button => button.classList.toggle('active', button.dataset.range === preset));
            if (!graph._reviewPinned && (!graph._reviewDate || stamp(graph._reviewDate) < range[0] || stamp(graph._reviewDate) > range[1])) {
                const inside = dates.filter(date => stamp(date) >= range[0] && stamp(date) <= range[1]);
                if (inside.length) graph._reviewSelect(inside.at(-1));
            }
            remember();
        }
        $('presets').addEventListener('click', event => {
            const key = event.target.dataset.range;
            if (!key) return;
            let left = bounds[0];
            const end = new Date(bounds[1]);
            if (key === '2020') left = stamp('2020-01-01');
            if (key.endsWith('m')) {end.setUTCMonth(end.getUTCMonth() - parseInt(key)); left = end.getTime();}
            if (key.endsWith('y')) {end.setUTCFullYear(end.getUTCFullYear() - parseInt(key)); left = end.getTime();}
            setRange([left, bounds[1]], key);
        });
        $('apply-dates').onclick = () => {
            const next = dateFields.map(input => stamp(input.value));
            if (!next.every(Number.isFinite) || next[0] >= next[1] || next[0] > bounds[1] || next[1] < bounds[0]) {
                $('range-status').textContent = '请选择有效的起止日期，结束日期需晚于起始日期。'; return;
            }
            setRange(next);
        };
        for (const [id, scale, shift] of [['zoom-in', .5, 0], ['zoom-out', 2, 0], ['pan-left', 1, -.5], ['pan-right', 1, .5]]) {
            $(id).onclick = () => setRange(shiftSessionRange(range, scale, shift, dates));
        }
        $('reset-view').onclick = () => setRange(bounds, 'all');
        for (const mode of ['pan', 'zoom']) $('drag-' + mode).onclick = () => {
            Plotly.relayout(graph, {dragmode: mode});
            for (const other of ['pan', 'zoom']) $('drag-' + other).classList.toggle('active', mode === other);
        };
        $('wheel-zoom').onchange = event => Plotly.react(graph, graph.data, graph.layout, {
            responsive: true, displaylogo: false, displayModeBar: false,
            doubleClick: 'reset', scrollZoom: event.target.checked
        });
        $('save-image').onclick = () => {
            // HTML controls are outside Plotly's image export; restore the names
            // of the currently drawn pressure curves in the exported legend.
            const exported = graph.data.map(trace => ({...trace,
                showlegend: trace.meta?.cursor_skip ? false : trace.meta?.rps ? true : trace.meta?.heat_index ? !trace.meta.snapshot : trace.meta?.pressure ? !trace.meta.snapshot : trace.showlegend}));
            return Plotly.downloadImage({data: exported, layout: graph.layout},
                {format: 'png', filename: 'market-review', width: 1500, height: 1970, scale: 2});
        };
        $('pin-date').onclick = () => {
            graph._reviewPinned = !graph._reviewPinned;
            $('pin-date').textContent = graph._reviewPinned ? '取消固定' : '固定读数';
            $('pin-date').classList.toggle('active', graph._reviewPinned);
        };
        for (const [i, code] of data.plotted.entries()) {
            const option = new Option(data.labels[code], String(i));
            option.selected = code === (data.plotted.includes('000300.SH') ? '000300.SH' : data.plotted[0]);
            $('index-select').add(option);
        }
        $('index-select').onchange = async event => {
            const selected = Number(event.target.value);
            if (!data.normalize) await Plotly.restyle(graph, {visible: data.plotted.map((_, i) => i === selected)}, data.plotted.map((_, i) => i));
            const heatTraces = graph.data.map((trace, i) => trace.meta?.heat_index ? i : null).filter(i => i != null);
            await Plotly.restyle(graph, {visible: heatTraces.map(i => graph.data[i].meta.heat_index === data.plotted[selected])}, heatTraces);
            await Plotly.relayout(graph, fitY(range));
            graph._reviewSelect(graph._reviewDate || dates.at(-1));
            remember();
        };
        let fitting = false;
        graph.on('plotly_relayout', async event => {
            if (fitting) return;
            const axis = axes.find(a => event[a + '.range[0]'] !== undefined || event[a + '.range'] || event[a + '.autorange']);
            if (!axis) return;
            const current = graph._fullLayout[axis].range;
            range = current.map(stamp);
            syncFields();
            remember();
            document.querySelectorAll('#presets button').forEach(button => button.classList.remove('active'));
            fitting = true;
            try {await Plotly.relayout(graph, {...fitY(range), ...dateTicks(range)});} finally {fitting = false;}
        });
        const factorDefs = [
            ['below_ma20', '低于 MA20 比例'], ['new_low20', '创前20日收盘新低比例'],
            ['drop5_share', '跌幅 ≥5% 比例'], ['basket_loss5', '篮子五日损失'], ['downside_vol20', '20日下行年化波动']
        ];
        const rps = data.rps || {};
        $('rps-latest').textContent = '仅盘后 · 最新有效 ' + (rps.latest || '暂无');
        $('rps-method').textContent = rps.method || '';
        $('rps-source').textContent = `最新有效日期：${rps.latest || '暂无'}。不将昨日值延伸到今日；未更新日期留空。` +
            (rps.sources?.length ? ' 来源：' + rps.sources.join('；') : ' 未找到兼容的家数统计文件。') +
            (rps.warnings?.length ? ' 警告：' + rps.warnings.join('；') : '');
        const risk = rps.risk || {};
        const reference = risk.comparisons?.find(item => item.threshold_pct === risk.threshold_pct);
        $('rps-risk-summary').textContent = `固定高位线 ${number(risk.threshold_pct)}%：依据 ${risk.sample_start || '—'} 至 ${risk.sample_end || '—'} 的 ${risk.records ?? 0} 条有效记录，95分位 ${number(risk.p95_pct)}%。` +
            (reference ? ` 达线 ${reference.high_records} 条（${number(reference.share_pct)}%），按已有RPS记录分为 ${reference.episodes} 段，最长连续 ${reference.longest_records} 条。` : '') +
            (risk.partial_price_records ? ` 其中 ${risk.partial_price_records} 条存在部分价格缺失；假设缺价候选全部合格，95分位上界 ${number(risk.p95_upper_bound_pct)}%，另有 ${risk.ambiguous_high_records} 条可能达线。` : '') +
            ` 选定日期 ${risk.selected_on || '—'}；后续刷新不自动改变阈值。历史标记属事后复盘，未验证未来下跌概率；未达线也不代表低风险。`;
        function showFactors(date) {
            const breadth = rps.days?.[date];
            $('rps-date').textContent = date;
            $('rps-selected').textContent = breadth?.selected != null ? number(breadth.selected_pct) + '%（' + breadth.selected + ' 家）' : '未更新 / 缺失';
            $('rps-context').textContent = breadth?.selected != null ?
                `股票池 ${breadth.universe} 家 · 候选 ${breadth.candidates} 家 · 价格缺失 ${breadth.missing_price} 家` + (breadth.missing_price > 0 ? '（已确认下限）' : '') :
                '无当日盘后统计，不使用邻日值填补';
            const riskStatus = breadth?.risk_status || 'missing';
            const riskLabel = riskStatus === 'high' ? `高位提醒 ≥${number(risk.threshold_pct)}%` :
                riskStatus === 'below' ? `未达 ${number(risk.threshold_pct)}% 高位线` :
                riskStatus === 'incomplete' ? '数据不完整 · 暂不判定' : '待更新 · 无法判断';
            $('rps-risk').textContent = riskLabel;
            $('rps-risk').dataset.state = riskStatus;
            $('rps-risk').title = riskStatus === 'high' ? '占比偏高提醒，不是卖出信号；部分缺失时为已确认下限达线。' :
                '只提示这条RPS高位线是否达到，未达线不等于市场低风险。';
            let row = data.days[date], label = '日级';
            const snapshot = data.snapshot;
            const liveDate = String(snapshot?.quote_time_max || snapshot?.generated_at || '').slice(0, 10);
            if (date === liveDate && snapshot?.panic?.score != null && !snapshot.pressure_superseded) {
                row = {...snapshot.panic.features, ...snapshot.damage, score: snapshot.panic.score, pressure_score: snapshot.pressure_score};
                for (const [key] of factorDefs) row['p_' + key] = snapshot.panic.factors?.[key]?.percentile;
                label = '盘中估算';
            }
            $('factor-date').textContent = date + ' · ' + label;
            $('pressure-date').textContent = date + (label === '盘中估算' ? ' · 采样' : '');
            for (const [kind, field] of [['short', 'score'], ['long', 'damage_score'], ['combined', 'pressure_score']]) {
                $('pressure-' + kind).textContent = number(row?.[field]);
            }
            $('factors').replaceChildren(...factorDefs.map(([key, title]) => {
                const node = document.createElement('div'); node.className = 'factor';
                const rank = row?.['p_' + key], value = row?.[key];
                const name = document.createElement('span'); name.textContent = title;
                const valueNode = document.createElement('strong'); valueNode.textContent = value == null ? '缺失' : number(value * 100) + '%';
                const bar = document.createElement('div'); bar.className = 'bar';
                const fill = document.createElement('i'); fill.style.width = (rank ?? 0) + '%'; bar.append(fill);
                const hint = document.createElement('small'); hint.textContent = '历史分位 ' + number(rank) + ' / 100';
                node.append(name, valueNode, bar, hint); return node;
            }));
            $('factor-context').textContent = row ? `有效股票 ${row.count ?? '缺失'} 只 · 上涨比例 ${row.adv_share == null ? '缺失' : number(row.adv_share * 100) + '%'} · 评分 ${number(row.score)} / 100` : '当天无有效特征，未使用邻日数据填补。';
            const longDefs = [['median_dd252', '252日回撤中位数', '5% → 35%'], ['deep_loss_share', '回撤 ≥30% 的股票占比', '10% → 70%'], ['weakness60', '60日平均低于 MA60 比例', '35% → 85%']];
            $('damage-factors').replaceChildren(...longDefs.map(([key, title, scale]) => {
                const node = document.createElement('div'); node.className = 'factor';
                const name = document.createElement('span'); name.textContent = title;
                const value = document.createElement('strong'); value.textContent = row?.[key] == null ? '缺失' : number(row[key] * 100) + '%';
                const bar = document.createElement('div'); bar.className = 'bar';
                const fill = document.createElement('i'); fill.style.width = (row?.['d_' + key] ?? 0) + '%'; bar.append(fill);
                const hint = document.createElement('small'); hint.textContent = `固定刻度 ${scale} · 得分 ${number(row?.['d_' + key])}`;
                node.append(name, value, bar, hint); return node;
            }));
            $('damage-context').textContent = `长期有效股票 ${row?.damage_count ?? '缺失'} 只 · 长期损伤 ${number(row?.damage_score)} · 综合压力 ${number(row?.pressure_score)} · 长期高分可持续很久，不代表马上反转。`;
            const code = data.plotted[Number($('index-select').value)];
            const heat = data.heat?.[code]?.[date];
            $('heat-title').textContent = '指数过热度 · ' + data.labels[code];
            $('heat-date').textContent = date + (heat?.snapshot ? ' · 采样' : '');
            $('heat-score').textContent = number(heat?.heat_score);
            $('heat-extension').textContent = number(heat?.extension);
            $('heat-momentum').textContent = number(heat?.momentum);
            $('heat-factor-date').textContent = data.labels[code] + ' · ' + date;
            $('heat-factors').replaceChildren(...[['bias20', '偏离MA20'], ['bias60', '偏离MA60'], ['return5', '5日涨幅'], ['return20', '20日涨幅']].map(([key, title]) => {
                const node = document.createElement('div'); node.className = 'factor';
                const name = document.createElement('span'); name.textContent = title;
                const value = document.createElement('strong'); value.textContent = heat?.[key] == null ? '缺失' : number(heat[key] * 100) + '%';
                const hint = document.createElement('small'); hint.textContent = `历史分位 ${number(heat?.['p_' + key])} · 热度分项 ${number(heat?.['h_' + key])}`;
                node.append(name, value, hint); return node;
            }));
        }
        graph.addEventListener('review-date', event => showFactors(event.detail));
        function jump(date) {
            const time = stamp(date);
            graph._reviewPinned = true; $('pin-date').textContent = '取消固定'; $('pin-date').classList.add('active');
            setRange([time - 60 * DAY, time + 90 * DAY]).then(() => graph._reviewSelect(date));
            $('workspace').scrollIntoView({behavior: 'smooth'});
        }
        for (const year of data.years) {
            const tr = document.createElement('tr');
            const tdYear = document.createElement('td'), button = document.createElement('button');
            button.textContent = year.year; button.onclick = () => {setRange([stamp(year.year + '-01-01'), stamp(year.year + '-12-31')]); $('workspace').scrollIntoView({behavior: 'smooth'});};
            tdYear.append(button); tr.append(tdYear);
            for (const value of [`${year.valid}/${year.sessions}`, number(year.mean), year.high, year.panic, year.long_high, year.combined_high]) {
                const td = document.createElement('td'); td.textContent = value; tr.append(td);
            }
            const td = document.createElement('td'), peak = document.createElement('button');
            peak.textContent = number(year.peak); const small = document.createElement('small'); small.textContent = year.peak_date || '无有效评分';
            peak.append(small); peak.disabled = !year.peak_date; peak.onclick = () => jump(year.peak_date); td.append(peak); tr.append(td); $('year-rows').append(tr);
            $('event-year').add(new Option(String(year.year), String(year.year)));
        }
        function events() {
            const year = $('event-year').value;
            const kind = $('event-kind').value;
            const source = kind === 'short' ? data.events : data.damage_events.filter(e => e.kind === kind);
            const items = source.filter(e => year === 'all' || (e.start.slice(0, 4) <= year && e.end.slice(0, 4) >= year));
            $('event-list').replaceChildren(...items.map(event => {
                const node = document.createElement('button'); node.className = 'event'; node.onclick = () => jump(event.date);
                const content = document.createElement('div'), title = document.createElement('strong'), detail = document.createElement('small');
                title.textContent = event.start === event.end ? event.start : event.start + ' — ' + event.end;
                detail.textContent = `${event.days} 个触发日 · 峰值 ${event.date}` + (kind === 'short' ? ` · 跌≥5%占比 ${event.drop5 == null ? '缺失' : number(event.drop5 * 100) + '%'}` : ' · 持续高压不等于底部');
                content.append(title, detail); const score = document.createElement('span'); score.className = 'event-score'; score.textContent = number(event.score);
                node.append(content, score); return node;
            }));
            if (!items.length) $('event-list').textContent = '该范围没有记录到极端压力事件。';
        }
        events(); $('event-year').onchange = events; $('event-kind').onchange = events;
        const validation = data.damage_validation;
        for (const item of validation?.cases || []) {
            const tr = document.createElement('tr'), td = document.createElement('td'), button = document.createElement('button');
            button.textContent = item.date; button.onclick = () => jump(item.date); td.append(button); tr.append(td);
            for (const value of [item.label, number(item.short), number(item.long), number(item.combined)]) {
                const cell = document.createElement('td'); cell.textContent = value; tr.append(cell);
            }
            $('case-rows').append(tr);
        }
        const union = validation?.comparison.find(item => item.method === 'union_max');
        $('validation-summary').textContent = union ? `共同有效 ${union.paired_days} 日中，原短期≥90的 ${union.original_short_ge90} 日全部保留；综合≥85新增 ${union.new_days_ge85} 日。新增覆盖是压力天数，不是新增底部数量。` : '长期历史尚未补算，请运行 damage-backfill。';
        const coverage = data.score_coverage;
        $('score-coverage').textContent = `恐慌评分有效 ${coverage.valid} / ${coverage.total} 个交易日。`
            + ` 长期损伤有效 ${Object.values(data.days).filter(row => row.damage_score != null).length} 日。`
            + (coverage.missing.length ? ` 短期缺失 ${coverage.missing.length} 日（含初始预热），最近缺失：${coverage.missing.slice(-5).join('、')}。历史/覆盖不足时不计算评分。` : '所选历史区间短期评分完整。');
        for (const item of data.coverage) {
            const node = document.createElement('div'), title = document.createElement('span'), count = document.createElement('strong'), dates = document.createElement('small');
            title.textContent = item.label + ' · QVIX'; count.textContent = `${item.count} / ${item.total} 日`;
            dates.textContent = item.first ? `${item.first} — ${item.last}` : '无有效历史'; node.append(title, count, dates); $('coverage').append(node);
        }
        syncFields(); graph._reviewSelect(dates.at(-1));
        if (saved?.range?.length === 2 && saved.range.every(Number.isFinite)
            && saved.range[1] >= bounds[0] && saved.range[0] <= bounds[1]) {
            if (data.plotted[Number(saved.index)]) {
                $('index-select').value = saved.index;
                await $('index-select').onchange({target: $('index-select')});
            }
            await setRange(saved.range);
        } else {
            const initial = stamp(data.default_start);
            await setRange([initial < bounds[1] ? initial : bounds[0], bounds[1]], data.default_start === '2020-01-01' && initial < bounds[1] ? '2020' : undefined);
        }
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
})();
