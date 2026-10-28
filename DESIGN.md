# DESIGN.md — OpenAthlete 训练助手设计契约

> 本文件是产品的**设计契约**：后续实现以此为准，方案变更先改本文件再动代码。
> 本文件随仓库公开发布：禁止写入本地绝对路径、个人账号信息、个人训练数据。
> 工程事实源与调研结论见 [AGENTS.md](AGENTS.md)；本文件只放产品/架构设计的最终结论。

## 1. 产品愿景：通用训练助手

**跨会话记忆**

1. 记录过去训练状态与多年能力变化
2. 知道最近身体状况（手表自动读取 + 用户主动告知）
3. 记录历史伤病
4. 记住目标赛事

**建议**

5. 对近期训练给出建议
6. 对目标赛事，按日期、天气、排期给出训练建议与比赛战术

**其他**

7. 自动设置课表（替代手动编辑）
8. 跨平台：佳明/高驰，未来颂拓/apple，统一抽象、集各家独有指标之长
9. 易用：手机可达、可接飞书、每日主动推送

**私人定制化教练**：允许用户微调教练人格——训练哲学激进/保守、说话方式可选；
但运动科学底线**不可定制**：面对明显不合理不科学的需求，教练必须拒绝指导（机制见 §5）。

## 2. 总体架构：数据层独立，多运行时共用

```
┌────────────────────────────────────────────────────────┐
│ 渠道层   Kimi Code 插件（桌面）   OpenClaw+飞书（手机，可选） │
├────────────────────────────────────────────────────────┤
│ 工具层   openathlete-mcp（stdio MCP，插件内脚本→PyPI/uvx） │
├────────────────────────────────────────────────────────┤
│ 大脑层   教练人格与分析框架（插件 systemPrompt 与 SOUL.md 同源）│
├────────────────────────────────────────────────────────┤
│ 数据层   ~/.openathlete/（FIT 归档 + SQLite + Markdown 记忆）│
└────────────────────────────────────────────────────────┘
```

**核心决策：数据层不绑定任何 agent 运行时。** Kimi Code 插件和 OpenClaw 都只是渠道；
未来出现更好的 agent 框架时，替换渠道层即可，数据与工具原封不动。
平台差异的处理（统一核心 / 本地基线 / 特性透传）见 §4 三层策略。

## 3. 数据层契约

目录约定（用户 home，随配置向导创建）：

```
~/.openathlete/
├── config.toml        # 非密配置（区服、偏好、webhook 等）
├── coach-user.md      # 可微调的教练人格（§5；缺失时配置向导按插件模板生成）
├── credentials        # 凭据（系统 keychain 或本地受保护文件；Agent 不读取）
├── athlete.db         # SQLite（WAL）：活动、指标、睡眠/HRV 等时间序列
├── sync.db            # 平台抓取高水位与跨平台同步进度（与 athlete.db 职责分离）
├── fit/               # FIT 原始归档，长期 source of truth
├── logs/              # 运行日志（代理/同步/晨报，排错入口）
├── coros-mcp-proxy/   # 高驰代理 OAuth token 等运行时状态
├── scripts/           # AI 会话沉淀的可复用脚本 + README.md 索引（约定见 SKILL.md「落盘脚本约定」）
└── memory/            # 叙事性记忆（Markdown，LLM 直读、可 git 管理）；热详冷概分层
    ├── athlete.md     # 运动员画像（能力、偏好、历史结论）——常驻读取
    ├── injuries.md    # 伤病总览：未愈详述 + 历史伤病一行一条——常驻读取
    ├── injuries/      # 单次伤病完整病程（YYYY-MM-<部位>.md，按需读取）
    ├── races.md       # 目标赛事详述 + 历史成绩一行一场——常驻读取
    ├── races/         # 单场赛事完整复盘（YYYY-MM-DD-<赛事>.md，按需读取）
    ├── log/YYYY-MM/   # 每日日志（YYYY-MM-DD.md），全量保留，仅最近 7 天常驻
    ├── summary/YYYY-MM.md  # 月度概述：历史的读取入口（训练量/进展/伤病与状态要点）
    ├── plans/         # 训练计划档案（教练/AI 生成/平台导入，按需读取；规范见"课表与训练计划"小节）
    └── archive/       # 用户一次性倾倒的大段历史原文（多年病历/训练史，按需读取）
```

原则：

- **数字进库，叙事进文件**：量化时序用 SQL 聚合（多年趋势），伤病/赛事/主观感受用 Markdown。
- **记忆分层：热详冷概。** 常驻读取只含 athlete/injuries/races 三个总览文件 + 最近 7 天
  日志；历史查询先读 `summary/` 月度概述定位，需要细节再展开对应单日日志或单伤病/
  单赛事档案。每月首个会话把上月日志浓缩为 `summary/YYYY-MM.md`（原日志保留不删），
  规模可控、多年可查。
- **常驻读取预算制**：常驻层（三总览 + 近 7 天日志 + coach-user.md）预算约 8k token
  （256k 上下文的约 3%）；单文件超限当场浓缩，被挤掉的细节挪档案不直接删；
  用户一次性倾倒的大段历史（多年病历/训练史）只蒸馏进总览、原文入 `memory/archive/`。
  防止多年累积或一次性病史倾倒把每会话固定 context 开销推向不可控。执行规则在
  coach-core §三。
- **数据属于 `~/.openathlete/`，不属于插件**：卸载/升级插件不动数据；所有渠道读写同一份。
- **scripts/ 工具箱随数据保留**：AI 在会话中为解决实际问题写出的可复用脚本
  （赛事 API 抓取、格式转换等）落盘到 `scripts/`（PEP 723 `uv run` 直跑 +
  README.md 索引），与数据同生命周期；写作与复用约定见 SKILL.md「落盘脚本约定」。
- **存储收敛**：一切用户状态（库、归档、记忆、token、日志）只写 `~/.openathlete/`；
  插件目录与 uv 缓存不含用户数据——软删/硬删/移植都只操作这一个目录（§6）。
- 单机单用户规模，SQLite WAL 足够；不需要任何数据库服务进程。
- **sync.db 与 athlete.db 保持分离**：sync.db 是平台抓取与跨平台同步状态机
  （抓取高水位、哪条活动传过、失败重试），写入频繁、生命周期跟随同步操作；
  athlete.db 是分析事实库，活动与负荷派生表可从 FIT 归档重建而不丢同步进度；
  手动锚点与平台健康数据不属于 FIT 派生表，重建范围见"athlete.db 表结构"。
  两者经活动键关联，不互存冗余状态。上传前的服务端判重（查高驰已有活动）
  是换机/多来源场景的安全网，不取代本地状态机。
- **活动同一性（去重）**：业界无正式标准（intervals.icu 也仅对 Strava 来源判重）；
  自定义规则 = **开始时间 ±5 分钟，与运动类型无关**——人不会同时做两种运动，
  时间窗重叠即同一训练。运动类型不参与判重（越野跑/路跑/场地跑只是细分叫法；
  两块表一个选跑步一个误选骑车，也是同一训练），类型字段以来源优先级高者为准。
  其余来源标记为副本，分析时排除、原始文件不删。
