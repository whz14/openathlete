# AGENTS.md — OpenAthlete（运动数据统一 × AI 分析）

> 本文件是项目的工程事实源，也是面向 coding agent 的安装与使用说明书。
> **本文件随仓库公开发布**：禁止写入本地绝对路径、个人账号信息和个人训练数据；
> 私有状态写入 `STATUS.local.md`（已 gitignore）。本文只保留当前状态与长期结论，
> 决策和变更时间线由 git 历史承载，不在本文标注日期。

## 目标与交付形态

目标：统一佳明（Garmin）与高驰（COROS）的运动数据能力，让 coding agent 能分析
训练状态、规划训练。**产品不是同步工具，而是 agent 可用的运动数据能力。**

在此之上构建通用训练助手：跨会话记忆、训练建议与赛事战术、自动排课、每日推送和
可定制教练人格。数据层独立、多运行时共用：`~/.openathlete/`（SQLite + FIT 归档 +
Markdown 记忆）是核心资产，Kimi Code 插件与 OpenClaw 只是渠道。
**产品与架构契约见 [DESIGN.md](DESIGN.md)，方案变更先改 DESIGN 再动代码。**

交付形态：

- **Kimi Code 用户**：GitHub 分发的 Plugin（Skill + 教练人格 + PEP 723 Python 脚本）。
- **Codex 用户**：`.agents` marketplace + `.codex-plugin` 原生薄适配，复用同一 Skill、
  教练人格与 PEP 723 脚本，也可继续直接配置 stdio MCP。
- **其他 coding agent**：仓库 + 本文件即通用交付物；另提供 stdio MCP server，
  通过 PyPI / `uvx openathlete-mcp` 接入各客户端。
- 发布名 OpenAthlete；仓库和 Plugin id 为 `openathlete`，PyPI/MCP 为
  `openathlete-mcp`，MCP registry 为 `io.github.<用户名>/openathlete`。

### 开发与自用原则

1. **只交付源码，不出二进制**；所有失败路径给出结构化、可执行的下一步建议。
2. 日志写入 `~/.openathlete/logs/`；doctor 自检与真实排错案例持续补入本文件和 SKILL。
3. 真实账号验证一律用 `--dry-run` / `--limit` 小量运行，不做破坏性测试。
4. 公开文件保持脱敏；私有路径、账号区服和同步进度只写 `STATUS.local.md`。
5. 过渡期必须兼容 `.env`、`data/sync.db`、代理 token 和旧目录约定；
   新状态统一写入 `~/.openathlete/`。
6. 自用验证应安装插件后在仓库外目录进行：改代码 → 重装 → `/reload` → 仓库外验证。
   Windows 重装若报 EBUSY，先关闭相关会话或终止命令行含 `coros_mcp_proxy` 的进程。

## 当前状态速览

已完成并验证：

- **佳明→高驰活动同步**：`sync_garmin_to_coros.py` 已通过真实账号端到端与历史全量验证。
- **高驰官方 MCP**：经 `coros_mcp_proxy.py` 接入，33 个工具可用并通过 stdio 测试；
  代理修复官方写工具 schema 导致的 Moonshot API 400（见“已知坑”第 9 条）。
  首轮静态声明与后台初始化已通过 Codex 默认 optional 配置的真实 `queryDevices` 调用。
- **v0 插件骨架**：`kimi.plugin.json`、双层教练人格、会话入口 skill；
  入口脚本支持 PEP 723 / `uv run`，状态迁至 `~/.openathlete/` 并自动迁移旧布局。
- **Codex 原生薄适配**：隔离环境的 marketplace/plugin 安装、skill 发现、双 MCP 注册和
  统一九工具 stdio 已验证；OpenAthlete 使用 `required=true` 与 120 秒启动超时，
  `readOnlyHint` 与真实 `read_memory` 调用也已通过。optional server 的首轮目录宽限见
  已知坑第 15 条，不以 initialize/tools/list 冒充真实模型调用。
- **openathlete-mcp v0**：9 个统一工具已实现——训练看板、活动、负荷、恢复、
  手动同步、当前计划、记忆读写与单次课表；COROS 官方 MCP 与 Garmin API 双平台
  写回复用独立 adapter，不嵌套本地代理。读时同步使用分 stream 持久队列与连续覆盖，
  记忆支持白名单 list/模板/legacy。静态声明与共享后台初始化分离，超时/失败分别返回
  `STARTING` / `STARTUP_FAILED`。真实 stdio client 验证九工具与四种本地入口，
  本地 wheel 可离线 `uvx` 启动；所有测试在 Python 3.12 与 3.10 均全绿，
  Windows 与 WSL POSIX 文件锁分支已实测。**统一通道尚未做真实账号同步或写回验证**，
  不以 mock 替代真实账号结论。
