# disktest_extension

Ada-Store / CSAL 的 fio 实验脚本。新增实验检验：**前台写入和 compaction 带宽较低时，主动 GC 是否仍会影响 QLC 读取。**

| 目录 | 内容 |
| --- | --- |
| `gc_inf_onBW` | 原来的 GC 与 compaction 带宽实验 |
| `gc_sdisk` | 原来的 GC 相关测试 |
| `jitter` | 原来的抖动测试 |
| `gc_inf_onRead` | 低速写入、QLC 随机读取与主动 GC 干扰实验 |

原三个目录来自 [Wendge3072/disktest](https://github.com/Wendge3072/disktest)，提交 `8c053a082ebeecc83875bfbe1abea6aeeb29a23c`；原文件保留，`UPSTREAM.json` 记录来源和 SHA256。仓库不包含虚拟机镜像、认证信息或实验结果。

## 实验配置

| 项目 | 默认值 |
| --- | --- |
| Guest 设备 | `/dev/vda`，Ada-Store/CSAL 的 FTL0 |
| 写负载 | 512 MiB/s，64 KiB 随机写，QD128，1 job |
| 写范围 | 0–320 GiB |
| 读负载 | 4 KiB 随机读，QD32，1 job；读取 IOPS 在启动时指定 |
| 读范围 | 2048–3072 GiB，即 2–3 TiB |
| 时长 | 600 秒，读写同时运行 |
| GC 控制 | 开始前 T=0；240 秒时 T=920；600 秒时恢复 T=0 |

默认目标写量为 300 GiB，小于 320 GiB 写区。`make_job.sh` 生成实际 fio 配置，检查设备容量、地址范围不重叠、目标写量不超过写区。改变写速度或时长时，需要相应扩大写区，并继续将写入限制在预热覆盖的低地址区。

## 准备一次，再运行正式测试

247 宿主机需要已经运行 SPDK vhost、单个 FTL0 和 QEMU：P4510 为 TLC，P5316 为 QLC。Guest 已安装 fio，两端均保存本仓库，宿主机能通过 SSH 连接 Guest。脚本不创建 FTL、不格式化盘、不启动虚拟机。

先结束旧 fio 和旧 GC 控制脚本。完整准备命令见 [PREWARM.md](gc_inf_onRead/PREWARM.md)，流程是：

1. Guest 执行原随机写预热；已完成时可以跳过。
2. Guest 顺序写完整个逻辑盘，再顺序覆盖前 1700 GiB，最后等待 600 秒。
3. 宿主机独立检查 2–3 TiB 读区是否实际读取 QLC。
4. 检查通过后，正式测试只写低地址区，保护已经确认的读区。

`verify_qlc_range.py` 是准备后独立使用的完整读区检查工具。它通过 Guest 将该区域顺序读完，并对照 fio、FTL 分类计数和物理盘计数；不能把未写地址直接返回零当作 QLC 读取。其检查仍使用已审计的 SPDK 版本，但正式启动不再要求复制 JSON 到 Guest，也不再绑定二进制哈希或 FTL UUID。

不向读区写入或 unmap、设备和数据状态保持一致时，可以改变读取 IOPS、seed 并再次测试。重新初始化、重新预热、更换或覆盖读区后，需要重新检查。这里“读自 QLC”指用户数据；地址映射元数据仍可能访问 TLC。

## 正式启动：只执行宿主机命令

Guest 的默认脚本目录是 `/root/disktest_extension/gc_inf_onRead`。在宿主机的仓库目录执行：

```bash
cd /path/to/disktest_extension/gc_inf_onRead
sudo bash start_gc_host.sh read-gc-001 25000 512
```

三个参数依次是 **结果名称、读取 IOPS、写入 MiB/s**。`25000` 是待校准的示例，不是 P5316 的实测能力。

宿主机先设置 T=0，再通过一次 SSH 直接启动 Guest 的 `start.sh`。Guest 开始 fio 后返回启动时间，宿主机据此计时切换 GC。Guest 直接执行读写，不等待 ready 文件，也没有反复读取请求、失败标记的握手过程。

实际 IO 仍由 Guest 发起，GC RPC 仍由宿主机执行。默认连接 `root@127.0.0.1:32001`；需要修改环境时，可以传入变量：

```bash
sudo env SSH_PORT=32001 \
  KNOWN_HOSTS=/home/nly/csal/ada-fig5-guest-20261009.known_hosts \
  SPDK_DIR=/home/nly/csal/git/spdk \
  SPDK_LOG=/home/nly/csal/git/spdk/log/smoke_test \
  GUEST_DIR=/root/disktest_extension/gc_inf_onRead \
  bash start_gc_host.sh read-gc-002 25000 512
```

`KNOWN_HOSTS` 示例是当前 247 的 Guest 主机密钥文件；其他环境使用自己的 SSH 配置。`SPDK_LOG` 应指向本次运行的 SPDK 日志。当前 QEMU 镜像位于 `/home/nly/csal/img/`，控制脚本只连接已经运行的 Guest。

只需要在 Guest 独立运行同样的读写负载时，也可以直接执行：

```bash
cd /root/disktest_extension/gc_inf_onRead
bash start.sh read-baseline-001 25000 512
```

这种方式不会修改 GC 门槛，需要自行确认宿主机当前的 GC 状态。不要同时启动两个 fio 测试或两个 GC 控制脚本。

宿主机异常退出时会尝试恢复 T=0。断开 SSH 不保证远端 fio 停止；需要中止测试时，在 Guest 执行 `pkill -TERM -x fio`。

## 观察读取与 GC

两块 NVMe 由 SPDK 直接操作，宿主机使用 SPDK 的 [iostat.py](https://github.com/spdk/spdk/blob/v22.09/scripts/iostat.py) 查看物理 bdev：

```bash
cd /home/nly/csal/git/spdk
sudo python3 scripts/iostat.py -d -m -i 1 -t 20
```

看后续间隔报表：`nvme0n1` 为 TLC，`nvme1n1` 为 QLC，`FTL0` 为 Guest 逻辑设备。Guest 的 Linux iostat 只能观察逻辑盘；物理盘读量还包含 GC、compaction 和映射读取，不能只凭 QLC 总读量认定全部来自 fio。

`verify_read_path.py` 可做短时随机抽样，`sample_qlc_full_device.py` 可做全盘 32 区均匀抽样，二者均是独立诊断工具，不修改 GC 门槛。随机抽样不能替代完整读区检查。

2026-10-10 在 247 上完成了旧盘状态的 32 区抽样和当时 2.5–3.5 TiB 候选区检查：候选区 82.23% 请求实际读取 QLC，17.77% 请求读取未映射地址后直接返回零；重读时 TLC 与元数据读取均为 0。该事实说明旧 QLC 路径可用，**尚未验证新预热后的 2–3 TiB 读区**。

T=0 只关闭额外主动 GC，必要 GC 仍会执行，也不会立即取消在途 GC。T=920 只允许满足条件的主动 GC，不保证一定发生数据搬运。全盘顺序写会改变原随机预热后的数据分布，正式结果必须检查实际 GC 活动及空闲 band 数。

## 结果与分析

Guest 结果位于 `gc_inf_onRead/results/RUN_ID/`，保存实际 `profile.fio`、参数 `request.tsv`、启动和完成记录、fio 版本、JSON，以及每秒读写 BW/IOPS 和延迟直方图。这些文件用于保存结果，不再承担两端握手。

宿主机结果默认位于 `$SPDK_DIR/log/read_gc_results/RUN_ID/`，保存阈值事件、RPC 输出和 SPDK 日志。将两端结果复制到本机后执行：

```bash
python3 gc_inf_onRead/analyze.py \
  /local/results/guest/read-gc-001 \
  /local/results/host/read-gc-001 \
  --spdk-utc-offset=-04:00
```

时区须与该次宿主机日志一致；`-04:00` 是当前 247 日志的时区。默认比较 **60–240 秒** 和 **300–480 秒** 两个 180 秒窗口。p99/p99.9 使用窗口内合并的 fio 3.7 延迟直方图计算，不能使用整场 JSON 的 p99，也不能平均每秒 p99。

结论需要前窗口实际无 GC、后窗口实际有 GC，前台写入和 compaction 低于门槛，空闲 band 高于必要 GC 触发线。准备后读区检查证明当时的 QLC 驻留；正式窗口的读取来源和 GC 读取活动仍需证据，脚本运行成功不等于干扰现象成立。

不同读取强度可以连续测试，但连续测试不是独立重复；独立重复需要匹配初始盘状态。

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
