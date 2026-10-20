# W01 实现与复现说明

本文件说明为接入所选模型对标准产品做的适配、复现步骤和证据边界，供源码抽查使用。产品语义、业务规则、契约字段未改动。

## 1. 模型与适配

- 模型端点：`https://ark.cn-beijing.volces.com/api/coding`（Anthropic Messages 协议）
- 模型名：`deepseek-v4.1-flash`
- 协议标记：`anthropic_messages`（写入 `artifacts/w01/model_baseline.json`）
- 密钥只通过环境变量 `FDE_MODEL_KEY` 传入，不写入仓库、证据或日志。

标准产品原适配器只支持 Chat Completions。本次新增 `AnthropicModel`，在内部保持产品原有的消息/工具形状，仅做协议转换：

| 文件 | 改动 |
| --- | --- |
| `platform/fde_product/models.py` | 新增 `AnthropicModel` 与 `create_model()`；`CompatibleModel.protocol="chat_completions"`；Anthropic 适配器处理 system、user/assistant/tool 消息、tool_use/tool_result、用量与 `stop_reason`，保留一次输出截断重试 |
| `tools/product.py` | 用 `create_model()` 按 `FDE_MODEL_PROTOCOL` 选择适配器 |
| `tools/verify_product.py` | 用 `create_model()`；`model_baseline.protocol` 写实际协议，而不是固定 `chat_completions` |
| `evals/provider.py` | 用 `create_model()` |
| `evals/promptfooconfig.yaml` | Windows 下 provider 的 Python 路径改为 `../.venv/Scripts/python.exe` |
| `tools/07_fit_gap_to_json.py` | 从材料包复制到工程内，按任务书要求使用 |
| `platform/fde_product/env.py` | 新增本地 `.env` 加载器：`python-dotenv`，`override=False`，显式环境变量优先 |
| `.env.example` | 新增配置模板；真实 `.env` 由使用者本地创建，不提交 |
| `requirements.in` / `requirements.txt` / `pyproject.toml` | 增加 `python-dotenv==1.2.4` |
| `.gitignore` | 增加 `.runtime/`、`.pytest_cache/`、`*.sqlite*`、`node_modules/`、`.env*`、`*.pem`、`*.key`、`*.log`；保留 `.env.example` |
| `main.py` | 删除：`uv init` 生成的 Hello World 占位，无入口、测试、文档或 MANIFEST 引用 |

未改动：`product/system_prompt.txt`、`product/capabilities.json`、`product/semantics.json`、业务规则、四份 JSON 契约、样例数据。

## 2. 环境

- Python 3.14.4（工程 `.venv`）
- Node.js 24.10.0；Promptfoo 0.122.2
- Windows 11；路径含中文，因此所有 Python 命令需设置 `PYTHONUTF8=1`，否则读取样例 CSV 会因 GBK 解码失败
- `python-dotenv==1.2.4`：入口脚本读取工程根目录 `.env`；显式环境变量优先
- `openpyxl` 仅用于填写个人 xlsx 填写稿，不是产品运行依赖；`tools/07_fit_gap_to_json.py` 用标准库读取 xlsx，并用工程已有的 `jsonschema` 校验

## 3. 复现命令（PowerShell）

```powershell
$env:PYTHONUTF8="1"

# 方式 A：在工程根目录放置 .env（参考 .env.example；该文件已 gitignore）
# 方式 B：显式环境变量（优先级高于 .env）
# $env:FDE_MODEL_PROTOCOL="anthropic"
# $env:FDE_MODEL_URL="https://ark.cn-beijing.volces.com/api/coding"
# $env:FDE_MODEL_NAME="deepseek-v4.1-flash"
# $env:FDE_MODEL_KEY="<your-key>"   # 只放在当前终端，不写文件

.venv\Scripts\python.exe tools/product.py init
.venv\Scripts\python.exe tools/check_product.py --dry-run
.venv\Scripts\python.exe tools/verify_product.py --live --root .

cd evals
npx --yes promptfoo@0.122.2 eval --config promptfooconfig.yaml --no-cache --output ../artifacts/w01/promptfoo-results.json
cd ..

.venv\Scripts\python.exe tools/finalize_evaluation.py
.venv\Scripts\python.exe tools/check_product.py --root .
```

复测前先备份 `artifacts/w01/`；`verify_product.py` 会重写证据文件，且动作验证只操作独立教学数据库。

## 4. 证据完整性

- `runtime_evidence.json` 有 11 个 case：9 次真实模型运行（对应 `model_baseline.json` 的 9 条 run）+ 2 项 HTTP 账本检查（`idempotency`、`undo`）。
- `analysis.json` 与 `refund_explanation.json` 是同一次运行的同一份证据（SHA-256 与 `trace_id` 相同），分别服务“分析完整”和“退款说明”两个断言，不是两次独立运行。报告中已据实披露。
- 样例分析 `status=completed`，但 `compare_spending` 为 `partial`：S03 的 8 月无覆盖，未计入；M001/M004/M007 仅来自 S01/S02。
- 所有证据文件路径与 SHA-256 见 `artifacts/w01/runtime_evidence.json`；`check_product.py --root .` 会校验哈希。

## 5. 与原始 MANIFEST 的关系

`MANIFEST.json` 保留发布包原始文件哈希。上表列出的文件因模型适配、Windows 路径和 fit-gap 工具复制而与原始哈希不同，属于本次受控改动；其余文件未改动。`RELEASE.md` 记录官方修订，未改写。

## 6. 提交范围

仓库根目录为本工程目录。提交时保留固定路径：

- `artifacts/w01/environment_check.json`
- `artifacts/w01/product_fit_gap.json`
- `artifacts/w01/model_baseline.json`
- `artifacts/w01/runtime_evidence.json`
- `docs/w01/project_assessment.md`
- `artifacts/w01/evidence/`（四份 JSON 引用的证据与 trace）
- `artifacts/w01/promptfoo-results.json`
- `artifacts/w01/product_fit_gap.xlsx`（填写稿，可选但保留）
- `docs/w01/interview_shen_20261020.md` 与 `docs/w01/interviews/`（访谈证据）
- `.env.example`（配置模板；真实 `.env` 不提交）
- 源码、契约、样例数据、测试、评估配置、依赖说明

不要提交：`.venv/`、`.runtime/`、`.pytest_cache/`、`__pycache__/`、`node_modules/`、`.env`、密钥/证书、`*.log`、运行时 SQLite 数据库。