- **athlete.db 与 src 数据层 v0.1**：规范裸 FIT 归档入库、跑/骑结构解析、
  哈希与主平台驱动的确定性 canonical、multisport 段级同一性、完整快照阈值锚点、
  Edwards TRIMP、7 细区间分布、daily_metrics 与 daily_load；rebuild 保留非派生数据。
  骑行按有效功率或可配置距离阈值分派结构解析（默认 15km，首次候选需用户确认）。
  高驰 Training Hub adapter、统一 CLI、真实全量归档与 PMC 重算已验证。
  根目录 `athlete_db.py` / `workout_structure.py` 保留为兼容 shim。
- **佳明 adapter 只读验证**：当前阈值与日度健康/睡眠字段已真实读取成功；
  自动定时抓取、全历史健康回填和自动锚点写入仍未产品化。
- **佳明课表写回**：单段时间课、重复组距离课与指定日期排期均已真实创建并读回验证；
  `workout.py` 已沉淀统一处方→Garmin mapper 与创建/排期服务（run/ride），
  CLI 支持 dry-run；统一 `create_workout` 复用该服务，不为佳明嵌套另一个 MCP。

待做：doctor、每日定时抓取、PyPI 与 MCP registry、多周计划、Garmin 自动活动/健康抓取、
高驰→佳明同步、游泳趟级解析、record 流分段和 Zwift adapter。

## 目录结构与维护约定

```text
./
├── README.md / LICENSE         # 项目说明 / MIT 许可证
├── AGENTS.md / DESIGN.md     # 工程说明 / 产品设计契约
├── CONTRIBUTING.md           # 开发、测试、PR 与 main 保护规范
├── STATUS.local.md           # 私有状态，gitignore
├── kimi.plugin.json          # Kimi Plugin 清单
├── .github/workflows/ci.yml  # 跨系统、跨 Python 版本全量测试 CI
├── .github/workflows/release.yml # 标签触发的 PyPI Trusted Publishing
├── .github/pull_request_template.md # PR 验证检查清单
├── .agents/plugins/marketplace.json # Codex marketplace 清单
├── .codex-plugin/plugin.json # Codex 原生插件清单
├── .codex-plugin/mcp.json    # Codex 双 MCP 与 required/启动超时配置
├── .mcp.json                 # 其他宿主复用的通用双 MCP 配置
├── coach-core.md             # 核心教练人格，随插件分发
├── coach-user.default.md     # 用户人格模板
├── skills/openathlete/       # 会话入口、配置向导与排错
├── sync_garmin_to_coros.py   # 佳明→高驰同步入口
├── coros_mcp_proxy.py        # 高驰官方 MCP 的 stdio schema 修复代理（独立 L2）
├── coros_tools.json          # COROS 首轮静态工具声明；后台刷新运行时缓存
├── openathlete_mcp.py        # 统一九工具 stdio MCP 的 PEP 723 入口
├── fit_laps.py               # 从原始 FIT 还原真实课型结构
├── athlete_db.py             # 兼容 shim，实现位于 src/openathlete/db/
├── workout_structure.py      # 兼容 shim，实现位于 src/openathlete/
├── migrate.py / uninstall.py # 数据移植 / 卸载清理
├── pyproject.toml            # src layout，包名 openathlete-mcp
├── server.json               # 官方 MCP Registry 元数据
├── src/openathlete/
│   ├── home.py / config.py   # HOME 与非密配置（含时区/同步起点/写回平台）
│   ├── credentials.py        # 凭据读取
│   ├── structure.py          # 课型结构解析共享核心
│   ├── workout.py            # 双平台处方、预览确认与 Garmin API 服务
│   ├── queries.py            # canonical 活动/负荷/恢复/计划本地查询
│   ├── sync_state.py         # 分 stream 同步、连续覆盖、父/段队列
│   ├── memory.py             # 白名单记忆、front matter、legacy 与原子写
│   ├── locks.py              # Windows/POSIX 文件锁与进程内锁
│   ├── mcp/                  # 低层 SDK server、schema、envelope、dispatch
│   ├── db/athlete_db.py      # athlete.db schema / ingest / anchor / stats / load
│   ├── adapters/             # coros web/官方写 client；garmin 阈值/健康/课表 API
│   └── cli.py                # 统一 CLI（无参数或 serve 启动 MCP）
├── tests/                    # pytest 合成数据测试
├── dev/                      # 探针、响应样本与解析基准，不入产品路径
├── upstream/                 # XiaoSiHwang/garmin-sync-coros，只 import 不修改
└── data/                     # 旧运行时布局，仅作迁移源与日志残留
```

