import {byId, clock, dateTime, element} from "./views.js";

const state = {data: null, busy: false};

async function request(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message ?? `请求失败 ${response.status}`);
  return payload;
}

function showError(text = "") {
  const node = byId("backfill-error");
  node.textContent = text;
  node.hidden = !text;
}

function showNote(text = "") {
  const node = byId("backfill-note");
  node.textContent = text;
  node.hidden = !text;
}

function table(columns, rows) {
  const host = element("div");
  if (!rows.length) {
    host.append(element("p", "暂无记录。", "empty"));
    return host;
  }
  const tableNode = element("table", "", "ledger");
  const head = tableNode.createTHead().insertRow();
  for (const column of columns) {
    const th = element("th", column.label, column.numeric ? "num" : "");
    th.scope = "col";
    head.append(th);
  }
  const body = tableNode.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const column of columns) {
      const value = column.read(row);
      const td = tr.insertCell();
      if (column.numeric) td.className = "num";
      if (value && typeof value === "object" && value.level) {
        td.append(element("i", "", `lamp ${value.level}`), value.text);
      } else {
        td.textContent = value ?? "—";
      }
    }
  }
  host.append(tableNode);
  return host;
}

function renderTimeline(overview) {
  const summary = overview.summary;
  byId("wm-channels").textContent = summary.channels;
  byId("wm-confirmed").textContent = summary.confirmed_channels;
  byId("wm-waiting").textContent = summary.waiting_channels;
  byId("wm-gaps").textContent = summary.gap_segments;
  byId("wm-resume").textContent = overview.safe_resume_at ? clock(overview.safe_resume_at) : "窗口已追平";
  byId("waterline-summary").textContent =
    `${summary.observed_points} 个观测点 · ${overview.waiting_passes.length} 个车次仍待补`;

  const rows = overview.timelines.map((timeline) => {
    const confirmed = timeline.segments.filter((segment) => segment.kind === "confirmed").length;
    return {
      channel: timeline.channel,
      state: timeline.caught_up
        ? {text: "已追平", level: "ok"}
        : {text: `待补 ${timeline.gaps.length} 段`, level: "warn"},
      watermark: clock(timeline.watermark),
      observed: timeline.segments.reduce((total, segment) => total + segment.observed, 0),
      confirmed,
      gaps: timeline.gaps.map((gap) => `${clock(gap.start)} → ${clock(gap.end)}`).join(" ｜ ") || "—",
    };
  });
  byId("timeline-table").replaceChildren(table([
    {label: "通道", read: (row) => row.channel},
    {label: "状态", read: (row) => row.state},
    {label: "完整水位", read: (row) => row.watermark},
    {label: "观测点", read: (row) => row.observed, numeric: true},
    {label: "连续段", read: (row) => row.confirmed, numeric: true},
    {label: "待补空档", read: (row) => row.gaps},
  ], rows));
}

function renderPackages(payload) {
  const rows = payload.packages.map((pkg) => ({
    id: pkg.package_id,
    file: pkg.file_name,
    received: dateTime(pkg.received_at),
    raw: pkg.raw_rows,
    accepted: pkg.accepted_rows,
    rejected: pkg.rejected_rows,
    passes: (JSON.parse(pkg.affected_passes) ?? []).join(" / "),
    sha: pkg.sha256.slice(0, 12),
  }));
  byId("package-table").replaceChildren(table([
    {label: "包号", read: (row) => row.id},
    {label: "文件", read: (row) => row.file},
    {label: "接收时间", read: (row) => row.received},
    {label: "物理行", read: (row) => row.raw, numeric: true},
    {label: "采信", read: (row) => row.accepted, numeric: true},
    {label: "隔离", read: (row) => row.rejected, numeric: true},
    {label: "受影响车次", read: (row) => row.passes},
    {label: "内容指纹", read: (row) => row.sha},
  ], rows));
}

function renderConclusions(payload) {
  const rows = [];
  for (const [passId, versions] of Object.entries(payload.history)) {
    for (const version of versions) {
      rows.push({
        pass_id: passId,
        version: `v${version.version}`,
        published: dateTime(version.published_at),
        status: version.row.status === "matched"
          ? {text: "已核平", level: "ok"}
          : {text: "待复秤", level: "warn"},
        codes: (version.row.codes ?? []).join(" / ") || "—",
        wheel_kg: version.row.wheel_kg,
        difference_kg: version.row.difference_kg ?? "—",
        package: version.package_id ?? "基线",
      });
    }
  }
  byId("conclusion-table").replaceChildren(table([
    {label: "车次", read: (row) => row.pass_id},
    {label: "版本", read: (row) => row.version},
    {label: "发布时间", read: (row) => row.published},
    {label: "结论", read: (row) => row.status},
    {label: "原因码", read: (row) => row.codes},
    {label: "有效轮载 kg", read: (row) => row.wheel_kg, numeric: true},
    {label: "差额 kg", read: (row) => row.difference_kg, numeric: true},
    {label: "来源补传包", read: (row) => row.package},
  ], rows));
}

function paint() {
  if (!state.data) return;
  renderTimeline(state.data.overview);
  renderPackages(state.data);
  renderConclusions(state.data);
  byId("backfill-state").replaceChildren(
    element("i", "", `lamp ${state.data.packages.length ? "ok" : ""}`),
    state.data.packages.length ? `${state.data.packages.length} 个补传包已登记` : "等待补传包",
  );
}

async function refresh() {
  state.data = await request("/api/backfill/overview?sample=dirty");
  paint();
}

byId("backfill-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const input = byId("backfill-file");
  if (!input.files.length) return;
  state.busy = true;
  showError();
  showNote();
  byId("backfill-send").disabled = true;
  try {
    const form = new FormData();
    form.append("sample", "dirty");
    form.append("file", input.files[0], input.files[0].name);
    const result = await request("/api/backfill/packages?sample=dirty", {method: "POST", body: form});
    await refresh();
    if (result.idempotent) {
      showNote("补传包已接收过：本次重跑未新增水位、未重复处理。");
    } else {
      const changed = result.recomputed.filter((item) => item.changed).length;
      showNote(`补传包已登记：${result.package.accepted_rows} 行采信、`
        + `${result.package.rejected_rows} 行隔离，${result.recomputed.length} 个车次重算，`
        + `${changed} 个车次结论发生变化（旧版本已保留）。`);
    }
    input.value = "";
  } catch (error) {
    showError(`${error.message}，请检查后重试。`);
  } finally {
    state.busy = false;
    byId("backfill-send").disabled = false;
  }
});

refresh().catch((error) => showError(error.message));
