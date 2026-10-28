# OpenAthlete

**把你的运动数据交给 AI。** 统一佳明（Garmin）与高驰（COROS）的数据能力，
让 coding agent 成为懂你的 AI 教练：它记得你的训练史、伤病和目标赛事，
能分析训练状态、给出建议，并把排好的课表直接写进你的手表。
教练风格可以按你的喜好微调，但运动科学的底线它不会让你碰。

> English: OpenAthlete unifies your Garmin and COROS data for coding agents,
> turning the agent into a personal AI coach that remembers your training history,
> injuries and goal races, gives data-driven advice, and schedules workouts back
> to your watch. Data files stay on your machine; platform APIs and cloud models
> still receive the data needed for synchronization and AI conversations.

## 它能做什么

- **训练问答**：最近练得怎么样、负荷涨没涨、恢复够不够、今天该不该上强度——
  结合训练负荷、恢复状态、睡眠 HRV 和你的伤病/赛事语境作答
- **跨会话记忆**：训练日志、伤病史、目标赛事、能力画像全部记住，越用越懂你
- **佳明 → 高驰活动同步**：断点续传与服务端判重，降低换电脑或双表汇聚时的重复风险
- **课表写回**：AI 排的课表经你确认后写进佳明手表或高驰 App；
  佳明走已验证的 API 创建/排期服务，高驰走官方 MCP，多周计划按平台能力降级
- **课型识别**：间歇/变速课还原真实结构（几组 × 组距 × 组配速 × 恢复），
  不被平均配速误导

## 安装

- **Kimi Code 用户**：`/plugins install <本仓库 GitHub URL>`，装完 `/reload`，
  随便问一句训练相关的问题，就会自动进入配置向导——选平台区服、授权、
  环境配置全部由 AI 当场代办（包括唯一依赖 [uv](https://docs.astral.sh/uv/) 的自动安装）
- **Codex 用户**：原生插件执行
  `codex plugin marketplace add <本仓库 GitHub URL>`，再执行
  `codex plugin add openathlete@openathlete`；重开会话后按技能入口完成配置
- **其他 coding agent 用户（Claude Code 等）**：clone 本仓库，
  让你的 agent 读根目录 `AGENTS.md`——包括 uv 在内的环境配置都交给它完成

Codex 手动配置 MCP 时，OpenAthlete server 使用 `required=true` 和
`startup_timeout_sec=120`；若不希望 required server 失败时阻断会话，可改用
`mcp_optional_startup_grace_ms=0` 配合该启动超时，避免首轮工具清单缺工具。
当前插件的统一层与 COROS 代理均已采用静态声明先行、后台初始化的方式，
optional COROS 无需额外设置；上述宽限参数主要用于旧版本或手动配置。

Kimi 网页安装若返回 504，先核对已安装列表和 managed 文件是否已更新，不要仅凭超时
重复安装。Codex 以本地目录作为 marketplace 时会复制目录内容，只使用干净的发布拷贝，
不要指向含凭据或训练数据的工作目录。

所有数据（凭据、训练库、FIT 文件、记忆）都在你本机的 `~/.openathlete/`，
卸载插件不动数据；自带换机迁移与彻底删除（被遗忘权）工具。

## 统一 MCP

源码与本地包已提供 9 个统一工具：训练看板、活动查询、统一负荷、恢复、
手动同步、当前计划、记忆读写和双平台单次课表写回。

```bash
# 源码入口（插件也使用这一入口）
uv run ./openathlete_mcp.py

# 安装后的等价入口；裸 openathlete-mcp 也会启动同一 server
openathlete-mcp serve
```

首次使用先完成非密配置与凭据向导（详见 `AGENTS.md` / 插件配置向导）：

```bash
openathlete-mcp db config set --timezone Asia/Shanghai \
  --activity-since YYYY-MM-DD --workout-platform garmin \
  --primary-platform garmin
```

课表写回始终先预览、经用户确认才执行；COROS 走官方 MCP OAuth，Garmin 走
已验证的 API 创建/排期服务。天气、多周计划和 Garmin 自动活动/健康抓取仍属后续。
**尚未发布 PyPI**；本地 wheel 可用 `uvx --offline --from <wheel> openathlete-mcp`
启动。统一通道尚未做真实账号同步或写回验证；mock 与真实 MCP client 测试不冒充
真实账号验证。

## 隐私与风险（使用前请读）

- **数据文件保存在本机，不等于数据不出机**：安装与更新会访问 GitHub、PyPI 等下载源；
  同步和查询会访问佳明/高驰接口；与云端大模型对话时，随上下文发送的训练、健康和
  记忆数据也会出站，请按所用模型服务的隐私政策自行评估
- **官方与非官方接口并存**：高驰官方 MCP 之外，数据抓取还使用非官方 Training Hub
  web API；佳明 API 基于社区逆向成果。非官方接口有风控、限流和失效风险，请自行评估
- 高驰是单点登录：同步或授权期间不要打开高驰 App/网页，否则会互踢导致失败
- 佳明账号需先关闭两步验证（MFA）
- AI 分析与每日推送会消耗你所用 coding agent 的云端模型额度
- AI 教练不提供医疗诊断；出现严重伤病迹象请就医

## 致谢

- 佳明→高驰同步基于 [XiaoSiHwang/garmin-sync-coros](https://github.com/XiaoSiHwang/garmin-sync-coros)
  作者的工作，非常感谢
- 佳明通信基于 [garth](https://github.com/matin/garth)（MIT）

## License

本仓库自有代码以 [MIT](LICENSE) 发布。
面向开发者和 coding agent 的完整文档：[AGENTS.md](AGENTS.md)（工程事实源与运行手册）、
[DESIGN.md](DESIGN.md)（产品设计契约）。