- **canonical 选择**：同一逻辑活动的来源副本先比较 `fit_sha256`。哈希一致说明
  平台原样保存了同一文件，按固定 `(platform, platform_activity_id)` 顺序选择即可，
  不受入库顺序影响；哈希不一致时才按 `config.toml` 中用户声明的 `primary_platform`
  优先。双平台初始化必须询问哪个平台保存主表原生记录；缺少声明时把活动标为
  `canonical_conflict`、暂不进入统一统计并明确提示确认，不得“谁先入库谁 canonical”。
  用户更正声明后可删派生库重建。canonical 变化时必须从当选 FIT 重算并替换整条
  activity、结构与 time_in_zone，不能只翻转来源标记。
  当前平台列表/详情没有已确认、可通用的原生/导入标记：FIT `manufacturer/product`
  只表示文件创建设备，设备字段有无、`isOriginal`、lap 展示差异均只作异常告警的
  启发式证据，不静默改变用户声明。
- 铁三等多段运动：multisport FIT 按 session 拆分入库——每段一行 activity
  （sport 取段类型），activity_source 的 platform_activity_id 加段序号后缀以保
  UNIQUE；训练负荷按单项分别累计，赛事复盘按同来源 ID + 时间邻近在分析层聚合展示。
  同一性匹配还要比较段序号，同一父活动的不同段绝不互并，跨来源对应段再按时间合并。
  平台本已提供多个 FIT 时逐段落盘并在 id 后加 `#i`；一个原始 FIT 含多个 session 时
  保留原字节，多个 source 共享同一 fit_path，不为 `#i` 重编码或伪造逐段 FIT。
- **FIT 归档命名**：`fit/YYYY/MM/<本地开始 yyyymmdd-hhmmss>-<运动类型>-<来源平台>-<平台活动id>.fit`
  （multisport 逐段拆落盘时段序号缀在 id 后：`...-<labelId>#2.fit`），
  确定性命名保证重复下载幂等；平台活动 id 兼作 activity_source 的判重键。
  长期归档只存裸 `.fit`；平台下载或上传使用的 ZIP 仅是临时传输封装，不写入归档，
  入库与读取工具不提供旧 ZIP 归档回退。
- **FIT 解析选型**：用官方 `garmin-fit-sdk`——与 fitparse 对比
  60 个真实 FIT：成功率与标准字段覆盖一致（步态/心率/增强速度等），速度快约 40%，
  profile 更新（含增强呼吸率）；fitparse 多出的仅是未命名未知字段。对比脚本
  `dev/fit_bench.py`，可复跑。
- **命名映射取主流无歧义词**：负荷沿用 PMC 通名 `ctl/atl/tsb` + `trimp`
  （intervals.icu 的 fitness/fatigue/form 仅作别名注释）；健康时序用
  `resting_hr / hrv_ms / sleep_*_s / spo2_pct / body_battery` 等自解释列名。
- **表级 DDL 以"athlete.db 表结构"小节为准**；修订先改本节再动代码，
  任何会话不得临场发明表结构。

### 训练内容建模与负荷模型

**建模分层：负荷会计入库，处方体系入大脑。**

- **负荷会计（入库）**：单次负荷用 Edwards TRIMP（5 心率区间 × 权重），长期状态用 PMC：
  CTL（42 天指数均值=体能）/ ATL（7 天=疲劳）/ TSB（=CTL−ATL=状态）。
  选型理由：TRIMP+PMC 是业界事实标准（TrainingPeaks / intervals.icu / 颂拓官方同框架，
  佳明 EPOC 与高驰 EvoLab 也是 TRIMP 家族），跨平台可比性最好。
  **跨平台统一负荷只由 canonical FIT + 活动日有效锚点按同一算法计算**；
  TRIMP 与 time_in_zone 均读取 FIT record 的 `timestamp` / `heart_rate`，按相邻记录
  时长累计，不得以 session/lap 平均心率替代。这里的 record 读取只用于负荷与分区，
  不等于启用留档的 record 流结构分段。平台原生 trainingLoad / EvoLab / Training Status
  不混入统一 daily_load，以免换表或双表造成口径漂移，原生负荷只可作为独立 L2 参考。
- **阈值锚点表（入库，按日期版本化）**：LT1/LT2 心率、阈值配速、FTP/临界速度、最大心率
  （每运动类型），带有效期起止。阈值随能力变化，历史活动必须按当日有效阈值解读；
  更新只影响生效日之后。每个版本是完整快照：自动或手动写入共用一个合并入口，
  在既有有效版本上做字段级合并；自动接口只覆盖实际读到的非空字段，缺项继承
  旧值/手填值，空响应是完全 no-op，同日期写入幂等，补写历史日期时同时维护前后
  版本的 valid_to，不允许重叠区间。resting_hr 是全局指标，计算时按活动日取所有
  sport 行中最近生效的全局值，不因某一 sport 的稀疏行而回退到旧静息心率。
  缺少 TRIMP 必需的 maxHR/restingHR 或区间必需的 LT1/LT2 时，对应派生指标留空并
  报告覆盖范围，不得把 latest 锚点伪装成全历史有效。
  **跨类型回退**：HR 系指标（TRIMP / time_in_zone）按类型
  取锚点；该类型无锚点时跑步与骑行互为近似（两者 LT 心率接近），游泳不做回退
  （HR 体系不同，负荷走 sTSS）；佳明按运动类型维护心率区间设置（骑行有独立分区），
  API 可读出时作为锚点来源之一，读不出再走回退。**冷启动**：初始值由配置向导引导二选一——自动读取
  （高驰体能评估 / 佳明区间设置）或手动填写（可直接给近期比赛成绩用于推算）；
  数据层建成前先记入 `memory/athlete.md` 的能力基线小节。
- **强度分布（入库）：存细粒度时间分区，不存固定通道。** 以阈值锚点定义 7 个生理细区间
  （恢复 / 耐力 < LT1 / 节奏 / 阈值≈LT2 / 最大摄氧量 / 无氧 / 冲刺，兼容 Coggan 七区——
  主流体系的最细粒度，任何更粗词汇都是它的无损分组），每次活动存各细区间时间 +
  当日区间边界。分布词汇只是细区间的命名分组：

  | 词汇体系 | 分组（细区间） |
  |---|---|
  | 佳明三通道 / Seiler 三区 / 有氧·混氧·无氧 | Z1-2 / Z3-4 / Z5-7（同一 LT1/LT2 划分的三种叫法） |
  | Daniels 五区 | E=Z1-2、M≈Z3、T=Z4、I=Z5、R=Z6-7 |
  | Coggan 七区 | 恒等映射 |

  新增词汇体系 = 新增一行映射，零迁移；教练汇报使用用户习惯的词汇（§5）。
- **处方体系（不入库，属大脑层）**：Daniels VDOT、临界速度、双阈值/挪威模式、极化/金字塔
  是训练哲学而非负荷模型，作为 `coach-user.md` 的可定制维度（§5），不写入 schema。
- **不存训练类型标签**：标签在体系间有歧义（Daniels 的 T ≠ 日常"tempo 定速巡航"），
  混合课（先节奏后间歇）无法单值归类；细区间分布已无损记录刺激本质，课的"类型"
  由大脑在读取时按用户词汇现场推断。**粗粒度区分（连续 vs 间歇）由课型结构的
  type 派生列天然给出，无需标签**；反过来标签给不了组配速、渐进对比与负荷分布——
  标签是结构的投影，不是替代品（AGENTS.md 已知坑 #13：没有结构时，10×1.2km
  间歇被平均配速误判为节奏跑）。结构由解析器从 FIT 机械生成、零人工归类，
  不规则课型退化为 steps 全列而非报错。平台原始的课表名称/结构化步骤（如有）作为
  不解释的透传元数据保留；计划内课程通过与计划的关联天然获得类型。
