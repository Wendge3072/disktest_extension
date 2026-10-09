# disktest_extension

Ada-Store / CSAL 的 fio 实验配置与启动脚本。新增实验检验：**前台写入和 compaction 带宽较低时，主动 GC 是否仍会损害 QLC 前台读取。**

| 目录 | 内容 |
| --- | --- |
| `gc_inf_onBW` | 原 disktest 的 GC 与 compaction 带宽实验，原文件保留 |
| `gc_sdisk` | 原 disktest 的 GC 相关测试，原文件保留 |
| `jitter` | 原 disktest 的抖动测试，原文件保留 |
| `gc_inf_onRead` | 新增低速写入 + QLC 随机读取 + 主动 GC 干扰实验 |

原三个目录来自 [Wendge3072/disktest](https://github.com/Wendge3072/disktest)，提交 `8c053a082ebeecc83875bfbe1abea6aeeb29a23c`。`UPSTREAM.json` 记录每个原文件的 SHA256。新的 Git 仓库不包含上游 `.git`、虚拟机镜像、认证信息或实验结果。

## 新实验的默认配置

| 项目 | 设置 |
| --- | --- |
| Guest 设备 | `/dev/vda`，必须是 Ada-Store/CSAL FTL 设备 |
| 写负载 | 512 MiB/s 目标，64 KiB randwrite，QD128，1 job |
| 写范围 | offset=97%，size=io_size=320 GiB |
| 读负载 | 4 KiB randread，QD32，1 job；`READ_IOPS` 显式指定 |
| 候选读范围 | offset=2560 GiB，size=1024 GiB |
| 单次时长 | 600 秒；读写并行、分别统计 |
| GC 时序 | 开始前 T=0；运行240秒后 T=920；运行600秒时恢复 T=0 |

600秒目标写量300 GiB小于320 GiB工作集，复用原低速写点的设计。脚本检查容量、读写范围不重叠及目标写量不超过工作集。改变时间时需要一起调整范围。例如120秒预热加600秒测量会写360 GiB，不能继续使用320 GiB范围；可统一采用 `WRITE_OFFSET=96% WRITE_SIZE=400G` 并重新检查容量。

这是一次 GC 前后配对的**现象定位**。不同读取强度可逐次运行，但连续测量不是独立重复。正式 A/B 重复需要匹配初始盘状态。

## 运行前需要已有的环境

- 247 宿主机已运行 SPDK vhost 和单个 FTL0；TLC 为 P4510，QLC 为 P5316。此仓库不格式化 SSD、不创建 FTL、不启动 QEMU。
- Guest 安装 fio；当前环境验证目标为 CentOS 7、Bash 4、fio 3.7。新 Guest 脚本不要求 Python 3；本机分析器要求 Python 3。
- 宿主机的 RPC 可用，Guest SSH 可从宿主机连接。`start_gc_host.sh` 使用一次 SSH 主连接，不保存密码。
- 读区已写入，并确认主要位于 QLC。**未写过的 LBA 可能直接返回零，不能当作真实 QLC 读取。** Guest 脚本能检查地址范围，但不能仅凭 fio 配置证明数据驻留位置。
- 已产生适合 GC 的无效数据，但整个测量期间 free bands 仍高于实际必要 GC 触发线。当前代码的默认 START limit 为5；阈值0只关闭额外主动GC。
- 结束旧 `gc_inf_onBW` 的 Guest/Host 控制任务后再运行新实验，不能同时让两套脚本修改 FTL0 阈值。

若要沿用原随机预处理，可显式运行 `gc_inf_onRead/prepare.sh`。它只调用原前三个一小时随机写、最后1800秒的4-job写及600秒等待，不运行后续16点扫描。该流程写入整个FTL地址范围，耗时约3小时40分钟，不自动执行，也不保证候选读区已完整覆盖或驻留QLC。

## 两端启动

把本仓库放到 Guest 的 `/root/disktest_extension`，并在宿主机也保存一份。下面示例 `READ_IOPS=25000` **只是命令示例，不是 P5316 的实测能力**；正式实验应在相同写负载、读范围和主动GC关闭条件下校准，再选择固定绝对IOPS。

在 Guest：

```bash
cd /root/disktest_extension/gc_inf_onRead
RUN_ID=read-gc-001 READ_IOPS=25000 bash start.sh
```

Guest 先生成 job 和请求文件，然后等待宿主机控制器。相同 RUN_ID 的结果目录不会被覆盖。

在 247 宿主机的仓库目录：

```bash
cd /path/to/disktest_extension/gc_inf_onRead
sudo env RUN_ID=read-gc-001 \
  SSH_PORT=32001 \
  KNOWN_HOSTS=/home/nly/csal/ada-fig5-guest-20261009.known_hosts \
  SPDK_DIR=/home/nly/csal/git/spdk \
  SPDK_LOG=/home/nly/csal/git/spdk/log/smoke_test \
  bash start_gc_host.sh
```

`KNOWN_HOSTS` 示例是当前247上已核验的主机密钥文件，不是qcow2镜像。另一台环境应指定自己的已验证文件，或先完成默认SSH配置下的主机密钥验证。当前QEMU镜像位于 `/home/nly/csal/img/`；实验控制脚本只连接已运行的Guest。

两端 RUN_ID 必须相同。宿主机读取本次请求、先设置T=0并等待基线过渡，再通知Guest启动fio；之后根据本次 `started.tsv` 的时间设置正阈值和恢复0。控制器失败会通知Guest停止本次fio；控制器收到退出信号后会尝试恢复自己开启的主动GC阈值。

**阈值0不会立即取消已在途的GC。** 固定过渡等待不构成“无GC”证据，需要检查实际日志。T=920也不保证GC一定执行，还受无效比例和候选band条件约束。4096只适合宽松阈值的干扰诊断，不能单凭该诊断声称原920策略损害读取。

## 仅生成配置，不启动fio

在Guest或者本机上，为不存在的本机块设备提供准确/模拟容量，只生成文件：

```bash
RUN_ID=preview-001 READ_IOPS=25000 \
  DEVICE_BYTES=14541900267520 DRY_RUN=1 \
  bash gc_inf_onRead/start.sh
```

`DRY_RUN=1` 不发I/O、不连接Guest、不调用RPC。结果目录仍要求唯一。生成后可在有fio的环境执行 `fio --parse-only profile.fio` 验证参数解析；parse-only不证明盘状态、QLC路由或GC正在执行。

## 结果与分析

Guest 的 `gc_inf_onRead/results/RUN_ID/` 保存：

- `profile.fio`、`request.tsv`：实际参数和地址范围；
- `started.tsv`、`finished.tsv`、`events.tsv`：本次启动/结束与执行事件；
- `result.json`：fio整次读写报告；
- 独立读写的1秒BW/IOPS日志和完成延迟直方图。

宿主机默认在 `$SPDK_DIR/log/read_gc_results/RUN_ID/` 保存本次阈值事件、RPC输出与SPDK日志截取；可通过 `HOST_RESULTS_ROOT` 指定根目录。Guest还保存 `fio.version`。将两端结果复制到本机后，例如执行：

```bash
python3 gc_inf_onRead/analyze.py \
  /local/results/guest/read-gc-001 \
  /local/results/host/read-gc-001 \
  --spdk-utc-offset=-04:00
```

时区参数必须与该次宿主机日志相符；上述 `-04:00` 对应当前247的日志时区，不能用本机北京时间解释。分析使用预先固定的等长窗口：开始后60–240秒与GC切换后60–240秒。需要真实阈值事件与完整日志；不会把整次fio JSON中的p99当作GC前后的p99。

分析器支持已确认的fio3.7、`log_hist_coarseness=0` 的1856桶纳秒完成延迟日志：合并窗口内各完整区间的IO计数后计算p99/p99.9，排除跨窗口边界的直方图区间，记录实际覆盖时间。未知版本/布局保留缺失值，不自动猜测延迟单位。算法与格式依据 [fio3.7 stat.h](https://github.com/axboe/fio/blob/fio-3.7/stat.h)、[stat.c](https://github.com/axboe/fio/blob/fio-3.7/stat.c)、[iolog.c](https://github.com/axboe/fio/blob/fio-3.7/iolog.c)。

结论至少需要：前窗口实际无GC、后窗口确实有GC、前台写与compaction低于原阈值、free bands始终高于必要线，以及真实QLC读路由证据。尾延迟应由测量窗口内合并直方图计算，不能平均每秒p99。没有路由统计时，分析必须保留QLC驻留“未验证”状态。

推荐主图对齐展示读取p99/实际IOPS、GC活动、前台写/compaction三个时间序列。成功运行脚本不等于motivation已经成立。

## 保留原实验入口

旧脚本依赖相对路径，从其所在目录执行，例如：

```bash
cd gc_inf_onBW
bash start64.sh
```

这保持原配置与行为，也保持其中固定 `/dev/vda` 的设备设置。原Fig5仍需要独立的宿主机GC控制；本仓库新增控制器服务 `gc_inf_onRead/start.sh` 的握手协议，不能直接配原 `start64.sh`。

## 检查

```bash
python3 -m unittest discover -s tests -v
```

自动检查使用临时文件、模拟fio/SSH/RPC，不访问真实SSD。另记录fio3.7的parse-only兼容性检查；没有提交性能结果。

参数语义依据 [fio 3.7 官方 HOWTO](https://github.com/axboe/fio/blob/fio-3.7/HOWTO)。原目录来源与修改边界见 `UPSTREAM.json`。
