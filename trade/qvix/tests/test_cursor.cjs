const {test} = require('node:test');
const assert = require('node:assert/strict');
const {nearestDate, indexTraces, readDay, cursorPoints} = require('../report_cursor.js');

test('display-only gap guides never create cursor values or dots', () => {
    const indexed = indexTraces([
        {name: 'RPS', yaxis: 'y4', meta: {rps: true, cursor_key: 'selected_pct'},
            x: ['2026-09-14', '2026-09-15', '2026-09-16'], y: [1, null, 2]},
        {name: '缺数连接', yaxis: 'y4', meta: {rps: true, cursor_skip: true, display_bridge: 'missing'},
            x: ['2026-09-14', '2026-09-16', null], y: [1, 2, null]}
    ]);
    assert.equal(indexed.length, 1);
    assert.deepEqual(cursorPoints(indexed, '2026-09-15'), []);
    assert.match(readDay(indexed, '2026-09-15')[3][0].value, /缺失/);
});

test('one nearest trading date, including gaps and range boundaries', () => {
    const dates = ['2026-09-24', '2026-09-28', '2026-09-29'];
    assert.equal(nearestDate(dates, Date.parse('2026-09-27')), '2026-09-28');
    assert.equal(nearestDate(dates, Date.parse('2026-09-01')), dates[0]);
    assert.equal(nearestDate(dates, Date.parse('2026-10-01')), dates[2]);
});

test('hover snaps by rendered distance rather than the calendar distance across a holiday', () => {
    const {nearestPixelDate} = require('../report_cursor.js');
    const dates = ['2026-09-29', '2026-09-30', '2026-10-08'];
    const positions = [0, 100, 200], position = date => positions[dates.indexOf(date)];
    assert.equal(nearestPixelDate(dates, 199, position), '2026-10-08');
    assert.equal(nearestPixelDate(dates, 151, position), '2026-10-08');
    assert.equal(nearestPixelDate(dates, 149, position), '2026-09-30');
});

test('all panels read exactly the same date, never a neighbouring observation', () => {
    const indexed = indexTraces([
        {name: '指数', type: 'candlestick', x: ['2026-09-24', '2026-09-28'], open: [1, 2], high: [2, 3], low: [0, 1], close: [1, 2]},
        {name: '恐慌', yaxis: 'y2', x: ['2026-09-24'], y: [80]},
        {name: '一年', yaxis: 'y3', x: ['2026-09-28'], y: [95]},
        {name: 'QVIX', yaxis: 'y4', x: ['2026-09-28'], y: [25], customdata: [-2]}
    ]);
    const rows = readDay(indexed, '2026-09-28');
    assert.match(rows[0][0].value, /收2.00/);
    assert.equal(rows[1][0].value, '缺失');
    assert.equal(rows[2][0].value, '95.00');
    assert.equal(rows[3][0].value, '25.00 · 5日 -2.00百分点');
});

test('index switch and hidden legend traces are respected; snapshot only on its date', () => {
    const indexed = indexTraces([
        {name: '隐藏指数', visible: false, x: ['2026-09-28'], y: [1]},
        {name: '隐藏曲线', visible: 'legendonly', x: ['2026-09-28'], y: [1]},
        {name: '日级', yaxis: 'y2', meta: {cursor_key: 'panic'}, x: ['2026-09-24'], y: [70]},
        {name: '采样（非定稿）', yaxis: 'y2', meta: {cursor_key: 'panic', snapshot: true}, x: ['2026-09-28'], y: [88]}
    ]);
    assert.equal(readDay(indexed, '2026-09-24')[1][0].name, '日级');
    const rows = readDay(indexed, '2026-09-28');
    assert.deepEqual(rows[0], []);
    assert.deepEqual(rows[1], [{name: '采样（非定稿）', value: '88.00'}]);
});

