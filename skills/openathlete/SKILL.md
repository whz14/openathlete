---
name: openathlete
description: OpenAthlete 训练助手的会话入口——每次会话开始执行 bootstrap（读取教练偏好与记忆），首次使用进入配置向导；含日常使用引导与排错知识库。
---

# OpenAthlete 会话入口

数据层目录：`$OPENATHLETE_HOME`，未设置时为 `~/.openathlete/`（Windows 即
`C:\Users\<你>\.openathlete\`）。以下统称 **HOME 目录**。读写其中的文件一律用绝对路径。

本文件固定位于 **`<插件根目录>/skills/openathlete/SKILL.md`**。加载后从本文件的
实际位置向上两级解析 `<插件根目录>`；从仓库直接使用时，插件根目录就是仓库根目录。
本文所有脚本命令都必须带这个前缀——用户的会话目录在别处（如 `~/training`），
裸写脚本名必然找不到文件。

## 一、每次会话开始（bootstrap，必做）

1. 检查 HOME 目录与 `coach-user.md` 是否存在。优先用统一 `read_memory`
   （`name=coach-user`）判断；`exists:false` 或 HOME 不存在 → 进入第二节配置向导
   （人格缺失时可跳过建目录步骤），完成后继续。统一工具未上线时可先按绝对路径检查。
2. 存在则读取**常驻层**（文件缺失时跳过该项，不报错），优先使用统一工具：
   - `read_memory(name=coach-user)`（语气、激进度、体系词汇，全程生效）
   - `read_memory(name=athlete)`（画像与能力基线）
   - `read_memory(name=races)`（目标赛事与历史成绩简表）
   - `read_memory(name=injuries)`（未愈伤病与历史简表）
   - 最近 7 天日志：用 `read_memory(list=log:YYYY-MM)` 定位，再逐日读取；跨月时
     同时列上个月。直接读绝对路径只作统一工具未上线时的回退。

   更早日志、`summary/`、`injuries/`、`races/` 下的完整档案**默认不读**——
   用户问到历史时先用 `read_memory(list=summary)` 定位，需要细节再展开对应文件。
   常驻文件有预算上限（各 ≤2k token），超限时按 coach-core §三当场浓缩。
3. **月初检查**：用 list 检查上月日志与 `summary:YYYY-MM`；有日志但无月度 summary 时，
   先浓缩成月度概述（原日志保留不删），再继续。
4. **工具缺失检查**：统一 openathlete 工具未上线时先确认插件已更新；Kimi 使用 `/reload`，
   其他宿主按各自文档重载或新开会话。若 COROS 工具缺失，**不要重走向导**——跑
   `uv run <插件根目录>/coros_mcp_proxy.py --selftest` 诊断：token 问题按第四节
   排错表处理，selftest 正常则重载让工具上线。
5. 读完不要主动汇报全文；用户问"最近怎么样"类问题时再按 coach-core 的分析工作流取数作答。
6. 之后全程遵守 coach-core：Kimi 已由 `systemPromptPath` 注入；其他宿主若未注入，
   先读取 `<插件根目录>/coach-core.md`。人格微调只写 `coach-user.md`，
   永不修改插件内的 coach-core.md。

## 二、配置向导（首次使用，对话式）

一次只问一两件事，不要让用户填长表单。**任何时候不替用户填写密码、token**，
只告诉用户往哪个文件填。

1. **环境、目录与同步依赖**：
   **uv（唯一前置）**：`uv --version` 确认可用；缺失则当场替用户安装——
   Windows 优先 `winget install astral-sh.uv`，备选官方脚本
   `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`；
   macOS/Linux 用 `curl -LsSf https://astral.sh/uv/install.sh | sh`；
   装完重验，装不上再引导用户手动装。
   **建目录**：创建 HOME 目录及子目录 `memory/log/`、`memory/summary/`、
   `memory/injuries/`、`memory/races/`、`memory/plans/`、`memory/archive/`、
   `logs/`、`fit/`、`coros-mcp-proxy/`、`scripts/`。
   **同步客户端库**：检查 `<插件根目录>/upstream/` 是否存在（佳明→高驰同步依赖它；
   上游无 LICENSE 不随插件分发，需装后单独下载）。缺失则按第四节
   "重装 upstream（钉定版本）"块的命令补齐——用户经 GitHub 从上游作者处直接下载，
   我们不构成分发；下载执行由 AI 当场完成，无需用户操作。网络失败重试即可。