维护约定：

- 所有变更从最新 `main` 新建分支并通过 PR 合并；禁止直推、强推或删除 `main`。
  `CI` 的 `tests` 汇总检查必须全绿，完整流程与仓库保护规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 新功能优先进 `src/openathlete/`；根目录入口保持可用，逐步变薄 shim。
- 新模块必须配必要的 pytest 测试；提交前 `uv run --locked --extra dev python -m pytest -q` 全绿。
  解析/契约逻辑与 IO 分离，纯函数优先，阈值常量化并注明出处。
- `.env` 与凭据文件由用户自己填写，Agent 不读取；模板见 `.env.example`。
- `.kimi-code/mcp.json` 含本机配置，不直接提交；官方直连留档不代替当前代理方案。

## 核心调研结论（已沉淀，勿重复调研）

### 健康数据

- 两平台都只接受活动文件导入，**均不提供睡眠/HRV/步数等健康数据写入接口**；
  健康数据只能两端读出、本地汇总。Body Battery 没有高驰等价字段。
- 佳明只读使用 garth / python-garminconnect；高驰只读可使用官方 MCP。
- 佳明可写单次结构化课表并排期，但不能创建多周计划实体；高驰 MCP 可建 4–16 周计划。
  因而跨平台计划统一抽象为一组按日期排期的 workout。
- 活动同步以 XiaoSiHwang/garmin-sync-coros 为基础；其他同步工具也只解决活动，
  不改变上述健康数据边界。

### 阈值锚点数据源

- 佳明可直读跑步 LT2 心率/阈值速度、按运动类型的心率区间、骑行 FTP 和静息心率；
  跑步 LT 历史区间也可读取。阈值速度的换算为 **阈值配速 s/km = 100 / speed**。
- 佳明没有 LT1 直接 API，需手填或估算；高驰可读取 lthr/ltsp，maxHR 缺口需手填。
- 端点探针见 `dev/query_garmin_thresholds.py`；原始响应写入本机忽略的
  `dev/last_*.json`，不随仓库发布，也不在本文件重复维护 API 参数清单。

### 原生/导入活动来源判别

- 目前没有已确认、可跨平台通用的“手表原生 vs 上传导入”读取标记。
  设备字段、`isOriginal` 和 lap 展示差异只能作启发式告警，不能当稳定契约。
- FIT `manufacturer/product` 只说明文件创建设备，不说明摄入渠道。当前实测支持
  原始 FIT 跨平台上传后仍保留原字节/字段，因此字节一致副本不存在分析差别。
- 设计约定：副本保存 `fit_sha256`；字节一致时固定顺序选 canonical，内容分叉时按
  用户声明的 `primary_platform` 优先。双平台初始化必须询问主表原生平台（DESIGN §3）。

### EvoLab 与导入数据

- EvoLab 核心指标基于 TRIMP，可从导入 FIT 重算，但只消费最近 42 天；
  恢复指标不依赖睡眠/HRV，缺失健康数据主要影响独立健康监测层。
- 跑步功率、步频和步态等标准 FIT 字段可随原始文件导入；云端衍生评分不会迁移。
- 中转必须使用原始 FIT，第三方平台改写可能丢失步态等字段。

### 高驰官方 MCP

- 国区 `https://mcpcn.coros.com/mcp`，国际 `https://mcp.coros.com/mcp`；
  Streamable HTTP + OAuth2，个人免费。
- 33 个工具覆盖活动、EvoLab、健康只读与训练写入；FIT 下载限 50 个/24h，
  无删除操作，排期范围为今天起 90 天。
- 官方写工具 schema 会触发 Moonshot API 400，当前必须经本地代理接入；
  MCP 配置变更需 `/reload` 或新会话生效。

### 高驰 Training Hub web API

- Partner API 不面向个人发凭据；数据层抓取使用非官方 Training Hub web API，
  不经官方 MCP，因此不受其 FIT 下载限额约束。
- 登录 token 缓存 24h，响应 regionId 决定区域 API 地址；活动列表、详情、FIT 下载、
  日度指标和体能评估的实现见 `src/openathlete/adapters/coros.py`。
- 睡眠分期需要单独 mobile token，且会踢掉手机 App，**不碰**；睡眠数据走官方 MCP。

## 已知坑 / 操作注意

