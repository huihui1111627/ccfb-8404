/* Unit tests for app/static/core.js — run with: node --test tests/core.test.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const OD = require("../app/static/core.js");

test("fmtNum handles null, strings, large numbers", () => {
  assert.equal(OD.fmtNum(null), "-");
  assert.equal(OD.fmtNum(undefined), "-");
  assert.equal(OD.fmtNum("abc"), "-");
  assert.equal(OD.fmtNum(1234, 0), "1,234");
  assert.equal(OD.fmtNum(3.14159, 2), "3.14");
});

test("fmtPct converts ratios", () => {
  assert.equal(OD.fmtPct(0.9617), "96.2%");
  assert.equal(OD.fmtPct(null), "-");
  assert.equal(OD.fmtPct(1, 0), "100%");
});

test("fmtTime keeps month-day and clock", () => {
  assert.equal(OD.fmtTime("2026-11-09 08:15:00"), "11-09 08:15");
  assert.equal(OD.fmtTime("2026-11-09T08:15:00"), "11-09 08:15");
  assert.equal(OD.fmtTime(null), "-");
  assert.equal(OD.fmtClock("2026-11-09 08:15:42"), "08:15");
});

test("fmtDuration renders seconds, minutes, hours", () => {
  assert.equal(OD.fmtDuration(45), "45 秒");
  assert.equal(OD.fmtDuration(125), "2 分 5 秒");
  assert.equal(OD.fmtDuration(3720), "1 时 2 分");
  assert.equal(OD.fmtDuration(null), "-");
});

test("sortRows sorts numerically and textually, ascending and descending", () => {
  const rows = [{ n: 10 }, { n: 2 }, { n: 33 }];
  assert.deepEqual(OD.sortRows(rows, "n", false).map((r) => r.n), [2, 10, 33]);
  assert.deepEqual(OD.sortRows(rows, "n", true).map((r) => r.n), [33, 10, 2]);
  const txt = [{ s: "b" }, { s: "a" }, { s: "c" }];
  assert.deepEqual(OD.sortRows(txt, "s", false).map((r) => r.s), ["a", "b", "c"]);
});

test("sortRows keeps nulls last when ascending", () => {
  const rows = [{ n: 3 }, { n: null }, { n: 1 }];
  const order = OD.sortRows(rows, "n", false).map((r) => r.n);
  assert.equal(order[order.length - 1], null);
});

test("uniqueValues ignores empties and sorts", () => {
  const rows = [{ p: "B" }, { p: "A" }, { p: null }, { p: "" }, { p: "B" }];
  assert.deepEqual(OD.uniqueValues(rows, "p"), ["A", "B"]);
});

test("sumBy aggregates correctly", () => {
  const rows = [{ k: "a", v: 1 }, { k: "a", v: 2 }, { k: "b", v: 5 }];
  assert.deepEqual(OD.sumBy(rows, "k", "v"), { a: 3, b: 5 });
});

test("color ramp is bounded and deterministic", () => {
  assert.match(OD.color(-1), /^rgba\(\d+,\d+,\d+,/);
  assert.match(OD.color(2), /^rgba\(\d+,\d+,\d+,/);
  assert.equal(OD.color(0.5), OD.color(0.5));
  assert.match(OD.colorHex(0.2), /^#[0-9a-f]{6}$/);
});

test("reconcilePeriod falls back when a period disappears", () => {
  assert.equal(OD.reconcilePeriod("早高峰", ["早高峰", "平峰"]), "早高峰");
  assert.equal(OD.reconcilePeriod("夜间", ["早高峰", "平峰"]), "all");
  assert.equal(OD.reconcilePeriod("all", ["早高峰"]), "all");
  assert.equal(OD.reconcilePeriod("x", []), "x");
});

test("query skips empty and 'all' filters", () => {
  assert.equal(OD.query({ a: 1, b: null, c: "", d: "all", e: "x" }), "?a=1&e=x");
  assert.equal(OD.query({}), "");
});

test("dirLabel maps direction codes", () => {
  assert.equal(OD.dirLabel(0), "上行");
  assert.equal(OD.dirLabel(1), "下行");
  assert.equal(OD.dirLabel(null), "-");
});

test("errorText extracts FastAPI detail shapes", () => {
  assert.equal(OD.errorText("boom", 500), "boom");
  assert.equal(OD.errorText({ detail: "缺失" }, 400), "缺失");
  assert.equal(
    OD.errorText({ detail: { message: "参数不合法", errors: ["a", "b"] } }, 400),
    "参数不合法：a；b");
  assert.equal(
    OD.errorText({ detail: [{ msg: "field required" }] }, 422), "field required");
  assert.equal(OD.errorText(null, 503), "请求失败 (503)");
});

test("qualityTone thresholds", () => {
  assert.equal(OD.qualityTone(0.99, 0.97, 0.9), "tone-good");
  assert.equal(OD.qualityTone(0.92, 0.97, 0.9), "tone-warn");
  assert.equal(OD.qualityTone(0.5, 0.97, 0.9), "tone-bad");
  assert.equal(OD.qualityTone(null, 0.97, 0.9), "");
});

test("odDelta finds increases and decreases across two result sets", () => {
  const a = [{ origin_name: "A", dest_name: "B", passengers: 10 },
             { origin_name: "C", dest_name: "D", passengers: 5 }];
  const b = [{ origin_name: "A", dest_name: "B", passengers: 4 },
             { origin_name: "E", dest_name: "F", passengers: 9 }];
  const d = OD.odDelta(a, b);
  const by = Object.fromEntries(d.map((x) => [x.key, x]));
  assert.equal(by["A→B"].diff, -6);
  assert.equal(by["C→D"].diff, -5);
  assert.equal(by["E→F"].diff, 9);
  // sorted by absolute change
  assert.equal(d[0].key, "E→F");
});

test("methodLabel and confLabel have Chinese names", () => {
  assert.equal(OD.methodLabel("next_board_same_stop"), "下次刷卡同站");
  assert.equal(OD.methodLabel("unknown_method"), "unknown_method");
  assert.equal(OD.confLabel("high"), "高");
});

test("clamp", () => {
  assert.equal(OD.clamp(5, 0, 3), 3);
  assert.equal(OD.clamp(-2, 0, 3), 0);
  assert.equal(OD.clamp(1.5, 0, 3), 1.5);
});