test('pointer in any panel draws the same full-height line, including after zoom/resize', () => {
    const vm = require('node:vm'), fs = require('node:fs');
    const element = () => ({style: {}, children: [], appendChild(x) {this.children.push(x);},
        replaceChildren(...xs) {this.children = xs;}});
    const time = element(), values = element(), host = element(), handlers = {};
    host.parentElement = {querySelector: () => ({querySelector: s => s === 'time' ? time : values})};
    const axis = {_offset: 60, _length: 500, p2d: () => '2026-09-28', d2p: () => 100};
    const yaxis = {_offset: 300, _length: 200, d2p: value => 200 - 2 * value};
    const wrapper = element();
    const graph = {parentElement: wrapper, closest: selector => selector === '.plot-host' ? host : null,
        data: [{name: '指数', x: ['2026-09-28'], y: [3000]},
            {name: '短期', xaxis: 'x2', yaxis: 'y2', x: ['2026-09-28'], y: [75]}],
        _fullLayout: {xaxis: axis, xaxis2: axis, yaxis2: yaxis, _size: {l: 60, t: 80, w: 500, h: 800}},
        getBoundingClientRect: () => ({left: 10, top: 20}),
        on: (e, f) => {handlers[e] = f;}, addEventListener: (e, f) => {handlers[e] = f;}};
    vm.runInNewContext(fs.readFileSync(require.resolve('../report_cursor.js'), 'utf8'), {
        document: {getElementById: () => graph, createElement: element}
    });
    for (const y of [100, 300, 500, 800]) {
        handlers.pointermove({clientX: 170, clientY: y + 20});
        const line = host.children[0];
        assert.equal(line.style.left, '160px');
        assert.equal(line.style.height, '800px');
        assert.match(time.textContent, /2026-09-28/);
        assert.equal(values.children.length, 4);
        assert.equal(line.children[1].children.length, 1);
        assert.equal(line.children[1].children[0].style.top, '270px');
        assert.equal(line.children[1].children[0].style.left, '0px');
    }
    axis.d2p = () => 220;
    graph._fullLayout._size.h = 700;
    yaxis._offset = 250;
    handlers.plotly_afterplot();
    handlers.pointermove({clientX: 170, clientY: 300});
    assert.equal(host.children[0].style.left, '280px');
    assert.equal(host.children[0].style.height, '700px');
    assert.equal(host.children[0].children[1].children[0].style.top, '220px');
    graph._reviewPinned = true;
    graph._reviewSelect('2026-09-24');
    handlers.pointermove({clientX: 170, clientY: 300});
    assert.equal(host.children[0].children[0].textContent, '2026-09-24');
    assert.match(time.textContent, /2026-09-24/);
    assert.equal(host.children[0].children[1].children.length, 0); // Never borrow a neighbouring day's point.
    handlers.pointerleave();
    assert.equal(host.children[0].style.display, 'block');
    graph._reviewPinned = false;
    handlers.pointerleave();
    assert.equal(host.children[0].style.display, 'none');
});

test('cursor dots use only visible exact-date subplot values, with snapshot precedence', () => {
    const base = {xaxis: 'x2', yaxis: 'y2', x: ['2026-09-28'], meta: {cursor_key: 'panic'}};
    const indexed = indexTraces([
        {...base, name: '历史', y: [70]},
        {...base, name: '采样', y: [88], meta: {cursor_key: 'panic', snapshot: true}},
        {...base, name: '隐藏', y: [99], visible: false},
        {...base, name: '标记', y: [99], meta: {cursor_skip: true}},
        {...base, name: 'QVIX', yaxis: 'y3', xaxis: 'x3', y: [null]},
        {name: '主图', x: ['2026-09-28'], y: [100]}
    ]);
    assert.deepEqual(cursorPoints(indexed, '2026-09-28'), [{name: '采样', value: 88, xaxis: 'xaxis2', yaxis: 'yaxis2'}]);
    assert.deepEqual(cursorPoints(indexed, '2026-09-29'), []);
});

test('fifth panel leaves heat scores distinct from QVIX changes', () => {
    const traces = indexTraces([
        {name: '过热度', yaxis: 'y3', xaxis: 'x3', x: ['2026-06-22'], y: [91]},
        {name: 'QVIX分位', yaxis: 'y4', xaxis: 'x4', x: ['2026-06-22'], y: [45]},
        {name: 'QVIX原值', yaxis: 'y5', xaxis: 'x5', x: ['2026-06-22'], y: [20], customdata: [-1]},
        {name: '隐藏指数过热', yaxis: 'y3', xaxis: 'x3', x: ['2026-06-22'], y: [80], visible: false}
    ]);
    const rows = readDay(traces, '2026-06-22');
    assert.equal(rows.length, 5);
    assert.equal(rows[2][0].value, '91.00');
    assert.equal(rows[3][0].value, '45.00');
    assert.equal(rows[4][0].value, '20.00 · 5日 -1.00百分点');
    assert.equal(cursorPoints(traces, '2026-06-22').length, 3);
});