2. **平台盘点：活跃设备、账号持有、写回目标**（三件事逐个确认，不合并成
   一道选择题，也不要问"主要用哪个"——活跃设备、持有的账号、课表写回目标
   相互独立）：
   - **活跃平台**：现在实际戴表训练的是哪家——佳明 / 高驰 / 都是。
     这决定活动数据从哪来。
   - **账号持有**：有没有其他家的账号（不活跃也算）？持有的账号可作数据
     汇聚/分析后端或备用写回通道，没有该家的手表也成立。
   - **课表写回目标**：课表要同步到哪块表——默认跟随活跃手表，用户可另行
     指定（见下方"写回目标分支"）。
   - 每个要用到的平台确认区服（佳明 CN/国际；高驰 国区/国际）。三项结果
     （活跃平台 / 持有账号 / 写回目标）记入 `memory/athlete.md`。

   **分析通道分支**：
   - 活跃含高驰 → 做第 3 步（高驰授权）。
   - 活跃含佳明 → 做第 4 步（佳明凭据）；若同时持有高驰账号，佳明活动经
     同步汇聚进高驰统一分析，第 3 步也要做。
   - 只用佳明且无高驰账号 → **坦诚说明现状**：v0 面向 AI 的结构化数据工具
     只有高驰通道；佳明直读（活动与健康）已用第三方库验证可行，产品化随
     数据层落地。两个选择——
     (a) 先用教练对话与记忆功能，平台选择记入 `memory/athlete.md`，数据层
     就绪后自动激活佳明 adapter；(b) 若愿意，注册免费高驰账号做数据汇聚
     后端（佳明活动同步过去），立即获得完整分析能力（含 EvoLab）。

   **写回目标分支**：
   - 目标=高驰 → 官方 MCP 7 个写工具可用；统一 `create_workout` 也走同一 OAuth。
   - 目标=佳明 → 统一 `create_workout(platform=garmin)` 或既有 CLI
     `openathlete-mcp garmin workout` 均可创建/排期，走 Garmin API，不走 Garmin MCP。
3. **高驰授权（活跃含高驰或持有高驰账号就当场做完，不要留到"以后"）**：coros 工具由高驰官方
   MCP 经本插件代理提供，**授权完成前宿主报"MCP server 启动失败"属预期**，
   提前告诉用户别慌。按顺序走：
   a. 先跑 `uv run <插件根目录>/coros_mcp_proxy.py --selftest`；通过则本步完成。
   b. 失败则问一句：以前在 kimi-code 里 `/mcp-config login coros` 登录过官方
      高驰 MCP 吗？登录过 → 跑
      `uv run <插件根目录>/coros_mcp_proxy.py --import-kimi-tokens` 再 selftest。
   c. 没登录过或导入后仍失败 → 跑
      `uv run <插件根目录>/coros_mcp_proxy.py --login`，告诉用户浏览器会打开
      高驰授权页、5 分钟内完成、期间不要打开高驰 App（单点登录互踢）；
      完成后 selftest 确认。
   d. **告知检查点**：授权成功后 coros 工具要 `/reload` 或新会话才上线。
      向导其余步骤不依赖 MCP，当场继续；唯一例外的"阈值锚点自动读取"
      （第 6 步）可改手动填写，或记入待办、reload 后的会话里补做。
   e. **统一活动/健康同步另需 Training Hub web 凭据**：引导用户自己在
      `~/.openathlete/credentials` 填 `COROS_EMAIL/COROS_PASSWORD`，再跑
      `uv run --project <插件根目录> openathlete-mcp coros login-check`。
      这套凭据用于数据抓取，不能由上面的官方 OAuth 代替。