- **课型结构（入库）**：单次训练的**实际执行结构**从 FIT 计圈解析，
  用"有序块序列 + 一层循环嵌套"模型存储（与 FIT workout_step 重复组、高驰课表
  intervalGroup 同构，课表写回时可互转）：
  - `blocks`：steady 块（热身/放松/主体连续段：距离/时长/配速/心率）+ group 块
    `{cycles, steps, recovery}`——steps 是一个循环周期的组成：规整间歇（10×1.2km）
    steps 长 1，梯次/复合重复（(4.0+3.2+2.4km)×2、4×(2.4km+400m)）steps 逐元列出，
    无重复模式 cycles=1、steps 全列；recovery 含中位时长/距离/是否站休；
  - `reps`/`recoveries`：平铺的每组实际距离/时间/配速/心率（无损明细，渐快渐慢不丢）；
  - `nl`：规范自然语言摘要，**由结构机器生成、禁止模型凭摘要反推结构**（结构以
    blocks 为准）。nl 语法：匀速课 `12km @3:55 连续`；规整间歇
    `10×1.2km @3:21-3:33(均3:30)，间休 1:00(140m)`；梯次/复合
    `(4.0km@3:45+3.2km@3:42+2.4km@3:39)×2，间休 1:30(200m)`；无重复模式
    `4组(5.0km+4.0km+3.0km+820m) @3:34-3:48(均3:40)，间休 1:58(210m)`；
    含热身/放松按时间序用 ` → ` 连接；恢复 <45s 且 <60m 记"站休"，否则记慢跑距离；
  - 存储：`activity.structure_json` 整棵 JSON + 派生过滤列（type、rep_count、
    rep_dist_modal_m、rep_pace_mean_s、interval_dist_total_m）。同属 FIT 可重算的
    派生数据（存原语原则的延伸），随解析器版本重算；
  - 参考实现：`fit_laps.py --json` 输出即此结构（解析与折叠规则见脚本 docstring 与
    AGENTS.md 已知坑 #13）；入库由数据层在 FIT 归档落库环节生成。平台 lap 接口
    对导入活动只有自动距离圈，不能作为结构来源。
  - 术语对照（概念业界通用，命名混合借用 + 两个自造）：steady ≈ Zwift ZWO 的
    SteadyState/稳态段；steps ≈ FIT workout_step 组内步骤；recovery、reps 是运动学
    通用词；group ≈ FIT RepeatStep 重复组 ≈ 高驰 intervalGroup ≈ intervals.icu
    的 repeat；**block 与 cycles 是自造**——block 避免与已占用的 step/section
    混淆，cycles 避免 repeats 与 reps（平铺明细）近义碰撞。
    但要求**组距与恢复距离都与圈粒度对齐**——任一不对齐就产生混合圈
    （140m 恢复 + 400m 圈 → 混合圈约 4:33/km，够不到恢复判定阈值，退化为
    "圈速有波动的连续跑"，组距组配速全糊）；站休（0 距离恢复）不破坏对齐
    但也不产生慢圈，纯自动圈下会判为连续跑。两类盲区的彻底解都是秒级
    record 流分段（增强方向）。自动圈粒度随表上运动模式而异（实测佳明 FIT：操场模式
    400m/圈、路跑默认 1km/圈，均 distance 触发），**操场模式 + 手动按恢复圈是
    最理想的记录方式**——细粒度边界 + 恢复标记齐全，非 400m 整数倍的组距
    （如 1.2km = 3 圈合并）也能无损还原。映射示例：热身 2km → 10×1km（间休 400m）
    → 放松 1km 存为 [steady(2km), group{cycles:10, steps:[1km], recovery:400m}, steady(1km)]。
  - 两类边界情况：① 表上预设课表（FIT 含 workout_step 先验结构）：计划结构作为
    不解释的透传元数据保留（见"不存训练类型标签"），执行结构仍以计圈解析为准，
    计划 vs 执行（如 10 组只完成 7 组）在分析层对照；解析器读取 workout_step
    交叉验证留作增强。② 时间型课（如 5×3:00、骑行 10min @70%FTP）：
    steps/recovery 支持时长计量（dimension: distance|duration）、派生列补
    rep_dur_modal_s、nl 语法补时长型（`5×3:00 @3:35-3:42(均3:38)，间休 1:30`）、
    折叠按时长聚类——**随数据层首批实现**：骑行课以时间型为主，是骑行解析的
    前置；高驰课表 targetType 时间/距离双维度，写回互转不受影响。
  - record 流分段（第一阶段不做，设计留档）：无圈或圈不可用的记录按秒级
    record 流切成稳态段、再归并进同一 blocks/group 骨架——骑行台 ERG 是典型
    场景（功率阶跃清晰、用户不手动计圈、手表连骑行台也无自动圈，整段无圈
    可用），按功率阶跃检测切段；跑步无圈课与站休检测同走此路径（配速/停走）。

**schema 原则：存原语，不存模型输出。** TRIMP/分布投影/CTL/ATL/TSB 均为可重算的派生数据
（视图或缓存）；更换或新增负荷模型只需重算，不做数据迁移。

### athlete.db 表结构

多来源副本经"活动同一性"合并；活动与负荷派生数据可从 FIT 归档重算，
同步进度由独立的 sync.db 保存。`ingest --rebuild` 只重建 `activity` /
`activity_source` / `time_in_zone` / `daily_load`；`threshold_anchor` 含手动输入，
`daily_metrics` 来自平台健康拉取，两者都不是 FIT 派生数据，重建时必须保留。

**activity —— 逻辑活动（一次训练一行，去重后）**

| 列 | 说明 |
|---|---|
| `id` | PK |
| `start_ts` / `tz_offset_s` | 真实 UTC 开始时刻（epoch 秒）与“本地减 UTC”的时区偏移秒 |
| `sport` / `sub_sport` | 归一词汇：run/ride/swim/…；track/trail/indoor/… |
| `duration_s` / `distance_m` / `ascent_m` | 总量（SI 基本单位；配速 s/km、时速 km/h 是展示层换算） |
| `avg_hr` / `max_hr` / `avg_power_w` / `avg_cadence_per_min` / `calories` | 全场汇总 |
| `avg_speed_mps` | 派生缓存 |
| `trimp` | Edwards TRIMP 缓存（可重算） |
| `structure_json` | 课型结构整棵 JSON（文法见下节） |
| `type` / `rep_count` / `rep_dist_modal_m` / `rep_dur_modal_s` / `rep_pace_mean_s` / `interval_dist_total_m` | 结构派生过滤列（查询用，可重算；距离/时长双维度） |
| `zone_boundaries_json` | 当日 7 细区间边界快照（bpm 或 W） |
| `raw_meta_json` | 平台透传元数据（课表名/结构化步骤，不解释） |
| `parser_version` | 结构/分布解析器版本（重算追踪） |

