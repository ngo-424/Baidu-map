# 任务 B1：百度 POI 数据层接口与离线验收说明

本文记录当前实现的 B1 工程边界。它不是百度真实采集报告，也不表示三类设施目录完整。

## 可复用接口

```python
from app.poi.service import collect_pois

result = await collect_pois(request, provider, runtime)
```

`PoiCollectRequest` 只允许 BD09LL、固定的分析窗口参数和 `market`、`pharmacy`、`primary_school` 三类。`RuntimeConfig` 保存本次运行的独立预算、类别额度、截止时间、QPS 和在线授权信息；密钥不属于请求或结果。

结果的 `queryStatus` 表示计划执行状态，`catalogCompleteness` 永远为 `unverified`。`pois` 只放合法 UID、名称、坐标且确定性分类为 accepted 的条目；`reviewCandidates` 保存冲突、类别政策存疑和证据不足项；`quarantine` 保存缺少必要字段、非法坐标和窗口外项。每条记录都保留检索格、关键词和页码来源。

## 离线命令

从 `backend` 目录运行：

```powershell
& .\.venv\Scripts\python.exe -m tools.poi_collect --mode plan `
  --config tools/poi-example.json --output .tmp/poi-plan

& .\.venv\Scripts\python.exe -m tools.poi_collect --mode replay `
  --config tools/poi-example.json `
  --fixtures tests/fixtures/poi/collection.json `
  --output .tmp/poi-replay-<run-id>
```

`plan` 只生成固定范围、4×4 检索格、关键词、页数和配置哈希，网络请求为 0。`replay` 只读取带 `source: synthetic` 的人工夹具，生成标准 JSON、CSV、覆盖、指标、脱敏检查和验收说明。

当前合成夹具包含跨检索词重复 UID、分页记录、缺失 UID、生鲜超市、北门和未知类别证据，用于验证去重、分类待核、排除和隔离逻辑。夹具不含真实百度 UID 或社区数据。

## 请求与分页规则

每个 1300 米检索格使用 BD09LL 圆形检索，半径为 925 米；16 个格与六个固定关键词（菜市场、农贸市场、菜场、药店、药房、小学）生成 96 个序列。每个序列最多 8 页、每页最多 20 条。首页全部完成后按固定顺序轮转后续页；重复页、变化总数、提前空页和百度保护上限会写入查询覆盖并降低状态。

同一供应方 UID 只保留一个实体，合并所有来源；位置或分类冲突进入待核，不随机覆盖。不同 UID 不自动合并，只按规范化名称、地址和 20 米候选规则标记疑似重复。

## 在线入口的安全闸门

`live-smoke` 和 `live-collect` 要求显式授权、配置哈希匹配、正数 POI QPS、有效时区运行窗口、独立持久化账本和未使用的运行 ID。账本在每次网络发送前记录保守预算占用；超时或无法确认的发送仍占额度。配额、权限、限流、账本失败、取消和截止会停止后续发送。真实入口不会从现有等时圈实验账本借用额度。

本轮未运行在线入口。未确认 AK、POI QPS、账号总额度和错峰窗口前，默认拒绝联网。

## 验收结果

当前离线 B1 测试覆盖：

- 计划边界、4×4 格角覆盖、BD09LL 米制范围裁剪；
- 短页继续分页、重复页、总数变化、空页、保护性上限；
- 合法和非法坐标、业务错误、非 POI 响应、取消、预算最后一次和重复运行；
- 分类接受／待核／排除、同 UID 冲突、跨 UID 疑似重复和附属入口；
- 结果固定字段、CSV 公式转义、凭据扫描、原子产物和不可覆盖的输出目录。

真实百度调用次数：0。B1 完成只表示离线工程能力交付；B2 设施展示与独立步行核验、B3 候选盲区及 B4 端到端体检仍需单独确认口径和授权。
