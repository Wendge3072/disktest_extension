# disktest_extension

Ada-Store / CSAL 的 fio 实验脚本。新增实验检验：**前台写入和 compaction 带宽较低时，主动 GC 是否仍会影响 QLC 读取。**

| 目录 | 内容 |
| --- | --- |
| `gc_inf_onBW` | 原来的 GC 与 compaction 带宽实验 |
| `gc_sdisk` | 原来的 GC 相关测试 |
| `jitter` | 原来的抖动测试 |
| `gc_inf_onRead` | 低速写入、QLC 随机读取与主动 GC 干扰实验 |

原三个目录来自 [Wendge3072/disktest](https://github.com/Wendge3072/disktest)，提交 `8c053a082ebeecc83875bfbe1abea6aeeb29a23c`；原文件保留，`UPSTREAM.json` 记录来源和 SHA256。仓库不包含虚拟机镜像、认证信息或实验结果。

## 本次默认流程

1. 保留 `gc_inf_onBW` 的三小时随机预热；已完成可跳过。
2. 只顺序写 **0–x% 一遍**，等待 600 秒；不再顺序覆盖全盘或重写前 1700 GiB。
3. 完整核验 **0–顺序预热终点减去 1700 GiB** 的用户读取是否全部来自 QLC。
4. 扫描 `1000、5000、10000、25000、50000、100000` Read IOPS，每档分别运行 GC 关闭/开启的两份 fio，直接从独立 JSON 比较 p99 等分位数。
5. 正式写入从 **顺序预热终点之后**开始，每份 fio 使用独立 128 GiB 写区，顺序向后推进；读取区域始终保持不变。

x 根据所有正式写区预算计算，默认自动策略上限 80%；六档一轮预热约 10.58 TiB，三轮因预留写区更多自动降到约 66%。80% 是初始设置，不是测出的最优值；顺序覆盖更多不保证剩余 GC 数据更多。准备、核验和扫描须使用相同矩阵设置。

默认读为 4 KiB 随机读、QD32，写为 64 KiB 顺序写、QD128、512 MiB/s。每份 fio 60 秒 ramp + 180 秒测量，总目标写量 120 GiB。默认一轮约 1 小时 27 分钟，不包含准备；三轮用 `REPEATS=3`。原三个实验目录保持不变。

[准备步骤和读区验证](gc_inf_onRead/PREWARM.md) · [多档测试流程、配置和分析](gc_inf_onRead/SWEEP.md)

## 两个窗口启动

完成准备和新读区核验后，Host 运行：

```bash
sudo env SPDK_LOG=/home/nly/csal/git/spdk/log/gc_influnce_on_read \
  bash /home/nly/csal/git/disktest_extension/gc_inf_onRead/start_gc_sweep.sh read-sweep-001
```

将 Host 打印的整行命令复制到 Guest。两端各运行一次命令，自动完成所有档位。Host 只控制 GC，Guest 只发 fio；时钟需同步。一端退出时要停止另一端。不要运行旧控制器。

Host 只在空间不足时回收，达到目标后立即关闭主动 GC；基线前默认检查至少 **2048 个空闲 band**及实际 GC 写入归零，GC 对照 ramp 内、测量前检查实际搬运。部分顺序写不能自动保证有足够空闲空间，因为原随机预热选择全盘地址范围，未保证写遍每个地址。必要 GC 的触发阈值仍是 5，2048 是本实验的准备要求。

## 观察读取与 GC

两块 NVMe 由 SPDK 直接操作，宿主机使用 SPDK 的 [iostat.py](https://github.com/spdk/spdk/blob/v22.09/scripts/iostat.py) 查看物理 bdev：

```bash
cd /home/nly/csal/git/spdk
sudo python3 scripts/iostat.py -d -m -i 1 -t 20
```

看后续间隔报表：`nvme0n1` 为 TLC，`nvme1n1` 为 QLC，`FTL0` 为 Guest 逻辑设备。Guest 的 Linux iostat 只能观察逻辑盘；物理盘读量还包含 GC、compaction 和映射读取，不能只凭 QLC 总读量认定全部来自 fio。

`verify_read_path.py` 可做短时随机抽样，`sample_qlc_full_device.py` 可做全盘 32 区均匀抽样，二者均是独立诊断工具，不修改 GC 门槛。随机抽样不能替代完整读区检查。

2026-10-10 在 247 上完成了旧盘状态的 32 区抽样和当时 2.5–3.5 TiB 候选区检查：候选区 82.23% 请求实际读取 QLC，17.77% 请求读取未映射地址后直接返回零；重读时 TLC 与元数据读取均为 0。该事实说明旧 QLC 路径可用，**不能代替本次完整读区核验**。

T=0 只关闭额外主动 GC，必要 GC 仍会执行，也不会立即取消在途 GC。T=920 只允许满足条件的主动 GC，不保证一定发生数据搬运。部分顺序写同样会改变无效数据分布，正式结果必须检查实际 GC 活动及空闲 band 数。

## 结果与分析

Guest：`gc_inf_onRead/results/SWEEP_ID/`，每档分别保存 `r01-iops1000-baseline/` 与 `r01-iops1000-gc/` 等目录。各目录包含实际 fio 配置、参数、版本、启动/完成时间和完整 JSON。Host：`$SPDK_DIR/log/read_gc_results/SWEEP_ID/`，保存同一份计划、每档 GC 控制事件和 SPDK 日志。

将两端结果取回后执行：

```bash
python3 gc_inf_onRead/analyze_sweep.py \
  /local/results/guest/read-sweep-001 \
  /local/results/host/read-sweep-001 \
  --spdk-utc-offset=-04:00
```

时区必须对应本次 SPDK 日志。输出 `pairs.csv`、`by-iops.csv` 和 `sweep-summary.json`，包含 p50、p95、p99、p99.9、p99.99、均值、实际 IOPS，以及逐档延迟升高比例和重复间标准差。默认实际读写速度需在目标的 ±5% 内；超过时保留原始数值并标为无效，不纳入匹配 IOPS 的升高比例曲线。

**延迟直接取每次独立 fio 的 JSON `read.clat_ns.percentile`，`ramp_time` 内的 IO 不进入统计。** 不平均每秒 p99，不把整场混合 GC 状态的 JSON 拆成阶段，也不使用 fio 3.7 有半份复制缺陷的分时直方图。当前方式可继续使用 fio 3.7；旧 `analyze.py` 默认拒绝未审计修复的 fio 3.7 直方图。

基线需实际无 GC、两阶段空闲 band 均高于必要 GC 触发线，GC 阶段需持续有 GC，遥测覆盖充分、两端计划一致、两次 fio 和控制器正常结束。脚本成功不等于产生有效对照；未满足条件的档位会附无效原因。

连续扫描会改变无效数据比例和空闲 band 数，三轮属于时间上的重复，不是恢复相同初始盘状态后的独立重复。分析保存这些状态，以及写入和 compaction 是否基本平衡；60 秒 ramp 并不保证盘已进入稳定状态。准备后的读区核验仍是前提，正式阶段没有逐请求的分层读取证明。

## 配置预览与检查

只生成 fio 配置，不发 IO：

```bash
READ_IOPS=25000 DEVICE_BYTES=14541900267520 \
  bash gc_inf_onRead/make_job.sh /tmp/read-gc-preview.fio
```

输出文件须不存在。可以用 `fio --parse-only /tmp/read-gc-preview.fio` 检查解析。

```bash
python3 -m unittest discover -s tests -v
```

自动检查使用临时文件和模拟 fio/SSH/RPC，不访问真实 SSD。原实验仍从各自目录执行，例如 `cd gc_inf_onBW` 后 `bash start64.sh`，并使用原来的宿主机 GC 控制。