`start_ts` 永远表示实际时间点，不得加时区偏移；同一性匹配和排序只使用 UTC。
本地活动日由 `start_ts + tz_offset_s` 派生，用于日期窗口和 `daily_load.date`，不另存
“本地 epoch”。缺少偏移时按 `config.toml` 的个人时区估算活动日，并在 coverage 标记；
不得让缺偏移影响跨来源同一性。旧库曾把偏移加进 `start_ts`，必须通过带版本号的
一次性迁移减回（或全量重建），以 `PRAGMA user_version` 防止重复迁移，随后重算负荷。

**activity_source —— 来源副本（活动同一性）**：`activity_id` FK / `platform` /
`platform_activity_id` / `fit_path` / `fit_sha256` / `is_canonical` / `note`；
UNIQUE(platform, platform_activity_id)。`fit_sha256` 用于识别字节一致副本及未来平台
改写造成的内容分叉；分析查询只过 canonical；副本标记存档不删。同一来源 ID 再次入库时，
只有哈希一致才是幂等 no-op；哈希不同必须保留原文件并报告冲突，不得直接跳过或覆盖。

**time_in_zone —— 强度分布（每活动 × 7 细区间）**：`activity_id` / `zone`(1-7) /
`time_s`，PK(activity_id, zone)；当日边界快照在 activity.zone_boundaries_json。

**threshold_anchor —— 阈值锚点（按日期版本化，每运动类型一行一版本）**：
`sport` / `valid_from` / `valid_to`（NULL=至今）/ `lt1_hr` / `lt2_hr` / `max_hr` /
`resting_hr` / `threshold_pace_s` / `ftp_w` / `critical_speed_mps` / `source` / `note`。
`resting_hr` 是 Edwards TRIMP 的必需输入：属全局指标（不随运动类型变），可随任一
sport 版本写入，计算时按活动日取所有 sport 行中最近生效的全局值。来源优先级：
平台 API 直读（高驰 queryRestingHeartRate /
佳明日度健康摘要，均为手表实测）> 手动填写；**不做从运动强度反推**——静息心率
是可直测指标，业界无反推算法惯例。

**daily_metrics —— 健康时序（每天每来源一行）**：`date` / `source` / `resting_hr` /
`hrv_ms` / `sleep_total_s` / `sleep_deep_s` / `sleep_light_s` / `sleep_rem_s` /
`sleep_awake_s` / `sleep_score` / `spo2_pct` / `stress_avg` / `body_battery` /
`steps` / `calories`，PK(date, source)；`body_battery` 属佳明 L2，他平台为空。

**daily_load —— PMC 派生缓存（可删可重建）**：`date` / `trimp` / `ctl` / `atl` /
`tsb`；由 canonical activity.trimp 按 42/7 天指数窗口聚合（视图或物化缓存）。
普通入库、全量 rebuild 和 canonical 切换后都必须重算；覆盖起点之前不是“零负荷”，
而是覆盖不足，覆盖范围内无活动的日期才可记为真实 0。任一活动 TRIMP 不可计算时，
当日总 TRIMP 为 `null`，且从首个未知负荷日起后续 CTL/ATL/TSB 都为 `null`，不得按 0
继续衰减；补齐锚点或来源后全量重建。查询可返回最后一个有效 PMC 日期，但必须显式
报告后续缺口，LT1/LT2 缺项只影响区间，不额外阻断 TRIMP/PMC 初始化。

sync.db 不在本 schema（同步状态机，经 platform_activity_id 关联）。

### 课型结构的形式化（存储文法）

`structure_json` 的树形只有一层循环嵌套，类 EBNF 可完整描述——其表达能力需求
甚至低于一般 CFG：

```
workout  := block+
block    := steady | group
steady   := { kind:"steady", role:"warmup"|"main"|"cooldown", segment }
group    := { kind:"group", cycles:int≥1, steps:step+, recovery? }
step     := segment + 统计 { pace_min_s, pace_max_s, avg_hr_max, count }
segment  := { dist_m } | { time_s }            -- dimension: distance | duration
recovery := { time_s_med, dist_m_med, standing:bool }
-- 平铺明细（文法之外的无损层）：reps 挂 group（每组实际 segment+pace+hr）
-- 或 steady（连续课的逐圈/逐趟明细，如游泳连续稳态逐趟）；recoveries=每次恢复实际
-- nl=由结构机械生成的人话摘要；禁止凭 nl 反推结构，故 nl 不进文法
```

三个例子：

```json
// 连续 13.32km @3:50（节奏）
{"type":"continuous","blocks":[{"kind":"steady","role":"main","dist_m":13320,"time_s":3061,"pace_s":229.8}]}

// 10×1.2km @3:21-3:33，间休 1:00(140m)（间歇）
{"type":"intervals","blocks":[{"kind":"group","cycles":10,
  "steps":[{"dist_m":1200,"pace_min_s":201,"pace_max_s":213}],
  "recovery":{"time_s_med":60,"dist_m_med":140,"standing":false}}],
 "reps":[{"dist_m":1190,"time_s":249.9,"pace_s":210.0,"avg_hr":156}, "…×10"],
 "recoveries":[{"dist_m":160,"time_s":60}, "…×9"]}

// 热身 2km → (2.4km+400m)×4 → 放松 1km（复合组）
{"type":"intervals","blocks":[
  {"kind":"steady","role":"warmup","dist_m":2000,"time_s":640},
  {"kind":"group","cycles":4,"steps":[{"dist_m":2400,"pace_min_s":225,"pace_max_s":230},
   {"dist_m":400,"pace_min_s":200,"pace_max_s":205}],"recovery":{"time_s_med":90,"dist_m_med":200,"standing":false}},
  {"kind":"steady","role":"cooldown","dist_m":1000,"time_s":330}]}
```

**文法管形状，不管两件事**（训练内容"超出 CFG"的真正位置）：
① 圈序列 → 结构的折叠推断是统计模式识别（配速比判恢复、容差聚类找周期），
是解析器的推断逻辑而非语法分析；② 跨字段约束是上下文相关语义——
standing⇔time<45 且 dist<60、pace_min≤pace_max、group 下 cycles×len(steps)
与 reps 计数一致（steady 下 reps 计数=圈/趟数）——由构建端校验器断言，不进文法。

### 跨运动类型适配（骑行等交叉训练）

- **结构与负荷模型跨类型成立**：block/segment 与度量无关（距离或时长 + 强度统计）；
  TRIMP（心率×时长）全类型通用；7 细区间体系本就源自骑行功率（Coggan）。
- **按类型换指标**：强度主指标跑步用配速/HR、骑行用功率/HR；阈值锚点按运动类型
  版本化（跑步阈值配速，骑行 FTP/临界速度）；存储统一 SI 原语（m/s、W），
  s/km、km/h 只是展示层换算。