4. **佳明**（活跃平台含佳明）：确认佳明区服（CN/国际，对应凭据中的区域设置）；说明佳明→高驰
   活动同步、健康读取与 Garmin API 课表写回需要佳明账号，引导用户自己把凭据写入
   `~/.openathlete/credentials`（模板见 `<插件根目录>/.env.example`，KEY=VALUE 格式）；
   提醒佳明需关闭两步验证（MFA）。
5. **非密配置**：执行 `uv run --project <插件根目录> openathlete-mcp db init`，再确认
   个人 IANA 时区、首次活动起点（建议最近 90 天，不默认全历史）与写回目标并保存：
   `uv run --project <插件根目录> openathlete-mcp db config set --timezone <IANA时区> --activity-since <YYYY-MM-DD> --workout-platform <coros|garmin>`。
   双平台用户还需单独确认主表原生平台并加 `--primary-platform <coros|garmin>`；
   它只决定 canonical 选源，不决定课表写回目标。
6. **阈值锚点（影响负荷与区间计算）**：让用户二选一——
   - **自动读取**（推荐，需 coros 工具已上线）：经 MCP 工具
     `queryFitnessAssessmentOverview` 读取阈值心率/配速；佳明用户后续经数据层读取；
   - **手动填写**：用户直接提供 LT1/LT2 心率、阈值配速或最近比赛成绩（用于推算）。
   结果连同日期记入 `memory/athlete.md` 的"能力基线"小节，并写入 `threshold_anchor`。
7. **生成 `coach-user.md`**：把 `<插件根目录>/coach-user.default.md` 复制到 HOME 目录，
   然后问 2-3 个偏好问题当场填入——**先问角色定位**（"你有教练吗？课表谁来定？"，
   有教练的按 coach-core 第六节确定 AI 的辅助角色与授权范围），
   再问激进度、语气、每周可训练时间。
8. **初始化记忆骨架**：以下文件不存在则创建带模板的空文件——`memory/athlete.md`
   （画像+能力基线）、`memory/injuries.md`（含"当前未愈 / 历史伤病"两节）、
   `memory/races.md`（含"目标赛事 / 历史赛事"两节）。
9. **建议专用目录**：建议用户固定在一个目录（如 `~/training`）使用，并在该目录建
   自己的 `AGENTS.md` 写一句"本目录会话默认围绕我的训练"，开放提问即自动进入训练语境。

## 三、你能帮用户做什么（使用引导）

- **训练问答**：最近练得怎么样、负荷涨没涨、恢复够不够、今天该不该练强度。
- **数据同步**：佳明活动同步到高驰——
  `uv run <插件根目录>/sync_garmin_to_coros.py --since YYYY-MM-DD`
  （**必须带 `--since`**；裸跑会注册全部历史）。

  **判重机制**：上传前会自动查询高驰账号里已有的活动，
  按开始时间（±120 秒）匹配，命中的直接跳过并标记——本地 sync.db 丢失
  （换电脑未迁移）、双表用户的高驰原生记录、其他工具（RunGap 等）导入的
  记录都不会重复上传。匹配只比开始时间、不比运动类型；高驰侧查询失败时
  退回本地 sync.db 判重并打印提示。可用 `--no-server-dedup` 关闭。
  残余盲区：两条运动类型不同但开始时间相差 120 秒内的活动会互相顶掉
  （罕见）；已产生的重复只能在高驰 App 逐条删除。
- **课表写回**：按用户的写回目标（`memory/athlete.md` 与 `workout_platform`）——
  统一 `create_workout` 先预览，向用户复述后带原 workout 与 `confirm_token` 再执行；
  COROS 走官方远程 MCP OAuth，Garmin 走 API 创建/读回/排期（不经过 Garmin MCP）。
  也可用既有高驰官方写工具或佳明 CLI；多周计划不在统一 v0 内，佳明 CLI 先 `--dry-run`。
  任何平台写入都必须用户明确确认；超时/结果不明不后台自动重试。
- **记忆**：用户提到伤病/赛事/感受时主动记录（见 coach-core 第三节），
  用户也可随时说"记一下：……"。
