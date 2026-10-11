# 一次部分顺序写准备 QLC 读区

保留原 `gc_inf_onBW` 的前三段全盘随机预热：`rnd.job → rnd2.job → rnd3.job`，各 1 小时，seed 分别为 2252、9917、4026。取消额外的 `rnd64.job`。随后只增加 **0–x% 一遍顺序写**，等待 600 秒；不再顺序覆盖全盘或重写前 1700 GiB。

| 地址区 | 用途 |
|---|---|
| 0–x% | 部分顺序预热，一次完整写入 |
| 0–顺序预热终点减去 1700 GiB | 默认完整读区；实际核验后才能认定来自 QLC |
| 顺序预热终点之后 | 正式写入；各次 fio 使用单独的 128 GiB 区间，向后推进 |

`layout.sh` 根据逻辑容量、IOPS 档位数、重复次数和每份 fio 的写区大小计算边界，预热、核验和两端测试使用同一计算。六档一轮共有 12 份 fio，各目标写量 120 GiB（含 ramp），总目标 1440 GiB；预留 12×128=1536 GiB 地址区。预算允许的最大 x 约 88.7%，**默认自动策略上限为 80%**，给正式写入额外地址余量，并保留部分原随机数据分布；80% 是初始实验设置，不是测出的最优值。六档三轮需预留 4608 GiB，自动减小到约 66.0%。

最大化 x 不能保证 GC 更持久：顺序写产生的无效数据也可能在准备过程中被 GC 回收。因此还需观察无效数据比例、最大 band 无效比例和实际 GC 搬运。数据不足时应调整准备或测量时长，不靠扩大 x 或降低有效性检查强行得到曲线。

1700 GiB 是针对当前 P4510/CSAL 配置选取的缓存推进余量，不是读取来源证明。改缓存大小或 compaction 策略后要重新评估。**原来的随机预热选择全盘逻辑地址范围，但不保证实际写遍每个地址，所以缩小顺序写范围不等于物理盘出现大量空闲空间。** 正式控制器会在必要时主动回收，并检查空闲 band；不能只根据 offset 保证没有必要 GC。

## Guest 准备

先结束旧 fio 和旧 GC 控制器。Host 将主动 GC 门槛设为 0：

```bash
cd /home/nly/csal/git/spdk
sudo scripts/rpc.py bdev_ftl_set_bggc_comp_threshold -b FTL0 -t 0
```

Guest 第一次从原随机预热开始：

```bash
cd /root/disktest_extension/gc_inf_onRead
PREPARE_RUN_ID=prepare-partial-001 bash prepare.sh
```

如果已完成原三段随机预热，保留这个盘状态，只补部分顺序写：

```bash
cd /root/disktest_extension/gc_inf_onRead
SKIP_RANDOM=1 PREPARE_RUN_ID=prepare-partial-001 bash prepare.sh
```

已有相同布局的部分顺序写及本次读区验证时，正式扫描不会再次预热。`completed.json` 只证明规定写量完成，不证明读自 QLC。错误、短写、重复结果目录都会停止。

空间设置集中在 `layout.sh`。默认 `PREPARE_PERCENT=auto`、`CACHE_GUARD_BYTES=1825361100800`（1700 GiB）。可设整数百分比，例如 `PREPARE_PERCENT=80`，或精确字节数 `PREPARE_END_BYTES`。自定义百分比超过正式扫描预算允许的上限时拒绝执行。改变档位、REPEATS、WRITE_SIZE 会改变自动边界，必须在准备、核验、Host、Guest 使用相同设置；不要预热后随意改变矩阵导致读写区域变化。

这里全部使用二进制 TiB/GiB；默认读取长度向下对齐到 1 MiB。

## Host 验证整个读区

核验脚本自动生成相同布局的配置，无需手算终点。检查过程中不要运行正式 fio，也不要开启主动 GC。

```bash
cd /home/nly/csal/git/disktest_extension
sudo bash gc_inf_onRead/verify_prepared.sh partial-001
```

如果准备时指定了 `REPEATS=3` 等矩阵设置，此处也用 `sudo env REPEATS=3 ...` 传递。现有 Guest SSH 认证要可用；known_hosts 只校验服务器身份，不提供认证。此独立工具仍使用已审计的 SPDK 构建，要求 USER 数据全部从 QLC 读取，TLC 读取只能对应 L2P 元数据。必须退出 0，且输出目录下 `verification/verification.json` 中 `passed=true`、`complete=true`，再启动 [多档实验](SWEEP.md)。

默认一轮的整个读区约 8.92 TiB，完整核验会比此前 1 TiB 扫描更耗时；这是证明整个正式读取范围都已映射且位于 QLC 的代价。不能用旧 2–3 TiB 验证或小样本测试替代。

只生成准备配置、不发 IO：

```bash
DRY_RUN=1 DEVICE_BYTES=14541900267520 PREPARE_RUN_ID=preview-partial bash prepare.sh
```