1. **高驰单点登录**：同步或 API 登录期间不要打开高驰 App/网页，避免互踢。
2. **佳明 MFA**：garth 不支持两步验证，需先关闭佳明账号 MFA。
3. **凭据保护**：Agent 不读写 `.env` 或凭据文件，只引导用户自己填写，不让用户贴密码。
4. **Windows 中文输出**：入口脚本统一做 UTF-8 reconfigure，新脚本复用该处理。
5. **OSS 中转**：不需要用户云账号；STS 凭据由高驰签发，文件进入高驰自己的 bucket。
6. **小文件导入**：高驰 API 没有网页导入器的 20KB 下限。
7. **同步续传**：进度存 `~/.openathlete/sync.db`；连续失败 3 次会中止，重跑可续传；
   状态 2 的异常条目会跳过，需删 db 后重试。
8. **上游变动**：非官方接口可能失效；佳明对数据中心 IP 有 429 限流。
9. **高驰 MCP × Moonshot 400**：官方写工具的嵌套 `$defs` 引用不被接受，
   `coros_mcp_proxy.py` 将其提升到根部。排障用 `--selftest`，token 失效用 `--login`，
   日志在 `~/.openathlete/logs/coros-mcp-proxy.log`。
10. **全历史同步**：`sync_garmin_to_coros.py` 不带 `--since` 会注册全部历史并从最老开始；
    只想同步近期必须显式带 `--since YYYY-MM-DD`。
11. **同步判重**：上传前按高驰活动开始时间 ±120s 做服务端判重，失败时退回本地 db；
    `--no-server-dedup` 可关闭。高驰不能批量删除，重复只能手动处理；
    长期方案是 DESIGN §3 的活动同一性规则。
12. **高驰 STS 旧端点已关闭**：国区修复要求 upstream 包含 06cf52e，并传入登录
    accessToken；国际区同类问题在上游尚未完整修复，不能照搬国区结论。
13. **导入活动 lap 失真**：高驰 lap 接口对导入活动通常只给自动距离圈，不能据此判课型；
    用 `fit_laps.py` 解析原始 FIT。不要通过伪造设备 manufacturer/product 冒充原生记录。
14. **Windows 本地化时区名**：`datetime.astimezone()` 可能返回“中国标准时间”等显示名，
    不是 IANA key。缺省系统时区统一经 `tzlocal` 解析并验证；显式配置仍只接受 IANA，
    不做中文名硬编码映射，也不在错误 envelope 中再次调用会失败的解析路径。
15. **Codex optional MCP 首轮宽限**：optional server 的首轮目录捕获默认只有 1 秒宽限，
    `startup_timeout_sec` 只限制单个 server 启动/初始 tools/list，不延长该宽限；
    `mcp_optional_startup_grace_ms=0` 改为等待各自启动超时，`required=true` 还会把
    初始化失败提升为会话失败。OpenAthlete 与 COROS 代理都使用静态声明与共享后台初始化：
    COROS 首轮从 `coros_tools.json` 或较新的运行时缓存取声明，官方连接与刷新在后台完成，
    过早调用返回 `STARTING`，失败返回 `STARTUP_FAILED`。OpenAthlete 在 Codex 专用配置为
    `required=true` + 120 秒启动超时，COROS 保持 optional；`notifications/tools/list_changed`
    不保证刷新模型目录。宽限参数只作旧版本/手动配置兜底，验收看真实 tools/call，
    不得用 resources/read 冒充。
16. **Kimi 网页安装 504**：设备隧道超时后仍可能安装成功；先核对已安装列表、managed
    manifest/SKILL 与 selftest，确认未安装或版本未更新才重试，避免重复安装。

## 运行手册（Runbook）