- **写代码解决问题**：宿主是完整代码环境，查赛事官网 API、格式转换、
  一次性深度分析都可以现场写脚本；有复用价值的按下方"落盘脚本约定"
  存 HOME 目录 `scripts/`，下次先查索引复用。

### 数据能力地图（AI 用什么读数据）

**当前——统一 openathlete 九工具 + 独立 COROS L2 + 本地 FIT**：

| 要读什么 | 用什么 |
|---|---|
| 近期总览 | `get_overview`：训练、负荷、恢复和下一赛事一页返回，并带 coverage/stale |
| 活动列表/详情 | 统一 `query_activities`（只读 canonical）；`detail=true` 附 FIT 结构、细区间与来源 |
| 统一 TRIMP / PMC | `get_training_load`：本地统一计算，含锚点与 PMC 初始化覆盖，不混 EvoLab |
| 恢复 | `get_recovery_status`：按来源返回 HRV/静息心率等；没有统一恢复评分时 `recovery_pct=null` |
| 手动同步 | `sync_now`：活动与日度指标各至多一批；按返回剩余量由模型继续，不自动循环 |
| 当前计划 | `get_current_plan`：本地进行中计划与本周日志；v0 `platform_schedule.available=false` |
| 记忆与历史 | `read_memory` / `write_memory`；历史先用 `read_memory(list=summary)`、plans/races/injuries/archive 或 `log:YYYY-MM` 定位 |
| **课型结构/逐圈** | 先用 `query_activities(detail=true)`；需要逐圈或大范围扫描时用 `uv run <插件根目录>/fit_laps.py <日期>` 解析原始 FIT，不用平台自动圈代替 |
| EvoLab / 平台体能参考 | 独立 COROS L2：`queryTrainingLoadAssessment`、`queryRecoveryStatus`、`queryFitnessAssessmentOverview`，不混入统一 PMC |
| 睡眠分期 / 更多健康字段 | 独立 COROS L2：`querySleepOverview`、`querySleepHrv`、`queryRestingHeartRate`、`queryAvgHeartRate`；统一 v0 没有的分期明确 missing，不伪造 |
| 平台日程 | 独立 COROS L2 `queryTrainingSchedule`、`queryTrainingPlanLibrary`；统一 v0 不读取平台排期 |
| 佳明活动 | 经既有同步进入高驰后由统一 canonical 查询；Garmin 自动活动/健康抓取仍属后续，课表 API 写回不因此受限 |
| 只用佳明（无高驰账号） | 活动/健康结构化读取仍未产品化；记忆和 Garmin API 课表写回可用，向导第 2 步说明数据汇聚选项 |

**单圈数据的陷阱**：间歇等非匀速课次的全场平均配速是
组配速与恢复的混合（10×1.2km @3:30 + 1 分钟慢跑 → 均配 3:50，看起来像节奏跑），
曾导致"无 ≤3:45 速度课"的错误结论。规则：对某节课下结论或写入记忆前，先用
`query_activities(detail=true)` 或 `fit_laps.py` 看真实结构；只把平均配速当作
轻松/长距离等匀速课次的指标。按天查询秒级完成；大范围扫描适合一次性后台跑。

高驰官方写工具、统一 `create_workout` 与佳明 API 创建/排期都要先给用户看内容、
确认后再执行；佳明 CLI 先 `--dry-run`，失败不自动重建。FIT 下载
（`downloadActivityFitFiles`）有 50 个/24h 限额，只在小样分析时用；统一同步走
Training Hub web，不受这个官方 MCP 限额约束。

### 落盘脚本约定：可复用的代码留在 HOME 目录 `scripts/`

很多任务（查赛事官网 API、格式转换、一次性深度分析）靠现场写脚本解决。
**写过的脚本是资产**——下次换个赛事、换份数据还要再用，只留在会话里等于丢了。

何时落盘（满足任一即存 `scripts/`；老布局的 HOME 没有该目录就直接创建）：

