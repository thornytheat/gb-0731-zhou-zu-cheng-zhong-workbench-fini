# 轴组称重工作台

园区地磅稽核员可复核两轮道触发、轴组和静态磅单。样例包含冲突标定、去抖回弹、多轮争配和最新磅单冲突。

## 启动

```sh
docker compose up --build
```

打开 `http://localhost:18420/`。接口文档在 `/docs`，健康检查在 `/api/health`。容器用命名卷保存历史。需要隔离新数据库时，可用其他 Compose 项目名启动，端口仍为 18420。

本地 Python 3.11 或 3.12 可执行下列命令。

```sh
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 18420
```

环境变量 `WEIGH_DB` 和 `WEIGH_SAMPLE_DIR` 可覆盖本地数据库和受信输入目录，默认使用 `var/weigh.sqlite3` 和 `sample_data/`。

## 演示

1. 首页默认读取晚班脏样例预览，点击「运行样例」归档。核对 50 原始、22 有效、3 重复、25 隔离。
2. 暂停触发日志滚动。点击「重跑」，批次号和计数保持一致。点击「继续滚动」，可见「重算核验一致」事件。
3. 左右轮载选择 `0007`，检查轴组及 10% 和 20% 的偏载。切到「整车磅单勾稽」，检查实际差额和复秤原因。`0023` 的差额为 0 kg，但仍需复秤。
4. 「导出复核包」下载归档 ZIP。切换「南门校验 · 对照采样」可查看 28 条全有效、7 车次全核平的对照结果。
5. 打开「补传水位工作台」（页脚入口，地址 `/backfill`），提交 `sample_data/backfill-pack-a.csv` 与 `backfill-pack-b.csv`。查看 14 条通道的连续时段、待补空档和安全继续处理起点；重复提交同一文件会提示幂等、不新增水位。

## 补传水位

晚班控制器断网后，晚到的补传记录带着旧采样时刻回来。工作台不做"收到一条就抬水位"，而是按车次 × 轮道把观测合并为连续段：水位只承认从观测包络起点开始、没有空档的前缀，空档即待补时段，安全起点取所有通道最早空档的左端点。连续窗按最大轴距 12 m 与车次速度推算，正常跨轴组间隔不会被误报成空档。

- 补传原行逐行保留，坏行隔离但不影响同包；受影响车次合并流水后从去抖、轴组到勾稽整体重算。
- 车次对外结论按版本固化，重算只追加新版本并给出字段差异，旧版本不会被静默改写。
- 补传包以内容 SHA-256 为幂等键，同包重跑返回 `idempotent=true`，不新增水位、补传行或结论版本。
- 接口：`GET /api/backfill/overview`、`POST /api/backfill/packages`（multipart 上传）、`GET /api/backfill/packages/{package_id}`。

## 模块入口

`GET /api/intake`、`/api/calibration`、`/api/debounce`、`/api/axles`、`/api/balance`、`/api/reconcile` 均支持 `sample=dirty` 或 `sample=clean`，也可传 `run_id` 读取归档。`POST /api/batches` 接收 `{"sample":"dirty"}`，`GET /api/batches` 列出归档。批次号下的 `/rerun` 和 `/export` 分别用于重算与下载。

定义在 [数据字典](docs/DATA-DICTIONARY.md)，模块与样例断言在 [深度登记](docs/DEPTH.md)。样例按文件做有界分析，轴距按车次档案恒速估计。

```sh
docker compose exec -T app pytest -q
```