- **解析按类型分派（随数据层首批实现）**：
  - 路跑/操场：圈级配速比判恢复（已实现，fit_laps.py 为参考实现）；
  - 骑行：先按 `ride_training_min_km`（默认 15、用户可配置）决定解析路径——
    有有效功率数据或距离达到阈值时，圈级**功率比**判恢复（无功率计退心率比，
    心率有滞后需放宽）；无功率且低于阈值时直接生成 continuous steady，不强行
    用心率波动找间歇。该分派只控制结构解析，不保存“通勤/训练”语义标签。
    首次扫到阈值候选时才请用户确认并保存；非交互进程按默认值完成、返回待确认，
    不挂起也不伪存确认，修改阈值后全量重建受影响结构。训练骑以时间型组为主
    （10min @70%FTP），按时长折叠与时长型 nl 渲染；无圈的骑行台 ERG 记录走
    record 流分段（见课型结构一节，第一阶段不做）；
  - **Zwift 本地是骑行的另一数据来源（第二阶段，设计留档）**：
    `Documents/Zwift/Activities/*.fit` 为执行数据一等来源（功率逐秒 record
    完整，正是 record 流分段的输入）；`Workouts/*.zwo` 课表 XML
    （SteadyState/IntervalsT，与 steady/group 同构）提供计划先验，可与执行
    对照；与手表 FIT 双录时走同一活动同一性规则（activity_source 多副本），
    来源优先级实现时细化；Zwift 即本地文件 adapter，不依赖其云端 API。
  - 游泳：**v0 不做趟级结构解析，只入 session 汇总、structure 置 NULL**——
    除四种泳姿外还有分解训练（只打腿/只划手等），趟级语义更复杂，解析器缓做；
    开放水域无 length 结构，视同"超大泳池单趟"，同样只入汇总。
    以下趟级规格为后续实现留档（经佳明 50m 池蛙泳 FIT 实测）：游泳 lap 消息
    无结构意义（手动一圈到底），圈=length 消息；session 带 pool_length（泳池长度）；
    length 含泳姿、划次、趟时长、平均速度(m/s)、length_type(active/idle)；
    SWOLF=趟时长+划次可派生；休息检测双通道——idle 显式休息趟 +
    相邻 active 趟 start_time 差 − 本趟时长推导隐式休息；存在伪趟（转身误检），
    需异常过滤（阈值由实现者按样本调，过滤必须记日志）；分组：休息=组内
    recovery（与跑步恢复圈同构，fold_reps 共享骨架），泳姿变化只在 cycle 内
    区分 steps（IM）；HR 存在但不保证（实测 avg 113 可用，sTSS 兜底仍必要）；
  - **越野跑不做结构解析**：上下坡使配速严重失真，圈级快慢交替不可判——
    只入 session 汇总，structure 置 NULL；坡组类结构待 record 流增强。
- **游泳（留档，随趟级解析器一同实现；经佳明 FIT 实测字段）**：模型层天然契合——游泳是结构化程度
  最高的运动（几乎全是"N×距离+休息"），group/steps/recovery 直接映射；
  包干制（on 1:45 出发）恢复=周期时长−游泳时长，存派生中位数；混合泳 IM =
  一个 cycle 内不同泳姿的 steps 逐元列出（休息=组内 recovery，泳姿变化只区分
  steps，分组折叠复用共享骨架）；开放水域无趟级结构，只入汇总
  （近似"超大泳池单趟"）。
  需要的扩展：① step 统计袋加游泳维度（泳姿 swim_stroke、SWOLF、划频、
  装备如脚蹼/划水掌）——统计袋本就按类型扩展（跑步配速/HR、骑行功率/HR）；
  ② 阈值锚点用 CSS（临界游泳速度），SI 存 m/s、展示层换算 s/100m；
  游泳 LT 心率比跑步低约 10bpm（卧姿+静水压），锚点按类型版本化已兼容；
  ③ 负荷模型：泳池光学 HR 不可靠，TRIMP 常无输入，需 sTSS（以 CSS 为基准）
  作游泳负荷模型——"负荷模型可替换重算"原则的延伸。

### 课表与训练计划

**长期粗、短期精。** 长期计划只定阶段与课程框架，短期课表才精确到距离/配速，
临近执行时按最近状态细化。两个形态、三个来源（教练 / AI 生成 / 平台导入）、
三个存储位：plans/ 档案是意图的权威文本，日志承载本周执行，平台日程是执行副本。

- **长期计划 → `memory/plans/` 档案**：每个计划一个文件
  （`YYYY-MM-DD-<名称>.md`，开始日期排序，与 injuries/races 档案同惯例）。新文件以
  `+++` 包围的 TOML front matter 提供机器可读元数据，正文仍为 Markdown：

  ```markdown
  +++
  title = "示例训练计划"
  status = "in_progress"
  start_date = 2026-11-02
  end_date = 2027-01-24
  source = "coach"
  +++

  # 阶段目标与每周框架
  ```

  必填 `title/status/start_date/end_date/source`；`status` 只用
  `in_progress / completed / aborted`，`source` 只用 `coach / ai / platform`；
  文件名日期与 `start_date` 一致。目标赛事与平台落位可用同名可选字段或正文补充。
  正文为阶段目标 + 每周课程框架（课型、大致量、强度方向），**不锁定具体配速**——
  精确参数临近执行时按最近负荷、恢复、天气与用户状态确定；尾部变更日志一行一条
  （日期、改动、原因），每周常规细化不算变更。旧计划缺元数据时可读并给 warning，
  不猜状态、不参与当前计划判定；新建或显式迁移后才使用上述格式。
- **其他记忆格式**：新建 `race:` 档案的 front matter 必填 `title/date/status`
  （status=`completed/dnf/dns`），新建 `injury:` 必填 `title/status/start_date`
  （status=`active/recovered`）；既有仅含 Markdown 标题的档案保持可读、可写，
  不要求先迁移，也不因旧格式拒绝更新。
  `races.md` 的“目标赛事”只把 `- ` 列表项视为赛事，统一写为
  `- 日期：YYYY-MM-DD；赛事：<名称>`（可分号追加备注）；非列表的“无”占位不是赛事。
  倒计时只取不早于配置时区今天、按日期升序的第一场，历史赛事与 `race:` 复盘不参与。
- **短期课表 → 日志 + 平台执行副本**：进入执行窗口（通常为当周）的内容细化为
  精确到距离/配速/心率的单次课表——本周要点写日志，经确认排入平台日程
  （目标平台默认跟随用户活跃手表，机器默认值记于 `config.toml` 的 `workout_platform`，
  athlete.md 只保留给人读的语境：高驰逐日单次课表，4–16 周完整计划可建训练计划实体，
  自带完成度跟踪；佳明无多周实体，降级为一组按日期排期的单次课表）。平台副本被临时
  改动时先记档案变更日志、再改平台，两边不漂移；执行反馈（完成状态/评分）读回写日志。
- **可发现性**：进行中计划在 athlete.md 挂一行指针（当前计划 + 第几周），
  bootstrap 凭此知道有活跃计划再按需展开；plans/ 档案不常驻。
- **生命周期**：完成/中止只改状态字段，档案永不删（历史可查"哪次成绩前执行
  什么计划"）；月度 summary 记一行当期计划与完成度；赛事复盘关联所执行的计划。
- **生成规则属大脑层**：AI 生成课表的讨论确认流程与设计思路要求见 §5 与
  coach-core.md §七。

### 路径约定

`OPENATHLETE_HOME` 环境变量优先，默认 `~/.openathlete/`。全部组件读写同一 home，不分叉。
`config.toml` 至少支持根键 `timezone`、`activity_since`、`workout_platform` 与
`primary_platform`；配置向导和 CLI 都能设置。首次查询前必须完成非密配置；使用 COROS
数据查询还需用户自行填写 Training Hub web 凭据，官方 MCP OAuth 只用于 L2 与 COROS
写回，Garmin 凭据只用于 Garmin API，三者不得互相冒充。缺配置时返回可执行的
`SETUP_REQUIRED` / `CREDENTIALS_MISSING`，不索要密码，也不默认全历史抓取。