```bash
# 开发用上游库；插件用户的钉定版本安装见 skills/openathlete/SKILL.md
git clone --depth 1 https://github.com/XiaoSiHwang/garmin-sync-coros upstream

# 开发环境（Windows 使用 .venv\Scripts\activate）
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# 佳明→高驰同步：先演练、再小量，日常增量必须带 --since
uv run ./sync_garmin_to_coros.py --limit 3 --dry-run
uv run ./sync_garmin_to_coros.py --limit 3
uv run ./sync_garmin_to_coros.py --since YYYY-MM-DD

# 高驰 MCP 代理
uv run ./coros_mcp_proxy.py --selftest
uv run ./coros_mcp_proxy.py --login
uv run ./coros_mcp_proxy.py --import-kimi-tokens

# 统一 MCP（无参数与 serve 等价；启动/list 不触网）
uv run ./openathlete_mcp.py
openathlete-mcp serve
openathlete-mcp

# 数据层与首配（workout-platform 是写回平台，primary-platform 是 canonical 选源）
openathlete-mcp coros login-check
openathlete-mcp db init
openathlete-mcp db config set --timezone Asia/Shanghai \
  --activity-since YYYY-MM-DD --workout-platform garmin --primary-platform garmin
openathlete-mcp db config set --ride-training-min-km 15

# COROS 增量/历史回补：同一持久状态机；dry-run/limit 不扩展连续覆盖
openathlete-mcp coros pull --since YYYY-MM-DD --limit 5 --dry-run
# 不带 limit 的完整连续窗口全部入库后才扩展覆盖
openathlete-mcp coros pull --since YYYY-MM-DD --until YYYY-MM-DD
openathlete-mcp coros metrics --days 28
openathlete-mcp db ingest
openathlete-mcp db rebuild-load
openathlete-mcp db stats

# 佳明课表（统一 workout JSON；dry-run 不触网）
openathlete-mcp garmin workout create --file WORKOUT.json --dry-run
openathlete-mcp garmin workout create --file WORKOUT.json --date YYYY-MM-DD
openathlete-mcp garmin workout schedule --id WORKOUT_ID --date YYYY-MM-DD

# 测试（与 CI 相同，运行完整测试集）
uv run --locked --extra dev python -m pytest -q
```

### PyPI 与 MCP Registry 发布

发布配置不等于发布完成。首次发布前，在 PyPI 配置 pending trusted publisher：项目
`openathlete-mcp`、仓库 `whz14/openathlete`、workflow `release.yml`、environment
`pypi`；并在 GitHub 创建同名 environment。Pending publisher 不预留包名。

每次发布先通过 PR 同步 `pyproject.toml`、`src/openathlete/__init__.py` 与
`server.json` 的版本并全量测试。标签必须等于包版本前加 `v`；标签 workflow 校验
版本、构建并冒烟后，才由 `pypi` environment 通过 Trusted Publishing 上传。
必须等 PyPI 包可见后再发布同版本 MCP 元数据，不能反序。

```bash
# 本地发布检查；重新构建，不上传旧 dist
uv run --locked --extra dev python -m pytest -q
uv build --clear
uvx --from dist/openathlete_mcp-X.Y.Z-py3-none-any.whl openathlete-mcp --help

# main 上已通过 CI 的发布提交；X.Y.Z 替换为实际版本
git tag -a vX.Y.Z -m vX.Y.Z
git push origin vX.Y.Z

# PyPI 页面可见后验证安装，再发布 MCP Registry
uvx --refresh-package openathlete-mcp openathlete-mcp --help
mcp-publisher login github
mcp-publisher publish
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=openathlete"
```

## 路线图与依赖

- **阶段一（已完成）**：同步脚本、高驰 MCP 配置和基础调研。
- **阶段二**：补齐 doctor、运行日志和排错知识，完善仓库即交付物。
- **阶段三**：完善 Kimi Plugin 命令并发布；骨架、状态外移和 PEP 723 已完成。
- **阶段四（本地已实现）**：stdio MCP 九工具、读时同步 TTL、分 stream 队列与连续覆盖、
  双平台单次课表写回已落地；长任务按约 20 条分批，使用 sync.db 续传并返回剩余量。
  四种本地入口与离线 wheel 已验证；PyPI 与 MCP registry 发布仍待做。
- **阶段五**：完善训练助手、每日晨报、课表写回和可选 OpenClaw 渠道。

用户侧前置条件统一为 **uv**。当前脚本使用 PEP 723 + `uv run`；MCP 发布后切换
`uvx openathlete-mcp`。不采用 PyInstaller 单文件 exe，因为二进制违背 agent 可见、可修原则。

## 许可证与合规

- `upstream/` 无正式 LICENSE，作者在
  [issue #43](https://github.com/XiaoSiHwang/garmin-sync-coros/issues/43)
  公开允许使用；其佳明模块来自 running_page（MIT），收编时需补 MIT 署名。
- upstream 不随仓库分发，配置向导下载钉定版本；不使用 git submodule，
  因为插件的 codeload zip 不会包含 submodule 内容。
- README 必须告知：非官方接口有风控/限流和失效风险，高驰存在单点登录互踢。
- 凭据、数据库和 FIT 均留在本地；安装/更新会访问代码与依赖下载源，平台查询和同步
  会访问对应 API，云端大模型对话会带出随上下文发送的分析数据，README 需明确说明。