test('zoom and pan preserve interval width at history bounds', () => {
    const {clampRange, shiftRange} = require('../report_controls.js');
    const D = 86400000, bounds = [0, 100 * D];
    assert.deepEqual(clampRange(-10 * D, 20 * D, ...bounds), [0, 30 * D]);
    assert.deepEqual(shiftRange([80 * D, 100 * D], 1, .5, bounds), [80 * D, 100 * D]);
    assert.deepEqual(shiftRange([0, 100 * D], .5, 0, bounds), [25 * D, 75 * D]);
});

test('compressed session zoom and pan skip a long holiday and never return a closed-only range', () => {
    const {snapSessionRange, shiftSessionRange} = require('../report_controls.js');
    const dates = ['2026-09-24', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-08', '2026-10-09'];
    const times = dates.map(Date.parse);
    assert.deepEqual(snapSessionRange([Date.parse('2026-10-01'), Date.parse('2026-10-07')], dates), [times[3], times[4]]);
    assert.deepEqual(shiftSessionRange([times[1], times[3]], 1, .5, dates), [times[2], times[4]]);
    assert.deepEqual(shiftSessionRange([times[3], times[5]], 1, 1, dates), [times[3], times[5]]);
    assert.deepEqual(shiftSessionRange([times[0], times[4]], .5, 0, dates), [times[1], times[3]]);
    assert.deepEqual(snapSessionRange([times[5], times[5]], dates), [times[4], times[5]]);
});

test('date ticks only label real sessions, including both sides of a long closure', () => {
    const {sessionTicks} = require('../report_controls.js');
    const dates = ['2026-09-28', '2026-09-29', '2026-09-30', '2026-10-08'];
    assert.deepEqual(sessionTicks([Date.parse(dates[0]), Date.parse(dates[3])], dates), dates);
    assert.deepEqual(sessionTicks([Date.parse(dates[0]), Date.parse(dates[3])], dates, 2), [dates[0], dates[3]]);
    assert.deepEqual(sessionTicks([Date.parse('2026-10-01'), Date.parse('2026-10-07')], dates), []);
});

test('RPS sixth-panel layout uses exact dates, integer counts and no stale red point', () => {
    const indexed = indexTraces([
        {name: 'RPS', yaxis: 'y4', xaxis: 'x4', meta: {rps: true}, x: ['2026-09-24', '2026-09-30'], y: [36, null]},
        {name: 'QVIX', yaxis: 'y6', xaxis: 'x6', x: ['2026-09-30'], y: [20], customdata: [1]}
    ]);
    assert.equal(readDay(indexed, '2026-09-24')[3][0].value, '36 家');
    const rows = readDay(indexed, '2026-09-30');
    assert.equal(rows.length, 6);
    assert.equal(rows[3][0].value, '未更新 / 缺失（仅盘后）');
    assert.equal(rows[5][0].value, '20.00 · 5日 +1.00百分点');
    assert.equal(cursorPoints(indexed, '2026-09-30').length, 1);
});

test('RPS percent units remain separate from count units and QVIX changes', () => {
    const indexed = indexTraces([{name: '符合条件占比', yaxis: 'y4', xaxis: 'x4', meta: {rps: true, rps_unit: '%'}, x: ['2026-09-24'], y: [1.2345]}]);
    assert.equal(readDay(indexed, '2026-09-24')[3][0].value, '1.23%');
    assert.equal(readDay(indexed, '2026-09-30')[3][0].value, '未更新 / 缺失（仅盘后）');
});

test('pressure view never overlays the redundant union and keeps snapshots with their series', () => {
    const {pressureVisible} = require('../report_controls.js');
    const kinds = ['short', 'long', 'combined', 'short', 'long', 'combined'];
    assert.deepEqual(kinds.map(k => pressureVisible(k, 'compare')), [true, true, false, true, true, false]);
    for (const mode of ['short', 'long', 'combined']) {
        assert.deepEqual(kinds.filter(k => pressureVisible(k, mode)), [mode, mode]);
    }
});
