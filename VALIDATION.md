# 验证记录

日期：2026-10-09。没有执行真实读写压力测试，没有发送 SPDK RPC。

- 上游 `main` 经 `git ls-remote` 核验仍为 `8c053a082ebeecc83875bfbe1abea6aeeb29a23c`。
- 原三个目录的44个文件与 `UPSTREAM.json` 所记 SHA256 一致。
- 新增4个 Bash 脚本通过本机语法检查及当前Guest的GNU Bash 4.2语法检查。
- 自动化检查使用临时普通文件、模拟 fio、模拟 SSH、模拟 RPC，覆盖地址/写预算错误、控制器等待超时、正常两端握手、fio失败以及RPC失败后的阈值恢复。
- 延迟分析检查使用明确标记的合成日志，覆盖Unix epoch对齐、按IO计数合并直方图、窗口边界排除、未知布局拒绝，不包含实测性能结果。
- 最终11项自动化检查全部通过。
- 在当前CentOS7 Guest的真实 `fio-3.7` 上，仅执行 `fio --parse-only`。`w512-r25000.job`、`w640-r25000.job` 的解析退出码均为0；验证配置保存在Guest `/tmp` 的临时目录中，检查后删除。parse-only不发磁盘I/O，不证明读区驻留QLC或GC条件满足。

复查命令：

```bash
python3 -m unittest discover -s tests -v
```

真正开始测量前，应完成读区驻留核验、GC状态检查和匹配初始状态准备。`READ_IOPS=25000`是示例参数，尚无对应P5316能力校准结果。
