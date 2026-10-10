# 当前vhost的L2P读取位置

核验日期2026-10-10；源码HEAD为`f4df175c817c63f39d8ccc18bda75bb0a14c71c0`。相关L2P/cache/layout源码未修改，源文件时间早于对应.o与binary；没有做可复现重建。

已核验运行vhost与磁盘文件的SHA256均为：

```text
5f5ab19671476a73d1d8fb2ea3a31b55d1bcb0971eec7d671b1f8566d5e3c2d4
```

运行二进制中`ftl_l2p_init`跳转`ftl_l2p_cache_init`，采用cache实现。

- `lib/ftl/ftl_layout.c:112`的`set_region_bdev_nvc`绑定`dev->nv_cache.bdev_desc`与cache channel；`:155–166`为L2P区域调用它。
- `lib/ftl/ftl_l2p_cache.c:502–505`保存这个L2P区域设备；`:258–268`的访问函数直接返回它。
- `:589–592`与`:1248–1251`两条page-in读使用该设备。`:707`与`:1234`完成回调归到`FTL_STATS_TYPE_L2P`。其余3处L2P统计调用是同设备写完成。
- `lib/ftl/ftl_nv_cache_io.h:40–51`直接向传入设备提交；VSS分支`:149–159`还断言它是nv_cache设备。没有发现L2P读走base/QLC的例外。

当前FTL0的cache为P4510/TLC、base为P5316/QLC。因此在其它非USER读取均为0时，QLC总读可归因USER；TLC总读减L2P元数据读才是TLC USER读。完整扫描还要求所有逻辑读都有USER物理读，无其它读者、无GC/compaction混入、无计数异常。

`verify_qlc_range.py`检查磁盘与live executable哈希，保证使用这次审计的构建。更换预期哈希意味着操作者另外完成了同等路由审计；哈希本身不证明新构建的元数据位置。
