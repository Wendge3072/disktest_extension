# 不同 Read IOPS 下的 GC 延迟影响

每档分别运行基线 fio 和 GC fio，两份 JSON 各自记录整个测量阶段。比较公式：`100 × (p99_GC / p99_baseline − 1)`，p50、p95、p99.9、p99.99 与均值同样取完整 JSON。不会使用 fio 3.7 损坏的分时直方图，也不需要为了本次实验升级 Guest fio。

## 每个档位的 IO 顺序

在执行一次 [准备与读区验证](PREWARM.md) 后开始。下面的秒数相对于该档基线开始。

| 时间 | Host | Guest |
|---|---|---|
| 开始前 | T=0 检查空间；不足时 T=920 回收，达到目标后立即恢复 0 | 不发 IO |
| -30 到 0 秒 | 检查至少 2048 个空闲 band，以及最近 GC 写入为 0 | 等待 |
| 0–60 秒 | T=0 | 4K 随机读 + 64K 顺序写，ramp，不记录测量统计 |
| 60–240 秒 | T=0 | 同样读写，JSON 测量 180 秒 |
| 240–245 秒 | T=0 | 上一份 fio 结束，时间余量 |
| 245–300 秒 | T=0，保留 GC 候选数据 | 不发 IO |
| 300–305 秒 | T=920 | 等待第二份 fio 开始 |
| 305–365 秒 | T=920 | 同一读取档位、范围、QD；下一个写区，ramp；Host 在测量前检查 GC 活动 |
| 365–545 秒 | T=920 | 同样读写，另一份 JSON 测量 180 秒 |
| 545–550 秒 | 随后 T=0 | fio 结束，时间余量 |
| 550–850 秒 | 下档空间准备；足够时维持 T=0 | 等待 |

原三小时随机预热只执行一次；每档不重新预热，不格式化、不 unmap。读取始终覆盖 0–预热终点减去 1700 GiB 的已验证区域。每次 fio 分配预热终点之后单独的 128 GiB 写区，按计划向后推进；每次总目标写量含 ramp 为 120 GiB，不会绕回同一写区。

默认一轮六档共用 1536 GiB 写区；自动预热 80%，约 10.58 TiB，完整读区约 8.92 TiB。三轮共需 4608 GiB，自动预热约 66.0%，读区约 7.07 TiB。这里使用实际 FTL 容量，而非取整的 13.2 TiB；最终整数边界以 settings.tsv / plan.tsv 为准。预热、核验和扫描的矩阵参数必须相同。

顺序前进使正式写入数据较少在 TLC 内被重复覆盖，减少由小写区反复覆盖引起的写入/compaction 差异；是否平衡仍由日志验证。原全盘随机预热可能已映射这些逻辑地址，所以“往后写”也会使原 QLC 版本失效，不能称为从未写过的新地址，也不能保证没有必要 GC。

两端在发 IO/RPC 前检查完整扫描的读区保护、每份写量预算和全部写区是否超出逻辑容量。空间准备达到目标后马上停止 GC；两阶段间隙主要维持 T=0，避免在没有前台读取时先耗尽候选数据。

## 两个窗口，各一条命令

先更新两端仓库，结束旧 fio 和旧 GC 控制器，保证 FTL 状态日志每秒持续输出。

Host：

```bash
sudo env SPDK_LOG=/home/nly/csal/git/spdk/log/gc_influnce_on_read \
  bash /home/nly/csal/git/disktest_extension/gc_inf_onRead/start_gc_sweep.sh read-sweep-001
```

把 Host 打印的 **整行 Guest 命令** 复制到 Guest 执行。它带有完全相同的开始时间、档位、地址与时长。Guest 只运行 fio，Host 只运行 GC RPC；两端需要时钟同步，没有相互传递失败状态，一端报错时在另一端 Ctrl+C。

默认六档：`1000 5000 10000 25000 50000 100000` IOPS；默认只跑一轮，正式扫描约 **1 小时 27 分钟**，不包含准备。需要三轮时在 Host 命令中增加 `REPEATS=3`，约 **4 小时 17 分钟**；偶数轮逆序。`READ_IOPS_LIST="1000 5000 10000 25000"` 可缩短首次验证。

常用设置都加在 Host 的 `sudo env ...` 中，由打印的命令传递给 Guest：

| 设置 | 默认值 | 含义 |
|---|---|---|
| WRITE_MIB / WRITE_RW | 512 / write | 顺序写；需要随机写时设 randwrite |
| WRITE_SIZE | 128G | 每份 fio 独立的写区；含 ramp 的写量需小于它 |
| READ_QD / WRITE_QD | 32 / 128 | 固定队列深度 |
| RAMP_SECONDS / MEASURE_SECONDS | 60 / 180 | 每份 fio 的先行运行与统计时间 |
| RECOVER_SECONDS | 300 | 档位间空间准备时长上限；不会整段强制运行 GC |
| BASELINE_SETTLE / GC_SETTLE_SECONDS | 30 / 60 | 基线静止等待／两份 fio 之间的间隙 |
| MIN_FREE_BANDS | 2048 | 为缓存未下沉数据和测量写入预留空间；不是必要 GC 的触发阈值 |
| GC_THRESHOLD | 920 | 主动 GC 的写负载门槛 |

2048 按当前约 1 GiB band、1700 GiB 缓存余量及 120 GiB 测量总写量留出较大空间。更换 band/cache 大小或加长运行时要重新评估。若在开始时间前无法取得空间、基线 GC 未静止或 GC 对照未实际搬运，Host 退出，须停止另一端，不能把无效测试当作有效结果。不要为了继续测试盲目降低空间要求。

## 分析

取回两端目录后：

```bash
python3 gc_inf_onRead/analyze_sweep.py \
  /local/results/guest/read-sweep-001 \
  /local/results/host/read-sweep-001 \
  --spdk-utc-offset=-04:00
```

输出 `pairs.csv`（每档两组实际 IOPS、p99 等及升高比例）、`by-iops.csv`（同档重复汇总）和 `sweep-summary.json`（状态与有效性）。真实 p99 来自独立 JSON 的 `read.clat_ns.percentile`；有 fio 桶宽精度，不平均每秒 p99。默认实际读写速度与目标相差超过 ±5% 的组保留观察值但不进入匹配负载曲线。高负载饱和点会显示实际 IOPS，不能伪称达到目标。

仅基线实际无 GC、两组均未到必要 GC 边界、GC 对照持续搬运、日志覆盖充分且两端计划一致的对照进入正式升高比例。分析同时报告写入/compaction 是否平衡、GC 强度与空闲空间。连续运行会改变盘状态和剩余无效数据；时间重复不等于重新恢复相同状态的独立重复。若 GC 候选数据耗尽，不能保证后续档位有效，需要依据日志补充准备，而不是放宽有效性检查。

预览全部档位，不发 IO：

```bash
DRY_RUN=1 DEVICE_BYTES=14541900267520 bash gc_inf_onRead/start_sweep.sh preview-sweep
```

旧 `start_gc_manual.sh` / `start_gc_host.sh` 是单次混合阶段方式，不能生成这次的多档 p99 对照，不要同时启动。
