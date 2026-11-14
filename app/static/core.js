/* Shared, DOM-free helpers.  Loaded by index.html and by the Node tests. */
(function (root) {
  const OD = {};

  const METHODS = {
    next_board_same_stop: "下次刷卡同站",
    next_board_walk: "下次刷卡邻近站",
    return_anchor: "返程锚点",
    od_prior: "OD 先验兜底",
    hop_prior: "跳数先验",
    terminal_fallback: "终点站兜底",
    prior_fallback: "无证据兜底",
    terminal_unresolved: "终点无法推算",
    chain_conflict: "链冲突",
  };
  const PERIOD_LABELS = {
    "早间": "早间", "早高峰": "早高峰", "平峰": "平峰", "晚高峰": "晚高峰",
    "夜间": "夜间", "未分时段": "未分时段",
  };
  const CONF_LABELS = { high: "高", medium: "中", low: "低" };

  OD.methodLabel = (m) => METHODS[m] || m || "-";
  OD.confLabel = (c) => CONF_LABELS[c] || c || "-";

  OD.fmtNum = (v, digits) => {
    if (v === null || v === undefined || v === "") return "-";
    const n = Number(v);
    if (!Number.isFinite(n)) return "-";
    const d = digits === undefined ? (Math.abs(n) >= 100 ? 0 : 2) : digits;
    // Manual grouping: toLocaleString inserts NBSP separators in some locales
    // which browsers then treat as line-break opportunities.
    const abs = Math.abs(n).toFixed(d);
    const [intPart, decPart] = abs.split(".");
    const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (n < 0 ? "-" : "") + grouped + (decPart ? "." + decPart : "");
  };

  OD.fmtPct = (v, digits) => {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return "-";
    return (Number(v) * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  };

  OD.fmtDuration = (sec) => {
    if (sec === null || sec === undefined || !Number.isFinite(Number(sec))) return "-";
    const s = Math.abs(Math.round(Number(sec)));
    if (s < 60) return s + " 秒";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " 分 " + (s % 60) + " 秒";
    const h = Math.floor(m / 60);
    return h + " 时 " + (m % 60) + " 分";
  };

  OD.fmtTime = (iso) => {
    if (!iso) return "-";
    // ISO strings from the API look like "2026-11-09 08:15:00"
    const s = String(iso).replace("T", " ");
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})(:\d{2})?/);
    if (m) return m[2] + "-" + m[3] + " " + m[4];
    return s;
  };

  OD.fmtClock = (iso) => {
    if (!iso) return "-";
    const m = String(iso).match(/(\d{2}:\d{2})(:\d{2})?/);
    return m ? m[1] : String(iso);
  };

  // ---- colour ramp for passenger volumes (blue -> teal -> amber -> red)
  const RAMP = [
    [0.0, [58, 96, 150]], [0.35, [55, 211, 164]], [0.65, [244, 183, 64]],
    [1.0, [239, 100, 97]],
  ];
  OD.color = (t, alpha) => {
    const x = Math.max(0, Math.min(1, Number(t) || 0));
    let lo = RAMP[0], hi = RAMP[RAMP.length - 1];
    for (let i = 0; i < RAMP.length - 1; i++) {
      if (x >= RAMP[i][0] && x <= RAMP[i + 1][0]) { lo = RAMP[i]; hi = RAMP[i + 1]; break; }
    }
    const f = hi[0] === lo[0] ? 0 : (x - lo[0]) / (hi[0] - lo[0]);
    const c = lo[1].map((v, i) => Math.round(v + (hi[1][i] - v) * f));
    return `rgba(${c[0]},${c[1]},${c[2]},${alpha === undefined ? 0.82 : alpha})`;
  };
  OD.colorHex = (t) => {
    const s = OD.color(t);
    const m = s.match(/rgba\((\d+),(\d+),(\d+)/);
    if (!m) return "#4f9cf9";
    return "#" + [1, 2, 3].map((i) => Number(m[i]).toString(16).padStart(2, "0")).join("");
  };
  OD.rampLegend = () => RAMP.map(([t, c]) => ({ t, color: `rgb(${c.join(",")})` }));

  // ---- generic table helpers
  OD.sortRows = (rows, key, desc) => {
    const blank = (v) => v === null || v === undefined || v === "";
    const out = rows.slice();
    out.sort((a, b) => {
      const av = a[key], bv = b[key];
      // missing values always sink to the bottom, whatever the direction
      if (blank(av) && blank(bv)) return 0;
      if (blank(av)) return 1;
      if (blank(bv)) return -1;
      const an = Number(av), bn = Number(bv);
      let cmp;
      if (Number.isFinite(an) && Number.isFinite(bn)) cmp = an - bn;
      else cmp = String(av).localeCompare(String(bv), "zh-CN");
      return desc ? -cmp : cmp;
    });
    return out;
  };

  OD.uniqueValues = (rows, key) => {
    const seen = new Set();
    for (const r of rows) {
      const v = r[key];
      if (v !== null && v !== undefined && v !== "") seen.add(v);
    }
    return Array.from(seen).sort((a, b) =>
      String(a).localeCompare(String(b), "zh-CN"));
  };

  OD.sumBy = (rows, key, valueKey) => {
    const out = {};
    for (const r of rows) {
      const k = r[key];
      out[k] = (out[k] || 0) + Number(r[valueKey] || 0);
    }
    return out;
  };

  // ---- period filter guard: keep the selection valid after a re-run
  OD.reconcilePeriod = (current, periods) => {
    if (!periods || !periods.length) return current;
    if (current === "all" || current === "全部") return "all";
    return periods.indexOf(current) >= 0 ? current : "all";
  };

  // ---- direction formatting
  OD.dirLabel = (d) => {
    if (d === null || d === undefined || d === "") return "-";
    const n = Number(d);
    if (!Number.isFinite(n)) return String(d);
    return n === 0 ? "上行" : n === 1 ? "下行" : `方向 ${n}`;
  };

  // ---- query-string builder that skips empty filters
  OD.query = (params) => {
    const parts = [];
    for (const k of Object.keys(params)) {
      const v = params[k];
      if (v === null || v === undefined || v === "" || v === "all") continue;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    }
    return parts.length ? "?" + parts.join("&") : "";
  };

  // ---- metric quality helpers used for colour coding
  OD.qualityTone = (value, good, ok) => {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "";
    const v = Number(value);
    if (v >= good) return "tone-good";
    if (v >= ok) return "tone-warn";
    return "tone-bad";
  };

  // ---- diff two OD tables (used by the compare page)
  OD.odDelta = (a, b, keyFn) => {
    const keyOf = keyFn || ((r) => `${r.origin_name}→${r.dest_name}`);
    // A is base, B is new; indexes are [origin|dest] aggregated (not identical layout)
    const agg = {};
    for (const r of a) {
      const k = keyOf(r);
      agg[k] = agg[k] || { key: k, a: 0, b: 0, origin: r.origin_name, dest: r.dest_name };
      agg[k].a += Number(r.passengers || 0);
    }
    for (const r of b) {
      const k = keyOf(r);
      agg[k] = agg[k] || { key: k, a: 0, b: 0, origin: r.origin_name, dest: r.dest_name };
      agg[k].b += Number(r.passengers || 0);
      if (!agg[k].origin) { agg[k].origin = r.origin_name; agg[k].dest = r.dest_name; }
    }
    return Object.values(agg).map((x) => ({ ...x, diff: x.b - x.a }))
      .sort((p, q) => Math.abs(q.diff) - Math.abs(p.diff));
  };

  OD.clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // ---- JSON-safe fetch error text
  OD.errorText = (payload, status) => {
    if (!payload) return "请求失败 (" + status + ")";
    if (typeof payload === "string") return payload;
    if (payload.detail) {
      if (typeof payload.detail === "string") return payload.detail;
      if (Array.isArray(payload.detail)) {
        return payload.detail.map((d) => d.msg || JSON.stringify(d)).join("；");
      }
      if (payload.detail.message) {
        const errs = payload.detail.errors;
        return payload.detail.message + (errs && errs.length ? "：" + errs.join("；") : "");
      }
    }
    if (payload.message) return payload.message;
    return JSON.stringify(payload);
  };

  root.OD = OD;
  if (typeof module !== "undefined" && module.exports) module.exports = OD;
})(typeof window !== "undefined" ? window : globalThis);