## 4. MCP 工具契约（openathlete-mcp）

设计原则：

1. **读时同步 + 手动同步 + 定时同步**：查询工具按所需数据流检查 TTL（约 6h）、
   请求窗口覆盖和持久队列；只有“发现完成、相关队列清空、窗口已覆盖”时，`last_success`
   才可单独代表新鲜。仍有 backlog 时，即使上一批刚成功也对每个相关 stream 各继续
   单批处理，并返回 `complete:false/stale:true`，不得停顿一个 TTL。`sync_now` 覆盖“刚训练完立即分析”；
   系统级定时增量同步（§7）保持常新。
2. **进程随宿主会话起止**：启动只做本地初始化，不触网；空闲零 CPU。
3. **启动与就绪分离**：`initialize` / `tools/list` 只依赖静态 schema，不等待数据库或
   adapter 初始化。本地初始化只创建一个后台任务，工具调用共享并有界等待它；超时返回
   `STARTING`（`retryable:true`、`details.retry_after_s`），初始化异常返回
   `STARTUP_FAILED`，不并发重复初始化，stdio 关闭时回收任务异常。`readOnlyHint` 只标
   真正无副作用的工具，带读时同步、下载或写入副作用的调用不得标成只读。
   COROS L2 代理遵循同一契约：首轮立即返回随插件版本提供且已完成 `$defs` 修复的
   工具声明，运行时缓存只作为较新副本回退；官方连接、声明刷新与缓存写在后台共享
   一个任务，成功后才透传调用。缓存原子替换，上游会话活到 stdio 关闭。不得为了等待
   官方 tools/list 再阻塞首轮目录，也不依赖后发的目录变更通知补齐首轮工具。
   Codex 适配还必须处理 optional server 的首轮目录捕获宽限：OpenAthlete server 在
   Codex 专用配置中标记 `required=true` 并设置 `startup_timeout_sec=120`，让宿主先完成
   目录初始化，失败时明确报错而不是静默缺工具；COROS 保持 optional，避免 OAuth 未配置
   阻断整个会话。手动配置也可用
   `mcp_optional_startup_grace_ms=0` 配合 `startup_timeout_sec` 等待；不把
   `notifications/tools/list_changed` 当作模型目录刷新的保证。
4. **读写分离**：写工具（课表/计划写回手表）默认要求用户确认，不静默执行。
5. **错误即文档**：认证失效返回结构化引导（重新授权路径）；任何失败给下一步建议。
6. **长任务分批**：大同步拆小批（约 20 条/次）+ 断点续传，返回剩余量由模型循环。
7. **记忆读写走 MCP 工具**：`~/.openathlete/` 在宿主 workspace 之外，agent 通过
   `read_memory`/`write_memory` 访问；历史枚举用 `read_memory` 的白名单 list 模式，
   不增加任意路径读取能力。
8. **平台差异抹平：统一“问题”而非“API”。** 接口由教练的分析工作流决定，不由平台能力
   清单决定；不为任何平台重造别家的云端模型。分三层：
   - **L0 统一核心**（下表工具）：每平台 adapter 必须实现，返回统一 schema——同一工具
     回答同一问题；平台原生数据作为原始活动、健康指标和阈值锚点输入，而不是各用
     各的云端模型回答统一负荷问题；
   - **L1 本地统一计算**：Edwards TRIMP + PMC + 细粒度时间分区，从 canonical FIT 与
     活动日有效锚点计算（§3），一次实现全平台受益；统一负荷不混用平台原生结果；
   - **L2 平台特有能力**：命名空间透传（如 `coros.*` 工具、佳明 Body Battery 字段，
     以及需要单独展示的原生负荷），不归一、有则用之，不回写统一 daily_load。
   新平台（Suunto 等）只加 adapter，不改变接口形状。

易用性契约（**第一前提**——用户可能是非技术人群，只会轻度使用）：

1. **对话直达**：用户永远不需要知道工具名与参数。查询参数尽量有默认值，默认即
   最常用场景（时间窗最近 7 天 / 今天）；记忆定位与内容、课表日期与课程、第二次
   写调用的 confirm_token 等按 schema 必填，保证模型“一次调对”。
2. **返回自包含**：一次调用返回关键数字 + 数据覆盖范围 + 简短解读，避免模型多次
   往返拼装。
3. **术语归一**：统一工具的字段名只讲统一词汇（恢复、负荷、睡眠），平台差异不外泄；
   平台特有能力只出现在 `coros.*` 等命名空间。
4. **写操作两段确认**：写工具首次调用只返回预览 + 一次性 `confirm_token`
   （10 分钟有效）；模型向用户复述内容、用户同意后带 token 和原课表再调才执行。
   token 绑定规范化课表与目标平台，用于正常流程下的防重放和内容核对，不承诺跨进程
   exactly-once，也不要求稳定账号 identity_hash 或租约恢复协议。重复排期不是数据损坏：
   若同一天出现两份课表，用户开跑前选择一份即可。超时或结果不明时不做后台自动重试；
   用户可先查平台日程，也可在知情后重新预览、再次确认创建。

**数据抓取不经平台 MCP**：活动列表、FIT 下载、健康数据的拉取是数据层内部职责，
adapter 直连平台 API（佳明走 garth / python-garminconnect；高驰走 web API），
FIT 归档在本地完成后入库；统一 MCP 工具只面向 AI 交互（查库、记忆、写回、L2 透传）。
高驰官方 MCP 的 FIT 下载限额（50 个/24h）因此不构成数据层瓶颈。

**同步覆盖契约**：`get_overview` 需要活动与日度指标两路，`query_activities` /
`get_training_load` 只需活动，`get_recovery_status` 只需日度指标。活动发现按 COROS
日期接口固定窗口分页，48 小时 overlap 转成天级查询后再按秒过滤；日度指标用独立的
日期覆盖窗口，首次 28 天、后续重取最近日期，不能套用活动分页游标。multisport 队列
按父活动计 20 条上限，下载与入库按 `parent#i` 段及段哈希幂等，全部段完成后才标 done。
每次查询或 `sync_now` 对每个所需 stream 各最多处理一批，不在 server 内自动循环。
首次活动范围由 `activity_since` 决定（向导建议最近 90 天）；更早活动、指标或 PMC
初始化历史不被静默伪造成空值/零负荷，而是返回实际覆盖、`complete:false` 和显式 CLI
回补建议，不由 MCP 暗中发起全历史下载。显式回补也复用同一持久状态机，只有整个连续
窗口发现完整且全部入库成功才扩展 coverage；dry-run、limit、部分失败或只归档未入库
均不得把窗口标为已覆盖。

**统一课表写回**：`create_workout` 根据 `workout.platform`（缺省取 `config.toml` 的
`workout_platform`）路由。COROS 由包内 client 调官方远程 MCP，使用官方 OAuth；
Garmin 直接复用已验证的 API mapper 与创建/排期服务，不经过任何 Garmin MCP。
预览只做本地能力校验；不支持的字段明确报错，不在平台间静默换算。任意层级未知字段
同样拒绝，不允许预览保留、执行时静默丢弃。Garmin 创建成功但排期失败时必须明确
“课表已在库中”，不得自动重新创建。平台结果已知后，本地确认审计写库失败不得覆盖
原平台结果或部分成功异常；返回原事实并附 `outcome_recorded:false` / warning。
两套平台凭据与 COROS web 抓取凭据分别加载，互不替代。

