# 位置获取与 POI 搜索实施计划书（Auto-location）

## 1. 背景与目标

默认 API 模式（`ApiApp`）目前只能手动输入 BD09LL 经纬度或点击地图选点。为让用户以便捷方式确定分析中心，本轮新增：

1. **获取当前位置**：用户点击按钮、授权浏览器定位后，自动填入当前坐标并平移地图。
2. **POI 关键词搜索**：统一输入框输入地点关键词，返回地点列表，点击结果定位到对应位置。

本轮范围为**仅 API 模式**；演示模式（demo）不改动。**道路/门牌地址解析（原方法三）本轮不实现**，但模块设计预留扩展点（见第 8 节）。

## 2. 技术依据

| 项 | 结论 | 来源 |
| --- | --- | --- |
| 定位 | `BMapGL.Geolocation.getCurrentPosition()`，直接返回 BD09LL 坐标、精度和地址分量；失败自动 IP 兜底 | 百度 JSAPI GL 定位指南 |
| POI 检索 | `BMapGL.LocalSearch`，支持 `searchNearby(keyword, center, radius)`，结果含名称、地址、坐标 | 百度 JSAPI GL 类参考（`jsapi_webgl_1_0`） |
| 坐标系 | 两类接口均返回 BD09LL，与后端分析坐标系一致，无需转换，符合“不静默转换”规范 | 项目 N04 文档、百度坐标转换说明 |
| 权限 | 浏览器 AK 需开通 JavaScript API GL 与**地点检索**服务；referer 白名单需含实际来源 | 百度开放平台控制台 |
| 定位安全域 | 浏览器定位要求 HTTPS 或 localhost/127.0.0.1；开发环境满足 | 百度定位指南 |

坐标必须走百度官方接口获得，不使用本地公式转换（官方文档明确“请勿使用其他非官方转换方法”）。

## 3. 方案设计

### 3.1 新增模块

- `src/analysis/location.ts`
  - `locate(api, options?): Promise<LocatedPosition>`：封装 `Geolocation`，含超时保护与状态码映射。
  - `searchPlaces(api, keyword, center, options?): Promise<PlaceResult[]>`：封装 `LocalSearch.searchNearby`，规范化结果、校验坐标。
  - `LocationError`：错误码 `unsupported | permission | timeout | unavailable | invalid | empty | failed`，对应中文提示。
  - 纯函数、SDK 由参数注入，便于离线单测。
- `src/analysis/LocationControls.tsx`
  - 「获取当前位置」按钮 + 统一搜索输入框 + 结果列表 + 状态/错误提示。
  - 地图不可用（AK 缺失或脚本失败）时只显示说明，不影响手输坐标。
- `src/map/baiduMapTypes.ts` 扩展
  - 新增 `BMapGeolocation`、`BMapLocalSearch`、`BMapLocalSearchResult`、`BMapLocalResultPoi` 等最小类型。
  - `BaiduMapApi` 上以**可选**构造器挂载，保持既有测试替身兼容。

### 3.2 交互流程

1. **定位**：点击「获取当前位置」→ 浏览器授权 → 成功则 `choose(center)` 复用既有中心点变更链路（重置控制器、平移地图）；提示精度与地址；精度大于 200 米或缺失时按“粗略定位”警示。失败按错误码给出可操作提示（拒绝授权 / 超时 / 服务不可用）。
2. **搜索**：输入关键词，回车或点击「搜索」触发（不做逐键联想，节省配额）→ 结果列表（名称 + 地址）→ 点击条目 `choose(center)`。
3. 定位与搜索都**不自动触发分析**，用户仍需点击「开始分析」。
4. 异步竞态用请求序号隔离，过期回调不写入状态；组件卸载后忽略结果。

### 3.3 错误与边界

| 场景 | 处理 |
| --- | --- |
| AK 未配置 / 脚本加载失败 | 控件区域显示说明文字，手输坐标仍可用 |
| 浏览器拒绝授权（状态码 6） | 「定位权限被拒绝，请在浏览器设置中允许后重试」 |
| 定位超时（状态码 8）或 SDK 无响应 | 「定位超时，请重试或手动输入坐标」 |
| 定位不可用（状态码 2） | 「当前环境无法获取位置，请搜索地点或手动输入坐标」 |
| 搜索无结果 | 「未找到相关地点，请尝试其他关键词」 |
| 搜索服务无响应 | 「搜索服务无响应，请确认浏览器地图密钥已开通地点检索服务」 |
| 坐标非法 | 丢弃该条结果；全部非法按无结果处理 |

