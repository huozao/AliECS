# Future Sync Pipeline（阶段一骨架）

该目录当前只提供“可读、可导航、可扩展”的主流程骨架，不实现复杂业务。

主流程：
- 拉取（fetch） -> 校验标准化（validate/normalize） -> 写入更新（upsert） -> 回写保护（writeback_guard） -> 回写（writeback）

阶段一行为：
- fetch 返回演示数据
- normalize 做最小规范化
- upsert 只做计数占位
- writeback 默认关闭