工具清单（核心工具总表 + 平台命名空间；分阶段范围见落地顺序）：

| 工具 | 参数（查询有默认值；定位/写入按 schema 必填） | 返回 |
|---|---|---|
| `get_overview` | days=7 | 一页看板：训练次数/时长/负荷趋势、今日恢复、下一赛事倒计时——“最近怎么样”一次拿全 |
| `query_activities` | days=7、sport、limit=20、detail=false | 活动列表与关键指标；detail=true 附细区间分布与 FIT 详情 |
| `get_training_load` | days=42 | 统一本地 TRIMP+PMC 的 CTL/ATL/TSB 趋势 + 锚点/活动覆盖范围 + 人话解读；平台原生负荷仅作独立 L2 参考，不混入 |
| `get_recovery_status` | date=今天 | 各来源睡眠 HRV、静息心率等；保留 sources，主平台只选择展示源，不平均、不伪造统一恢复评分，缺源明确标出 |
| `get_weather_forecast` | date、location=个人资料默认 | 训练/赛事日天气（第三方 API，后续切片） |
| `sync_now` | — | 立即增量同步；长任务分批，返回本次完成数与剩余数，由模型循环 |
| `get_current_plan` | date=今天 | 进行中计划 + 本周细化课表 + 平台排期状态——“今天练什么”与晨报一次拿全 |
| `read_memory` / `write_memory` | name 或白名单 list；写入时 name/content | 读写/枚举 memory/ 下的 Markdown（含分层档案）；write 做模板与并发校验 |
| `create_workout` / `create_training_plan` | 课程结构、目标平台、confirm_token | 首次返回预览 + token；带 token 执行写回。多周计划后续切片中，佳明降级为一组按日期排期的单次课表 |
| `coros.*` 等 | 平台命名空间 | L2 平台特有能力透传（EvoLab 成绩预测、Body Battery 等），不归一、有则用之 |

**openathlete-mcp v0 切片**：只实现 `get_overview` / `query_activities` /
`get_training_load` / `get_recovery_status` / `sync_now` / `get_current_plan` /
`read_memory` / `write_memory` / `create_workout` 九工具；天气、多周计划和
Garmin 自动抓取后置，**COROS 与 Garmin 的单次课表创建/排期均在 v0 内**。
v0 的 `get_current_plan` 只读本地计划与日志，`platform_schedule.available=false`，
平台排期读取后续接入。官方 `coros` server 继续独立提供 L2，统一 server 不嵌套本地
stdio 代理；Garmin API 写回也不需要额外 server。

## 5. 大脑层：教练人格与分析框架

- 一份 Markdown 定义人格、分析工作流（先负荷趋势 → 再恢复 → 对照伤病/赛事倒计时 → 给建议）、
  记忆维护规则（对话涉及伤病/赛事/主观感受时主动落盘）。
- Kimi Code 侧注入插件 `systemPrompt`；OpenClaw 侧作为 `SOUL.md` 主体。**内容同源维护**。
- AI 永不索要密码，只引导用户自行配置凭据。
- **人格文件拆两层**：
  - `coach-core.md`：随插件分发、**用户不可改**——运动科学底线与安全规则（健康优先、
    伤病分级处理、循序渐进、拒绝以危害健康的方式追求成绩），随插件版本升级（升级覆盖还原）；
  - `coach-user.md`：`~/.openathlete/` 下、**可微调**——训练哲学（激进/保守、处方体系偏好、
    强度分布词汇偏好）、说话方式与语气、建议偏好；缺失时由配置向导生成默认模板。
  注入时 core 在前并声明其优先级高于 user；core 同步为 OpenClaw SOUL.md 的固定段。
- **注入机制（Kimi Code v0）**：core 经插件 `systemPromptPath` 常驻系统提示；user 文件在
  `~/.openathlete/`、不在插件包内，无法经 manifest 注入，由 sessionStart skill 的
  bootstrap 协议在每次会话开始时读取（缺失时配置向导按插件内模板生成）。
- **角色模式：AI 全托管 / 已有教练 / 混合**。已有真人教练的用户，课表权威在教练，
  AI 退为数据解读、执行跟踪（完成度/身体反馈）、风险提醒与经确认的写回执行；
  与教练方案有分歧时摆数据给用户去与教练沟通，不擅自改处方。模式与调整授权范围
  写在 `coach-user.md`（配置向导询问生成），角色边界规则在 `coach-core.md`（不可定制）。
  AI 全托管 / 混合（授权范围内）模式下 AI 可生成课表：必须先与用户讨论、讲清
  设计思路（训练框架/目标赛事/各阶段与关键课练什么）、核对执行可行性
  （气候季节/可用时间/场地）、综合已存用户信息，确认后落盘——完整规则在
  coach-core.md §七，存储规范在 §3"课表与训练计划"。
- **"可改/不可改"的实现**：agent 的行为规则是**人格微调一律写 coach-user.md，永不改
  coach-core.md**——用户说"教练应该用 xxx 语气说话"，agent 只改 user 文件；
  用户若自己找到 core 手动修改，是其自由（升级会覆盖还原，属预期行为）。
- **护栏只到 prompt 层**：底线约束只经 core 文件 + prompt 注入生效，MCP 写工具不做参数
  硬校验——固定规则无法区分"冒进"与"精英运动员恢复训练/高跑量既有水平首次接入"，
  硬性拒绝会误伤。用户若说服模型绕过底线，属于用户与模型的责任边界。

## 6. Kimi Code 插件形态（第一交付物）的能力与边界

manifest：`skills/` + `systemPromptPath`（教练人格）+ `mcpServers`。
MCP server 分两阶段：v0 为插件内 PEP 723 脚本（`uv run`）；PyPI 发布后切换
`uvx openathlete-mcp`。安装后首次提问自动进入配置向导（引导区服/账号/webhook）。
**卸载与移植**：一切用户状态（库、归档、记忆、token、日志）只写 `~/.openathlete/`，
插件目录与 uv 缓存不含用户数据，三种操作都只围绕这一个目录：
`/plugins remove` + `uv run uninstall.py` 清插件残留（数据保留，默认）；
`--purge-data` 软删（数据挪为时间戳备份，可后悔）；
`--hard-delete` 硬删（数据/备份/token/托管副本/登记全删，不可恢复——用户的生物数据，
提供被遗忘的能力；平台侧授权只能由用户在佳明/高驰账号设置中撤销，脚本结尾给出指引；
`--include-kimi-credentials` 可同时清理 kimi 凭据库中的 coros 直连 token）。
`uv run migrate.py export/import` 把数据 home 整体打包换机，token 随包恢复。

**行为与启动目录无关**：插件 per-user 全局生效、`~/.openathlete/` 为绝对路径，
用户在任何目录启动会话，产品行为与数据访问完全一致。产品行为只能经
`systemPromptPath`/SKILL.md 注入；仓库内的文档只在 cwd 位于仓库内时被读到，
不构成插件行为。晨报脚本固定在中性目录（如 home）运行，避免 cwd 项目内容干扰。

**最佳实践：固定一个专用目录**（如 `~/training`），并初始化用户自己的 AGENTS.md 写明
"本目录的会话默认围绕我的训练"——"最近怎么样"这类开放提问即被自动解读为训练语境
（负荷变化、课表完成度、离目标成绩的差距）。配置向导应主动引导这一步。

