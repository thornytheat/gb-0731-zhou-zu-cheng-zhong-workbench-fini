# 轴组称重工作台深度登记

- 规模为中型。园区地磅稽核员按轮道审查晚班触发，确认轴组，交给复秤岗核对异常车次。
- 核心规则是同轮道去抖后，以唯一对侧候选和轴距窗确认车轴，保留缺证依据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 采样验收 | 查看分段、物理行、乱序记录和坏行 | `GET /api/intake` | `app/modules/m1_intake/reader.py` `app/modules/m1_intake/service.py` | `tests/test_intake.py` |
| M2 传感器标定 | 按采样时刻核证，并复核重叠证书 | `GET /api/calibration` | `app/modules/m2_calibration/service.py` `app/modules/m2_calibration/certificates.py` | `tests/test_calibration.py` |
| M3 脉冲去抖 | 核对重送、回弹和冲突记录号 | `GET /api/debounce` | `app/modules/m3_debounce/identity.py` `app/modules/m3_debounce/service.py` | `tests/test_debounce.py` |
| M4 轴组重建 | 选择车次查看轮载、轴距与隔离证据 | `GET /api/axles` | `app/modules/m4_axles/pair.py` `app/modules/m4_axles/spacing.py` | `tests/test_axles.py` |
| M5 偏载判级 | 核查左重、右重和临界偏载 | `GET /api/balance` | `app/modules/m5_balance/thresholds.py` `app/modules/m5_balance/service.py` | `tests/test_balance.py` |
| M6 整车磅单勾稽 | 审核磅单版次、差额和待复秤原因 | `GET /api/reconcile` | `app/modules/m6_reconcile/tickets.py` `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py` |
| M7 称重批次 | 提交样例、重算、追溯并下载复核包 | `POST /api/batches` | `app/modules/m7_batches/service.py` `app/modules/m7_batches/export.py` `app/store.py` | `tests/test_batches.py` |
| M8 补传水位 | 提交补传包、查看连续时段/待补空档/安全起点与结论版本 | `GET /api/backfill/overview` `POST /api/backfill/packages` | `app/modules/m8_backfill/segments.py` `app/modules/m8_backfill/service.py` `app/modules/m8_backfill/handler.py` | `tests/test_backfill.py` `tests/test_backfill_segments.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | `certificates.csv` 为 GB18030 | 中文证书读不出 | 严格解码回退 | `app/modules/m1_intake/decode.py` | `tests/test_calibration.py::test_gb18030_recovers_certificate_headers_and_ids` |
| TRAP-02 | `dirty-triggers.csv` 有 BOM | 记录号列失配 | 识别并剥离 BOM | `app/modules/m1_intake/decode.py` | `tests/test_intake.py::test_bom_header_uses_record_id` |
| TRAP-03 | 脏触发第 3–4 行为一条记录 | 备注被切成两行 | CSV 表增量迭代并保存起止物理行 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_multiline_note_keeps_physical_source_range` |
| TRAP-04 | 脏触发第 13 行重复表头 | 多算一条触发 | 表头分段并从触发账剔除 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_repeated_header_and_blank_do_not_inflate_raw_count` |
| TRAP-05 | 脏触发第 26 行多一列 | 字段错位 | 隔离原行 | `app/modules/m1_intake/service.py` | `tests/test_intake.py::test_extra_column_is_quarantined_at_source_line` |
| TRAP-06 | 脏样例有全角小数和千分位 | 数值丢失 | 先规格化，再精确解析 | `app/values.py` | `tests/test_intake.py::test_full_width_decimal_and_thousands_grouping` |
| TRAP-07 | 脏触发第 24 行是 N/A | 当作零重 | 原值留在隔离表 | `app/values.py` | `tests/test_intake.py::test_missing_sentinel_never_contributes_zero` |
| TRAP-08 | 车次 0007 | 前导零丢失 | 全流程文本车次 | `app/modules/m6_reconcile/tickets.py` | `tests/test_reconcile.py::test_latest_semantic_duplicate_and_inclusive_tolerance` |
| TRAP-10 | 明细叠加半分边界轮载 | 合计或边界失准 | Decimal 逐轮修正至 0.01 kg 后汇总 | `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py::test_decimal_rollup_matches_independently_summed_axles` |
| TRAP-11 | 脏触发和证书的时区写法不同 | 生效边界错判 | UTC 比较 | `app/values.py` | `tests/test_calibration.py::test_mixed_offsets_align_to_same_utc_clock` |
| TRAP-13 | 晚班触发次序错位 | 丢脉冲或误去抖 | 标记乱序，按时间排序去抖 | `app/modules/m3_debounce/service.py` | `tests/test_intake.py::test_out_of_order_sensor_row_is_retained_and_marked` |
| TRAP-14 | 脏触发第 45–46 行同记录号不同载荷 | 后写覆盖 | 两条都隔离且互相引用 | `app/modules/m3_debounce/identity.py` | `tests/test_debounce.py::test_same_id_conflict_rejects_both_records` |
| TRAP-15 | 脏触发混用斤、吨和千克 | 混加原始数值 | 按单位统一到千克再标定 | `app/values.py` | `tests/test_calibration.py::test_affine_calibration_keeps_mixed_unit_weights_consistent` |
| TRAP-18 | 空文件、空行、仅表头 | 输出无意义结果 | 三个明确失败码 | `app/modules/m1_intake/reader.py` | `tests/test_intake.py::test_empty_blank_and_header_only_have_distinct_errors` |
| TRAP-19 | 0019 两个左轮争同一右轮 | 贪心配对凭空造轴 | 双向唯一才配对 | `app/modules/m4_axles/pair.py` | `tests/test_axles.py::test_nearest_pair_would_invent_axle_when_candidates_conflict` |
| TRAP-23 | 0007 触发恰好在前证书右端 | 一轮发给两版证书 | 生效区间左闭右开 | `app/modules/m2_calibration/service.py` | `tests/test_calibration.py::test_exact_handoff_uses_new_certificate_and_no_double_match` |
| TRAP-25 | `certificates.csv` 两张右轮证书叠期 | 随机取一张 | 隔离触发并带两张证书行 | `app/modules/m2_calibration/service.py` | `tests/test_calibration.py::test_overlap_isolates_with_both_certificates` |
| TRAP-26 | 补传记录带旧时刻晚到 | 收到一条就抬高水位，空档被当成已完整 | 只承认无空档的观测包络前缀，水位停在首个空档前 | `app/modules/m8_backfill/segments.py` | `tests/test_backfill_segments.py::test_late_arrival_does_not_lift_watermark_across_gap` |
| TRAP-27 | 同轮道跨轴组事件间隔本就很大 | 固定小连续窗把正常间隔全报成空档 | 连续窗按最大轴距与车次速度推算 | `app/modules/m8_backfill/segments.py` | `tests/test_backfill.py::test_backfill_advances_watermark_and_exposes_safe_resume_point` |
| TRAP-28 | 补传后局部替换单条触发 | 去抖锚点与轴配对失真 | 按整车次合并流水后从去抖到勾稽整体重算 | `app/modules/m8_backfill/service.py` | `tests/test_backfill.py::test_submit_package_keeps_original_rows_and_recomputes_affected_passes` |
| TRAP-29 | 重算覆盖已发布车次结论 | 对外结论被静默改写 | 结论按版本固化，只追加新版本并给字段差异 | `app/modules/m8_backfill/handler.py` `app/store.py` | `tests/test_backfill.py::test_published_conclusion_is_versioned_not_silently_rewritten` |
| TRAP-30 | 同一补传包重复提交 | 重复处理、水位重复累加 | 内容 SHA-256 幂等键 + 事务串行，重跑不新增任何行 | `app/modules/m8_backfill/handler.py` | `tests/test_backfill.py::test_identical_package_rerun_is_idempotent` |

## 对账规则

- 原始触发 = 有效触发 + 重复触发 + 隔离触发。空行、重复表头、磅单异常独立计数。
- 整车差额 = 静态总重 − 确认车轴的左右轮载合计。缺少有效磅单时差额留空。

## 已知不覆盖

- 输入限定配置目录内样例文件，按文件 1 MiB 和 4,000 条逻辑记录封顶。只支持带显式时区偏移和毫秒精度以内的采样时刻。
- 轴距用档案内的车次恒速推算。缺轮、歧义或异常轴距产生现场复秤待办，现场结果需在外部系统处置。
