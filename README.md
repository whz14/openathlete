# OpenAthlete

<!-- mcp-name: io.github.whz14/openathlete -->

**让 coding agent 成为真正懂你的 AI 教练。**

OpenAthlete 统一佳明（Garmin）与高驰（COROS）的数据和能力，并跨会话记住你的
训练历史、伤病、目标与偏好，让 AI 在完整的个人语境中理解和帮助你。

## 能帮你做什么

训练与恢复、伤病与风险、比赛目标与战术、营养与装备、数据分析与资料查询……
都可以直接讨论。它不局限于一套固定命令：凡是你能想到、AI 能结合数据和工具
完成的运动相关需求，都可以先问它。

## 安装与开始

**Kimi Code**：在会话中输入：

```text
/plugins install https://github.com/whz14/openathlete
```

**Codex**：在终端执行：

```bash
codex plugin marketplace add https://github.com/whz14/openathlete && codex plugin add openathlete@openathlete
```

**Claude Code 或其他 MCP 宿主**：把这句话发给 agent：

```text
使用 uvx openathlete-mcp 配置 OpenAthlete，并按 https://github.com/whz14/openathlete 的 AGENTS.md 完成初始化。
```

独立 MCP 包已发布到 [PyPI](https://pypi.org/project/openathlete-mcp/) 和
[MCP Registry](https://registry.modelcontextprotocol.io/v0.1/servers?search=openathlete)，
可由 agent 按需查询。

首次安装后按宿主要求重载或重启（Kimi Code 使用 `/reload`），然后另开一个与运动相关的
会话，即可进入初始化向导。环境、MCP、平台授权和偏好设置由 AI 引导或代办，
无需手动编写 MCP 配置。

之后不用背命令。直接问“你能做到什么”“你能不能……”“如何……”，它会说明
能做什么、需要哪些数据或授权，并带着你完成。

## 使用边界、隐私与风险

- **需要你自己的 agent 与模型**：OpenAthlete 不提供模型订阅或使用额度。分析能力
  取决于所选模型，易用性取决于 coding agent、可用工具与数据完整度；AI 可能出错，
  重要判断请复核。
- **数据保存在本机，不等于数据不出机**：安装更新会访问代码与依赖下载源，平台查询
  和同步会访问对应 API，云端模型会收到随对话上下文发送的训练、健康和记忆数据。
  账号凭据由你按向导在本机填写，不要贴进聊天。
- **平台接口有边界**：部分能力使用非官方接口，存在风控、限流和失效风险；数据是否
  完整也取决于平台开放能力和你的账号。高驰采用单点登录，同步或授权期间打开高驰
  App/网页可能互踢；佳明账号需关闭两步验证（MFA）。
- **写入与健康边界**：课表、计划等写回平台前会先展示内容并经你确认。AI 教练不提供
  医疗诊断；出现严重伤病迹象请就医。

## 反馈与致谢

欢迎通过 [Issues](https://github.com/whz14/openathlete/issues) 提问题、需求或使用
场景，也欢迎按 [贡献指南](CONTRIBUTING.md) 提交改进。

感谢 [XiaoSiHwang/garmin-sync-coros](https://github.com/XiaoSiHwang/garmin-sync-coros)
提供的佳明→高驰同步基础，以及 [garth](https://github.com/matin/garth) 提供的
佳明通信能力。

本仓库自有代码以 [MIT](LICENSE) 发布。面向 coding agent 的完整文档见
[AGENTS.md](AGENTS.md)，产品与架构约定见 [DESIGN.md](DESIGN.md)。
