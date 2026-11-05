/* Embedded into the standalone HTML; one cursor and one exact-date readout. */
(() => {
    const day = value => String(value).slice(0, 10);
    const finite = value => typeof value === 'number' && Number.isFinite(value);
    const format = value => finite(value) ? value.toFixed(2) : '缺失';
    const visible = trace => trace.visible !== false && trace.visible !== 'legendonly';

    function nearestDate(dates, target) {
        let left = 0, right = dates.length;
        while (left < right) {
            const mid = (left + right) >>> 1;
            if (Date.parse(dates[mid]) < target) left = mid + 1;
            else right = mid;
        }
        if (!left) return dates[0];
        if (left === dates.length) return dates[left - 1];
        return target - Date.parse(dates[left - 1]) <= Date.parse(dates[left]) - target
            ? dates[left - 1] : dates[left];
    }

    function indexTraces(traces) {
        return traces.filter(t => visible(t) && !t.meta?.cursor_skip).map(trace => ({
            trace, positions: new Map(Array.from(trace.x || [], (x, i) => [day(x), i]))
        }));
    }

    function nearestPixelDate(dates, pixel, position) {
        // Calendar distance is wrong at a collapsed holiday boundary. Choose
        // the nearest visible session using the axis's rendered coordinates.
        let left = 0, right = dates.length;
        while (left < right) {
            const mid = (left + right) >>> 1;
            if (position(dates[mid]) < pixel) left = mid + 1;
            else right = mid;
        }
        if (!left) return dates[0];
        if (left === dates.length) return dates[left - 1];
        return pixel - position(dates[left - 1]) <= position(dates[left]) - pixel ? dates[left - 1] : dates[left];
    }

    function readDay(indexed, date) {
        const count = Math.max(4, ...indexed.map(({trace}) => Number((trace.yaxis || 'y').slice(1) || 1)));
        const panels = Array.from({length: count}, () => new Map());
        for (const {trace, positions} of indexed) {
            const i = positions.get(date);
            if (trace.meta?.snapshot && i === undefined) continue;
            const panel = Number((trace.yaxis || 'y').slice(1) || 1) - 1;
            let value;
            if (trace.type === 'candlestick') {
                value = ['open', 'high', 'low', 'close'].map((key, j) =>
                    ['开', '高', '低', '收'][j] + format(trace[key]?.[i])).join(' / ');
            } else {
                value = format(trace.y?.[i]);
                if (trace.meta?.rps) value = finite(trace.y?.[i]) ?
                    (trace.meta.rps_unit === '%' ? format(trace.y[i]) + '%' : trace.y[i] + ' 家') : '未更新 / 缺失（仅盘后）';
                if (trace.customdata !== undefined) {
                    const change = trace.customdata?.[i];
                    value += ' · 5日 ' + (finite(change)
                        ? (change >= 0 ? '+' : '') + change.toFixed(2) + '百分点' : '缺失');
                }
            }
            panels[panel].set(trace.meta?.cursor_key || trace.name, {name: trace.name, value});
        }
        return panels.map(panel => Array.from(panel.values()));
    }

    function cursorPoints(indexed, date) {
        const points = new Map();
        for (const {trace, positions} of indexed) {
            if (!trace.yaxis || trace.yaxis === 'y') continue;
            const i = positions.get(date);
            if (trace.meta?.snapshot && i === undefined) continue;
            const key = trace.yaxis + ':' + (trace.meta?.cursor_key || trace.name);
            if (finite(trace.y?.[i])) points.set(key, {name: trace.name, value: trace.y[i],
                xaxis: (trace.xaxis || 'x').replace('x', 'xaxis'), yaxis: trace.yaxis.replace('y', 'yaxis')});
            else points.delete(key);
        }
        return [...points.values()];
    }

    // Pure functions are also exercised by Node's built-in test runner.
    if (typeof module !== 'undefined') module.exports = {nearestDate, nearestPixelDate, indexTraces, readDay, cursorPoints};
    if (typeof document === 'undefined') return;
    const graph = document.getElementById('{plot_id}');
    // Plotly's HTML exporter adds a wrapper div around the graph. The overlay
    // belongs to our positioned host, not to that exporter wrapper.
    const host = graph.closest ? graph.closest('.plot-host') : graph.parentElement;
    const readout = host.parentElement.querySelector('.cursor-readout');
    const cursor = document.createElement('div');
    cursor.className = 'linked-cursor';
    const dateTag = document.createElement('span');
    dateTag.className = 'cursor-date-tag';
    cursor.appendChild(dateTag);
    const dots = document.createElement('div');
    dots.className = 'cursor-dots';
    cursor.appendChild(dots);
    host.appendChild(cursor);
    let indexed, dates, lastDate, dotsDate;
    function rebuild() {
        const shown = cursor.style.display === 'block' || graph._reviewPinned;
        indexed = indexTraces(graph.data);
        dates = [...new Set(indexed.flatMap(t => [...t.positions.keys()]))].sort();
        lastDate = null; dotsDate = null;
        cursor.style.display = 'none';
        if (shown && graph._reviewDate) {selectDate(graph._reviewDate); paintCursor(graph._reviewDate);}
    }
    rebuild();
    graph.on('plotly_afterplot', rebuild); // Index switch, legend, zoom and resize.
    function selectDate(date) {
        lastDate = date;
        graph._reviewDate = date;
        readout.querySelector('time').textContent = date + ' · 同日对照';
        const cells = readDay(indexed, date).map((rows, i) => {
            const cell = document.createElement('div'), title = document.createElement('b');
            title.textContent = (graph.layout?.meta?.cursor_titles || ['指数', '市场压力 / 分', 'QVIX分位 / %', '波动率 / 年化%'])[i];
            cell.appendChild(title);
            for (const row of rows) {
                const line = document.createElement('div');
                line.textContent = row.name + '：' + row.value;
                cell.appendChild(line);
            }
            return cell;
        });
        readout.querySelector('.cursor-values').replaceChildren(...cells);
        if (graph._reviewPinned || cursor.style.display === 'block') paintCursor(date);
        if (graph.dispatchEvent) graph.dispatchEvent(new CustomEvent('review-date', {detail: date}));
    }
    graph._reviewSelect = selectDate;
    function paintCursor(date, pointerY) {
        const {xaxis, _size: size} = graph._fullLayout;
        const pixel = xaxis.d2p(date) + xaxis._offset;
        if (!Number.isFinite(pixel) || pixel < size.l || pixel > size.l + size.w) {
            cursor.style.display = 'none'; return;
        }
        Object.assign(cursor.style, {display: 'block', left: pixel + 'px', top: size.t + 'px', height: size.h + 'px'});
        dateTag.textContent = date;
        if (pointerY !== undefined) dateTag.style.top = Math.max(0, pointerY - size.t - 32) + 'px';
        dateTag.style.left = pixel > size.l + size.w - 120 ? '-108px' : '7px';
        if (dotsDate === date) return;
        dotsDate = date;
        const markers = [];
        for (const point of cursorPoints(indexed, date)) {
            const xa = graph._fullLayout[point.xaxis], ya = graph._fullLayout[point.yaxis];
            if (!xa || !ya) continue;
            const x = xa.d2p(date) + xa._offset, y = ya.d2p(point.value) + ya._offset;
            if (!Number.isFinite(x) || !Number.isFinite(y) || x < xa._offset || x > xa._offset + xa._length
                || y < ya._offset || y > ya._offset + ya._length) continue;
            const marker = document.createElement('span'); marker.className = 'cursor-dot';
            marker.title = point.name + '：' + format(point.value);
            Object.assign(marker.style, {left: (x - pixel) + 'px', top: (y - size.t) + 'px'});
            markers.push(marker);
        }
        dots.replaceChildren(...markers);
    }
    graph.addEventListener('pointermove', event => {
        const box = graph.getBoundingClientRect();
        // Plotly 2.x exposes axis pixel conversion here. Read it on each event so
        // zoom/resize coordinates stay correct; no Plotly relayout on mouse moves.
        const {xaxis, _size: size} = graph._fullLayout;
        const x = event.clientX - box.left, y = event.clientY - box.top;
        if (x < size.l || x > size.l + size.w || y < size.t || y > size.t + size.h || !dates.length) {
            if (!graph._reviewPinned) cursor.style.display = 'none';
            return;
        }
        // A pinned readout and its vertical line must point to the same date.
        const date = graph._reviewPinned && graph._reviewDate ? graph._reviewDate :
            nearestPixelDate(dates, x - xaxis._offset, day => xaxis.d2p(day));
        paintCursor(date, y);
        if (graph._reviewPinned) return;
        if (date === lastDate) return;
        selectDate(date);
    });
    graph.addEventListener('pointerleave', () => {if (!graph._reviewPinned) cursor.style.display = 'none';});
})();
