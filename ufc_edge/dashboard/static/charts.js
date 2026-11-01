/* Small SVG chart kit for the dashboard. No dependencies.
   - Thin 2px lines, recessive grid, one y-axis only.
   - Colours are CSS variables, so light/dark themes switch without redrawing.
   - Every chart has a hover layer: a crosshair with a tooltip for lines, per-mark tooltips for bars and dots. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };

  function niceTicks(min, max, count = 5) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }

  function frame(container, { height = 260, left = 56, right = 70, top = 14, bottom = 30 } = {}) {
    container.innerHTML = "";
    container.classList.add("chart");
    const width = Math.max(container.clientWidth || 640, 300);
    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" }, container);
    const tip = document.createElement("div");
    tip.className = "tooltip";
    tip.hidden = true;
    container.appendChild(tip);
    return { svg, tip, width, height, left, right, top, bottom, iw: width - left - right, ih: height - top - bottom };
  }

  /* lineChart(container, {series:[{name, color, points:[{x:Date|number, y:number}]}], yFormat, xFormat, ariaLabel, step}) */
  function lineChart(container, opts) {
    const f = frame(container, opts);
    const { svg, tip } = f;
    svg.setAttribute("aria-label", opts.ariaLabel || "Line chart");
    const all = opts.series.flatMap((s) => s.points);
    if (!all.length) return;
    const xs = all.map((p) => +p.x);
    const ys = all.map((p) => p.y).concat(opts.includeZero ? [0] : []);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const ticks = niceTicks(Math.min(...ys), Math.max(...ys), 5);
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];
    const X = (v) => f.left + (xMax === xMin ? f.iw / 2 : ((+v - xMin) / (xMax - xMin)) * f.iw);
    const Y = (v) => f.top + f.ih - ((v - yMin) / (yMax - yMin || 1)) * f.ih;
    const yFmt = opts.yFormat || ((v) => v);
    const spanDays = (xMax - xMin) / 864e5;
    const xFmt = opts.xFormat || ((v) => new Date(v).toLocaleDateString("en-GB", { month: "short", year: "numeric" }));

    const grid = el("g", { class: "grid" }, svg);
    const axis = el("g", { class: "axis" }, svg);
    ticks.forEach((t) => {
      el("line", { x1: f.left, x2: f.left + f.iw, y1: Y(t), y2: Y(t) }, grid);
      const tx = el("text", { x: f.left - 8, y: Y(t) + 4, "text-anchor": "end" }, axis);
      tx.textContent = yFmt(t);
    });
    if (opts.refLine != null) el("line", { class: "zero", x1: f.left, x2: f.left + f.iw, y1: Y(opts.refLine), y2: Y(opts.refLine), "stroke-dasharray": "4 3" }, svg);
    const nX = Math.min(spanDays < 2 ? 3 : 6, Math.max(1, Math.floor(f.iw / (spanDays < 2 ? 170 : 110))));
    for (let i = 0; i <= nX; i++) {
      const v = xMin + ((xMax - xMin) * i) / nX;
      const tx = el("text", { x: X(v), y: f.height - 8, "text-anchor": i === 0 ? "start" : i === nX ? "end" : "middle" }, axis);
      tx.textContent = xFmt(v);
    }

    const g = el("g", { class: "series" }, svg);
    const labels = [];
    opts.series.forEach((s) => {
      const pts = s.points;
      if (!pts.length) return;
      let d = "";
      pts.forEach((p, i) => {
        if (i === 0) d += `M${X(p.x)},${Y(p.y)}`;
        else if (opts.step) d += `H${X(p.x)}V${Y(p.y)}`;
        else d += `L${X(p.x)},${Y(p.y)}`;
      });
      el("path", { d, style: `stroke:${s.color}` }, g);
      const last = pts[pts.length - 1];
      el("circle", { cx: X(last.x), cy: Y(last.y), r: 4, style: `fill:${s.color};stroke:var(--panel);stroke-width:2` }, g);
      if (opts.directLabels !== false) labels.push({ x: X(last.x) + 9, y: Y(last.y) + 4, text: s.label ? s.label(last) : yFmt(last.y) });
    });
    // Direct labels: push apart any that would overlap (at least 14px between baselines).
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 14) labels[i].y = labels[i - 1].y + 14;
    labels.forEach((l) => { el("text", { x: l.x, y: l.y, class: "label", style: "fill:var(--ink-2)" }, g).textContent = l.text; });

    // hover layer: crosshair and tooltip
    const cross = el("line", { class: "crosshair", y1: f.top, y2: f.top + f.ih, visibility: "hidden" }, svg);
    const dots = opts.series.map((s) => el("circle", { r: 4.5, visibility: "hidden", style: `fill:${s.color};stroke:var(--panel);stroke-width:2` }, svg));
    const hit = el("rect", { x: f.left, y: f.top, width: f.iw, height: f.ih, fill: "transparent" }, svg);
    const bisect = (pts, x) => {
      let best = 0, bd = Infinity;
      for (let i = 0; i < pts.length; i++) { const dd = Math.abs(+pts[i].x - x); if (dd < bd) { bd = dd; best = i; } }
      return pts[best];
    };
    hit.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      const sx = ((ev.clientX - r.left) / r.width) * f.width;
      const xv = xMin + ((sx - f.left) / f.iw) * (xMax - xMin);
      const nearest = opts.series.map((s) => (s.points.length ? bisect(s.points, xv) : null));
      const ref = nearest.find(Boolean);
      if (!ref) return;
      const cx = X(ref.x);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      nearest.forEach((p, i) => {
        if (!p) return;
        dots[i].setAttribute("cx", X(p.x)); dots[i].setAttribute("cy", Y(p.y)); dots[i].setAttribute("visibility", "visible");
      });
      tip.innerHTML = `<div>${opts.tipTitle ? opts.tipTitle(ref) : xFmt(ref.x)}</div>` + nearest.map((p, i) => p ? `<div><i style="background:${opts.series[i].color}"></i>${opts.series[i].name}: <b>${yFmt(p.y)}</b></div>` : "").join("");
      tip.hidden = false;
      tip.style.left = `${(cx / f.width) * 100}%`;
      tip.style.top = `${(Math.min(...nearest.filter(Boolean).map((p) => Y(p.y))) / f.height) * 100}%`;
    });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); });
  }

  /* barChart(container, {bars:[{label, value, sub}], yFormat, ariaLabel}): diverging around zero (gain/loss) */
  function barChart(container, opts) {
    const f = frame(container, { ...opts, right: 16, bottom: 34 });
    const { svg, tip } = f;
    svg.setAttribute("aria-label", opts.ariaLabel || "Bar chart");
    const vals = opts.bars.map((b) => b.value).concat([0]);
    const ticks = niceTicks(Math.min(...vals), Math.max(...vals), 4);
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];
    const Y = (v) => f.top + f.ih - ((v - yMin) / (yMax - yMin || 1)) * f.ih;
    const yFmt = opts.yFormat || ((v) => v);
    const grid = el("g", { class: "grid" }, svg);
    const axis = el("g", { class: "axis" }, svg);
    ticks.forEach((t) => {
      el("line", { x1: f.left, x2: f.left + f.iw, y1: Y(t), y2: Y(t) }, grid);
      const tx = el("text", { x: f.left - 8, y: Y(t) + 4, "text-anchor": "end" }, axis);
      tx.textContent = yFmt(t);
    });
    const n = opts.bars.length;
    const slot = f.iw / n;
    const bw = Math.max(4, Math.min(34, slot - 4));
    opts.bars.forEach((b, i) => {
      const x = f.left + slot * i + (slot - bw) / 2;
      const y0 = Y(0), y1 = Y(b.value);
      const top = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
      const color = b.value >= 0 ? "var(--gain)" : "var(--loss)";
      // rounded data end, square at the baseline
      const r = Math.min(4, h, bw / 2);
      const d = b.value >= 0
        ? `M${x},${y0}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${y0}Z`
        : `M${x},${y0}V${top + h - r}Q${x},${top + h} ${x + r},${top + h}H${x + bw - r}Q${x + bw},${top + h} ${x + bw},${top + h - r}V${y0}Z`;
      const bar = el("path", { d, style: `fill:${color}` }, svg);
      if (n <= 16 || i % Math.ceil(n / 12) === 0) {
        const tx = el("text", { x: x + bw / 2, y: f.height - 10, "text-anchor": "middle" }, axis);
        tx.textContent = b.label;
      }
      const hit = el("rect", { x: f.left + slot * i, y: f.top, width: slot, height: f.ih, fill: "transparent" }, svg);
      hit.addEventListener("mousemove", () => {
        tip.innerHTML = `<div><b>${b.label}</b></div><div>${opts.valueName || "Value"}: <b>${yFmt(b.value)}</b></div>${b.sub ? `<div>${b.sub}</div>` : ""}`;
        tip.hidden = false;
        tip.style.left = `${((x + bw / 2) / f.width) * 100}%`;
        tip.style.top = `${(top / f.height) * 100}%`;
        bar.style.opacity = 0.8;
      });
      hit.addEventListener("mouseleave", () => { tip.hidden = true; bar.style.opacity = 1; });
    });
    el("line", { class: "zero", x1: f.left, x2: f.left + f.iw, y1: Y(0), y2: Y(0) }, svg);
  }

  /* calibrationChart(container, {points:[{predicted, actual, n}]}) */
  function calibrationChart(container, opts) {
    const f = frame(container, { height: 280, left: 50, right: 20, bottom: 36, ...opts });
    const { svg, tip } = f;
    svg.setAttribute("aria-label", "Calibration: predicted against actual win rate");
    const lo = 0.5, hi = 1.0;
    const X = (v) => f.left + ((v - lo) / (hi - lo)) * f.iw;
    const Y = (v) => f.top + f.ih - ((v - lo) / (hi - lo)) * f.ih;
    const grid = el("g", { class: "grid" }, svg);
    const axis = el("g", { class: "axis" }, svg);
    [0.5, 0.6, 0.7, 0.8, 0.9, 1].forEach((t) => {
      el("line", { x1: f.left, x2: f.left + f.iw, y1: Y(t), y2: Y(t) }, grid);
      el("line", { y1: f.top, y2: f.top + f.ih, x1: X(t), x2: X(t) }, grid);
      el("text", { x: f.left - 8, y: Y(t) + 4, "text-anchor": "end" }, axis).textContent = `${Math.round(t * 100)}%`;
      el("text", { x: X(t), y: f.height - 16, "text-anchor": "middle" }, axis).textContent = `${Math.round(t * 100)}%`;
    });
    el("text", { x: f.left + f.iw / 2, y: f.height - 1, "text-anchor": "middle" }, axis).textContent = "Model's probability for its pick";
    el("line", { x1: X(lo), y1: Y(lo), x2: X(hi), y2: Y(hi), class: "zero", "stroke-dasharray": "4 3" }, svg);
    const maxN = Math.max(...opts.points.map((p) => p.n));
    opts.points.forEach((p) => {
      const r = 4 + 6 * Math.sqrt(p.n / maxN);
      const c = el("circle", { cx: X(p.predicted), cy: Y(p.actual), r, style: "fill:var(--active);stroke:var(--panel);stroke-width:2" }, svg);
      c.addEventListener("mousemove", () => {
        tip.innerHTML = `<div><b>Predicted ${(p.predicted * 100).toFixed(1)}%</b></div><div>Actually won ${(p.actual * 100).toFixed(1)}%</div><div>${p.n.toLocaleString()} fights</div>`;
        tip.hidden = false;
        tip.style.left = `${(X(p.predicted) / f.width) * 100}%`;
        tip.style.top = `${((Y(p.actual) - r) / f.height) * 100}%`;
      });
      c.addEventListener("mouseleave", () => { tip.hidden = true; });
    });
  }

  window.Charts = { lineChart, barChart, calibrationChart };
})();
