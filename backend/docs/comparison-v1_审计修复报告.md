# comparison-v1 审计修复与冻结

## 结论

冻结上一轮实验记录及实验工具；算法、Provider、RateGate 和业务 API 均与 `guodingyi-baseline-20260913`（a147c81）一致。远端 main 的 fd3f63a 更新集中在前端，未纳入实验工具分支，也不改变本轮算法基线。

上一轮 1559 次预算预留、1558 次确认发送。自适应事件 761 缺少发送交接证据，继续计入消耗预算。另有均匀算法本地 Provider 异常；历史具体根因未确定，不能由本次注入的文件写入错误倒推其原因。

## 修复范围

- 各阶段 JSON、CSV、Markdown 统一列出预算预留、确认发送、发送未确认、重试和完整响应。`actual_calls` 为传输交接计数，不等价于百度控制台结算计数。
- HTTP 完整响应必须有 HTTP 状态证据，不能把超时结束时间当作完整响应。
- 账本发送前和响应后写入失败均停止后续请求；预留额度不退回。
- 新增独占持久化 `started.marker`，即使启动账本写入失败，重启也不能自动重跑。已有历史启动记录同样阻止重跑。
- 新增纯离线 `tools.comparison_audit`，只向独立目录写更正统计和文件哈希，不改写原始实验。

## 验证

先复现两个失败测试：完整响应字段缺失、启动账本写入失败后可重复启动；修复后通过。发送前写失败、响应后单次和持续写失败、重启、取消、预算与限流等回归全部通过。冻结前完整 Python 回归为 235 项通过，2 条既有依赖弃用提示；XML 记录位于 D:/CodexOutputs/guodingyi-comparison-v1-audit/pytest.xml。

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONPYCACHEPREFIX='D:/CodexCaches/baidu-pycache'
$env:PYTHONPATH='backend'
& D:/CodexCaches/baidu-map-algorithm-venv/Scripts/python.exe -m pytest backend/tests life-circle-algorithm/tests -q -o cache_dir=D:/CodexCaches/live-pytest --basetemp=D:/CodexCaches/freeze-tests
& D:/CodexCaches/baidu-map-algorithm-venv/Scripts/python.exe -m tools.comparison_audit --source D:/CodexOutputs/guodingyi-stability-comparison-20260913 --output D:/CodexOutputs/guodingyi-comparison-v1-audit
```

## 冻结与后续

`comparison-v1_冻结清单.json` 保存原始产物哈希和各阶段更正计数。原始运行源码快照位于原产物的 runtime-source 目录，修复源码则由本次 Git 提交标识，两者不混称。

审计通过后创建不可覆盖的 `comparison-v1` 标签，推送实验工具分支并发 PR。冻结表示记录可追溯，不表示所有历史记录无异常。新边界比较使用独立分支、点集和 1392 次预算，不修改旧账本或移动标签。