- **换输入还能用**：赛事成绩/分段 API 抓取、FIT/GPX 转换、报表生成等参数化工具；
- **踩过坑才跑通**：端点、认证方式、字段结构、不稳定参数值得留档；
- **用户明确说"留着"**。

一次性数据搬运、纯计算片段不落盘。不便参数化的退而求其次**成笔记**——
把端点/字段/坑写进 memory 日志，下次按笔记重写也比从零摸索快。

脚本规范（与插件入口脚本同一套约定）：

- PEP 723 内联依赖，`uv run` 直跑，不依赖会话环境；
- Windows 中文输出：复用入口脚本的 UTF-8 reconfigure 处理；
- 凭据零硬编码：token/账号走命令行参数或环境变量，docstring 写明获取方式；
  绝不读取 `credentials`；
- 文件头 docstring 写清：用途、用法示例、输入输出、数据出处与**验证日期**、
  已知坑（接口随时可能变，验证日期是判断时效的依据）；
- 输出文件写到用户指定的位置，不随处散落。

索引与复用：

- 每个落盘脚本在 `scripts/README.md` 登记一行：名称 / 一句话用途 / 用法示例 /
  添加日期；大改时同步更新；
- **动手写新脚本前先查索引**——有现成的就复用或在其基础上改，不重复造轮子。

## 四、排错知识库

| 症状 | 处理 |
|---|---|
| Kimi 重装/升级插件报 `EBUSY: resource busy or locked` | 旧插件的 coros 代理进程还在运行，把插件目录锁住了。先关闭其他 kimi 会话再重试；或让 agent 查出命令行含 `coros_mcp_proxy` 的 python/uv 进程并终止，然后重装 |
| Kimi 网页安装返回 504 | 设备隧道超时也可能已安装成功。先核对已安装列表、managed manifest 与 SKILL，并跑 selftest；确认不存在或版本未更新才重试，不要仅凭 504 重复安装 |
| 会话里没有 coros 工具 / "MCP server closed unexpectedly" | 多为 token 未配置或失效：跑下方自检命令诊断，按输出处理（`--login` 或 `--import-kimi-tokens`）；修复后重载或新会话让工具上线（Kimi 用 `/reload`） |
| Codex initialize/tools/list 成功但模型没有 MCP 工具入口 | optional server 首轮目录捕获默认只有 1 秒宽限，更新插件以使用静态声明、共享后台初始化及 Codex `required=true` + 120 秒启动超时。手动配置可加 `required=true`，或设 `mcp_optional_startup_grace_ms=0` 配合 `startup_timeout_sec=120`；单独调大后者不延长首轮宽限。不要用 resources/read 代替 tools/call，以真实工具调用验收 |
| 高驰工具报认证失效 | `uv run <插件根目录>/coros_mcp_proxy.py --login` 重新授权，再 `--selftest` 验证 |
| 整会话报 400 / `$defs not found` | 宿主走了高驰官方 MCP 直连。必须改走本代理（schema 修复），检查 MCP 配置指向插件的 coros server |
| 高驰操作中途失败 | 高驰单点登录互踢：同步/授权期间不要打开高驰 App 或网页版 |
| 佳明登录失败 | 佳明开了两步验证（MFA），garth 不支持，需先关闭 |
| 想看代理日志 | HOME 目录 `logs/coros-mcp-proxy.log` |
| 想卸载插件 | Kimi：`/plugins remove openathlete` 后 `uv run <插件根目录>/uninstall.py`（数据保留）；软删加 `--purge-data`（数据挪备份）；硬删（被遗忘权）`--hard-delete`（数据/备份/token 全删，不可恢复，并按指引去佳明/高驰账号撤销授权） |
| 换电脑迁移 | 旧机 `uv run <插件根目录>/migrate.py export` 打包数据 home；新机装好插件后 `uv run <插件根目录>/migrate.py import <包>`，token 随包恢复 |
| 同步中断/部分失败 | 直接重跑同一命令断点续传；状态异常条目需删 `sync.db` 重试 |
| 同步报"获取阿里云OSS STS Token异常" | 2026-10-03 高驰关闭了免登录 STS 接口（旧 `faq.coros.com/openapi/oss/sts` 已 404），旧版脚本必现。升级到含上游 PR #42 修复的版本（重装插件；仓库则 `git -C upstream pull` 到 06cf52e+）。国际区（regionId 1/3）的 AWS 通道上游未修，需按同模式改造 |
| 同步脚本报 `ModuleNotFoundError: No module named 'coros'`（或 'garmin'/'oss'） | 插件目录缺 `upstream/` 客户端库（上游无 LICENSE、不随插件分发）→ 按下方"重装 upstream"块的命令执行。**配置向导第 1 步已含此检查**，此报错多见于插件更新后 managed 目录被重置、upstream 随之丢失，重新执行即可 |

