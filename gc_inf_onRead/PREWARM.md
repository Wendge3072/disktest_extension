# 准备 QLC 读区

当前 247 使用单个 FTL0：P4510 为 TLC cache，P5316 为 QLC base，Guest 设备为 `/dev/vda`。先准备数据，再独立确认读取来源，最后启动正式测试。

## IO 顺序

| 顺序 | Guest 操作 | 目的 |
| --- | --- | --- |
| 1 | 原 `rnd.job`、`rnd2.job`、`rnd3.job`、`rnd64.job` 随机写预热 | 沿用原来的预热；已完成可跳过 |
| 2 | 顺序写完整个 FTL 逻辑盘 | 写过每个地址，避免未写地址直接返回零 |
| 3 | 再顺序覆盖 0–1700 GiB | 推动上一轮留在 TLC 的尾部数据下沉，最新写入集中在低地址 |
| 4 | 停止 fio，等待 600 秒 | 给 compaction 等后台工作留出时间 |
| 5 | 完整读取 2–3 TiB 并核对统计 | 独立确认正式读区实际位于 QLC |

顺序写按完整字节数结束，不按固定秒数截断。全盘主 job 使用 64 KiB 请求，盘尾不足 64 KiB 时另用 4 KiB 请求补齐；当前容量的尾部为 48 KiB。

当前 TLC 数据区为 1746 GiB，compaction 在满 chunk 达到 80% 时启动，静止时可能保留约 1397 GiB 数据。1700 GiB 前缀覆盖提供推进余量，但等待时间和写入顺序本身不能证明读区已经下沉，需要随后检查。

全盘逻辑覆盖会改变原随机预热的失效数据和空闲 band 分布，不保证保留原 GC 状态，也不保证每个物理 band 只含最新数据。正式测试仍要观察实际 GC 搬运。

## 1. 关闭旧测试，准备盘内数据

宿主机和 Guest 都保存更新后的仓库。先结束旧 fio 与旧 GC 控制器，然后在宿主机将额外主动 GC 门槛设为 0：

```bash
cd /home/nly/csal/git/spdk
sudo scripts/rpc.py bdev_ftl_set_bggc_comp_threshold -b FTL0 -t 0
```

必要 GC 和正常 compaction 仍按 FTL 规则运行。`prepare.sh` 不替你修改 GC 门槛。

在 Guest 执行：

```bash
cd /root/disktest_extension/gc_inf_onRead
PREPARE_RUN_ID=prepare-seq-001 SKIP_RANDOM=1 bash prepare.sh
```

`SKIP_RANDOM=1` 适用于已经完成原随机预热的状态；需要从随机预热开始时去掉它。每个结果名称只能使用一次。脚本保存实际 fio 配置、JSON、阶段记录和操作命令；错误或短写会停止流程。

`completed.json` 表示规定写入已完成，其 `qlc_verified=false` 表示读取来源还需要检查。Guest 使用现有 fio、Bash 和 Python 2.7 或 Python 3。

## 2. 宿主机独立检查读区

在宿主机仓库生成对应配置，再执行完整读区检查：

```bash
cd /path/to/disktest_extension
READ_IOPS=25000 DEVICE_BYTES=14541900267520 \
  LOG_PREFIX=/tmp/read-gc-profile \
  bash gc_inf_onRead/make_job.sh /tmp/read-gc-profile-seq-001.fio

sudo python3 gc_inf_onRead/verify_qlc_range.py \
  --profile /tmp/read-gc-profile-seq-001.fio \
  --known-hosts /home/nly/csal/ada-fig5-guest-20261009.known_hosts \
  --output-dir /home/nly/csal/git/spdk/log/qlc_range_verification/seq-001
```

配置文件和结果目录必须不存在。`25000` 是待校准示例；检查时实际使用 1 MiB 顺序读覆盖完整 1 TiB 读区，正式实验仍使用 4 KiB 随机读。

工具先等后台静止，再对照 fio、FTL 分类计数与两块物理盘计数，要求用户数据完整来自 QLC；地址映射元数据可以访问 TLC。它不修改 GC 门槛、不提交 fio 写入，正常元数据写回会单独记录。扫描最长 7200 秒，等待后台静止最长 600 秒，可用 `--scan-timeout`、`--wait-timeout` 调整。

**退出 0，并且 `verification.json` 中 `passed=true`、`complete=true`，再进入正式实验。** 结果留在宿主机即可，不需要复制 JSON 到 Guest。这个独立检查工具仍使用已审计的 SPDK 构建；重编译后须重新核对读取分类。

## 3. 只在宿主机启动正式实验

```bash
cd /path/to/disktest_extension/gc_inf_onRead
sudo bash start_gc_host.sh read-gc-001 25000 512
```

宿主机通过一次 SSH 直接启动 Guest 的 fio，并按返回的启动时间执行 T=0 → 240 秒 T=920 → 600 秒 T=0。不需要提前在 Guest 启动等待脚本。连接、路径等可通过 `SSH_PORT`、`KNOWN_HOSTS`、`SPDK_DIR`、`SPDK_LOG`、`GUEST_DIR` 配置，见 [README.md](../README.md)。

默认写区为 0–320 GiB，读区为 2–3 TiB。正式测试不再自动检查 JSON、二进制哈希或 FTL UUID。检查后应保护读区：不向其写入或 unmap；不要重新初始化设备。重新预热、更换或覆盖读区后，再做一次独立检查。

## 仅预览预热配置

```bash
cd /root/disktest_extension/gc_inf_onRead
DRY_RUN=1 DEVICE_BYTES=14541900267520 \
  PREPARE_RUN_ID=prepare-preview-001 bash prepare.sh
```

此命令不发 IO。高级配置中的 `PREFIX_SIZE_BYTES`、`READ_FLOOR_BYTES` 单位为整数字节，默认分别为 `1825361100800`、`2199023255552`；修改时须让完整检查的 `--prefix-size-bytes`、`--read-floor-bytes` 保持一致，并重新评估缓存推进余量。
