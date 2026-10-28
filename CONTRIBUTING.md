# 参与开发

本文规定 OpenAthlete 的日常开发流程。项目结构、运行手册和产品设计契约分别见
[AGENTS.md](AGENTS.md) 与 [DESIGN.md](DESIGN.md)。

## 开发环境

前置条件是 Git 和 [uv](https://docs.astral.sh/uv/)；Python 由 uv 按项目版本管理。

```bash
git clone https://github.com/whz14/openathlete.git
cd openathlete
uv sync --locked --extra dev
```

同步客户端等额外开发依赖及完整命令见 `AGENTS.md`。不得修改或重新分发
`upstream/`；真实账号验证必须使用 `--dry-run` / `--limit` 小量运行。

## 分支与提交

1. 从最新 `main` 创建短生命周期分支，分支名使用 `<类型>/<简述>`，例如
   `feat/recovery-view`、`fix/timezone` 或 `chore/ci`：

   ```bash
   git switch main
   git pull --ff-only origin main
   git switch -c feat/short-description
   ```

2. **所有变更必须通过 Pull Request 合并到 `main`，包括维护者自己的变更。**
   禁止直接推送、强制推送或删除 `main`，不得使用管理员权限绕过保护规则。
3. 一个 PR 聚焦一个目标，提交信息说明实际改动。不要混入格式化、重命名等无关变更。
4. 新功能优先进 `src/openathlete/`，新模块必须配必要的 pytest 测试；
   解析与契约逻辑尽量和 IO 分离。
5. 依赖变化必须同时更新 `pyproject.toml` 与 `uv.lock`，不得为绕过失败而删除测试、
   缩小测试范围或放宽断言。

## 测试与文档

提交 PR 前运行完整测试，不能只运行与改动相关的子集：

```bash
uv run --locked --extra dev python -m pytest -q
```

`CI` 工作流会在 Ubuntu、Windows × Python 3.10、3.12 下运行同一完整测试命令；
它先按既定提交 `06cf52e` 检出测试所需但未入库的 `upstream/`，不依赖本机文件。
四个矩阵任务不会因其中一个失败而提前取消；稳定的 `tests` 汇总任务只有在整个矩阵
成功时才通过。定向测试可用于开发调试，但不能替代提交前的全量验证。

行为、接口、安装方式或开发流程变化必须同步文档；产品或架构方案变化先更新
`DESIGN.md`，工程事实与运行手册变化更新 `AGENTS.md`。

## Pull Request 与合并

1. 推送功能分支并创建目标为 `main` 的 PR，填写模板中的变更说明和真实验证结果。
2. 处理审查意见，保持分支与最新 `main` 同步且无冲突。
3. 只有 `CI` 工作流的 **`tests` 检查成功**后才能合并。不得将本地测试、
   其他 job 成功或失败的非必需检查当作替代，也不得跳过红色检查。
4. 当前不强制审批人数；协作人数允许时可再启用至少一人审查，不能要求作者批准自己的 PR。

## `main` 保护规则（仓库管理员）

文档约定必须由 GitHub 仓库设置落实。进入 **Settings → Rules → Rulesets → New branch
ruleset**，创建名为 `protect-main` 的规则集并设为 **Active**：

- **Target branches**：默认分支，或显式包含 `refs/heads/main`。
- **Bypass list**：为空，不给维护者或应用额外绕过权限。
- 启用 **Require a pull request before merging**，所需审批数保持为 0。
- 启用 **Require status checks to pass**，选择 `tests`，并要求分支在合并前保持最新。
- 启用 **Block force pushes** 和 **Restrict deletions**。

首次配置时若还搜索不到 `tests`，先保存其余规则，在首个 PR 触发 CI 后立即将它补为
必需检查；补齐前不得合并该 PR。规则集不是 Active 或缺少 `tests` 时，不能声称已强制
执行上述流程。

## 凭据与私人数据

- 不提交 `.env`、credentials、token、数据库、FIT、训练记忆、`STATUS.local.md`、
  本机绝对路径或本机 MCP 配置；也不要在 PR 描述和日志中贴出这些内容。
- 不让 CI 访问真实佳明或高驰账号。测试使用合成数据和 mock，真实账号结论必须单独
  标明验证范围，不能以 mock 代替。
- 调试日志和临时响应若可能包含个人数据，只保留在本机已忽略的路径。
