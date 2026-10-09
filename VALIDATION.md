# 验证记录

日期：2026-10-09。没有执行新增读写压力测试或读取路由探测。

首次创建仓库时仅使用模拟RPC；后续为读取核验执行了只读SPDK统计RPC和iostat，没有发阈值设置/开关RPC。

- 上游 `main` 经 `git ls-remote` 核验仍为 `8c053a082ebeecc83875bfbe1abea6aeeb29a23c`。
- 原三个目录的44个文件与 `UPSTREAM.json` 所记 SHA256 一致。
- 新增4个 Bash 脚本通过本机语法检查及当前Guest的GNU Bash 4.2语法检查。
- 自动化检查使用临时普通文件、模拟 fio、模拟 SSH、模拟 RPC，覆盖地址/写预算错误、控制器等待超时、正常两端握手、fio失败以及RPC失败后的阈值恢复。
- 延迟分析检查使用明确标记的合成日志，覆盖Unix epoch对齐、按IO计数合并直方图、窗口边界排除、未知布局拒绝，不包含实测性能结果。
- 首次创建仓库的11项自动化检查全部通过；增加读取核验后，最终28项检查全部通过。
- 在当前CentOS7 Guest的真实 `fio-3.7` 上，仅执行 `fio --parse-only`。`w512-r25000.job`、`w640-r25000.job` 的解析退出码均为0；验证配置保存在Guest `/tmp` 的临时目录中，检查后删除。parse-only不发磁盘I/O，不证明读区驻留QLC或GC条件满足。
- 新增读取核验的17项检查覆盖：全QLC抽样、未映射LBA零返回、TLC驻留、GC混入、元数据扣除、未分类物理读、计数回退、时钟/字段异常、部分fio完成及宿主机全流程模拟。模拟流程也检查专用探测为`randread --readonly`、没有设置类RPC、SSH/采集进程清理与生成的Guest Bash语法。
- 在247实机上，新Python代码编译通过；读取到的真实bdev/FTL统计结构通过校验，包括这版FTL write.errors不含crc的格式。Guest `fio-3.7`对专用64MiB探测job执行`fio --readonly --parse-only`，退出0。
- 实机检查识别到旧`start_gc.sh`控制器PID11163/11165，读取核验会在发探测前拒绝执行。没有停止旧fio，也没有发送新读探测。
- 北京时间21:16的5.312秒统计窗口：FTL0读增量0、P5316读增量0，P4510读约1048.4MiB/s；FTL分类中CMP与L2P读增加、USER读为0。此时Guest仍运行原`rnd64.job`，因此这些后台读不是新实验前台读路由证据。

复查命令：

```bash
python3 -m unittest discover -s tests -v
```

真正开始测量前，应完成读区驻留核验、GC状态检查和匹配初始状态准备。`READ_IOPS=25000`是示例参数，尚无对应P5316能力校准结果。
