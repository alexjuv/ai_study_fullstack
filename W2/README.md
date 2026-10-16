# W2：转账工具作业需求

来源：[AI Agent 全栈工程师训练营—作业二](https://geekbang.feishu.cn/docx/Vxprd4TiqoRJfsxzST8cPq37nVh)。阅读日期：2026-10-16。

六项任务已完成。官方五项验收测试原样保留，另补工程边界测试。

2026-10-16 本地验证：官方 **5/5** 通过；含边界及回归测试共 **51/51** 通过；离线演示通过；所有 Python 文件可编译。使用仓库根目录 `.venv/` 的 Python 3.12.10；未运行可选真实模型入口。

## 运行

需要 Python 3.12+。以下安装命令从仓库根目录（本 README 所在目录的上一级）开始执行，创建虚拟环境并安装 W2 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\W2\requirements.txt
```

然后进入 W2，使用根目录虚拟环境运行测试和演示，无需激活环境：

```powershell
Set-Location .\W2
$python = '..\.venv\Scripts\python.exe'
& $python -X utf8 -m pytest tests/test_tool_governance.py -v -k "transfer"
& $python -X utf8 -m pytest tests -q
& $python -X utf8 tool_governance_demo.py
```

相对于仓库根目录，虚拟环境位于 `.venv/`，依赖清单位于 `W2/requirements.txt`；进入 `W2/` 后，虚拟环境路径为 `../.venv/`。

目录结构：

```text
W2/
  tool_governance_demo.py          完成版，保留原框架与可选模型入口
  requirements.txt                与课程 2-4 相同的依赖范围
  originals/                      从飞书附件预览保存的原始文本
    tool_governance_demo-无答案.py
    test_tool_governance.py
  tests/
    conftest.py                   附录中的导入路径配置
    test_tool_governance.py        原始五项验收测试
    test_transfer_boundaries.py    工程边界与回归测试
```

## 实现与工程边界

- 沿用单文件教学结构，新增逻辑集中于参数模型、预检、handler、注册与脱敏；`PermissionEngine` 全类保持原文一致，并有自动比对测试。
- 保留作业要求的 `float` 参数与账户余额，结算使用 `Decimal`，拒绝不足一分的金额；同时拒绝 NaN、无穷值、额外字段、字符串金额和同账户转账。
- 金额 `1` 与 `1.0` 规范化为相同审批摘要，避免 Pydantic 转为浮点后使有效审批不匹配。不同金额、收款账户、用户或租户仍不能复用审批。
- 使用 `(tenant_id, account_id)` 隔离账户；缺失或其他租户账户不会导致扣款。handler 在结算前复核余额，先计算两侧结果，再同时更新。
- 预检与结算不含挂起点；模拟慢调用发生在所有余额修改之前，取消与超时均保持余额不变。内存实现仅保证单进程单事件循环下的并发安全；真实服务需数据库事务、持久化审批、业务幂等与对账。
- 原始五个测试外，补测区间端点、精度、账户隔离、一次性审批、过期与身份绑定、并发余额与审批重放、取消、嵌套脱敏、deny 优先级等。
- 离线演示输出未审批的 `confirm`、审批后的成功结果和结构化审计；账号在工具结果中脱敏，审计只保留字段名，不写入原始账号。

## 目标与基线

在作业附录的治理框架中接入模拟 `transfer` 工具，贯通参数校验、业务预检、权限、人工审批、超时、脱敏和审计。账户与余额保存在内存，不涉及真实转账。

以飞书附录的 `tool_governance_demo-无答案.py` 为基线，完成版命名为 `tool_governance_demo.py`。附录的 `test_tool_governance.py` 和 `conftest.py` 已保存在 `tests/`。

**不要直接套用本地课程同名示例。** 本地 `course_code/week02/2-4/tool_governance_demo.py` 使用 `build_governance()`、两参数 handler 和不同的结果结构；作业练习版使用 `build_tools()` / `build_runtime()`、三参数 handler，以及带 `action` / `code` 的结果。测试依赖后者的接口。

## 六项任务

1. **账户数据**：在 `ORDERS` 后新增可变字典 `ACCOUNTS`，以 `(tenant_id, account_id)` 为键，余额为 `float`。

   | 租户 | 账户 | 初始余额 |
   | --- | --- | ---: |
   | tenant_a | ACC-A-123456 | 100000.0 |
   | tenant_a | ACC-A-654321 | 5000.0 |
   | tenant_a | ACC-A-888888 | 20000.0 |
   | tenant_b | ACC-B-111111 | 50000.0 |

2. **参数模型**：`TransferArgs` 继承 `StrictArgs`。`from_account`、`to_account` 均匹配 `^ACC-[A-Z]-[0-9]{6}$`；`amount` 是 `float`，满足 `0 < amount <= 100000`。保留继承的严格校验及额外字段拒绝。

3. **预检**：`transfer_precheck(args, context)` 不改余额。先拒绝 `50000 < amount <= 80000`，错误码为 `EXCEED_LIMIT`；再读取当前租户的转出账户余额，不足则返回 `INSUFFICIENT_BALANCE`。这是教学区间规则，不能写成所有大于 50000 的金额都拒绝，否则超时测试无法到达 handler。

4. **处理器**：`transfer_handler(tool_call_id, args, context)` 在金额大于 80000 时，先 `await asyncio.sleep(3.0)`，且必须在扣款之前。检查当前租户下的转入账户是否存在，不存在则抛出 `PolicyDenied("ACCOUNT_NOT_FOUND", "转入账户不存在")`，检查也应先于余额修改。成功后从转出账户扣款、向转入账户入账，返回 `txn_id`（调用 ID 后六位）、`from`、`to`、`amount`、`status="accepted"`。

5. **注册工具**：向 `build_tools()` 追加 `transfer` 定义，绑定参数模型、handler、precheck 和可区分参数组合的 `canonical_target`。声明写操作、人工审批、`transfer:execute` 权限、非幂等、零重试，超时必须小于 3 秒。调用上下文仍须具备权限和工具白名单，注册本身不授予权限。

6. **账号脱敏**：扩展 `_redact()` 的字符串分支，保留现有邮箱脱敏；账号保留前缀与末四位，例如 `ACC-A-123456` 显示为 `ACC-A-****3456`。仅修改对外展示，不改变账户字典键。

## 五个验收测试实际检查什么

以下按飞书测试附件逐项核对，而非根据正文标题推测。

| 测试组 | 主要断言 |
| --- | --- |
| 参数与注入 | 额外的 `approved` / `user_id`、错误账号格式、零金额、超过 100000，均得到 `INVALID_ARGUMENT`；余额不变。 |
| 业务预检 | 转出 60000 得到 `EXCEED_LIMIT`；从余额 20000 的账户转出 30000 得到 `INSUFFICIENT_BALANCE`；即使已有审批也不能跳过预检。 |
| 权限边界 | 缺少权限、缺少工具白名单、plan 模式分别得到 `PERMISSION_DENIED`、`TOOL_NOT_ALLOWED`、`PLAN_MODE_DENIED`；审批不能覆盖这些边界。 |
| 审批与成功 | 未审批返回 `CONFIRM / APPROVAL_REQUIRED`；审批后改金额仍需确认；原参数成功转出 1200 后，两账户余额为 98800 和 6200；返回账号脱敏；审批只能消费一次。 |
| 超时 | 审批后转出 90000，需在 3 秒前返回 `TIMEOUT_UNKNOWN`，两账户余额不变，执行审计记录同一错误码。 |

审批测试还精确检查审计顺序：两条 `decision / APPROVAL_REQUIRED`，随后一条 `decision / APPROVED` 和一条 `execution / OK`。不能只让最终余额正确而忽略治理过程。

## 不可改动与验收方式

- 不修改 `PermissionEngine.decide` 的任何一行；保持既定策略优先级。
- 所有被测调用都经过 `ToolRuntime.invoke()`，不得直接调用 handler。
- 不删除 `StrictArgs` 继承给 `TransferArgs` 的 `extra="forbid"`。
- 测试之间应还原模块级账户状态；附录测试已提供自动隔离 fixture。

在 `W2/` 目录下运行：

```powershell
& ..\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_tool_governance.py -v -k "transfer"
```

要求五项测试全部通过，并检查审批前的 `CONFIRM`、账号脱敏结果和审计记录。附录审计结构仅保存参数键等元数据，不保存完整账号值；脱敏账号可在工具结果中观察，不应为了日志展示把原始账号加入审计。

当前任务不需要网站、数据库、MCP 接入或真实模型调用。作业练习版虽带可选 DeepSeek 入口，五项确定性测试通过 Runtime 即可验收。

## 资料读取状态

已读取飞书正文与三个附录。直接下载按钮未得到可用文件，改从源码预览保存附件文本；练习源码与官方测试均已比对预览文本的长度及 FNV-1a 校验值，内容一致。原始文件保存在 `originals/`，官方测试工作副本未修改。正文“任务 5”和“五个测试”标题下未显示具体内容，已通过附录补足。

原始练习文本校验：31002 字符，FNV-1a `4039020735`；原始测试文本校验：8499 字符，FNV-1a `803913925`。校验针对 Unicode 文本，不是远端附件的字节级下载校验。