隐私与安全：坐标仅在用户点击「开始分析」时按既有链路发送至后端；搜索关键词直达百度服务；不落库、不写日志、不加埋点。浏览器 AK 延续现有 `.env.local` 配置方式，服务端步行 AK 不进入前端。

## 4. 前置条件（部署/验收前需人工确认）

1. 浏览器 AK 在百度控制台开通 JavaScript API GL 与地点检索服务，referer 白名单包含 `http://127.0.0.1:5173`（或实际来源）。
2. 验收使用 `127.0.0.1`（安全域）访问；局域网 IP 或 HTTP 域名下浏览器定位可能不可用。
3. 真实定位与检索无法离线自动验证，需一次真实浏览器手测并记录（离线集成测试使用 SDK 替身）。

## 5. 文件改动清单

| 文件 | 改动 |
| --- | --- |
| `src/map/baiduMapTypes.ts` | 新增定位/检索最小类型，挂为可选构造器 |
| `src/analysis/location.ts` | 新增定位与检索封装、错误类型 |
| `src/analysis/location.test.ts` | 新增单元测试 |
| `src/analysis/LocationControls.tsx` | 新增控件组件 |
| `src/analysis/ApiApp.tsx` | 在「选择分析中心」卡片接入控件 |
| `src/analysis/api.css` | 新增控件样式 |
| `tests/integration/analysis.spec.ts`（或新增 location 规范） | 扩展 SDK 替身，新增定位/搜索用例 |
| `README.md` | 补充 AK 服务、安全域与隐私说明 |

## 6. 测试计划

- 单元测试（Vitest）：定位成功/拒绝/超时/不可用/不支持/非法坐标；搜索命中/空结果/不支持/超时/空白关键词；地址去重格式化。
- 集成测试（Playwright 离线替身）：定位成功更新坐标与提示；拒绝授权提示且坐标不变；搜索命中并点击结果后，开始分析请求中心为所选坐标；无结果提示。
- 构建：`npm test`、`npm run build`。
- 集成命令 `npm run test:integration` 由使用者在本地环境执行（依赖 D 盘 Python 环境）。

## 7. 验收标准

1. 配置浏览器 AK 且授权后，点击「获取当前位置」可定位并显示精度与地址。
2. 搜索关键词返回列表，点击结果后地图中心与坐标输入同步更新。
3. 拒绝授权、超时、无结果均有明确中文提示，页面不崩溃、不影响手输坐标分析。
4. 无 AK 时控件降级为说明文字，原有选点与分析流程不受影响。
5. 单测、构建、集成测试全部通过；demo 模式回归不受影响。

## 8. 后续扩展（非本轮）

- 方法三：在 `location.ts` 增加 `geocodeAddress` 封装（`BMapGL.Geocoder.getPoint`），搜索管线按“POI 优先、空结果回落地址解析”组合；需要浏览器 AK 开通地理编码服务并核验城市上下文（`LocalCity`）。
- 输入联想（`BMapGL.Autocomplete`）、周边检索半径配置、检索结果地图临时标记等体验增强另行评估。

## 9. 执行记录（分支 Auto-location）

- [x] 类型扩展：`src/map/baiduMapTypes.ts` 新增 Geolocation / LocalSearch 及结果类型（可选构造器，兼容旧替身）。
- [x] 封装模块：`src/analysis/location.ts`，含定位、POI 检索、错误映射、超时与竞态防护。
- [x] UI：`src/analysis/LocationControls.tsx` 接入 `ApiApp.tsx`，样式在 `src/analysis/location.css`。
- [x] 单元测试：`src/analysis/location.test.ts` 新增 9 条；`npm test` 共 115 条全部通过。
- [x] 类型检查与构建：`npm run build` 通过。
- [x] 集成用例：`tests/integration/analysis.spec.ts` 新增 5 条，Playwright 可正常收集（共 14 条）；临时离线冒烟验证定位与检索交互通过（临时文件已清理）。
- [ ] 待本地执行：`npm run test:integration`（需 D 盘 Python 环境，或用 `ANALYSIS_TEST_PYTHON` 指向可用环境）。
- [ ] 待真实环境核验：浏览器 AK 开通 JavaScript API GL 与地点检索服务后的真实定位/检索手测与记录。
