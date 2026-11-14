/* Console application.  Plain ES2019 + Leaflet; no build step, no framework. */
(() => {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const state = {
    meta: null,
    datasets: [],
    runs: [],
    runId: null,
    run: null,
    metrics: null,
    page: "dashboard",
    odCache: [],
    map: null,
    layers: {},
    filters: { od: {}, me: {} },
    params: null,
    pager: {},
  };

  // ------------------------------------------------------------------ utils
  function toast(msg, isErr) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.className = "toast"; }, 3200);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let payload = null;
    const text = await res.text();
    if (text) {
      try { payload = JSON.parse(text); } catch (e) { payload = text; }
    }
    if (!res.ok) throw new Error(OD.errorText(payload, res.status));
    return payload;
  }

  function escapeHtml(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderTable(container, columns, rows, opts) {
    opts = opts || {};
    const visible = columns.filter((c) => !c.hidden);
    if (!rows.length) {
      container.innerHTML = '<div class="empty">暂无数据</div>';
      return;
    }
    const head = visible.map((c) =>
      `<th data-key="${c.key}"${c.align === "right" ? ' style="text-align:right"' : ""}>${escapeHtml(c.label)}</th>`).join("");
    const body = rows.map((r) => {
      const cells = visible.map((c) => {
        let v = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
        if (v === undefined || v === null || v === "") v = "-";
        const cls = c.align === "right" ? "num" : "";
        if (c.html) return `<td class="${cls}">${v}</td>`;
        return `<td class="${cls}">${escapeHtml(v)}</td>`;
      }).join("");
      return `<tr${opts.onRow ? ' class="clickable"' : ""} data-idx="${rows.indexOf(r)}">${cells}</tr>`;
    }).join("");
    container.innerHTML = `<table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    if (opts.onRow) {
      $$("tbody tr", container).forEach((tr) => {
        tr.style.cursor = "pointer";
        tr.onclick = () => opts.onRow(rows[Number(tr.dataset.idx)]);
      });
    }
    if (opts.onSort) {
      $$("th", container).forEach((th) => {
        th.onclick = () => opts.onSort(th.dataset.key, th.dataset.key === opts.sortKey ? !opts.desc : true);
      });
    }
  }

  function pager(container, total, page, pageSize, onChange) {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    container.innerHTML =
      `<span>共 ${OD.fmtNum(total, 0)} 条 · 第 ${page + 1}/${pages} 页</span>` +
      `<div class="spacer"></div>` +
      `<button data-p="prev" ${page <= 0 ? "disabled" : ""}>上一页</button>` +
      `<button data-p="next" ${page >= pages - 1 ? "disabled" : ""}>下一页</button>`;
    $$("button", container).forEach((b) => {
      b.onclick = () => onChange(b.dataset.p === "prev" ? page - 1 : page + 1);
    });
  }

  function barList(rows, opts) {
    if (!rows.length) return '<div class="empty">暂无数据</div>';
    const max = Math.max(...rows.map((r) => Number(r.value) || 0), 1);
    return '<div class="bars">' + rows.map((r) => {
      const pct = (Number(r.value) || 0) / max * 100;
      const tone = r.tone ? " " + r.tone : "";
      return `<div class="bar-row">
        <div class="name" title="${escapeHtml(r.name)}">${escapeHtml(r.name)}</div>
        <div class="track"><div class="fill${tone}" style="width:${pct.toFixed(1)}%"></div></div>
        <div class="val">${escapeHtml(r.text !== undefined ? r.text : OD.fmtNum(r.value, 0))}</div>
      </div>`;
    }).join("") + "</div>";
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    try {
      state.meta = await api("/api/meta");
      state.params = JSON.parse(JSON.stringify(state.meta.default_params));
    } catch (e) {
      toast("无法连接服务：" + e.message, true);
    }
    checkHealth();
    setInterval(checkHealth, 15000);
    await refreshDatasets();
    await refreshRuns();
    renderUploadSlots();
    renderParams();
    bindNav();
    bindButtons();
    if (state.runs.length) selectRun(state.runs[0].id);
    switchPage("dashboard");
  }

  async function checkHealth() {
    try {
      const h = await api("/api/health");
      $("#health").innerHTML = `<span class="ok">● 服务正常</span> · v${h.version}` +
        (h.queue ? ` · 队列 ${h.queue}` : "");
    } catch (e) {
      $("#health").innerHTML = `<span style="color:var(--danger)">● 服务异常</span>`;
    }
  }

  function bindNav() {
    $$("#nav button").forEach((b) => {
      b.onclick = () => switchPage(b.dataset.page);
    });
    document.addEventListener("click", (ev) => {
      const t = ev.target.closest("[data-goto]");
      if (t) { ev.preventDefault(); switchPage(t.dataset.goto); }
    });
  }

  const PAGE_META = {
    dashboard: ["总览", "关键指标、准确率与高峰断面"],
    import: ["数据导入", "上传三套系统数据，配置字段映射"],
    matching: ["站点匹配", "刷卡时间对齐车辆 GPS 停站，推断上车站"],
    chains: ["出行链", "按出行链推断下车站，标记不确定结果"],
    od: ["OD 矩阵", "分时段 OD 与地图流向"],
    metrics: ["指标看板", "断面客流、站点上下客、满载率"],
    compare: ["方案对比", "参数调整前后结果差异"],
    params: ["参数设置", "时间阈值、清洗规则、时段划分"],
  };

  function switchPage(page) {
    state.page = page;
    $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + page));
    $("#page-title").textContent = PAGE_META[page][0];
    $("#page-sub").textContent = PAGE_META[page][1];
    const needRun = !["dashboard", "import", "params", "compare"].includes(page);
    if (needRun && !state.runId) {
      const cont = $("#page-" + page);
      if (!cont.dataset.emptyShown) {
        cont.dataset.emptyShown = "1";
        toast("请先运行一次推算，或从右上角选择已有结果", true);
      }
    }
    if (page === "matching") loadMatching();
    if (page === "chains") loadChains();
    if (page === "od") loadOd();
    if (page === "metrics") loadMetrics();
    if (page === "compare") renderComparePicker();
    if (page === "dashboard") renderDashboard();
    if (page === "params") syncParamsDatasets();
  }

  // ------------------------------------------------------------------ runs
  async function refreshRuns() {
    const data = await api("/api/runs");
    state.runs = data.items;
    const sel = $("#run-select");
    const prev = state.runId;
    sel.innerHTML = state.runs.map((r) =>
      `<option value="${r.id}">${escapeHtml(r.name)} · ${statusLabel(r.status)}</option>`).join("")
      || '<option value="">暂无运行</option>';
    if (prev && state.runs.some((r) => r.id === prev)) sel.value = prev;
    sel.onchange = () => selectRun(sel.value);
    $("#cmp-select").innerHTML = state.runs.map((r) =>
      `<label class="chip"><input type="checkbox" value="${r.id}"> ${escapeHtml(r.name)}</label>`).join("");
  }

  function statusLabel(s) {
    return { queued: "排队中", running: "运行中", succeeded: "完成", failed: "失败" }[s] || s;
  }

  async function selectRun(runId) {
    if (!runId) {
      state.runId = null; state.run = null; state.metrics = null;
      $("#run-select").value = "";
      renderDashboard();
      return;
    }
    state.runId = runId;
    $("#run-select").value = runId;
    const data = await api(`/api/runs/${runId}`);
    state.run = data.run;
    state.metrics = data.metrics || (data.run.summary || null);
    if (data.run.status === "running" || data.run.status === "queued") {
      watchRun(runId);
    }
    state.filters.od = {};
    state.filters.me = {};
    if (state.page === "dashboard") renderDashboard();
    populateFilterSelects();
  }

  let watchTimer = null;
  function watchRun(runId) {
    clearInterval(watchTimer);
    watchTimer = setInterval(async () => {
      try {
        const d = await api(`/api/runs/${runId}`);
        state.run = d.run;
        if (!$("#pm-progress-wrap")) return;
        const pct = d.run.progress || 0;
        $("#pm-progress-wrap").style.display = "block";
        $("#pm-progress").style.width = pct + "%";
        const log = $("#pm-log");
        log.style.display = "block";
        log.innerHTML = `<div>[${pct.toFixed(0)}%] ${escapeHtml(d.run.stage || "")} · ${escapeHtml(d.run.message || "")}</div>`;
        if (d.run.status === "succeeded") {
          clearInterval(watchTimer);
          await refreshRuns();
          await selectRun(runId);
          toast("推算完成");
          renderParamsRunLog(d.run);
          if (state.page === "dashboard") renderDashboard();
        } else if (d.run.status === "failed") {
          clearInterval(watchTimer);
          toast("推算失败：" + (d.run.message || "未知错误"), true);
          renderParamsRunLog(d.run);
          await refreshRuns();
        }
        const sel = $("#run-select");
        if (sel && state.runId === runId) {
          const opt = Array.from(sel.options).find((o) => o.value === runId);
          if (opt) opt.textContent = `${d.run.name} · ${statusLabel(d.run.status)}`;
        }
      } catch (e) { clearInterval(watchTimer); }
    }, 900);
  }

  function renderParamsRunLog(run) {
    const log = $("#pm-log");
    const box = $("#pm-progress-wrap");
    if (box) box.style.display = "block";
    if (!log) return;
    log.style.display = "block";
    if (run.status === "failed") {
      log.innerHTML = `<div style="color:#ffb4b2">运行失败：${escapeHtml(run.message || "")}</div>` +
        (run.error ? `<div style="color:#8b98b4">${escapeHtml(run.error.split("\n").slice(-3).join("\n"))}</div>` : "");
    } else {
      const m = (state.metrics || {});
      log.innerHTML = `<div>运行完成 · 用时 ${m.elapsed_sec || run.elapsed_sec || "?"} 秒</div>` +
        `<div>刷卡 ${OD.fmtNum(m.taps, 0)} · 有效 ${OD.fmtNum(m.taps_valid, 0)} · 出行 ${OD.fmtNum(m.legs, 0)}</div>` +
        `<div>OD 总量 ${OD.fmtNum(m.od_total, 0)} · 下车站推算率 ${OD.fmtPct(m.alight_rate)}</div>`;
      $("#pm-progress").style.width = "100%";
    }
  }

  // ------------------------------------------------------------------ 总览
  function metricCard(label, value, foot, tone) {
    return `<div class="card${tone ? " " + tone : ""}">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value">${escapeHtml(value)}</div>
      <div class="foot">${escapeHtml(foot || "")}</div>
    </div>`;
  }

  function renderDashboard() {
    if (!state.metrics && !state.run) {
      $("#dash-empty").style.display = "";
      $("#dash-body").style.display = "none";
      return;
    }
    $("#dash-empty").style.display = "none";
    $("#dash-body").style.display = "";
    const m = state.metrics || {};
    const acc = m.accuracy || {};
    $("#dash-cards").innerHTML = [
      metricCard("刷卡记录", OD.fmtNum(m.taps, 0),
        `有效 ${OD.fmtNum(m.taps_valid, 0)} · 重复 ${OD.fmtNum(m.taps_duplicate, 0)}`),
      metricCard("上车站匹配率", OD.fmtPct(m.match_rate),
        "刷卡时间 ↔ 车辆停站", OD.qualityTone(m.match_rate, 0.97, 0.9)),
      metricCard("下车站推算率", OD.fmtPct(m.alight_rate),
        `不确定 ${OD.fmtPct(m.uncertain_rate)}`, OD.qualityTone(m.alight_rate, 0.95, 0.8)),
      metricCard("下车站准确率", acc.available ? OD.fmtPct(acc.alight_stop_accuracy) : "-",
        acc.available
          ? `邻近站容差 ${OD.fmtPct(acc.alight_stop_accuracy_adjacent)} · 确定结果 ${OD.fmtPct(acc.alight_stop_accuracy_confident)}`
          : "未提供真值",
        acc.available ? OD.qualityTone(acc.alight_stop_accuracy, 0.85, 0.7) : ""),
      metricCard("OD 客流", OD.fmtNum(m.od_total, 0),
        `${OD.fmtNum(m.od_pairs, 0)} 个 OD 对`),
      metricCard("换乘次数", OD.fmtNum(m.transfers, 0),
        `换乘率 ${OD.fmtPct(m.transfer_rate)}`),
      metricCard("最大断面客流", OD.fmtNum(m.max_load, 0),
        `平均满载率 ${OD.fmtPct(m.avg_load_factor)}`,
        m.avg_load_factor > 0.9 ? "tone-warn" : ""),
      metricCard("运行用时", (m.elapsed_sec || "?") + " 秒",
        `${OD.fmtNum(m.trips_tracked, 0)} 个班次 · ${OD.fmtNum(m.vehicles, 0)} 辆车`),
    ].join("");

    const steps = [
      ["数据导入", "刷卡 / GPS / 站点"],
      ["GPS 清洗", `剔除漂移 ${OD.fmtNum((m.gps || {}).drift_points_removed, 0)} 点`],
      ["停站识别", `${OD.fmtNum(m.stop_events, 0)} 个停站事件`],
      ["上车匹配", `匹配率 ${OD.fmtPct(m.match_rate)}`],
      ["下车站", `推算率 ${OD.fmtPct(m.alight_rate)}`],
      ["换乘/出行链", `${OD.fmtNum(m.journeys, 0)} 条链`],
      ["OD / 断面", `${OD.fmtNum(m.od_pairs, 0)} 个 OD 对`],
    ];
    $("#dash-steps").innerHTML = steps.map((s, i) =>
      `<div class="step done"><div class="n">STEP ${i + 1}</div><div>${s[0]}</div>
       <div style="color:var(--muted);font-size:11px">${escapeHtml(s[1])}</div></div>`).join("");

    if (acc.available) {
      $("#acc-hint").textContent = `真值 ${OD.fmtNum(acc.evaluated_legs, 0)} 条可比对`;
      const methods = (acc.per_method || []).map((x) => ({
        name: OD.methodLabel(x.method), value: x.n,
        text: `${OD.fmtPct(x.acc)} (${OD.fmtNum(x.n, 0)})`,
        tone: x.acc >= 0.9 ? "" : x.acc >= 0.5 ? "warn" : "bad",
      }));
      $("#acc-body").innerHTML = `
        <div class="cards" style="grid-template-columns:repeat(2,1fr);margin-bottom:12px">
          ${metricCard("下车站准确率", OD.fmtPct(acc.alight_stop_accuracy), "严格同站")}
          ${metricCard("邻近站容差",
            OD.fmtPct(acc.alight_stop_accuracy_adjacent),
            `误差 ≤ ${OD.fmtNum(acc.adjacent_tolerance_m, 0)} m`)}
          ${metricCard("上车站准确率", OD.fmtPct(acc.board_stop_accuracy), "对照真值")}
          ${metricCard("换乘 F1", OD.fmtNum(acc.transfer_f1), "换乘识别")}
        </div>
        ${barList(methods)}`;
    } else {
      $("#acc-hint").textContent = "";
      $("#acc-body").innerHTML = '<div class="empty">未导入真值数据，无法评估准确率。<br>' +
        '导入带真实下车站的对照文件后重新运行即可看到准确率。</div>';
    }

    const peak = (m.peak_sections || []).map((p) => ({
      name: `${p.route_id} ${OD.dirLabel(p.direction)} ${p.stop_name}`,
      value: p.avg_load, text: OD.fmtNum(p.avg_load, 0),
      tone: (p.peak_load_factor || 0) > 1 ? "bad" : (p.peak_load_factor || 0) > 0.8 ? "warn" : "",
    }));
    $("#dash-peak").innerHTML = peak.length ? barList(peak) : '<div class="empty">暂无断面数据</div>';

    const periods = (m.period_od || []).map((p) => ({
      name: p.period, value: p.trips, text: OD.fmtNum(p.trips, 0) + " 人次",
    }));
    $("#dash-periods").innerHTML = periods.length ? barList(periods) : '<div class="empty">暂无时段数据</div>';

    const top = (m.top_od || []);
    $("#dash-topod").innerHTML = top.length ? `<table class="grid"><thead><tr>
      <th>起点</th><th>终点</th><th>时段</th><th style="text-align:right">客流</th></tr></thead><tbody>` +
      top.map((r) => `<tr><td>${escapeHtml(r.origin_name)}</td><td>${escapeHtml(r.dest_name)}</td>
        <td>${escapeHtml(r.period)}</td><td class="num">${OD.fmtNum(r.passengers, 0)}</td></tr>`).join("") +
      "</tbody></table>" : '<div class="empty">暂无 OD 数据</div>';

    const warnings = (m.warnings || []);
    const gps = m.gps || {};
    $("#dash-warnings").innerHTML = `
      <div class="cards" style="grid-template-columns:repeat(4,1fr)">
        ${metricCard("GPS 漂移剔除", OD.fmtNum(gps.drift_points_removed, 0) + " 点", `输入 ${OD.fmtNum(gps.rows_in, 0)} 行`)}
        ${metricCard("重复刷卡", OD.fmtNum(m.taps_duplicate, 0) + " 条", "同卡同车短时重复")}
        ${metricCard("无法匹配", OD.fmtNum(m.taps_invalid, 0) + " 条", "刷卡时无车辆停站")}
        ${metricCard("不确定推算", OD.fmtNum(m.uncertain, 0) + " 条", "无直接证据，仅作参考")}
      </div>
      ${warnings.length ? warnings.map((w) => `<div class="warn-box">${escapeHtml(w)}</div>`).join("")
        : '<div class="hint">本次运行无清洗告警。</div>'}`;
  }

  // ------------------------------------------------------------------ 导入
  const SLOT_LABELS = {
    taps: ["刷卡数据 AFC", "必需", "卡号、刷卡时间、线路"],
    gps: ["车辆 GPS AVL", "必需", "车辆、时间、经纬度"],
    stops: ["线路站点表", "必需", "线路、站序、站点、经纬度"],
    truth: ["真值对照", "可选", "用于评估准确率"],
  };

  function renderUploadSlots() {
    $("#upload-slots").innerHTML = ["taps", "gps", "stops", "truth"].map((kind) => {
      const [label, need, desc] = SLOT_LABELS[kind];
      return `<div class="panel" style="margin:0">
        <div class="head"><h2>${label}</h2><div class="spacer"></div>
          <span class="badge ${need === "必需" ? "blue" : ""}">${need}</span></div>
        <div class="body">
          <div class="hint" style="margin-bottom:8px">${desc}</div>
          <input type="file" data-kind="${kind}" accept=".csv,.xlsx,.xls,.parquet,.json,.jsonl" style="width:100%;margin-bottom:8px">
          <div class="hint" id="upload-status-${kind}"></div>
        </div>
      </div>`;
    }).join("");
    $$('#upload-slots input[type=file]').forEach((input) => {
      input.onchange = () => uploadFile(input.dataset.kind, input.files[0], input);
    });
    renderDatasetList();
  }

  async function uploadFile(kind, file, input) {
    if (!file) return;
    const status = $("#upload-status-" + kind);
    status.textContent = "上传解析中…";
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    try {
      const res = await api("/api/datasets/upload", { method: "POST", body: fd });
      status.textContent = `已导入 ${res.dataset.n_rows} 行`;
      toast(`${SLOT_LABELS[kind][0]} 导入成功`);
      await refreshDatasets();
      renderDatasetList();
      syncParamsDatasets();
    } catch (e) {
      status.textContent = "导入失败";
      toast(e.message, true);
      if (e.message && e.message.indexOf("字段") >= 0) openMappingHint(kind);
    } finally {
      input.value = "";
    }
  }

  function openMappingHint(kind) {
    toast("请在已导入数据集中打开预览，调整字段映射后重新上传同一文件", true);
  }

  async function refreshDatasets() {
    const data = await api("/api/datasets");
    state.datasets = data.items;
  }

  function renderDatasetList() {
    const cont = $("#dataset-list");
    if (!state.datasets.length) {
      cont.innerHTML = '<div class="empty">尚未导入任何数据集</div>';
      return;
    }
    cont.innerHTML = state.datasets.map((d) => {
      const needs = (d.meta || {}).needs_mapping;
      const badge = needs
        ? '<span class="badge bad" style="margin-left:8px">需补字段映射</span>' : "";
      return `<div class="run-item" data-ds="${d.id}">
      <div><div class="name">${escapeHtml(d.name)}${badge}</div>
        <div class="meta">${escapeHtml(KIND_LABEL(d.kind))} · ${OD.fmtNum(d.n_rows, 0)} 行 · ${OD.fmtNum(d.size_bytes / 1024, 0)} KB · ${escapeHtml((d.created_at || "").replace("T", " ").slice(0, 16))}</div>
        ${needs ? `<div class="meta" style="color:var(--danger)">${escapeHtml((d.meta || {}).error || "")}</div>` : ""}</div>
      <div class="spacer"></div>
      <button class="ghost" data-act="preview" data-ds="${d.id}">${needs ? "配置映射" : "预览"}</button>
      <button class="ghost" data-act="delete" data-ds="${d.id}">删除</button>
    </div>`;
    }).join("");
    $$("#dataset-list button").forEach((b) => {
      b.onclick = async (ev) => {
        ev.stopPropagation();
        if (b.dataset.act === "preview") openDatasetPreview(b.dataset.ds);
        else if (b.dataset.act === "delete") deleteDataset(b.dataset.ds);
      };
    });
  }

  function KIND_LABEL(kind) {
    return ({ taps: "刷卡", gps: "GPS", stops: "站点", truth: "真值" })[kind] || kind;
  }

  async function deleteDataset(dsId) {
    if (!confirm("确定删除该数据集？")) return;
    try {
      await api(`/api/datasets/${dsId}`, { method: "DELETE" });
      await refreshDatasets();
      renderDatasetList();
      syncParamsDatasets();
      toast("已删除");
    } catch (e) { toast(e.message, true); }
  }

  async function openDatasetPreview(dsId) {
    const d = await api(`/api/datasets/${dsId}?limit=50`);
    const ds = d.dataset;
    const spec = d.field_spec || {};
    const mapping = d.mapping || {};
    const cols = d.columns || [];
    const needsMapping = (ds.meta || {}).needs_mapping;
    const banner = needsMapping
      ? `<div class="error-box">该文件缺少必填字段，请在上方选择正确的列并保存映射。
         ${escapeHtml((ds.meta || {}).error || "")}</div>` : "";
    // Datasets that imported cleanly are stored in canonical form, so the
    // selectable columns are the canonical names; only a failed upload keeps
    // the raw source headers for repair.
    const sourceCols = needsMapping
      ? ((ds.meta || {}).source_columns || cols.map((c) => c.name))
      : cols.map((c) => c.name);
    const mappingHtml = Object.keys(spec).map((field) => {
      const meta = spec[field];
      const current = needsMapping ? mapping[field]
        : (cols.some((c) => c.name === field) ? field : null);
      const opts = ['<option value="">— 不映射 —</option>'].concat(
        sourceCols.map((c) => `<option value="${escapeHtml(c)}"${current === c ? " selected" : ""}>${escapeHtml(c)}</option>`)
      ).join("");
      return `<div class="mapping-row">
        <div class="field">${escapeHtml(meta.label)}${meta.required ? '<span class="req">*</span>' : ""}</div>
        <select data-field="${field}"${needsMapping ? "" : " disabled"}>${opts}</select>
        <div class="dtype">${escapeHtml(meta.type || "文本")}${meta.required ? " · 必填" : ""}</div>
      </div>`;
    }).join("");
    const mappingNote = needsMapping
      ? '<div class="hint" style="margin-bottom:8px">选择源文件中的列，保存后系统会按新映射重建数据集。</div>'
      : '<div class="hint" style="margin-bottom:8px">该数据集已按下列字段完成解析，映射不可在此修改；如需调整请重新上传并指定映射。</div>';
    const table = cols.length ? `<table class="grid"><thead><tr>${cols.map((c) =>
      `<th>${escapeHtml(c.name)}<div style="font-weight:400;color:var(--muted);font-size:10px">${escapeHtml(c.dtype)}</div></th>`).join("")}</tr></thead>
      <tbody>${d.rows.map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c.name])}</td>`).join("")}</tr>`).join("")}</tbody></table>` : '<div class="empty">无数据</div>';
    openModal(`数据集预览 · ${ds.name}`, `
      ${banner}
      <div class="hint" style="margin-bottom:10px">共 ${OD.fmtNum(d.total, 0)} 行，显示前 ${d.rows.length} 行。字段映射对所有运行生效。</div>
      <div class="panel" style="margin-bottom:14px"><div class="head"><h2>字段映射</h2></div>
        <div class="body">${mappingNote}${mappingHtml}</div></div>
      <div class="panel"><div class="head"><h2>数据预览</h2></div>
        <div class="body tight"><div class="table-wrap">${table}</div></div></div>
      ${needsMapping ? '<div style="margin-top:12px"><button class="ghost" id="modal-map-save">保存映射</button></div>' : ""}`);
    const saveBtn = $("#modal-map-save");
    if (saveBtn) saveBtn.onclick = async () => {
      const payload = {};
      $$(".mapping-row select").forEach((s) => { payload[s.dataset.field] = s.value || null; });
      try {
        const res = await api(`/api/datasets/${dsId}/mapping`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mapping: payload }),
        });
        toast(res.rows ? `映射已应用，共 ${res.rows} 行可用` : "映射已保存");
        $("#modal-back").classList.remove("show");
        await refreshDatasets();
        renderDatasetList();
        syncParamsDatasets();
      } catch (e) { toast(e.message, true); }
    };
  }

  function openModal(title, body) {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = body;
    $("#modal-back").classList.add("show");
  }

  // ------------------------------------------------------------------ 匹配
  async function loadMatching() {
    if (!state.runId) return;
    try {
      const filters = await api(`/api/runs/${state.runId}/filters`);
      $("#mt-route").innerHTML = '<option value="all">全部线路</option>' +
        (filters.routes || []).map((r) => `<option value="${escapeHtml(r)}">${escapeHtml(r)}</option>`).join("");
      loadTaps(0);
      loadEvents(0);
      loadStopQuality();
    } catch (e) { toast(e.message, true); }
  }

  async function loadTaps(page) {
    const ps = 25;
    const q = OD.query({ route: $("#mt-route").value, status: $("#mt-status").value,
      limit: ps, offset: page * ps, sort: "tap_time", desc: true });
    const d = await api(`/api/runs/${state.runId}/taps${q}`);
    const cols = [
      { key: "tap_id", label: "流水号" }, { key: "card_id", label: "卡号" },
      { key: "tap_time", label: "刷卡时间", fmt: OD.fmtTime },
      { key: "route_id", label: "线路" }, { key: "vehicle_id", label: "车辆" },
      { key: "board_stop_name", label: "推断上车站" },
      { key: "match_delta_sec", label: "时间差(s)", align: "right", fmt: (v) => OD.fmtNum(v, 1) },
      { key: "status", label: "状态", html: true, fmt: (v) => statusBadge(v) },
    ];
    renderTable($("#mt-taps"), cols, d.rows);
    pager($("#mt-taps-pager"), d.total, page, ps, loadTaps);
    $("#mt-taps-pager").style.display = "";
  }

  function statusBadge(s) {
    const map = { valid: ["good", "匹配"], duplicate: ["warn", "重复"], invalid: ["bad", "无法匹配"] };
    const it = map[s] || ["", s];
    return `<span class="badge ${it[0]}">${it[1]}</span>`;
  }

  async function loadEvents(page) {
    const ps = 25;
    const q = OD.query({ route: $("#mt-route").value, limit: ps, offset: page * ps,
      sort: "arr_time", desc: false });
    const d = await api(`/api/runs/${state.runId}/events${q}`);
    const cols = [
      { key: "vehicle_id", label: "车辆" }, { key: "route_id", label: "线路" },
      { key: "direction", label: "方向", fmt: OD.dirLabel },
      { key: "stop_id", label: "站点" },
      { key: "arr_time", label: "到达", fmt: OD.fmtClock },
      { key: "dep_time", label: "离开", fmt: OD.fmtClock },
      { key: "dwell_sec", label: "停站(s)", align: "right", fmt: (v) => OD.fmtNum(v, 0) },
      { key: "snap_m", label: "吸附(m)", align: "right", fmt: (v) => OD.fmtNum(v, 1) },
    ];
    renderTable($("#mt-events"), cols, d.rows);
    pager($("#mt-events-pager"), d.total, page, ps, loadEvents);
  }

  async function loadStopQuality() {
    const d = await api(`/api/runs/${state.runId}/stops`);
    const items = d.items || [];
    const cols = [
      { key: "stop_name", label: "站点" }, { key: "stop_id", label: "编号" },
      { key: "stop_events", label: "停站事件", align: "right" },
      { key: "board_taps", label: "刷卡数", align: "right" },
      { key: "vehicles", label: "服务车辆", align: "right" },
      { key: "avg_snap_m", label: "平均吸附(m)", align: "right", fmt: (v) => OD.fmtNum(v, 1) },
      { key: "avg_match_delta", label: "平均时间差(s)", align: "right", fmt: (v) => OD.fmtNum(v, 1) },
    ];
    renderTable($("#mt-stops"), cols, items.slice(0, 200));
  }

  // ------------------------------------------------------------------ 出行链
  async function loadChains() {
    if (!state.runId) return;
    try {
      const filters = await api(`/api/runs/${state.runId}/filters`);
      const cur = $("#ch-method").value;
      $("#ch-method").innerHTML = '<option value="all">全部</option>' +
        (filters.methods || []).map((m) =>
          `<option value="${escapeHtml(m)}"${m === cur ? " selected" : ""}>${escapeHtml(OD.methodLabel(m))}</option>`).join("");
    } catch (e) { /* filters are optional */ }
    loadLegs(0);
  }

  async function loadLegs(page) {
    const ps = 30;
    const card = $("#ch-card").value.trim();
    const un = $("#ch-uncertain").value;
    const q = OD.query({
      card_id: card, method: $("#ch-method").value,
      uncertain: un === "all" ? "" : un,
      limit: ps, offset: page * ps, sort: "board_time", desc: false,
    });
    const d = await api(`/api/runs/${state.runId}/legs${q}`);
    const cols = [
      { key: "card_id", label: "卡号" },
      { key: "board_time", label: "上车时间", fmt: OD.fmtTime },
      { key: "board_stop_name", label: "上车站" },
      { key: "route_id", label: "线路" },
      { key: "alight_time", label: "下车时间", fmt: OD.fmtTime },
      { key: "alight_stop_name", label: "下车站" },
      { key: "alight_method", label: "推算方式", fmt: OD.methodLabel },
      { key: "confidence", label: "置信度", html: true, fmt: (v) => confBadge(v) },
      { key: "is_transfer", label: "换乘", html: true, fmt: (v) => v ? '<span class="badge blue">换乘</span>' : "" },
    ];
    renderTable($("#ch-table"), cols, d.rows, { onRow: (r) => showCardChain(r.card_id) });
    pager($("#ch-pager"), d.total, page, ps, loadLegs);
    $("#ch-summary").textContent = `共 ${OD.fmtNum(d.total, 0)} 条出行记录`;
  }

  function confBadge(c) {
    const map = { high: ["good", "高"], medium: ["", "中"], low: ["bad", "低"] };
    const it = map[c] || ["", c];
    return `<span class="badge ${it[0]}">${it[1]}</span>`;
  }

  async function showCardChain(cardId) {
    if (!cardId) return;
    $("#ch-card").value = cardId;
    const d = await api(`/api/runs/${state.runId}/legs?card_id=${encodeURIComponent(cardId)}&limit=200&sort=board_time&desc=false`);
    const rows = d.rows;
    $("#ch-detail-panel").style.display = "";
    $("#ch-detail").innerHTML = `<div class="hint" style="margin-bottom:10px">卡号 ${escapeHtml(cardId)} · ${rows.length} 段出行</div>` +
      rows.map((r, i) => `<div class="run-item" style="cursor:default;align-items:flex-start">
        <div style="min-width:34px;color:var(--muted)">${i + 1}</div>
        <div>
          <div class="name">${escapeHtml(r.board_stop_name || "-")} → ${escapeHtml(r.alight_stop_name || "未推算")}</div>
          <div class="meta">${escapeHtml(r.route_id || "")} · ${OD.fmtTime(r.board_time)} 上车 ·
            ${r.alight_time ? OD.fmtTime(r.alight_time) + " 下车" : "-"} ·
            ${escapeHtml(OD.methodLabel(r.alight_method))} · ${escapeHtml(OD.confLabel(r.confidence))}</div>
          ${r.is_transfer ? `<div class="meta">换乘等待 ${OD.fmtDuration(r.transfer_wait_sec)} · 步行 ${OD.fmtNum(r.transfer_walk_m, 0)} m</div>` : ""}
          ${r.uncertain ? '<div class="meta" style="color:var(--warn)">⚠ 该下车站为推算兜底结果，不确定</div>' : ""}
        </div>
      </div>`).join("");
    $("#ch-detail-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ------------------------------------------------------------------ OD
  async function populateFilterSelects() {
    if (!state.runId) return;
    try {
      const f = await api(`/api/runs/${state.runId}/filters`);
      const mk = (sel, allLabel, items, cur, fmt) => {
        sel.innerHTML = `<option value="all">${allLabel}</option>` +
          items.map((x) => `<option value="${escapeHtml(x)}">${escapeHtml(fmt ? fmt(x) : x)}</option>`).join("");
        sel.value = OD.reconcilePeriod(cur || "all", items.concat(["all"]));
        if (!Array.from(sel.options).some((o) => o.value === sel.value)) sel.value = "all";
      };
      mk($("#od-period"), "全部时段", f.periods || [], state.filters.od.period);
      mk($("#od-route"), "全部线路", f.routes || [], state.filters.od.route);
      mk($("#me-period"), "全部时段", f.periods || [], state.filters.me.period);
      mk($("#me-route"), "全部线路", f.routes || [], state.filters.me.route);
    } catch (e) { /* keep existing */ }
  }

  async function loadOd() {
    if (!state.runId) return;
    await populateFilterSelects();
    const period = $("#od-period").value;
    const route = $("#od-route").value;
    const dir = $("#od-dir").value;
    const min = Number($("#od-min").value) || 0;
    state.filters.od = { period, route, dir, min };
    let d;
    try {
      d = await api(`/api/runs/${state.runId}/od` + OD.query({
        period, route: route === "all" ? "" : route,
        direction: dir === "-1" ? "" : dir, min_passengers: min, limit: 200,
        offset: 0, sort: "passengers", desc: true,
      }));
    } catch (e) { toast(e.message, true); return; }
    // The map wants the whole flow picture, not just the visible table page.
    try {
      const mapResp = await api(`/api/runs/${state.runId}/od` + OD.query({
        period, route: route === "all" ? "" : route,
        direction: dir === "-1" ? "" : dir, min_passengers: min,
        limit: 2000, offset: 0, sort: "passengers", desc: true,
        aggregate: "true",
      }));
      state.odCache = mapResp.rows.length ? mapResp.rows : d.rows;
    } catch (e) {
      state.odCache = d.rows;
    }
    const s = d.summary || {};
    const cache = state.odCache.length ? state.odCache : d.rows;
    const topFlow = cache.slice().sort((a, b) => (b.passengers || 0) - (a.passengers || 0))[0];
    const aggPairs = new Set(cache.map((r) => r.board_stop_id + "|" + r.alight_stop_id)).size;
    $("#od-cards").innerHTML = [
      metricCard("OD 客流", OD.fmtNum(s.passengers, 0),
        `${OD.fmtNum(aggPairs, 0)} 个起讫对 · ${OD.fmtNum(s.pairs, 0)} 个细分格`),
      metricCard("最大单向流量", OD.fmtNum(topFlow ? topFlow.passengers : 0, 0),
        topFlow ? `${topFlow.origin_name} → ${topFlow.dest_name}` : ""),
      metricCard("覆盖时段", (s.by_period || []).length, "筛选条件内"),
    ].join("");
    const cols = [
      { key: "origin_name", label: "起点" }, { key: "dest_name", label: "终点" },
      { key: "period", label: "时段" }, { key: "route_id", label: "线路" },
      { key: "direction", label: "方向", fmt: OD.dirLabel },
      { key: "passengers", label: "客流", align: "right", fmt: (v) => OD.fmtNum(v, 0) },
      { key: "uncertain_share", label: "不确定", align: "right", fmt: (v) => OD.fmtPct(v) },
    ];
    renderTable($("#od-list"), cols, d.rows);
    $("#od-count").textContent = `${d.total} 个 OD 对`;
    renderOdMap(state.odCache);
    $("#od-legend").innerHTML = "";
    $("#od-legend").appendChild(buildLegend(state.odCache));
  }

  function buildLegend(rows) {
    const max = Math.max(...rows.map((r) => Number(r.passengers) || 0), 1);
    const wrap = document.createElement("div");
    wrap.innerHTML = `<div class="hint" style="margin-bottom:7px">线宽 = 客流强度（最大 ${OD.fmtNum(max, 0)} 人次）</div>`;
    const ramp = document.createElement("div");
    ramp.style.cssText = "display:flex;height:12px;border-radius:6px;overflow:hidden;margin-bottom:5px";
    OD.rampLegend().forEach((s) => {
      const seg = document.createElement("div");
      seg.style.cssText = `flex:1;background:${s.color}`;
      ramp.appendChild(seg);
    });
    wrap.appendChild(ramp);
    const labels = document.createElement("div");
    labels.style.cssText = "display:flex;justify-content:space-between;font-size:11px;color:var(--muted)";
    labels.innerHTML = "<span>低</span><span>高</span>";
    wrap.appendChild(labels);
    return wrap;
  }

  const TILE_PROVIDERS = [
    {
      name: "Esri 街道",
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
      maxZoom: 19,
      attribution: "Tiles &copy; Esri",
    },
    {
      name: "OSM 标准",
      url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    },
    {
      name: "Carto 暗色",
      url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
      subdomains: "abcd", maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    },
  ];

  function ensureMap() {
    if (state.map) return state.map;
    const map = L.map("map", { zoomControl: true, attributionControl: true })
      .setView([30.685, 104.055], 12);
    // try providers in order: some networks can reach one CDN but not another
    let idx = 0;
    const addTiles = () => {
      const p = TILE_PROVIDERS[idx];
      const layer = L.tileLayer(p.url, {
        maxZoom: p.maxZoom, minZoom: 9,
        subdomains: p.subdomains || "abc",
        attribution: p.attribution,
        errorTileUrl: "",
      });
      let failed = 0;
      layer.on("tileerror", () => {
        failed += 1;
        if (failed >= 3 && idx < TILE_PROVIDERS.length - 1) {
          map.removeLayer(layer);
          idx += 1;
          addTiles();
        }
      });
      layer.addTo(map);
      state.tileLayer = layer;
      state.tileProvider = p.name;
    };
    addTiles();
    state.map = map;
    return map;
  }

  function buildLegend(rows) {
    const max = Math.max(...rows.map((r) => Number(r.passengers) || 0), 1);
    const wrap = document.createElement("div");
    wrap.innerHTML = `<div class="hint" style="margin-bottom:7px">线宽 = 客流强度（最大 ${OD.fmtNum(max, 0)} 人次）</div>`;
    const ramp = document.createElement("div");
    ramp.style.cssText = "display:flex;height:12px;border-radius:6px;overflow:hidden;margin-bottom:5px";
    OD.rampLegend().forEach((s) => {
      const seg = document.createElement("div");
      seg.style.cssText = `flex:1;background:${s.color}`;
      ramp.appendChild(seg);
    });
    wrap.appendChild(ramp);
    const labels = document.createElement("div");
    labels.style.cssText = "display:flex;justify-content:space-between;font-size:11px;color:var(--muted)";
    labels.innerHTML = "<span>低</span><span>高</span>";
    wrap.appendChild(labels);
    return wrap;
  }

  function renderOdMap(rows) {
    const map = ensureMap();
    rows = rows || [];
    Object.values(state.layers).forEach((l) => { try { map.removeLayer(l); } catch (e) {} });
    state.layers = {};
    const stops = {};
    const group = L.layerGroup();
    const n = Number($("#od-topn").value) || 120;
    const top = rows.slice(0, n);
    // scale colour/width against the full filtered set so a slider change
    // rescales honestly instead of re-normalising to the shown subset
    const maxP = Math.max(...rows.map((r) => Number(r.passengers) || 0), 1);
    top.forEach((r) => {
      const o = [Number(r.board_lat), Number(r.board_lon)];
      const dd = [Number(r.alight_lat), Number(r.alight_lon)];
      if (!isFinite(o[0]) || !isFinite(dd[0])) return;
      stops[r.board_stop_id] = { lat: o[0], lon: o[1], name: r.origin_name };
      stops[r.alight_stop_id] = { lat: dd[0], lon: dd[1], name: r.dest_name };
      const t = (Number(r.passengers) || 0) / maxP;
      const line = L.polyline([o, dd], {
        color: OD.colorHex(t), weight: 1.5 + 5.5 * Math.pow(t, 0.6),
        opacity: 0.72, lineCap: "round",
      });
      line.bindPopup(`<b>${escapeHtml(r.origin_name)} → ${escapeHtml(r.dest_name)}</b><br>
        客流 ${OD.fmtNum(r.passengers, 0)} 人次 · ${escapeHtml(r.period)}<br>
        线路 ${escapeHtml(r.route_id)} ${OD.dirLabel(r.direction)}<br>
        不确定占比 ${OD.fmtPct(r.uncertain_share)}`);
      group.addLayer(line);
    });
    const stopGroup = L.layerGroup();
    if ($("#od-show-stops").checked) {
      Object.keys(stops).forEach((sid) => {
        const s = stops[sid];
        L.circleMarker([s.lat, s.lon], {
          radius: 4, color: "#7fb2ff", weight: 1, fillColor: "#2b3a55",
          fillOpacity: 0.8,
        }).bindPopup(`${escapeHtml(s.name)}<br><small>${escapeHtml(sid)}</small>`)
          .addTo(stopGroup);
      });
    }
    group.addTo(map);
    stopGroup.addTo(map);
    state.layers["od"] = group;
    state.layers["stops"] = stopGroup;
    if (top.length) {
      const bounds = L.latLngBounds(top.flatMap((r) => [
        [Number(r.board_lat), Number(r.board_lon)],
        [Number(r.alight_lat), Number(r.alight_lon)]]).filter((p) => isFinite(p[0]) && isFinite(p[1])));
      if (bounds.isValid()) map.fitBounds(bounds.pad(0.12));
    }
    setTimeout(() => map.invalidateSize(), 50);
    $("#map-hint").textContent =
      `显示前 ${top.length} 条流向 · 点击线条查看明细 · 底图：${state.tileProvider || "-"}`;
  }

  // ------------------------------------------------------------------ 指标
  async function loadMetrics() {
    if (!state.runId) return;
    await populateFilterSelects();
    const period = $("#me-period").value;
    const route = $("#me-route").value;
    const dir = $("#me-dir").value;
    let flow, sections, board, metrics;
    try {
      const q = OD.query({ period, route: route === "all" ? "" : route,
        direction: dir === "-1" ? "" : dir, limit: 200 });
      flow = await api(`/api/runs/${state.runId}/flow${q}`);
      sections = await api(`/api/runs/${state.runId}/sections${q}`);
      board = await api(`/api/runs/${state.runId}/stop-board${OD.query({ period, limit: 300 })}`);
      metrics = await api(`/api/runs/${state.runId}/metrics`);
    } catch (e) { toast(e.message, true); return; }
    const m = metrics.metrics || {};
    const s = flow.summary || {};
    const sec = sections.rows || [];
    const maxLF = sec.length ? Math.max(...sec.map((r) => Number(r.load_factor) || 0)) : 0;
    const overCap = sec.filter((r) => Number(r.load_factor) > 1).length;
    $("#me-cards").innerHTML = [
      metricCard("最大断面客流", OD.fmtNum(s.peak_load, 0), `定员 ${m.seat_capacity || "-"} 人`),
      metricCard("最高满载率", OD.fmtPct(maxLF), overCap ? `${overCap} 个断面超载` : "无超载",
        maxLF > 1 ? "tone-bad" : maxLF > 0.8 ? "tone-warn" : "tone-good"),
      metricCard("平均满载率", OD.fmtPct(m.avg_load_factor), "全线平均"),
      metricCard("站点数", board.rows.length, "含上客与下客"),
    ].join("");
    // load factor by route
    const byRoute = s.by_route || [];
    $("#me-load").innerHTML = barList(byRoute.map((r) => ({
      name: r.route_id, value: r.peak_load, text: OD.fmtPct(r.peak_load_factor),
      tone: r.peak_load_factor > 1 ? "bad" : r.peak_load_factor > 0.8 ? "warn" : "",
    })), {});
    $("#me-load").insertAdjacentHTML("beforeend",
      `<div class="hint" style="margin-top:10px">柱长 = 峰值断面客流；右侧 = 峰值满载率</div>`);
    // stop board/alight
    // aggregate by physical stop across periods, then keep the top stops
    const byStop = new Map();
    (board.rows || []).forEach((r) => {
      const name = r.stop_name || r.stop_id;
      const cur = byStop.get(name) || { name, b: 0, a: 0 };
      cur.b += Number(r.boardings) || 0;
      cur.a += Number(r.alightings) || 0;
      byStop.set(name, cur);
    });
    const merged = Array.from(byStop.values())
      .sort((x, y) => (y.b + y.a) - (x.b + x.a))
      .slice(0, 24)
      .map((x) => ({
        name: x.name, value: x.b + x.a,
        text: `↑${OD.fmtNum(x.b, 0)} ↓${OD.fmtNum(x.a, 0)}`,
      }));
    $("#me-stops").innerHTML = merged.length
      ? '<div class="hint" style="margin-bottom:8px">↑ 上车 / ↓ 下车（同一站名跨时段合并）</div>' + barList(merged)
      : '<div class="empty">暂无站点客流</div>';
    const cols = [
      { key: "route_id", label: "线路" }, { key: "direction", label: "方向", fmt: OD.dirLabel },
      { key: "period", label: "时段" }, { key: "stop_name", label: "断面起点" },
      { key: "from_stop", label: "起始站编号" },
      { key: "avg_load", label: "平均断面客流", align: "right", fmt: (v) => OD.fmtNum(v, 0) },
      { key: "peak_load", label: "峰值", align: "right", fmt: (v) => OD.fmtNum(v, 0) },
      { key: "load_factor", label: "满载率", align: "right", fmt: (v) => OD.fmtPct(v) },
      { key: "trips", label: "班次数", align: "right" },
    ];
    renderTable($("#me-sections"), cols, sec.slice(0, 200));
    $("#me-sections").insertAdjacentHTML("afterbegin", sec.length > 200 ?
      '<div class="hint" style="padding:8px 12px">仅显示前 200 个断面，请用筛选缩小范围</div>' : "");
    renderQuality(m);
  }

  function renderQuality(m) {
    const gps = m.gps || {};
    const acc = m.accuracy || {};
    const methods = m.alight_methods || {};
    const methodRows = Object.keys(methods).map((k) => ({
      name: OD.methodLabel(k), value: methods[k],
    }));
    $("#me-quality").innerHTML = `
      <div class="cards" style="grid-template-columns:repeat(4,1fr)">
        ${metricCard("GPS 输入", OD.fmtNum(gps.rows_in, 0), `保留 ${OD.fmtNum(gps.rows_out, 0)}`)}
        ${metricCard("漂移点剔除", OD.fmtNum(gps.drift_points_removed, 0), "速度/距离异常")}
        ${metricCard("重复刷卡", OD.fmtNum(m.taps_duplicate, 0), "同卡同车窗口内")}
        ${metricCard("无法匹配", OD.fmtNum(m.taps_invalid, 0), "无车辆停站记录")}
      </div>
      ${acc.available ? `<div class="cards" style="grid-template-columns:repeat(4,1fr)">
        ${metricCard("下车站准确率", OD.fmtPct(acc.alight_stop_accuracy), "严格同站")}
        ${metricCard("邻近站容差", OD.fmtPct(acc.alight_stop_accuracy_adjacent), `误差 ≤ ${OD.fmtNum(acc.adjacent_tolerance_m, 0)} m`)}
        ${metricCard("确定结果准确率", OD.fmtPct(acc.alight_stop_accuracy_confident), `${OD.fmtPct(acc.confident_share)} 为确定`)}
        ${metricCard("下车时间 MAE", OD.fmtDuration(acc.alight_time_mae_sec), "对照真值")}
        ${metricCard("重复刷卡召回", OD.fmtPct(acc.duplicate_recall), "真值重复被识别")}
      </div>` : ""}
      <div class="split">
        <div><div class="hint" style="margin-bottom:8px">推算方式分布</div>${barList(methodRows)}</div>
        <div><div class="hint" style="margin-bottom:8px">告警</div>
          ${(m.warnings || []).length ? (m.warnings || []).map((w) => `<div class="warn-box">${escapeHtml(w)}</div>`).join("") : '<div class="hint">无</div>'}
        </div>
      </div>`;
  }

  // ------------------------------------------------------------------ 对比
  function renderComparePicker() {
    // populated in refreshRuns()
  }

  async function runComparison() {
    const checked = $$("#cmp-select input:checked").map((c) => c.value);
    if (checked.length < 2) { toast("请至少选择两次运行", true); return; }
    try {
      const d = await api(`/api/compare?ids=${checked.join(",")}`);
      renderComparison(d.runs);
    } catch (e) { toast(e.message, true); }
  }

  function renderComparison(items) {
    const metricDefs = [
      ["match_rate", "上车站匹配率", "pct", 0.97, 0.9],
      ["alight_rate", "下车站推算率", "pct", 0.95, 0.8],
      ["uncertain_rate", "不确定占比", "pct", null, null, true],
      ["transfers", "换乘次数", "num"],
      ["od_total", "OD 客流", "num"],
      ["od_pairs", "OD 对数", "num"],
      ["alight_stop_accuracy", "下车站准确率", "pct", 0.85, 0.7],
      ["alight_stop_accuracy_adjacent", "邻近站容差准确率", "pct", 0.9, 0.8],
      ["alight_stop_accuracy_confident", "确定结果准确率", "pct", 0.9, 0.8],
      ["transfer_f1", "换乘 F1", "num"],
      ["alight_time_mae_sec", "下车时间 MAE(s)", "num"],
      ["max_load", "最大断面客流", "num"],
      ["avg_load_factor", "平均满载率", "pct"],
      ["elapsed_sec", "运行用时(s)", "num"],
    ];
    const head = items.map((it) => `<th>${escapeHtml(it.run.name)}<div style="font-weight:400;color:var(--muted);font-size:10px">
      ${escapeHtml((it.run.finished_at || it.run.created_at || "").replace("T", " ").slice(0, 16))}</div></th>`).join("");
    const rows = metricDefs.map(([key, label, kind]) => {
      const vals = items.map((it) => it.metrics[key]);
      const best = pickBest(vals, key === "uncertain_rate" ? "min" : key === "alight_time_mae_sec" ? "min" : "max");
      const cells = vals.map((v, i) => {
        let text = v === null || v === undefined ? "-" : kind === "pct" ? OD.fmtPct(v) : OD.fmtNum(v);
        return `<td class="num"${i === best ? ' style="color:var(--accent-2);font-weight:650"' : ""}>${escapeHtml(text)}</td>`;
      }).join("");
      return `<tr><td>${label}</td>${cells}</tr>`;
    }).join("");
    const paramKeys = ["transfer_window_min", "transfer_walk_max_m", "dup_tap_window_sec",
      "boarding_tap_tolerance_sec", "use_return_anchor", "use_hop_prior", "seat_capacity"];
    const paramRows = paramKeys.map((k) => {
      const vals = items.map((it) => {
        const p = it.params || {};
        return p[k] === undefined ? "-" : p[k];
      });
      const same = vals.every((v) => String(v) === String(vals[0]));
      return `<tr><td>${paramLabel(k)}</td>${vals.map((v) =>
        `<td${same ? ' style="color:var(--muted)"' : ' style="color:var(--warn)"'}>${escapeHtml(String(v))}</td>`).join("")}</tr>`;
    }).join("");
    const topTables = items.map((it) => `<div style="flex:1;min-width:260px">
      <div class="hint" style="margin-bottom:6px">${escapeHtml(it.run.name)} · Top OD</div>
      <table class="grid"><thead><tr><th>OD</th><th style="text-align:right">客流</th></tr></thead><tbody>
      ${(it.top_od || []).map((r) => `<tr><td>${escapeHtml(idx0(r))}</td><td class="num">${OD.fmtNum(r.p, 0)}</td></tr>`).join("") || '<tr><td colspan="2">-</td></tr>'}
      </tbody></table></div>`).join("");
    $("#cmp-result").innerHTML = `
      <div class="panel"><div class="head"><h2>指标对比</h2></div>
        <div class="body tight"><div class="table-wrap"><table class="grid">
          <thead><tr><th>指标</th>${head}</tr></thead><tbody>${rows}</tbody></table></div></div></div>
      <div class="panel"><div class="head"><h2>参数差异</h2><div class="spacer"></div>
        <span class="hint">黄色表示参数不同</span></div>
        <div class="body tight"><div class="table-wrap"><table class="grid">
          <thead><tr><th>参数</th>${head}</tr></thead><tbody>${paramRows}</tbody></table></div></div></div>
      <div class="panel"><div class="head"><h2>主要 OD 差异</h2></div>
        <div class="body"><div style="display:flex;gap:16px;flex-wrap:wrap">${topTables}</div>
        <div class="hint" style="margin-top:10px">提示：OD 差异较大时，通常是时间阈值或换乘判定改动导致出行链重新拼接。</div>
        </div></div>`;
  }

  function idx0(r) { return r[0] !== undefined ? `${r[0]}→${r[1]}` : (r.origin_name + "→" + r.dest_name); }

  function pickBest(vals, mode) {
    let best = -1, bestV = null;
    vals.forEach((v, i) => {
      const n = Number(v);
      if (v === null || v === undefined || !Number.isFinite(n)) return;
      if (bestV === null || (mode === "min" ? n < bestV : n > bestV)) { bestV = n; best = i; }
    });
    return best;
  }

  function paramLabel(k) {
    return (state.meta && state.meta.param_labels && state.meta.param_labels[k]) || k;
  }

  // ------------------------------------------------------------------ 参数
  const PARAM_UNITS = {
    hop_prior_decay: "站", anchor_bonus: "分",
    prior_w_od: "权重", prior_w_alight: "权重", prior_w_dist: "权重",
    gps_speed_max_kmh: "km/h", gps_drift_max_m: "m", stop_snap_radius_m: "m",
    dwell_speed_kmh: "km/h", min_dwell_sec: "s", boarding_tap_tolerance_sec: "s",
    boarding_search_sec: "s", dup_tap_window_sec: "s", transfer_window_min: "min",
    transfer_walk_max_m: "m", return_anchor_gap_min: "min",
    walk_speed_mps: "m/s", timing_slack_sec: "s", max_ride_min: "min",
    seat_capacity: "人", chain_link_window_min: "min",
    hop_prior_decay: "", anchor_bonus: "", prior_w_od: "", prior_w_alight: "",
    prior_w_board: "", prior_w_route: "", prior_w_dist: "",
  };
  const PARAM_GROUPS = [
    ["GPS 清洗与停站识别", ["gps_speed_max_kmh", "gps_drift_max_m", "stop_snap_radius_m",
      "dwell_speed_kmh", "min_dwell_sec"]],
    ["刷卡匹配", ["boarding_tap_tolerance_sec", "boarding_search_sec", "dup_tap_window_sec"]],
    ["出行链与换乘", ["transfer_window_min", "transfer_walk_max_m",
      "return_anchor_gap_min", "timing_slack_sec", "max_ride_min",
      "chain_link_window_min", "use_return_anchor", "use_hop_prior"]],
    ["先验兜底（高级）", ["hop_prior_decay", "anchor_bonus", "prior_w_od",
      "prior_w_alight", "prior_w_dist"]],
    ["运营指标", ["seat_capacity"]],
  ];

  function renderParams() {
    const grid = $("#pm-grid");
    grid.innerHTML = PARAM_GROUPS.map(([title, keys]) => {
      const controls = keys.map((k) => paramControl(k)).join("");
      return `<div style="grid-column:1/-1;margin-top:4px">
        <div class="hint" style="margin-bottom:6px;color:#a9b6d3">${title}</div>
        <div class="param-grid">${controls}</div></div>`;
    }).join("");
    $$("#pm-grid input, #pm-grid select").forEach((el) => {
      el.onchange = () => {
        const k = el.dataset.key;
        if (el.type === "checkbox") state.params[k] = el.checked;
        else {
          const v = Number(el.value);
          if (Number.isFinite(v)) state.params[k] = v;
        }
      };
    });
    renderPeriods();
    $("#pm-hint").textContent = "改动参数后点击「用当前参数运行」，结果会作为一次新的运行保存，可在方案对比中查看差异。";
    syncParamsDatasets();
    $("#pm-reset").onclick = () => {
      state.params = JSON.parse(JSON.stringify(state.meta.default_params));
      renderParams();
      toast("已恢复默认参数");
    };
    $("#pm-run").onclick = () => { $("#pm-submit").click(); };
    $("#pm-add-period").onclick = () => {
      state.params.time_periods.push({ name: "新时段", start: "09:00", end: "10:00" });
      renderPeriods();
    };
  }

  function paramControl(k) {
    const v = state.params[k];
    const label = paramLabel(k);
    if (typeof v === "boolean") {
      return `<div class="param param-toggle"><input type="checkbox" data-key="${k}" ${v ? "checked" : ""}>
        <label>${escapeHtml(label)}</label></div>`;
    }
    const unit = PARAM_UNITS[k] || "";
    const step = Number.isInteger(v) ? 1 : 0.1;
    return `<div class="param"><label>${escapeHtml(label)}</label>
      <div class="row"><input type="number" data-key="${k}" value="${v}" step="${step}">
        <span class="unit">${escapeHtml(unit)}</span></div></div>`;
  }

  function renderPeriods() {
    const ps = state.params.time_periods || [];
    $("#pm-periods").innerHTML = ps.map((p, i) => `<div class="period-row" data-i="${i}">
      <input type="text" value="${escapeHtml(p.name)}" data-f="name" placeholder="时段名">
      <input type="text" value="${escapeHtml(p.start)}" data-f="start" placeholder="05:00">
      <input type="text" value="${escapeHtml(p.end)}" data-f="end" placeholder="09:00">
      <button class="ghost" data-del="${i}" title="删除">✕</button>
    </div>`).join("");
    $$("#pm-periods input").forEach((el) => {
      el.onchange = () => {
        const i = Number(el.closest(".period-row").dataset.i);
        const f = el.dataset.f;
        state.params.time_periods[i][f] = el.value;
      };
    });
    $$("#pm-periods button").forEach((b) => {
      b.onclick = () => {
        state.params.time_periods.splice(Number(b.dataset.del), 1);
        renderPeriods();
      };
    });
  }

  function syncParamsDatasets() {
    if (!state.datasets.length) return;
    const kinds = ["taps", "gps", "stops", "truth"];
    kinds.forEach((k) => {
      const sel = $("#pm-ds-" + k);
      if (!sel) return;
      const items = state.datasets.filter((d) => d.kind === k);
      const cur = sel.value;
      sel.innerHTML = (k !== "truth" ? "" : '<option value="">不提供</option>') +
        items.map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${OD.fmtNum(d.n_rows, 0)} 行)</option>`).join("");
      if (cur && items.some((d) => d.id === cur)) {
        sel.value = cur;
      } else if (items.length) {
        // default to the most recent dataset (list is newest-first), so a
        // fresh upload is picked automatically -- including the truth file
        sel.value = items[0].id;
      }
    });
  }

  async function submitRun() {
    const datasets = {
      taps: $("#pm-ds-taps").value,
      gps: $("#pm-ds-gps").value,
      stops: $("#pm-ds-stops").value,
      truth: $("#pm-ds-truth").value || null,
    };
    const missing = ["taps", "gps", "stops"].filter((k) => !datasets[k]);
    if (missing.length) { toast("请先选择刷卡、GPS 和站点数据集", true); return; }
    const name = $("#pm-run-name").value.trim() ||
      `推算 ${new Date().toLocaleString("zh-CN", { hour12: false }).slice(5, 16).replace(/[/]/g, "-")}`;
    const btn = $("#pm-submit");
    btn.disabled = true;
    try {
      const r = await api("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, params: state.params, datasets }),
      });
      toast("已提交推算任务");
      await refreshRuns();
      selectRun(r.run.id);
      watchRun(r.run.id);
      $("#pm-progress-wrap").style.display = "block";
      $("#pm-progress").style.width = "1%";
      switchPage("dashboard");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ bind
  function bindButtons() {
    $("#btn-run").onclick = () => switchPage("params");
    $("#pm-submit").onclick = submitRun;
    $("#btn-demo-load").onclick = async () => {
      const btn = $("#btn-demo-load");
      btn.disabled = true;
      try {
        const r = await api("/api/demo/run", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "演示推算 " + new Date().toLocaleTimeString("zh-CN", { hour12: false }) }),
        });
        toast("演示推算已提交");
        await refreshRuns();
        await refreshDatasets();
        renderDatasetList();
        selectRun(r.run.id);
        watchRun(r.run.id);
        switchPage("dashboard");
      } catch (e) {
        toast(e.message, true);
      } finally { btn.disabled = false; setTimeout(() => { btn.disabled = false; }, 500); }
    };
    $("#modal-close").onclick = () => $("#modal-back").classList.remove("show");
    $("#modal-back").onclick = (ev) => { if (ev.target.id === "modal-back") $("#modal-back").classList.remove("show"); };
    $("#mt-refresh").onclick = () => loadMatching();
    $("#mt-route").onchange = () => { loadTaps(0); loadEvents(0); };
    $("#mt-status").onchange = () => loadTaps(0);
    $("#ch-refresh").onclick = () => loadLegs(0);
    $("#ch-method").onchange = () => loadLegs(0);
    $("#ch-uncertain").onchange = () => loadLegs(0);
    $("#ch-card").onkeydown = (e) => { if (e.key === "Enter") loadLegs(0); };
    $("#ch-export").onclick = () => download(`/api/runs/${state.runId}/export?name=legs&fmt=csv`);
    $("#ch-detail-close").onclick = () => { $("#ch-detail-panel").style.display = "none"; };
    $("#od-refresh").onclick = () => loadOd();
    $("#od-period").onchange = () => loadOd();
    $("#od-route").onchange = () => loadOd();
    $("#od-dir").onchange = () => loadOd();
    $("#od-min").onchange = () => loadOd();
    $("#od-export").onclick = () => download(`/api/runs/${state.runId}/export?name=od_matrix&fmt=csv`);
    $("#od-show-stops").onchange = () => renderOdMap(state.odCache);
    $("#od-topn").oninput = (e) => { $("#od-topn-val").textContent = e.target.value; };
    $("#od-topn").onchange = () => renderOdMap(state.odCache);
    $("#me-refresh").onclick = () => loadMetrics();
    $("#me-period").onchange = () => loadMetrics();
    $("#me-route").onchange = () => loadMetrics();
    $("#me-dir").onchange = () => loadMetrics();
    $("#me-export").onclick = () => download(`/api/runs/${state.runId}/export?name=sections&fmt=csv`);
    $("#cmp-run").onclick = runComparison;
    $("#run-select").onchange = (e) => selectRun(e.target.value);
  }

  function download(url) {
    if (!state.runId) { toast("请先选择一次运行", true); return; }
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