形态上限：

1. 训练问答（同步、分析、建议，含伤病/赛事/天气综合）
2. **任意深度数据分析**：宿主是完整代码环境，可跑 Python、出图表、生成报告文件
3. **记忆自维护**：对话即记录，跨会话记忆在插件形态内闭环
4. 课表/训练计划写回手表（需确认）
5. 赛前战术文档（天气预报 + 赛道 GPX 解析 + 能力数据 → 配速/补给策略文件）

硬边界：

- 手机双向对话依赖电脑在线：经 Kimi Code 远程连接（Remote Control，`kimi rc` /
  `kimi web --remote-control`）可从手机接管桌面会话，电脑关机即不可用
- 推送单向；电脑关机时计划任务不执行
- 平台 API 风险：COROS 非官方 Training Hub web API 与 Garmin 非官方 API 可能变动；
  凭据或 token 过期需引导重新授权
- 每次会话/推送都是全新会话，"记忆"完全来自数据层文件（这是特性不是缺陷）
- 分析/推送消耗用户的 Kimi Code 额度，README 需说明
- **佳明→高驰同步判重有边界**：上传前按高驰开始时间 ±120 秒做服务端判重，
  查询失败时回退本地 sync.db；这不是完整的多来源同一性方案。双表/多来源仍须执行
  同步预检（问双表使用 + coros 工具查重叠 + 用户确认，见 SKILL.md），高驰不能批量删除；
  分析侧统一去重按 §3 活动同一性规则。
- **v0 面向 AI 的结构化数据读取仅有高驰通道**（佳明活动经同步汇聚后从高驰读出）；
  在 Garmin 活动/健康自动抓取产品化前，"只用佳明"的用户没有结构化活动/健康查询工具——
  配置向导必须如实告知并给出选项（等待该能力，或注册免费高驰账号做汇聚后端）。
  佳明阈值与日度健康直读已在 dev 探针和 adapter 验证，但尚未形成自动同步通道。
  这不限制已验证的 Garmin API 单次课表写回；数据读取与课表写入是独立能力边界。

## 7. 每日推送链路

**不用 Kimi Code 内置 cron**（会话级：退出停摆、循环任务 7 天自动过期），用操作系统级调度：

```
系统计划任务（每天定时）
  → kimi -p "生成今日训练晨报"     # 无头模式，插件全局生效，MCP 读时同步
  → 脚本将 stdout POST 到飞书自定义机器人 webhook（只推不聊）
```

推送脚本随 `openathlete-mcp` 包分发，安装时引导用户注册计划任务。
同一调度机制承担**定时增量同步**（如每小时一次轻量拉取），晨报与问答始终基于新鲜数据。
双向对话在电脑在线时经远程连接（§6）解决；常驻双向入口由 OpenClaw 渠道（§8）满足。

## 8. OpenClaw 移动渠道（可选，第二阶段）

- 定位：电脑不在线时的移动双向入口 + 常驻主动推送（电脑在线时手机对话可由远程连接达成）。
- **只服务已有 OpenClaw 的用户**：交付物为模板包（SOUL.md 教练片段 + memory 模板 +
  cron 晨报配置 + 挂载同一 `openathlete-mcp`），按 OpenClaw 社区 npm 插件模式分发。
- 不要求新用户为此安装 OpenClaw（gateway + 模型 key + 飞书自建应用成本过高）；
  如未来要降低门槛，再考虑 bootstrap 安装脚本。

## 9. 分发策略

| 目标用户 | 交付物 | 摩擦 |
|---|---|---|
| Kimi Code 用户 | GitHub 插件（`/plugins install`，内含 MCP——v0 为插件内脚本，后迁 uvx） | 极低 |
| 其他 coding agent 用户 | 仓库本身 + AGENTS.md；PyPI MCP | 低 |
| OpenClaw 用户 | 模板包（§8） | 低 |
| 普通跑者 | **当前无解**；终局是托管服务（后端 + 飞书应用/小程序），列为北极星，现在不建 | — |

数据层与 MCP 对以上全部渠道复用，永不为单一渠道重写核心逻辑。

### 跨 harness 适配矩阵

可移植性来自三个开放标准：MCP 协议（`uvx openathlete-mcp` 启动命令各家逐字不变）、
AGENTS.md/SKILL.md 跨家约定（SKILL.md 即 Anthropic Agent Skills 格式）、数据层纯文件。

| Harness | MCP 接入 | 人格注入 | 无头模式 | 分发形态 |
|---|---|---|---|---|
| Kimi Code | 插件清单声明 | `systemPromptPath` + SKILL.md | `kimi -p` | `kimi.plugin.json` |
| Claude Code | `claude mcp add` / `.mcp.json` | CLAUDE.md 引用 + 同一 SKILL.md | `claude -p` | 自家插件格式（薄适配） |
| Codex | 原生插件 `.mcp.json` / `config.toml` `[mcp_servers]` | AGENTS.md + 同一 SKILL.md 读取 coach-core | `codex exec` | `.agents` marketplace + `.codex-plugin` 薄适配，或 clone 仓库 |
| Gemini CLI | settings.json | GEMINI.md 引用 | `gemini -p` | 同 Codex 思路 |
| OpenClaw | gateway 挂 MCP | SOUL.md（同源） | 内置 cron | npm 模板包 |

**适配原则：每家只做"配置片段 + 指令入口 + 晨报命令映射"的薄适配，核心代码零改动。**
支持 `systemPromptPath` 的宿主由清单注入 coach-core；其他宿主在 SKILL 入口读取同一文件，
不复制人格文本。晨报脚本把 harness 命令抽象为可配置项。已知差异：各家模型不同，
同一教练 prompt 的分析质量需分别调优。

## 10. 硬边界与风险汇总

- 移动端双向对话依赖电脑在线（远程连接）；常驻移动入口与主动触达仍需 OpenClaw/未来托管
- 推送单向、依赖开机
- 上游接口变动（COROS 非官方 API、Garmin 限流/MFA、单点登录互踢，详见 AGENTS.md 已知坑）
- 健康/伤病数据敏感：全本地存储是卖点，不迁云；README 写明云端 LLM 会见到上下文数据

## 11. 落地顺序

1. **v0 插件 dogfood**（AGENTS.md 阶段三）——最小可用形态：coros 代理自足化
   （PEP 723 `uv run`、状态入 `~/.openathlete/`）+ 教练人格 v0 + 使用引导 skill；
   本地安装、在任意目录真实使用，反馈回流文档。不等数据层做完，先验证分发与使用形态；
2. **数据层 + 记忆体系**——`athlete.db` schema、记忆模板与维护规则、配置向导、
   读时同步落地（COROS 抓取走 Training Hub web API；COROS 官方 MCP 仅用于 L2、
   日程与写回；Garmin 数据抓取 adapter 后续接入，已有 Garmin API 课表服务直接复用）；
3. **openathlete-mcp 工具集完善**（§4，统一单次课表写回路由到 COROS 官方 MCP 或
   Garmin API）+ PyPI 发包切 uvx（阶段四）；
4. **webhook 晨报**（§7）；
5. **OpenClaw 模板包**（§8）；
6. **增强**：赛事战术自动化、课表写回完善、Suunto adapter。