自检命令（网络+凭据+schema 修复一键验证）：

```bash
uv run <插件根目录>/coros_mcp_proxy.py --selftest
```

### 重装 upstream（钉定版本）

当前钉定的上游版本：**06cf52e**（含 PR #42 STS 修复，已经作者自用验证）——
升级上游并验证后才前移此钉值。用 tarball 快照而非 git clone：
公开仓库匿名下载、无需 GitHub 账号、不依赖 git，且避免上游 master 的后续
变动直接砸到用户（上游曾改客户端签名导致插件脚本失配，见 STS 修复条目）。

```bash
cd <插件根目录>
[ -d upstream ] && mv upstream "upstream.bak.$(date +%Y%m%d%H%M%S)"
curl -L -o /tmp/openathlete-upstream.tar.gz \
  https://github.com/XiaoSiHwang/garmin-sync-coros/archive/06cf52ef4ec5e6bec5300b81ebd75a0be8cdb949.tar.gz
mkdir -p upstream && tar -xzf /tmp/openathlete-upstream.tar.gz -C upstream --strip-components=1
```

Windows（Git Bash，或 Win10+ 自带 curl/tar）、macOS、Linux 通用；旧目录改名
保留作备份，验证同步恢复后可删。

### 排错表未覆盖时：应急自修指引（平台接口变动类故障）

特征：某一步骤**突然全部失败**且报错指向特定平台端点（接口 404、返回结构
变化、鉴权方式改变）——多为佳明/高驰接口变动。本插件只交付源码，设计上
就允许 AI 直接修插件代码应急解封（"agent 可见、可修"）。按顺序执行：

1. **定位**：读 HOME 目录 `logs/` 日志、跑 `--dry-run` / `--selftest`，确定
   失败发生在哪一步（登录 / 拉列表 / 下载 / 取 STS / 上传 / 导入）。
2. **优先查上游官方修复**：同步功能的上游是
   `github.com/XiaoSiHwang/garmin-sync-coros`，先看其近期 commits/PR
   （`https://api.github.com/repos/XiaoSiHwang/garmin-sync-coros/commits?per_page=10`）。
   移植官方修复，不要自己发明协议字段。
3. **最小化移植，禁止整体替换 `upstream/`**：插件里的 upstream 没有 .git，
   且上游若改了客户端构造签名，插件自有脚本（同步入口等）的调用点必须
   配套改——先 grep 全部调用点再动手，改前把原文件备份为 `.bak`。
   整体升级 upstream 是作者发布流程的事，不在用户侧做。
4. **验证**：先 `--dry-run`，再 `--limit 3` 小量实跑（代理问题则 `--selftest`）。
   验证通过才恢复用户原任务；修不好就还原 `.bak` 并按原症状引导用户。
5. **收尾**：向用户说明改了哪些文件、为何有效。本地补丁是应急创可贴，
   会被下次插件更新覆盖——更新里带正式修复，覆盖属预期。代用户把
   故障现象与修复方式整理成 issue 文本，建议发到插件仓库；作者发布后
   `/plugins install` 更新即可。
6. **边界（不可越）**：不读写凭据文件；不删除/重置 sync.db 等数据文件
   （状态 2 条目的重试只给指引，让用户决定）；不为绕开故障而降低数据
   完整性（如跳过判重、关闭校验）。
